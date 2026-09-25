from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from desk.artifacts.registry import artifact_class
from desk.artifacts.trade import CheckRecord, Leg, RiskDecision, TradePlan


def plan(**overrides: object) -> TradePlan:
    thesis = uuid4()
    fields: dict[str, object] = {
        "produced_by": "trader",
        "runtime_ms": 0,
        "parents": [thesis],
        "thesis_id": thesis,
        "account_ref": "ibkr:0000",
        "subject": "SLV",
        "instrument": "SLV",
        "structure": "shares",
        "direction": "long",
        "legs": [
            {
                "action": "buy",
                "kind": "shares",
                "symbol": "SLV",
                "price": "30",
                "price_ref": "quotes_latest:SLV",
            }
        ],
        "entry": "30",
        "entry_ref": "quotes_latest:SLV",
        "stop": "28",
        "stop_ref": "levels:SLV:low_20d",
        "target": "34",
        "target_ref": "levels:SLV:high_52w",
        "unit_cost": "30",
        "unit_max_loss": "2",
        "conviction": 4,
        "rationale": "Shares keep it simple.",
        "alternatives_rejected": ["Calls: spread too wide."],
    }
    fields.update(overrides)
    return TradePlan.model_validate(fields)


def test_kinds_registered() -> None:
    assert artifact_class("trade_plan") is TradePlan
    assert artifact_class("risk_decision") is RiskDecision


def test_plan_links_one_source() -> None:
    assert plan().thesis_id is not None
    with pytest.raises(ValidationError, match="thesis or a holding rating"):
        plan(thesis_id=None)
    with pytest.raises(ValidationError, match="parents"):
        plan(parents=[])


def test_option_plan_needs_expiry_and_option_legs() -> None:
    leg = {
        "action": "buy",
        "kind": "option",
        "symbol": "SLV  261120C00031000",
        "expiry": date(2026, 11, 20),
        "strike": "31",
        "right": "call",
        "price": "1.20",
        "price_ref": "dxlink:quote",
    }
    made = plan(
        structure="long_call",
        legs=[leg],
        expiry=date(2026, 11, 20),
        unit_cost="120",
        unit_max_loss="120",
    )
    assert made.legs[0].right == "call"
    with pytest.raises(ValidationError, match="expiry"):
        plan(structure="long_call", legs=[leg], unit_cost="120", unit_max_loss="120")
    with pytest.raises(ValidationError, match="strike"):
        Leg.model_validate({**leg, "strike": None})


def decision(**overrides: object) -> RiskDecision:
    plan_id = uuid4()
    fields: dict[str, object] = {
        "produced_by": "risk",
        "runtime_ms": 0,
        "parents": [plan_id],
        "plan_id": plan_id,
        "decision": "approved",
        "tier_pct": "5",
        "cap": "125",
        "requested_size": 50,
        "size": 50,
        "max_loss": "100",
        "cost": "1500",
        "funding_needed": "0",
        "checks": [CheckRecord(name="max_loss", result="pass", detail="ok")],
    }
    fields.update(overrides)
    return RiskDecision.model_validate(fields)


def test_decision_consistency() -> None:
    assert decision().size == 50
    vetoed = decision(
        decision="vetoed",
        size=0,
        max_loss="0",
        cost="0",
        checks=[CheckRecord(name="liquidity", result="fail", detail="wide")],
        veto_reasons=["wide"],
    )
    assert vetoed.veto_reasons == ("wide",)
    with pytest.raises(ValidationError, match="veto"):
        decision(decision="vetoed", size=0)
    with pytest.raises(ValidationError, match="failed"):
        decision(checks=[CheckRecord(name="liquidity", result="fail", detail="wide")])
    with pytest.raises(ValidationError, match="resized"):
        decision(decision="resized")
    assert decision(decision="resized", size=20).decision == "resized"
