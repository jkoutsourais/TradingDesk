from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from desk.artifacts.registry import artifact_class
from desk.artifacts.trigger import ObservedValue, Trigger


def make_trigger(**overrides: object) -> Trigger:
    fields: dict[str, object] = {
        "produced_by": "watch.move_vs_prior_close",
        "runtime_ms": 3,
        "rule_id": "move_vs_prior_close",
        "instrument": "NVDA",
        "tier": 1,
        "importance": 0.62,
        "urgent": False,
        "summary": "NVDA up 3.1 ATR from prior close",
        "fingerprint": "move_vs_prior_close:NVDA:2026-09-23:up",
        "observed": [
            {
                "name": "last",
                "value": "231.40",
                "unit": "USD",
                "source_ref": "quotes_latest:NVDA:2026-09-23T14:31:00Z",
            },
            {
                "name": "atr_multiple",
                "value": "3.1",
                "unit": "ATR",
                "source_ref": "computed:atr20(price_bars:yahoo:NVDA:1d)",
            },
        ],
    }
    fields.update(overrides)
    return Trigger.model_validate(fields)


def test_registered_and_frozen() -> None:
    trigger = make_trigger()
    assert artifact_class("trigger") is Trigger
    with pytest.raises(ValidationError):
        trigger.importance = 0.9  # type: ignore[misc]


def test_observed_values_are_decimals_with_sources() -> None:
    trigger = make_trigger()
    assert trigger.observed[0] == ObservedValue(
        name="last",
        value=Decimal("231.40"),
        unit="USD",
        source_ref="quotes_latest:NVDA:2026-09-23T14:31:00Z",
    )


def test_observed_value_needs_a_source() -> None:
    with pytest.raises(ValidationError):
        make_trigger(observed=[{"name": "last", "value": "1", "unit": "USD", "source_ref": ""}])


@pytest.mark.parametrize("importance", [-0.01, 1.01])
def test_importance_is_bounded(importance: float) -> None:
    with pytest.raises(ValidationError):
        make_trigger(importance=importance)


@pytest.mark.parametrize("tier", [-1, 4])
def test_tier_is_bounded(tier: int) -> None:
    with pytest.raises(ValidationError):
        make_trigger(tier=tier)


def test_policy_trigger_without_tier_or_numbers() -> None:
    record_id = uuid4()
    trigger = make_trigger(
        rule_id="policy_keywords",
        instrument="policy:tariffs",
        tier=None,
        observed=[],
        parents=[record_id],
        fingerprint="policy_keywords:truth:123",
    )
    assert trigger.tier is None
    assert trigger.parents == (record_id,)


def test_fingerprint_and_rule_required() -> None:
    with pytest.raises(ValidationError):
        make_trigger(fingerprint="")
    with pytest.raises(ValidationError):
        make_trigger(rule_id="")
