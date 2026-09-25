"""FastAPI application: health and artifact read endpoints.

Endpoints are sync on purpose. psycopg's async mode cannot run on Windows' default
ProactorEventLoop, so database work runs in FastAPI's threadpool instead.
"""

import logging
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from desk.api.dev import dev_router
from desk.api.intake import intake_router
from desk.api.pages import pages_router
from desk.api.scores import scores_router
from desk.api.v1 import api_router
from desk.artifacts.store import ArtifactNotFoundError, get_artifact, get_lineage

logger = logging.getLogger(__name__)

OLLAMA_TIMEOUT_S = 2.0


def _database_health(engine: Engine) -> dict[str, Any]:
    try:
        with engine.connect() as conn:
            has_vector = conn.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            ).scalar_one()
    except SQLAlchemyError as exc:
        logger.warning("database health check failed: %s", type(exc).__name__)
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True, "pgvector": bool(has_vector)}


def _ollama_health(base_url: str, transport: httpx.BaseTransport | None) -> dict[str, Any]:
    try:
        with httpx.Client(
            base_url=base_url, timeout=OLLAMA_TIMEOUT_S, transport=transport
        ) as client:
            response = client.get("/api/version")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return {"ok": False, "error": type(exc).__name__}
    return {"ok": True, "version": response.json().get("version")}


def create_app(
    engine: Engine,
    *,
    ollama_base_url: str,
    ollama_transport: httpx.BaseTransport | None = None,
) -> FastAPI:
    app = FastAPI(title="desk", version="0.1.0")
    app.include_router(dev_router(engine))
    app.include_router(pages_router(engine))
    app.include_router(intake_router(engine))
    app.include_router(scores_router(engine))
    app.include_router(api_router(engine, ollama_base_url))

    @app.get("/health")
    def health() -> dict[str, Any]:
        database = _database_health(engine)
        if not database["ok"]:
            raise HTTPException(status_code=503, detail={"database": database})
        ollama = _ollama_health(ollama_base_url, ollama_transport)
        # Ollama is only needed during shifts and chat, so its absence degrades, not fails.
        status = "ok" if ollama["ok"] else "degraded"
        return {"status": status, "database": database, "ollama": ollama}

    @app.get("/artifacts/{artifact_id}")
    def read_artifact(artifact_id: UUID) -> dict[str, Any]:
        with engine.connect() as conn:
            try:
                artifact = get_artifact(conn, artifact_id)
            except ArtifactNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return artifact.model_dump(mode="json")

    @app.get("/artifacts/{artifact_id}/lineage")
    def read_lineage(artifact_id: UUID) -> list[dict[str, Any]]:
        with engine.connect() as conn:
            try:
                lineage = get_lineage(conn, artifact_id)
            except ArtifactNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [
            {"depth": entry.depth, "artifact": entry.artifact.model_dump(mode="json")}
            for entry in lineage
        ]

    return app
