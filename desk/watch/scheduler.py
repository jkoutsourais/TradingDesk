"""Watch desk scheduler: `uv run python -m desk.watch.scheduler`.

One process, two parts:
  planner  every few seconds: plans shifts a week ahead, enqueues due shifts (or marks
           them skipped when the process was down at their time), and enqueues scan jobs
           on the market calendar
  workers  claim jobs from the Postgres queue, run them, and record each run in job_runs

A shift in Phase 2 unloads any Ollama models (the GPU rule) and completes; the desks it
will run arrive in later phases.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import Engine, text

from desk.collectors.__main__ import configure_logging
from desk.config import TiersConfig, UniverseConfig, load_tiers, load_universe
from desk.db import make_engine
from desk.metrics import JobStatus, record_job_finish, record_job_start
from desk.settings import Settings
from desk.symbols import is_future
from desk.watch import queue
from desk.watch.calendar import MarketCalendar, MarketPhase, load_calendar_config
from desk.watch.rules import DailyBar, WatchConfig, load_watch_config
from desk.watch.scan import (
    ScanOutcome,
    close_hits,
    commodity_hits,
    emit,
    intraday_hits,
    load_daily_bars,
    load_features,
    load_session_bars,
    load_tier_map,
    news_hits,
)

logger = logging.getLogger("desk.watch")

DESK = "watch"
PLANNER_TICK_S = 5.0
WORKERS = 2
WORKER_IDLE_S = 1.0
JOB_TIMEOUT_S = 240.0
PLAN_DAYS_AHEAD = 8
DAILY_CACHE_TTL_S = 3600.0


def _slot(now: datetime, seconds: int) -> str:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC).strftime("%Y-%m-%dT%H:%M")


class Planner:
    def __init__(self, calendar: MarketCalendar, config: WatchConfig) -> None:
        self._calendar = calendar
        self._config = config
        self._planned_through: date | None = None

    def shift_times(self, day: date) -> list[tuple[str, datetime]]:
        schedule = self._config.schedule
        tz = self._calendar.tz
        times = []
        session = self._calendar.session(day)
        if session is not None:
            for kind, clock in schedule.shifts.items():
                at = datetime.combine(day, clock, tz).astimezone(UTC)
                if kind == "post_market" and session.early_close:
                    at = min(
                        at,
                        session.close
                        + timedelta(minutes=schedule.post_market_after_early_close_minutes),
                    )
                times.append((kind, at))
        if day.weekday() == 6:
            times.append(
                (
                    "sunday_futures",
                    datetime.combine(day, schedule.sunday_futures, tz).astimezone(UTC),
                )
            )
        return times

    def plan_shifts(self, engine: Engine, now: datetime) -> int:
        today = now.astimezone(self._calendar.tz).date()
        if self._planned_through is not None and self._planned_through >= today + timedelta(
            days=PLAN_DAYS_AHEAD - 1
        ):
            return 0
        rows = [
            {"id": uuid4(), "kind": kind, "at": at}
            for offset in range(PLAN_DAYS_AHEAD)
            for kind, at in self.shift_times(today + timedelta(days=offset))
            if at > now - timedelta(minutes=self._config.schedule.shift_grace_minutes)
        ]
        with engine.begin() as conn:
            inserted = 0
            for row in rows:
                inserted += conn.execute(
                    text(
                        "INSERT INTO shifts (id, kind, scheduled_for) VALUES (:id, :kind, :at) "
                        "ON CONFLICT (kind, scheduled_for) DO NOTHING"
                    ),
                    row,
                ).rowcount
        self._planned_through = today + timedelta(days=PLAN_DAYS_AHEAD - 1)
        return inserted

    def dispatch_shifts(self, engine: Engine, now: datetime) -> None:
        grace = timedelta(minutes=self._config.schedule.shift_grace_minutes)
        with engine.begin() as conn:
            due = conn.execute(
                text(
                    "SELECT id, kind, scheduled_for FROM shifts "
                    "WHERE status = 'scheduled' AND scheduled_for <= :now"
                ),
                {"now": now},
            ).all()
            for shift in due:
                if now - shift.scheduled_for > grace:
                    conn.execute(
                        text(
                            "UPDATE shifts SET status = 'skipped', finished_at = now(), "
                            "note = 'scheduler was not running at the scheduled time' "
                            "WHERE id = :id"
                        ),
                        {"id": shift.id},
                    )
                    logger.warning(
                        "skipped %s shift scheduled for %s", shift.kind, shift.scheduled_for
                    )
                    continue
                queue.enqueue(
                    conn,
                    "shift",
                    {"kind": shift.kind},
                    dedupe_key=f"shift:{shift.id}",
                    shift_id=shift.id,
                )

    def enqueue_scans(self, engine: Engine, now: datetime) -> None:
        cadence = self._config.schedule.scans
        phase = self._calendar.phase(now)
        today = now.astimezone(self._calendar.tz).date()
        jobs: list[tuple[dict[str, Any], str]] = []
        if phase is MarketPhase.REGULAR:
            jobs.append(
                (
                    {"group": "intraday", "tiers": [0, 1]},
                    f"scan:intraday01:{_slot(now, cadence.intraday_tier01_regular_seconds)}",
                )
            )
            jobs.append(
                (
                    {"group": "intraday", "tiers": [2]},
                    f"scan:intraday2:{_slot(now, cadence.intraday_tier2_regular_seconds)}",
                )
            )
        elif phase in (MarketPhase.PRE, MarketPhase.POST):
            jobs.append(
                (
                    {"group": "intraday", "tiers": [0, 1]},
                    f"scan:intraday01:{_slot(now, cadence.intraday_tier01_extended_seconds)}",
                )
            )
        elif self._calendar.futures_open(now):
            jobs.append(
                (
                    {"group": "intraday", "tiers": [0, 1], "futures_only": True},
                    f"scan:futures:{_slot(now, cadence.futures_overnight_seconds)}",
                )
            )
        session = self._calendar.session(today)
        if session is not None and now >= session.close + timedelta(
            minutes=cadence.close_after_minutes
        ):
            jobs.append(({"group": "close"}, f"scan:close:{today.isoformat()}"))
        jobs.append(({"group": "news"}, f"scan:news:{_slot(now, cadence.news_seconds)}"))
        jobs.append(
            ({"group": "commodity"}, f"scan:commodity:{_slot(now, cadence.commodity_seconds)}")
        )
        with engine.begin() as conn:
            for payload, key in jobs:
                queue.enqueue(conn, "scan", payload, dedupe_key=key)


@dataclass
class _DailyCache:
    day: date | None = None
    loaded_at: float = 0.0
    bars: dict[str, list[DailyBar]] | None = None


class Scanner:
    def __init__(
        self,
        engine: Engine,
        calendar: MarketCalendar,
        config: WatchConfig,
        tiers: TiersConfig,
        universe: UniverseConfig,
    ) -> None:
        self._engine = engine
        self._calendar = calendar
        self._config = config
        self._tiers = tiers
        self._universe = universe
        self._cache = _DailyCache()

    def _daily(self, conn: Any, symbols: list[str], today: date) -> dict[str, list[DailyBar]]:
        stale = (
            self._cache.bars is None
            or self._cache.day != today
            or time.monotonic() - self._cache.loaded_at > DAILY_CACHE_TTL_S
        )
        if stale:
            everything = sorted(
                {
                    *self._tiers.tier_1_symbols(),
                    *self._universe.symbols,
                    *self._tiers.tier_3_macro.symbols,
                    *symbols,
                }
            )
            self._cache = _DailyCache(
                today, time.monotonic(), load_daily_bars(conn, everything, today, self._calendar.tz)
            )
        bars = self._cache.bars or {}
        missing = [s for s in symbols if s not in bars]
        if missing:  # a new holding or promotion since the cache was built
            bars.update(load_daily_bars(conn, missing, today, self._calendar.tz))
        return bars

    def run(self, payload: dict[str, Any], now: datetime, shift_id: UUID | None) -> ScanOutcome:
        group = payload["group"]
        today = now.astimezone(self._calendar.tz).date()
        with self._engine.begin() as conn:
            tier_map = load_tier_map(conn, self._tiers, self._universe, today)
            if group == "intraday":
                symbols = tier_map.symbols(*payload["tiers"])
                if payload.get("futures_only"):
                    symbols = [s for s in symbols if is_future(s)]
                daily = self._daily(conn, symbols, today)
                features = load_features(conn, symbols, self._calendar, now, daily)
                hits = intraday_hits(
                    features, tier_map, self._config, self._calendar.phase(now), today
                )
            elif group == "close":
                symbols = tier_map.symbols(0, 1, 2)
                session = self._calendar.session(today)
                if session is None:
                    return ScanOutcome()
                # History from Yahoo (completed days) plus today's bar from the stream.
                history = self._daily(conn, symbols, today)
                today_bars = load_session_bars(conn, symbols, session)
                daily = {
                    s: [*history[s], today_bars[s]]
                    for s in symbols
                    if s in history and s in today_bars
                }
                features = load_features(conn, list(daily), self._calendar, now, daily)
                hits = close_hits(features, tier_map, self._config, today)
            elif group == "news":
                hits = news_hits(conn, tier_map, self._config, now, today)
            elif group == "commodity":
                hits = commodity_hits(conn, self._config)
            else:
                raise ValueError(f"unknown scan group {group!r}")
            return emit(conn, hits, tier_map, self._config, self._calendar, now, shift_id)


async def unload_models(base_url: str) -> list[str]:
    """Release every loaded Ollama model (keep_alive 0) so a shift starts with a free GPU."""
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        loaded = (await client.get("/api/ps")).json().get("models", [])
        for model in loaded:
            await client.post("/api/generate", json={"model": model["name"], "keep_alive": 0})
    return [model["name"] for model in loaded]


class Worker:
    def __init__(self, engine: Engine, scanner: Scanner, ollama_base_url: str) -> None:
        self._engine = engine
        self._scanner = scanner
        self._ollama = ollama_base_url

    def _claim(self) -> queue.Job | None:
        with self._engine.begin() as conn:
            return queue.claim(conn)

    def _start(self, job: queue.Job, name: str) -> UUID:
        with self._engine.begin() as conn:
            return record_job_start(conn, job=name, desk=DESK, shift_id=job.shift_id)

    def _finish(self, job: queue.Job, run_id: UUID, error: str | None) -> None:
        with self._engine.begin() as conn:
            queue.finish(conn, job, error)
            if error is None:
                record_job_finish(conn, run_id, status=JobStatus.OK)
            else:
                record_job_finish(conn, run_id, status=JobStatus.FAILED, error=error)

    def _set_shift(self, shift_id: UUID, status: str, note: str | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE shifts SET status = :status, note = coalesce(:note, note), "
                    "started_at = CASE WHEN :status = 'running' THEN now() ELSE started_at END, "
                    "finished_at = CASE WHEN :status IN ('ok', 'failed') THEN now() "
                    "ELSE finished_at END WHERE id = :id"
                ),
                {"status": status, "note": note, "id": shift_id},
            )

    async def _run_shift(self, job: queue.Job) -> str:
        assert job.shift_id is not None
        await asyncio.to_thread(self._set_shift, job.shift_id, "running")
        unloaded = await unload_models(self._ollama)
        note = f"unloaded models: {', '.join(unloaded) or 'none'}; no desks run yet (Phase 2)"
        await asyncio.to_thread(self._set_shift, job.shift_id, "ok", note)
        return note

    async def _run(self, job: queue.Job) -> str:
        if job.kind == "shift":
            return await self._run_shift(job)
        if job.kind == "scan":
            outcome = await asyncio.to_thread(
                self._scanner.run, job.payload, datetime.now(UTC), job.shift_id
            )
            urgent = sum(1 for t in outcome.triggers if t.urgent)
            return (
                f"{outcome.hits} hits, {len(outcome.triggers)} triggers ({urgent} urgent), "
                f"{outcome.suppressed} in cooldown, promoted {outcome.promotions or 'none'}"
            )
        raise ValueError(f"unknown job kind {job.kind!r}")

    async def run_forever(self, index: int) -> None:
        while True:
            job = await asyncio.to_thread(self._claim)
            if job is None:
                await asyncio.sleep(WORKER_IDLE_S)
                continue
            name = f"{job.kind}:{job.payload.get('group') or job.payload.get('kind')}"
            run_id = await asyncio.to_thread(self._start, job, name)
            try:
                async with asyncio.timeout(JOB_TIMEOUT_S):
                    summary = await self._run(job)
            except asyncio.CancelledError:
                self._finish(job, run_id, "cancelled at shutdown")
                if job.shift_id is not None and job.kind == "shift":
                    self._set_shift(job.shift_id, "failed", "cancelled at shutdown")
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:500]
                logger.exception("%s failed", name)
                await asyncio.to_thread(self._finish, job, run_id, error)
                if job.shift_id is not None and job.kind == "shift":
                    await asyncio.to_thread(self._set_shift, job.shift_id, "failed", error)
                continue
            await asyncio.to_thread(self._finish, job, run_id, None)
            logger.info("worker %d %s: %s", index, name, summary)


async def plan_forever(engine: Engine, planner: Planner) -> None:
    while True:
        now = datetime.now(UTC)
        try:
            await asyncio.to_thread(planner.plan_shifts, engine, now)
            await asyncio.to_thread(planner.dispatch_shifts, engine, now)
            await asyncio.to_thread(planner.enqueue_scans, engine, now)
        except Exception:
            logger.exception("planner tick failed")
        await asyncio.sleep(PLANNER_TICK_S)


def close_interrupted(engine: Engine) -> None:
    with engine.begin() as conn:
        released = queue.release_stale(conn)
        runs = conn.execute(
            text(
                "UPDATE job_runs SET status = 'failed', finished_at = now(), "
                "error = 'interrupted: process restarted' WHERE desk = :desk AND status = 'running'"
            ),
            {"desk": DESK},
        ).rowcount
        shifts = conn.execute(
            text(
                "UPDATE shifts SET status = 'failed', finished_at = now(), "
                "note = 'interrupted: scheduler restarted' WHERE status = 'running'"
            )
        ).rowcount
    if released or runs or shifts:
        logger.warning("closed %d jobs, %d runs, %d shifts left open", released, runs, shifts)


async def run(engine: Engine, settings: Settings) -> None:
    calendar = MarketCalendar(load_calendar_config())
    config = load_watch_config()
    planner = Planner(calendar, config)
    scanner = Scanner(engine, calendar, config, load_tiers(), load_universe())
    worker = Worker(engine, scanner, settings.ollama_base_url)
    async with asyncio.TaskGroup() as group:
        group.create_task(plan_forever(engine, planner), name="planner")
        for index in range(WORKERS):
            group.create_task(worker.run_forever(index), name=f"worker-{index}")


def main() -> None:
    configure_logging()
    settings = Settings()
    engine = make_engine(settings)
    try:
        close_interrupted(engine)
        logger.info("scheduler starting with %d workers", WORKERS)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run(engine, settings))
        logger.info("scheduler stopped")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
