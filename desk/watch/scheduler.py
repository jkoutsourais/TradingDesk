"""Watch desk scheduler: `uv run python -m desk.watch.scheduler`.

One process, two parts:
  planner  every few seconds: plans shifts a week ahead, enqueues due shifts (or marks
           them skipped when the process was down at their time), and enqueues scan jobs
           on the market calendar
  workers  claim jobs from the Postgres queue, run them, and record each run in job_runs

Every shift starts by unloading all Ollama models (the GPU rule), then runs its desks:
  pre_market   triage rounds on the small model
  briefing     morning brief on the deep model, pushed through ntfy
  post_market  research on the deep model, then fact-check on the small model
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
from desk.config import (
    ModelsConfig,
    TiersConfig,
    UniverseConfig,
    load_models,
    load_tiers,
    load_universe,
)
from desk.db import make_engine
from desk.desks.factcheck import run_factcheck
from desk.desks.research import run_research
from desk.front_office.briefing import build_briefing, push_briefing
from desk.front_office.notify import NotifyConfig, NtfyClient, load_notify_config
from desk.llm.client import OllamaChat, Usage
from desk.llm.embeddings import OllamaEmbedder
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
from desk.watch.triage import run_triage

logger = logging.getLogger("desk.watch")

DESK = "watch"
PLANNER_TICK_S = 5.0
WORKERS = 2
WORKER_IDLE_S = 1.0
JOB_TIMEOUT_S = 240.0
PLAN_DAYS_AHEAD = 8
DAILY_CACHE_TTL_S = 3600.0
SHIFT_TIMEOUT_S = 1800.0
TRIAGE_SECONDS = 60
# The pre-market shift clears the overnight triage backlog in at most this many batches.
PRE_MARKET_TRIAGE_ROUNDS = 8


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
        with engine.begin() as conn:
            queue.enqueue(conn, "triage", {}, dedupe_key=f"triage:{_slot(now, TRIAGE_SECONDS)}")
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


@dataclass
class Deps:
    """Everything the workers call besides the database and the scanner."""

    ollama_base_url: str
    chat: OllamaChat
    embedder: OllamaEmbedder
    models: ModelsConfig
    calendar: MarketCalendar
    tiers: TiersConfig
    notify: NotifyConfig
    ntfy: NtfyClient | None


@dataclass
class RunReport:
    summary: str
    usage: Usage | None = None
    error: str | None = None  # set when the job did its work but its outcome is a failure


class Worker:
    def __init__(self, engine: Engine, scanner: Scanner, deps: Deps) -> None:
        self._engine = engine
        self._scanner = scanner
        self._deps = deps
        self._ollama = deps.ollama_base_url

    def _claim(self) -> queue.Job | None:
        with self._engine.begin() as conn:
            return queue.claim(conn)

    def _start(self, job: queue.Job, name: str) -> UUID:
        with self._engine.begin() as conn:
            return record_job_start(conn, job=name, desk=DESK, shift_id=job.shift_id)

    def _finish(
        self, job: queue.Job, run_id: UUID, error: str | None, usage: Usage | None = None
    ) -> None:
        metrics: dict[str, Any] = {}
        if usage is not None:
            metrics = {
                "tokens_in": usage.tokens_in,
                "tokens_out": usage.tokens_out,
                "load_ms": usage.load_ms,
                "generation_ms": usage.generation_ms,
            }
        with self._engine.begin() as conn:
            queue.finish(conn, job, error)
            if error is None:
                record_job_finish(conn, run_id, status=JobStatus.OK, **metrics)
            else:
                record_job_finish(conn, run_id, status=JobStatus.FAILED, error=error, **metrics)

    def _shift_running(self) -> bool:
        with self._engine.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT EXISTS (SELECT 1 FROM shifts WHERE status = 'running')")
                ).scalar_one()
            )

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

    async def _triage(self) -> RunReport:
        deps = self._deps
        outcome = await run_triage(
            self._engine,
            deps.chat,
            deps.models.small,
            deps.tiers,
            deps.ntfy,
            deps.notify,
            deps.calendar.tz,
        )
        summary = (
            f"labelled {outcome.labelled} ({outcome.relevant} relevant), "
            f"urgent pushes sent {outcome.pushed}, held {outcome.held}"
        )
        return RunReport(
            summary, error=f"triage failed: {outcome.error}" if outcome.failed else None
        )

    async def _run_shift(self, job: queue.Job) -> RunReport:
        assert job.shift_id is not None
        deps = self._deps
        kind = job.payload.get("kind")
        await asyncio.to_thread(self._set_shift, job.shift_id, "running")
        # The GPU cannot hold both models: every shift starts from an empty GPU.
        unloaded = await unload_models(self._ollama)
        notes = [f"unloaded {', '.join(unloaded) or 'nothing'}"]
        report = RunReport("")
        try:
            if kind == "pre_market":
                for _ in range(PRE_MARKET_TRIAGE_ROUNDS):
                    triage = await self._triage()
                    notes.append(triage.summary)
                    if triage.error or triage.summary.startswith("labelled 0"):
                        report.error = triage.error
                        break
                await deps.chat.unload(deps.models.small.model)
            elif kind == "briefing":
                outcome = await build_briefing(
                    self._engine,
                    deps.chat,
                    deps.models.deep,
                    deps.calendar,
                    datetime.now(UTC),
                    job.shift_id,
                )
                report.usage = outcome.result.usage if outcome.result else None
                notes.append(f"brief {outcome.brief.id} status {outcome.brief.status.value}")
                if deps.ntfy is not None:
                    await push_briefing(self._engine, deps.ntfy, deps.notify, outcome.brief)
                    notes.append("pushed")
                else:
                    notes.append("not pushed: NTFY_TOPIC is not set")
                await deps.chat.unload(deps.models.deep.model)
            elif kind == "post_market":
                notes += await self._research_and_factcheck(job.shift_id, report)
            else:
                notes.append("no desks for this shift yet")
        except Exception:
            await asyncio.to_thread(self._set_shift, job.shift_id, "failed", "; ".join(notes))
            raise
        report.summary = "; ".join(notes)
        await asyncio.to_thread(
            self._set_shift, job.shift_id, "failed" if report.error else "ok", report.summary[:1000]
        )
        return report

    async def _research_and_factcheck(self, shift_id: UUID, report: RunReport) -> list[str]:
        deps = self._deps
        research = await run_research(
            self._engine,
            deps.chat,
            deps.models.deep,
            deps.embedder,
            deps.models.embedding,
            deps.tiers,
            datetime.now(UTC),
            shift_id,
        )
        await deps.chat.unload(deps.models.deep.model)
        notes = [
            f"research: {len(research.dossiers)} dossiers, {research.claims} claims, "
            f"{len(research.failed)} failed"
        ]
        notes += research.failed[:5]
        check = await run_factcheck(
            self._engine, deps.chat, deps.models.small, deps.calendar.tz, shift_id
        )
        await deps.chat.unload(deps.models.small.model)
        notes.append(
            f"fact-check: {check.verified} verified, {check.corrected} corrected, "
            f"{check.rejected} rejected, {check.failed} failed"
        )
        report.usage = check.usage
        if check.error:
            report.error = check.error
        elif research.failed and not research.dossiers:
            report.error = "research produced no dossiers"
        return notes

    async def _run(self, job: queue.Job) -> RunReport:
        if job.kind == "shift":
            return await self._run_shift(job)
        if job.kind == "triage":
            if await asyncio.to_thread(self._shift_running):
                return RunReport("paused: a shift holds the GPU")
            return await self._triage()
        if job.kind == "scan":
            outcome = await asyncio.to_thread(
                self._scanner.run, job.payload, datetime.now(UTC), job.shift_id
            )
            urgent = sum(1 for t in outcome.triggers if t.urgent)
            return RunReport(
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
            name = f"{job.kind}:{job.payload.get('group') or job.payload.get('kind') or 'run'}"
            run_id = await asyncio.to_thread(self._start, job, name)
            try:
                timeout = SHIFT_TIMEOUT_S if job.kind == "shift" else JOB_TIMEOUT_S
                async with asyncio.timeout(timeout):
                    report = await self._run(job)
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
            await asyncio.to_thread(self._finish, job, run_id, report.error, report.usage)
            if report.error:
                logger.warning(
                    "worker %d %s failed: %s (%s)", index, name, report.error, report.summary
                )
            else:
                logger.info("worker %d %s: %s", index, name, report.summary)


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
    tiers = load_tiers()
    models = load_models()
    planner = Planner(calendar, config)
    scanner = Scanner(engine, calendar, config, tiers, load_universe())
    ntfy = (
        NtfyClient(settings.ntfy_server, settings.ntfy_topic.get_secret_value())
        if settings.ntfy_topic
        else None
    )
    if ntfy is None:
        logger.warning("NTFY_TOPIC is not set: briefings and urgent alerts will not be pushed")
    deps = Deps(
        ollama_base_url=settings.ollama_base_url,
        chat=OllamaChat(settings.ollama_base_url),
        embedder=OllamaEmbedder(settings.ollama_base_url, models.embedding),
        models=models,
        calendar=calendar,
        tiers=tiers,
        notify=load_notify_config(),
        ntfy=ntfy,
    )
    worker = Worker(engine, scanner, deps)
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(plan_forever(engine, planner), name="planner")
            for index in range(WORKERS):
                group.create_task(worker.run_forever(index), name=f"worker-{index}")
    finally:
        await deps.chat.aclose()
        await deps.embedder.aclose()
        if ntfy is not None:
            await ntfy.aclose()


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
