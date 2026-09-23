"""Run every Data desk collector: `uv run python -m desk.collectors`.

Ctrl+C in a terminal and NSSM's console stop both cancel the collector tasks; each
in-flight run is closed out in job_runs before exit.
"""

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
    configure_logging()
    settings = Settings()
    schedule = load_schedule()
    engine = make_engine(settings)
    try:
        closed = close_interrupted_runs(engine)
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
        logger.info("starting %d collectors", len(built.collectors))
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run_all(engine, schedule, built))
        logger.info("collectors stopped")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
