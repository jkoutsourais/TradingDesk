import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from desk.api.app import create_app
from desk.artifacts.chat import ChatReply
from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.store import append_artifact, get_artifact
from desk.artifacts.trigger import Trigger
from desk.config import load_models
from desk.front_office.chat import ChatDraft, answer_pending, check_draft
from desk.llm.client import StructuredResult, Usage
from desk.llm.facts import Fact, FactTable

NY = ZoneInfo("America/New_York")


def test_chat_check_rejects_invented_numbers_and_unknown_cites() -> None:
    table = FactTable([Fact("hit_1", "TSTCH broke out", "", "TSTCH broke out", "watch hit", "t:1")])
    check = check_draft(table, {"hit_1"})
    assert check(ChatDraft(answer="The top hit: {hit_1}.", cites=["hit_1"])) == []
    assert any("'12'" in p for p in check(ChatDraft(answer="Up 12 percent.", cites=[])))
    assert any(
        "unknown fact ids" in p for p in check(ChatDraft(answer="See {hit_1}.", cites=["hit_9"]))
    )


class ScriptedChief:
    """Answers with the first watch-hit fact it finds, citing it."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def unload(self, model: str) -> None:
        return None

    async def structured(self, model: Any, prompt: Any, schema: Any, check: Any) -> Any:
        self.prompts.append(prompt.user)
        hits = re.findall(r"^\{(hit_\d+|art_\d+)\} = ", prompt.user, re.MULTILINE)
        cite = hits[0]
        value = ChatDraft(answer=f"The desk flagged {{{cite}}}.", cites=[cite])
        assert check(value) == []
        return StructuredResult(value, Usage(model=model.model), 1)


def test_chat_round_trip_through_api(db_engine: Engine) -> None:
    record = RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        fetched_at=datetime.now(UTC),
        payload={"headline": "TSTCH order"},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
    )
    trigger = Trigger(
        produced_by="watch.test",
        runtime_ms=0,
        parents=(record.id,),
        rule_id="range_break",
        instrument="TSTCH",
        importance=0.99,
        urgent=False,
        summary="TSTCH broke above its 20-day range",
        fingerprint=f"test:{uuid4()}",
    )
    with db_engine.begin() as conn:
        append_artifact(conn, record)
        append_artifact(conn, trigger)

    api = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    posted = api.post("/api/chat", json={"text": "What did the watch desk flag today?"}).json()
    explain = api.post(
        "/api/chat",
        json={
            "text": "Explain this",
            "subject_id": str(trigger.id),
            "thread_id": posted["thread_id"],
        },
    ).json()
    assert explain["thread_id"] == posted["thread_id"]
    with db_engine.begin() as conn:
        queued = conn.execute(text("SELECT count(*) FROM jobs WHERE kind = 'chat'")).scalar_one()
        # Jobs are operational rows; clear them so later queue tests start empty.
        conn.execute(text("DELETE FROM jobs WHERE kind = 'chat'"))
    assert queued >= 2
    pending = api.get(f"/api/chat/threads/{posted['thread_id']}").json()
    assert pending["pending"] is True

    chief = ScriptedChief()
    answered = asyncio.run(
        answer_pending(db_engine, chief, load_models().deep, {"TSTCH"}, NY)  # type: ignore[arg-type]
    )
    assert answered >= 2
    assert "explain this artifact" in chief.prompts[-1]
    assert "the artifact to explain" in chief.prompts[-1]

    conversation = api.get(f"/api/chat/threads/{posted['thread_id']}").json()
    assert conversation["pending"] is False
    replies = [i for i in conversation["items"] if i["role"] == "desk"]
    assert len(replies) == 2
    assert replies[0]["text"].startswith("The desk flagged ")
    with db_engine.connect() as conn:
        reply = get_artifact(conn, replies[0]["id"])
    assert isinstance(reply, ChatReply) and reply.cited_artifacts
    assert api.get("/api/chat/threads").status_code == 200
    assert api.post("/api/chat", json={"text": "x", "subject_id": str(uuid4())}).status_code == 404


def test_theses_book_and_scores_endpoints(db_engine: Engine) -> None:
    api = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    assert api.get("/api/theses").status_code == 200
    assert api.get(f"/api/theses/{uuid4()}").status_code == 404
    assert api.get(f"/api/plans/{uuid4()}").status_code == 404
    book = api.get("/api/book").json()
    assert set(book) == {"holdings", "ratings", "positions", "fills"}
    scores = api.get("/api/scores?horizon=5d").json()
    assert "dimensions" in scores and "recent" in scores
