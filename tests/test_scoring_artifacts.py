from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from desk.artifacts.registry import artifact_class
from desk.artifacts.scoring import Fill, Position, Score


def test_kinds_registered() -> None:
    assert artifact_class("fill") is Fill
    assert artifact_class("position") is Position
    assert artifact_class("score") is Score


def test_fill_requires_positive_quantity() -> None:
    fields = {
        "produced_by": "data.ibkr_flex",
        "runtime_ms": 0,
        "broker": "ibkr",
        "account_ref": "ibkr:0000",
        "exec_id": "0001",
        "symbol": "SLV",
        "contract": "SLV",
        "asset_class": "equity",
        "side": "buy",
        "quantity": "10",
        "price": "30",
        "multiplier": "1",
        "fees": "1",
        "executed_at": datetime(2026, 9, 24, 15, tzinfo=UTC),
    }
    assert Fill.model_validate(fields).side == "buy"
    with pytest.raises(ValidationError):
        Fill.model_validate({**fields, "quantity": "-10"})


def position(**overrides: object) -> Position:
    fill, plan = uuid4(), uuid4()
    fields: dict[str, object] = {
        "produced_by": "scoring.positions",
        "runtime_ms": 0,
        "parents": [fill, plan],
        "account_ref": "ibkr:0000",
        "symbol": "SLV",
        "contract": "SLV",
        "direction": "long",
        "state": "open",
        "quantity": "10",
        "max_quantity": "10",
        "realized_pnl": "-1",
        "opened_at": datetime(2026, 9, 24, 15, tzinfo=UTC),
        "fill_ids": [fill],
        "plan_id": plan,
        "link_status": "auto",
    }
    fields.update(overrides)
    return Position.model_validate(fields)


def test_position_links_and_state() -> None:
    assert position().link_status == "auto"
    with pytest.raises(ValidationError, match="link_status"):
        position(link_status="none")
    with pytest.raises(ValidationError, match="closed"):
        position(state="closed")
    with pytest.raises(ValidationError, match="parents"):
        position(fill_ids=[uuid4()])


def test_score_links_subject() -> None:
    subject = uuid4()
    fields = {
        "produced_by": "scoring",
        "runtime_ms": 0,
        "parents": [subject],
        "subject_kind": "thesis",
        "subject_id": subject,
        "horizon": "5d",
        "shadow": True,
        "entry_day": date(2026, 9, 24),
        "entry_price": "30",
        "entry_ref": "price_bars:yahoo:SLV:1d:2026-09-24",
        "exit_day": date(2026, 10, 1),
        "exit_price": "31",
        "return_pct": 3.33,
        "attribution": {"lane": "commodity", "origin": "desk"},
    }
    assert Score.model_validate(fields).attribution["lane"] == "commodity"
    with pytest.raises(ValidationError, match="parents"):
        Score.model_validate({**fields, "parents": []})
    with pytest.raises(ValidationError, match="exit_day"):
        Score.model_validate({**fields, "exit_day": date(2026, 9, 1)})
