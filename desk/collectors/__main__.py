"""Run every Data desk collector: `uv run python -m desk.collectors`.

Ctrl+C in a terminal and NSSM's console stop both cancel the collector tasks; each
in-flight run is closed out in job_runs before exit.
"""

import argparse
import asyncio
import contextlib
import logging

from desk.collectors.runner import build_collectors, close_interrupted_runs, run_all
from desk.config import load_models, load_schedule, load_sources, load_tiers, load_universe
from desk.db import make_engine
from desk.settings import Settings

logger = logging.getLogger("desk.collectors")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, query string included; FRED and EIA keys
    # travel in the query string, so these loggers stay at WARNING. The tastytrade SDK
    # uses its own httpx fork, which logs under "httpx2".
    for noisy in ("httpx", "httpx2", "httpcore", "tastytrade", "yfinance", "gridstatus"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Data desk collectors")
    parser.add_argument(
        "--only",
        help="comma-separated collector names; runs just these and leaves every other "
        "collector's runs and health rows to the process that owns them",
    )
    args = parser.parse_args()
    only = {name.strip() for name in args.only.split(",")} if args.only else None

    configure_logging()
    settings = Settings()
    schedule = load_schedule()
    engine = make_engine(settings)
    try:
        if only is not None:
            unknown = only - set(schedule.collectors)
            if unknown:
                raise SystemExit(f"unknown collectors: {', '.join(sorted(unknown))}")
        closed = close_interrupted_runs(engine, jobs=only)
        if closed:
            logger.warning("closed %d runs left open by a previous process", closed)
        built = build_collectors(
            settings,
            engine,
            schedule,
            load_tiers(),
            load_sources(),
            load_universe(),
            load_models(),
        )
        if only is not None:
            built.collectors = [c for c in built.collectors if c.name in only]
            built.disabled = {k: v for k, v in built.disabled.items() if k in only}
        logger.info("starting %d collectors", len(built.collectors))
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run_all(engine, schedule, built))
        logger.info("collectors stopped")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
