from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from desk.api.app import create_app
from desk.artifacts.store import append_artifact
from tests.probe import ProbeArtifact


def unreachable_ollama() -> str:
    # Port 9 (discard) is closed on a normal workstation, so the probe fails fast.
    return "http://127.0.0.1:9"


@pytest.fixture
def client(db_engine: Engine) -> TestClient:
    return TestClient(create_app(db_engine, ollama_base_url=unreachable_ollama()))


def test_health_reports_database_and_ollama(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["database"]["ok"] is True
    assert body["database"]["pgvector"] is True
    assert body["ollama"]["ok"] is False
    assert body["status"] == "degraded"


def test_health_is_ok_when_ollama_answers(db_engine: Engine) -> None:
    def fake_ollama(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/version"
        return httpx.Response(200, json={"version": "0.99.0"})

    app = create_app(
        db_engine,
        ollama_base_url="http://ollama.test",
        ollama_transport=httpx.MockTransport(fake_ollama),
    )
    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["ollama"] == {"ok": True, "version": "0.99.0"}


def test_get_artifact_and_lineage(db_engine: Engine, client: TestClient) -> None:
    parent = ProbeArtifact(produced_by="test.probe", runtime_ms=1, note="parent")
    child = ProbeArtifact(
        produced_by="test.probe", runtime_ms=2, note="child", parents=(parent.id,)
    )
    with db_engine.begin() as conn:
        append_artifact(conn, parent)
        append_artifact(conn, child)

    artifact_response = client.get(f"/artifacts/{child.id}")
    assert artifact_response.status_code == 200
    body = artifact_response.json()
    assert body["kind"] == "probe"
    assert body["note"] == "child"
    assert body["parents"] == [str(parent.id)]

    lineage_response = client.get(f"/artifacts/{child.id}/lineage")
    assert lineage_response.status_code == 200
    lineage = lineage_response.json()
    assert [(entry["depth"], entry["artifact"]["note"]) for entry in lineage] == [
        (0, "child"),
        (1, "parent"),
    ]


def test_missing_artifact_is_404(client: TestClient) -> None:
    assert client.get(f"/artifacts/{uuid4()}").status_code == 404
    assert client.get(f"/artifacts/{uuid4()}/lineage").status_code == 404
