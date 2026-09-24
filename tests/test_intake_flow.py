import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from desk.api.app import create_app
from desk.artifacts.store import append_artifact
from desk.config import ChatModel
from desk.desks.idea.run import attach_evidence
from desk.desks.idea.status import check_theses, open_theses
from desk.front_office.intake import IntakeReply, process_pending
from desk.llm.client import StructuredResult, Usage

NEW_YORK = ZoneInfo("America/New_York")
MODEL = ChatModel(model="stub", num_ctx=4096, temperature=0, keep_alive="0")


class StubChat:
    """Returns a fixed reply, checked by the same code check as a real one."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply

    async def structured(self, model: Any, prompt: Any, schema: Any, check: Any) -> Any:
        value = IntakeReply.model_validate(self.reply)
        problems = check(value)
        if problems:
            return StructuredResult(None, Usage(model="stub"), 2, error="; ".join(problems))
        return StructuredResult(value, Usage(model="stub"), 1)


BASE_REPLY = {
    "statement": "Long TSTIN on its new contract.",
    "instruments": ["TSTIN"],
    "direction": "long",
    "drivers": [{"statement": "Contract wins continue", "metric": "orders", "source": "edgar"}],
    "hard_level": None,
    "warning_level": None,
    "horizon": None,
    "conviction": None,
}


def test_intake_draft_follow_up_and_confirm(db_engine: Engine) -> None:
    client = TestClient(create_app(db_engine, ollama_base_url="http://127.0.0.1:9"))
    first = client.post("/intake/messages", json={"text": "Long TSTIN on the new contract"})
    assert first.status_code == 201
    message_id = first.json()["message_id"]
    assert client.get(f"/intake/messages/{message_id}").json()["status"] == "pending"

    today = date(2026, 9, 23)
    outcome = asyncio.run(process_pending(db_engine, StubChat(BASE_REPLY), MODEL, today))  # type: ignore[arg-type]
    assert outcome.drafted == 1
    shown = client.get(f"/intake/messages/{message_id}").json()
    assert shown["status"] == "drafted"
    assert shown["missing"] == ["invalidation", "horizon", "conviction"]
    draft_id = shown["draft_id"]
    assert client.post(f"/intake/drafts/{draft_id}/confirm").status_code == 409

    answer = client.post(
        "/intake/messages",
        json={"text": "Out below 40, warn at 42, weeks, conviction 3", "draft_id": draft_id},
    ).json()["message_id"]
    complete = {
        **BASE_REPLY,
        "hard_level": "40",
        "warning_level": "42",
        "horizon": "weeks",
        "conviction": 3,
    }
    asyncio.run(process_pending(db_engine, StubChat(complete), MODEL, today))  # type: ignore[arg-type]
    second = client.get(f"/intake/messages/{answer}").json()
    assert second["missing"] == []
    assert second["draft"]["previous_id"] == draft_id

    confirmed = client.post(f"/intake/drafts/{second['draft_id']}/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "active"
    page = client.get(f"/theses/{confirmed.json()['thesis_id']}")
    assert page.status_code == 200
    assert "confirmed by Jon" in page.text and "intake_message:" in page.text
    ideas = client.get("/dev/status").json()["ideas"]
    assert any(t["instrument"] == "TSTIN" and t["state"] == "active" for t in ideas["theses"])

    # An invented number fails the check twice and is stored as a failed draft.
    third = client.post("/intake/messages", json={"text": "Short TSTIN2"}).json()["message_id"]
    invented = {**BASE_REPLY, "instruments": ["TSTIN2"], "hard_level": "55"}
    failed = asyncio.run(process_pending(db_engine, StubChat(invented), MODEL, today))  # type: ignore[arg-type]
    assert failed.failed == 1
    assert client.get(f"/intake/messages/{third}").json()["status"] == "failed"

    # The active thesis takes new evidence as a new version, then the hard line
    # invalidates it once a daily close lands below it.
    with db_engine.connect() as conn:
        active = next(t for t in open_theses(conn) if t.primary_instrument == "TSTIN")
    assert active.state == "active"
    assert attach_evidence(active, [], None) is None
    close_day = datetime.now(UTC).astimezone(NEW_YORK).date()
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO price_bars (source, symbol, interval, ts, open, high, low, close, "
                "fetched_at) VALUES ('yahoo', 'TSTIN', '1d', :ts, 39, 39, 39, 39, now())"
            ),
            {"ts": datetime.combine(close_day, time(0), NEW_YORK)},
        )
        changed = check_theses(conn, NEW_YORK, close_day + timedelta(days=1))
        mine = [t for t in changed if t.primary_instrument == "TSTIN"]
        assert len(mine) == 1 and mine[0].state == "invalidated"
        assert mine[0].change_note is not None and "39" in mine[0].change_note
        assert mine[0].invalidation is not None
        assert mine[0].invalidation.hard.level == Decimal("40")
        for version in mine:
            append_artifact(conn, version)
    with db_engine.connect() as conn:
        assert all(t.primary_instrument != "TSTIN" for t in open_theses(conn))
