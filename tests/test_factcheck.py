import asyncio
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, text

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.research import Claim, ClaimNumber
from desk.artifacts.store import append_artifact
from desk.config import ChatModel
from desk.desks.factcheck import (
    MoveCheck,
    SupportEntry,
    SupportReply,
    check_market_move,
    number_in_quote,
    quote_in_source,
    run_factcheck,
    stated_direction,
)
from desk.llm.client import StructuredResult, Usage

NEW_YORK = ZoneInfo("America/New_York")

SOURCE = (
    "American Electric Power on Tuesday raised its five-year capital plan to $54 billion, "
    "citing “unprecedented” data-center load growth across its footprint."
)


def test_quote_found_verbatim_and_with_normalized_spacing_and_quotes() -> None:
    assert quote_in_source("raised its five-year capital plan to $54 billion", SOURCE)
    assert quote_in_source('citing "unprecedented"   data-center load growth', SOURCE)
    assert quote_in_source("RAISED ITS FIVE-YEAR CAPITAL PLAN", SOURCE)


def test_fabricated_quote_rejected() -> None:
    assert not quote_in_source("raised its capital plan to $60 billion", SOURCE)
    assert not quote_in_source("", SOURCE)


def test_reported_number_must_appear_in_quote() -> None:
    quote = "raised its five-year capital plan to $54 billion"
    assert number_in_quote(ClaimNumber(name="plan", text="$54 billion", kind="reported"), quote)
    assert number_in_quote(ClaimNumber(name="plan", text="54", kind="reported"), quote)
    assert not number_in_quote(ClaimNumber(name="plan", text="$56 billion", kind="reported"), quote)
    assert not number_in_quote(ClaimNumber(name="plan", text="5.4", kind="reported"), quote)


def test_stated_direction() -> None:
    assert stated_direction("AEP shares fell 2.1% on Tuesday") == -1
    assert stated_direction("Shares jumped 4% after earnings") == 1
    assert stated_direction("AEP moved 2% on the news") == 0


def move(stated: str, statement: str, closes: tuple[Decimal, Decimal] | None) -> MoveCheck:
    number = ClaimNumber(
        name="move", text=stated, kind="market_move", symbol="AEP", as_of=date(2026, 9, 22)
    )
    return check_market_move(number, statement, closes, "price_bars:yahoo:AEP:1d:2026-09-22")


def test_market_move_matches_within_tolerance() -> None:
    result = move("2.1%", "AEP fell 2.1%", (Decimal("100.00"), Decimal("97.92")))
    assert result.ok and result.recomputed is not None
    assert result.recomputed.value == Decimal("-2.08")


def test_market_move_wrong_size_is_corrected() -> None:
    result = move("5%", "AEP fell 5%", (Decimal("100"), Decimal("98")))
    assert not result.ok and result.recomputed is not None
    assert result.recomputed.value == Decimal("-2.00")
    assert "differs" in result.reason


def test_market_move_wrong_direction_is_corrected() -> None:
    result = move("2%", "AEP rose 2%", (Decimal("100"), Decimal("98")))
    assert not result.ok and "direction" in result.reason


def test_market_move_without_data_cannot_be_verified() -> None:
    result = move("2%", "AEP fell 2%", None)
    assert not result.ok and result.recomputed is None
    assert "no stored prices" in result.reason


# --- Runner ---------------------------------------------------------------------------------


def _record(text_body: str) -> RawRecord:
    return RawRecord(
        produced_by="data.test",
        runtime_ms=0,
        source="finnhub.company_news",
        source_id=str(uuid4()),
        fetched_at=datetime.now(UTC),
        tickers=("TSTFC",),
        payload={"headline": "Test Corp update", "summary": text_body},
        content_hash=content_hash("finnhub.company_news", str(uuid4())),
    )


def _claim(record: RawRecord, statement: str, quote: str, *numbers: ClaimNumber) -> Claim:
    return Claim(
        produced_by="research",
        runtime_ms=0,
        parents=(record.id,),
        subject="TSTFC",
        statement=statement,
        source_record_id=record.id,
        quoted_span=quote,
        numbers=numbers,
    )


class StubChat:
    """Labels every item strong unless its statement says "unsupported"."""

    def __init__(self) -> None:
        self.calls = 0

    async def structured(self, model: Any, prompt: Any, schema: Any, check: Any) -> Any:
        self.calls += 1
        statements = re.findall(r"^\[(\d+)\] statement: (.*)$", prompt.user, re.MULTILINE)
        reply = SupportReply(
            labels=[
                SupportEntry(
                    index=int(index),
                    support="none" if "unsupported" in statement else "strong",
                    reason="Quote states it directly",
                )
                for index, statement in statements
            ]
        )
        assert check(reply) == []
        return StructuredResult(reply, Usage(model="stub"), 1)


def test_runner_verifies_corrects_and_rejects(db_engine: Engine) -> None:
    record = _record(
        "Test Corp said revenue reached $4.2 billion. Shares fell sharply on the report."
    )
    move_number = ClaimNumber(
        name="move", text="3.5%", kind="market_move", symbol="TSTFC", as_of=date(2026, 9, 22)
    )
    no_bars = move_number.model_copy(update={"symbol": "TSTNONE"})
    claims = {
        "verified": _claim(
            record,
            "Revenue reached $4.2 billion.",
            "revenue reached $4.2 billion",
            ClaimNumber(name="revenue", text="$4.2 billion", kind="reported"),
        ),
        "corrected": _claim(record, "Shares fell 3.5%.", "Shares fell sharply", move_number),
        "fabricated": _claim(record, "Revenue doubled.", "revenue doubled"),
        "no_bars": _claim(record, "Shares fell 3.5%.", "Shares fell sharply", no_bars),
        "unsupported": _claim(record, "An unsupported outlook.", "Shares fell sharply"),
    }
    with db_engine.begin() as conn:
        append_artifact(conn, record)
        for claim in claims.values():
            append_artifact(conn, claim)
        for day, close in ((date(2026, 9, 21), "100.00"), (date(2026, 9, 22), "97.92")):
            conn.execute(
                text(
                    "INSERT INTO price_bars (source, symbol, interval, ts, open, high, low, "
                    "close, fetched_at) VALUES ('yahoo', 'TSTFC', '1d', :ts, :c, :c, :c, :c, "
                    "now())"
                ),
                {"ts": datetime.combine(day, time(0), NEW_YORK), "c": Decimal(close)},
            )

    chat = StubChat()
    model = ChatModel(model="stub", num_ctx=4096, temperature=0, keep_alive="0")
    outcome = asyncio.run(run_factcheck(db_engine, chat, model, NEW_YORK))  # type: ignore[arg-type]
    assert (outcome.verified, outcome.corrected, outcome.rejected) == (1, 1, 3)
    assert chat.calls == 1

    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload->>'claim_id' AS claim_id, payload FROM artifacts "
                "WHERE kind = 'verified_claim'"
            )
        ).all()
    by_claim = {row.claim_id: row.payload for row in rows}
    assert by_claim[str(claims["verified"].id)]["entailment"] == 0.9
    corrected = by_claim[str(claims["corrected"].id)]
    assert corrected["verdict"] == "corrected"
    assert corrected["recomputed"][0]["value"] == "-2.08"
    assert "not found" in by_claim[str(claims["fabricated"].id)]["reason"]
    assert by_claim[str(claims["fabricated"].id)]["entailment"] is None
    assert "no stored prices" in by_claim[str(claims["no_bars"].id)]["reason"]
    assert by_claim[str(claims["unsupported"].id)]["verdict"] == "rejected"

    # Closed claims are not checked again.
    again = asyncio.run(run_factcheck(db_engine, chat, model, NEW_YORK))  # type: ignore[arg-type]
    assert (again.verified, again.corrected, again.rejected) == (0, 0, 0)
