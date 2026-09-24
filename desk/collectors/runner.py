"""Collector runner: one asyncio task per collector, each on its own interval and window.

A failed run is logged, recorded in job_runs and collector_health, and retried at the
next interval; it never stops the other collectors. Database work runs in worker threads
because the engine is synchronous (see desk/api/app.py for why).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

import httpx
from sqlalchemy import Engine, text

from desk.collectors import health
from desk.collectors.base import Collector, CollectResult, describe_http_error
from desk.collectors.brokers import IbkrFlexPositions, TastytradePositions
from desk.collectors.edgar import EdgarClient, EdgarCompanyFilings, EdgarLatestFilings
from desk.collectors.embeddings import NewsEmbeddings
from desk.collectors.fed_rss import FedRssCollector
from desk.collectors.federal_register import (
    FederalRegisterDocuments,
    FederalRegisterPublicInspection,
)
from desk.collectors.filing_text import FilingText
from desk.collectors.finnhub import (
    FinnhubClient,
    FinnhubCompanyNews,
    FinnhubEarningsCalendar,
    FinnhubGeneralNews,
)
from desk.collectors.grid import GridCollector, gridstatus_factory
from desk.collectors.holdings import HeldSymbols
from desk.collectors.ingest import ingest
from desk.collectors.numeric import CftcCotCollector, EiaCollector, FredCollector
from desk.collectors.prices import TastytradeMetrics, YahooDailyBars, yfinance_downloader
from desk.collectors.purge import purge_raw_records
from desk.collectors.release_calendar import ReleaseCalendar
from desk.collectors.tastytrade_session import TastytradeConnection
from desk.collectors.tastytrade_stream import TastytradeStream
from desk.collectors.truth_social import TruthSocialCollector
from desk.config import (
    CollectorSchedule,
    ModelsConfig,
    ScheduleConfig,
    SourcesConfig,
    TiersConfig,
    UniverseConfig,
)
from desk.llm.embeddings import OllamaEmbedder
from desk.metrics import JobStatus, record_job_finish, record_job_start
from desk.settings import REPO_ROOT, Settings
from desk.symbols import is_future
from desk.watch.calendar import MarketCalendar, load_calendar_config

logger = logging.getLogger(__name__)

DESK = "data"
LOOP_RESTART_DELAY_S = 30.0
# Under data/ (git-ignored) so the service account and dev runs each own a writable cache.
YFINANCE_CACHE_DIR = REPO_ROOT / "data" / "cache" / "yfinance"


class RawRecordPurge:
    """Buffer purge scheduled like a collector so it gets the same run tracking."""

    name = "raw_record_purge"

    def __init__(self, engine: Engine, retention_days: int) -> None:
        self._engine = engine
        self._retention_days = retention_days

    def _purge(self) -> int:
        with self._engine.begin() as conn:
            return purge_raw_records(conn, self._retention_days)

    async def collect(self) -> CollectResult:
        deleted = await asyncio.to_thread(self._purge)
        logger.info("purged %d raw records older than %d days", deleted, self._retention_days)
        return CollectResult()

    async def aclose(self) -> None:
        return None


def latest_daily_bar_dates(engine: Engine) -> dict[str, date]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT symbol, max(ts) FROM price_bars "
                "WHERE source = 'yahoo' AND interval = '1d' GROUP BY symbol"
            )
        )
        return {symbol: latest.date() for symbol, latest in rows}


@dataclass
class BuiltCollectors:
    collectors: list[Collector]
    disabled: dict[str, str]  # collector name -> reason
    # Shared clients closed once at shutdown, after every collector has stopped.
    shared: list[TastytradeConnection] = field(default_factory=list)


def build_collectors(
    settings: Settings,
    engine: Engine,
    schedule: ScheduleConfig,
    tiers: TiersConfig,
    sources: SourcesConfig,
    universe: UniverseConfig,
    models: ModelsConfig,
) -> BuiltCollectors:
    """Build enabled collectors and record, for the rest, why each is disabled."""
    collectors: list[Collector] = []
    disabled: dict[str, str] = {}
    shared: list[TastytradeConnection] = []
    # Tier 0: whatever the latest broker snapshots hold joins every per-symbol feed.
    held = HeldSymbols(engine)

    def news_symbols() -> tuple[str, ...]:
        held_listed = {s for s in held() if not is_future(s)}
        return tuple(sorted({*tiers.tier_1_stocks(), *held_listed}))

    def stream_symbols() -> tuple[str, ...]:
        # Tier 2 streams too (decided 2026-09-23) so its intraday scans run on live data.
        return tuple(sorted({*tiers.tier_1_symbols(), *held(), *universe.symbols}))

    def candle_symbols() -> tuple[str, ...]:
        return tuple(sorted({*tiers.tier_1_symbols(), *held()}))

    def daily_symbols() -> tuple[str, ...]:
        return tuple(
            sorted(
                {*tiers.tier_1_symbols(), *held(), *universe.symbols, *tiers.tier_3_macro.symbols}
            )
        )

    def metrics_symbols() -> tuple[str, ...]:
        return tuple(sorted({*tiers.tier_1_symbols(), *held(), *universe.symbols}))

    collectors.append(
        YahooDailyBars(
            daily_symbols,
            lambda: latest_daily_bar_dates(engine),
            yfinance_downloader(YFINANCE_CACHE_DIR),
            schedule.tz,
        )
    )

    if settings.tastytrade_client_secret and settings.tastytrade_refresh_token:
        tastytrade = TastytradeConnection(
            settings.tastytrade_client_secret.get_secret_value(),
            settings.tastytrade_refresh_token.get_secret_value(),
        )
        shared.append(tastytrade)
        collectors += [
            TastytradeStream(tastytrade, stream_symbols, candle_symbols),
            TastytradeMetrics(tastytrade, metrics_symbols, schedule.tz),
            TastytradePositions(tastytrade),
        ]
    else:
        for name in ("tastytrade_stream", "tastytrade_metrics", "tastytrade_positions"):
            disabled[name] = "TASTYTRADE_CLIENT_SECRET or TASTYTRADE_REFRESH_TOKEN is not set"

    if settings.flex_query_api_token and settings.flex_query_id:
        collectors.append(
            IbkrFlexPositions(
                settings.flex_query_api_token.get_secret_value(),
                settings.flex_query_id,
                schedule.tz,
            )
        )
    else:
        disabled["ibkr_flex"] = "FLEX_QUERY_API_TOKEN or FLEX_QUERY_ID is not set"

    if settings.finnhub_api_key:
        finnhub = FinnhubClient(settings.finnhub_api_key.get_secret_value())
        collectors += [
            FinnhubCompanyNews(finnhub, news_symbols),
            FinnhubGeneralNews(finnhub),
            FinnhubEarningsCalendar(finnhub),
        ]
    else:
        for name in ("finnhub_company_news", "finnhub_general_news", "finnhub_earnings_calendar"):
            disabled[name] = "FINNHUB_API_KEY is not set"

    if settings.sec_user_agent:
        edgar = EdgarClient(settings.sec_user_agent)
        collectors += [
            EdgarLatestFilings(edgar),
            EdgarCompanyFilings(edgar, news_symbols),
            FilingText(engine, edgar, news_symbols),
        ]
    else:
        for name in ("edgar_latest_filings", "edgar_company_filings", "edgar_filing_text"):
            disabled[name] = "SEC_USER_AGENT is not set"

    if settings.fred_api_key:
        collectors.append(FredCollector(settings.fred_api_key.get_secret_value(), sources.fred))
    else:
        disabled["fred"] = "FRED_API_KEY is not set"

    if settings.eia_api_key:
        collectors.append(EiaCollector(settings.eia_api_key.get_secret_value(), sources.eia))
    else:
        disabled["eia"] = "EIA_API_KEY is not set"

    calendar_config = load_calendar_config()
    collectors.append(
        ReleaseCalendar(
            calendar_config,
            MarketCalendar(calendar_config),
            settings.fred_api_key.get_secret_value() if settings.fred_api_key else None,
        )
    )

    embedder = OllamaEmbedder(settings.ollama_base_url, models.embedding)
    collectors.append(NewsEmbeddings(engine, embedder, models.embedding.batch_size))

    grid_isos = tuple(
        name
        for name, iso in sources.grid.isos.items()
        if not iso.requires_key or (name == "pjm" and settings.pjm_api_key)
    )
    if "pjm" in sources.grid.isos and "pjm" not in grid_isos:
        logger.warning("gridstatus: PJM skipped until PJM_API_KEY is set")
    pjm_key = settings.pjm_api_key.get_secret_value() if settings.pjm_api_key else None
    collectors.append(GridCollector(sources.grid, gridstatus_factory(pjm_key), grid_isos))

    collectors += [
        FederalRegisterDocuments(),
        FederalRegisterPublicInspection(),
        FedRssCollector("fed_speeches"),
        FedRssCollector("fed_press_releases"),
        TruthSocialCollector(),
        CftcCotCollector(sources.cftc_cot),
        RawRecordPurge(engine, schedule.retention.raw_record_days),
    ]

    unscheduled = [c.name for c in collectors if c.name not in schedule.collectors]
    if unscheduled:
        raise ValueError(f"collectors missing from schedule.yaml: {unscheduled}")
    return BuiltCollectors(collectors, disabled, shared)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPError):
        return describe_http_error(exc)
    if isinstance(exc, TimeoutError):
        return "run timed out"
    return f"{type(exc).__name__}: {exc}"


class CollectorRunner:
    def __init__(self, engine: Engine, schedule: ScheduleConfig) -> None:
        self._engine = engine
        self._schedule = schedule

    def _start(self, collector: str) -> UUID:
        with self._engine.begin() as conn:
            return record_job_start(conn, job=collector, desk=DESK)

    def _store_and_finish(self, collector: str, run_id: UUID, result: CollectResult) -> int:
        with self._engine.begin() as conn:
            counts = ingest(conn, result)
            added = counts.stored
            if result.errors:
                error = "; ".join(result.errors)
                record_job_finish(conn, run_id, status=JobStatus.FAILED, error=error)
                health.record_failure(conn, collector, error, added)
            else:
                record_job_finish(conn, run_id, status=JobStatus.OK)
                health.record_success(conn, collector, added)
        return added

    def _fail(self, collector: str, run_id: UUID, error: str) -> None:
        with self._engine.begin() as conn:
            record_job_finish(conn, run_id, status=JobStatus.FAILED, error=error)
            health.record_failure(conn, collector, error)

    async def run_once(self, collector: Collector) -> None:
        run_id = await asyncio.to_thread(self._start, collector.name)
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._schedule.run_timeout_seconds):
                result = await collector.collect()
            added = await asyncio.to_thread(self._store_and_finish, collector.name, run_id, result)
        except asyncio.CancelledError:
            # Shutdown mid-run: close the job row synchronously so it does not stay "running".
            self._fail(collector.name, run_id, "cancelled at shutdown")
            raise
        except Exception as exc:
            # Not swallowed: logged, stored as a failed run and health error, retried next
            # interval. Catching broadly keeps one bad source from stopping the others.
            error = _describe(exc)
            # Tracebacks only for non-HTTP errors: httpx exception text can embed API keys.
            logger.error(
                "%s failed: %s",
                collector.name,
                error,
                exc_info=not isinstance(exc, httpx.HTTPError),
            )
            await asyncio.to_thread(self._fail, collector.name, run_id, error)
            return
        elapsed = time.monotonic() - started
        if result.errors:
            # Stored what arrived, but the run is recorded as failed; the log says the same.
            logger.warning(
                "%s failed (partial, %d new in %.1fs): %s",
                collector.name,
                added,
                elapsed,
                "; ".join(result.errors),
            )
        else:
            logger.info("%s ok: %d new in %.1fs", collector.name, added, elapsed)

    async def run_forever(self, collector: Collector) -> None:
        schedule: CollectorSchedule = self._schedule.collectors[collector.name]
        tz = self._schedule.tz
        while True:
            try:
                now = datetime.now(UTC)
                if schedule.window is not None and not schedule.window.contains(now, tz):
                    wake = schedule.window.next_open(now, tz)
                    await asyncio.sleep(max(1.0, (wake - now).total_seconds()))
                    continue
                started = time.monotonic()
                await self.run_once(collector)
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, schedule.interval_seconds - elapsed))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s loop crashed; restarting", collector.name)
                await asyncio.sleep(LOOP_RESTART_DELAY_S)


def close_interrupted_runs(engine: Engine, jobs: set[str] | None = None) -> int:
    """Mark data-desk runs left 'running' by a previous crash or hard stop as failed.

    With `jobs`, only those collectors' runs are touched, so a second process running a
    subset never closes runs that another live process still owns.
    """
    with engine.begin() as conn:
        closed = conn.execute(
            text(
                "UPDATE job_runs SET status = 'failed', finished_at = now(), "
                "error = 'interrupted: process restarted' "
                "WHERE desk = :desk AND status = 'running' "
                "AND (CAST(:jobs AS text[]) IS NULL OR job = ANY(CAST(:jobs AS text[])))"
            ),
            {"desk": DESK, "jobs": sorted(jobs) if jobs is not None else None},
        )
        return closed.rowcount


async def run_all(engine: Engine, schedule: ScheduleConfig, built: BuiltCollectors) -> None:
    def register() -> None:
        with engine.begin() as conn:
            for collector in built.collectors:
                health.register_collector(conn, collector.name, enabled=True)
            for name, reason in built.disabled.items():
                health.register_collector(conn, name, enabled=False, disabled_reason=reason)

    await asyncio.to_thread(register)
    for name, reason in built.disabled.items():
        logger.warning("%s disabled: %s", name, reason)

    runner = CollectorRunner(engine, schedule)
    try:
        async with asyncio.TaskGroup() as group:
            for collector in built.collectors:
                group.create_task(runner.run_forever(collector), name=collector.name)
    finally:
        for collector in built.collectors:
            await collector.aclose()
        for shared in built.shared:
            await shared.aclose()
