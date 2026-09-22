"""Run the API: `uv run python -m desk.api`.

uvicorn handles Ctrl+C in a terminal, and NSSM's default stop sends the same console
Ctrl+C event, so both paths drain requests and dispose the engine before exit.
"""

import logging

import uvicorn

from desk.api.app import create_app
from desk.db import make_engine
from desk.settings import Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    engine = make_engine(settings)
    app = create_app(engine, ollama_base_url=settings.ollama_base_url)
    try:
        uvicorn.run(app, host=settings.api_host, port=settings.api_port)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
