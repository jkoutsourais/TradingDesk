from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from desk.artifacts.idea import DroppedCandidate, IdeaSelection, LaneCandidate, ScoreIngredient
from desk.artifacts.intake import IntakeMessage
from desk.artifacts.registry import artifact_class
from desk.artifacts.thesis import Thesis


def condition(level: str, operator: str = "below", measure: str = "daily_close") -> dict:
    return {
        "instrument": "SLV",
        "measure": measure,
        "operator": operator,
        "level": level,
        "level_ref": "levels:SLV:low_20d",
    }


def thesis(**overrides: object) -> Thesis:
    evidence = uuid4()
    fields: dict[str, object] = {
        "produced_by": "idea",
        "runtime_ms": 0,
        "parents": [evidence],
        "statement": "Silver outperforms as real yields fall.",
        "origin": "commodity",
        "instruments": ["SLV", "/SI"],
        "direction": "long",
        "horizon": "weeks",
        "review_by": date(2026, 10, 30),
        "drivers": [
            {"statement": "Real yields keep falling", "metric": "DFII10", "source": "fred"}
        ],
        "catalysts": [{"name": "CPI", "on": date(2026, 10, 14), "ref": "calendar_events:cpi"}],
        "evidence": [evidence],
        "invalidation": {
            "warning": condition("29.50", measure="intraday_price"),
            "hard": condition("28.00"),
            "time_limit": None,
        },
        "conviction": 3,
        "state": "active",
    }
    fields.update(overrides)
    return Thesis.model_validate(fields)


def test_kinds_registered() -> None:
    assert artifact_class("thesis") is Thesis
    assert artifact_class("lane_candidate") is LaneCandidate
    assert artifact_class("idea_selection") is IdeaSelection
    assert artifact_class("intake_message") is IntakeMessage


def test_valid_thesis() -> None:
    made = thesis()
    assert made.primary_instrument == "SLV"
    assert made.invalidation is not None
    assert made.invalidation.hard.level == Decimal("28.00")


def test_evidence_must_be_parents() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        thesis(evidence=[uuid4()])


def test_desk_thesis_needs_evidence_but_jon_draft_does_not() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        thesis(evidence=[], parents=[])
    message = uuid4()
    draft = thesis(
        origin="jon",
        evidence=[],
        parents=[message],
        state="draft",
        invalidation=None,
        horizon=None,
        review_by=None,
        conviction=None,
    )
    assert draft.state == "draft"


def test_active_thesis_needs_required_fields() -> None:
    for missing in ("invalidation", "horizon", "review_by", "conviction"):
        with pytest.raises(ValidationError, match=missing):
            thesis(**{missing: None})


def test_long_warning_sits_above_hard_line() -> None:
    with pytest.raises(ValidationError, match="warning"):
        thesis(
            invalidation={
                "warning": condition("27.00"),
                "hard": condition("28.00"),
                "time_limit": None,
            }
        )
    with pytest.raises(ValidationError, match="same way"):
        thesis(
            invalidation={
                "warning": condition("29.50", operator="above"),
                "hard": condition("28.00"),
                "time_limit": None,
            }
        )


def test_short_uses_above_conditions() -> None:
    made = thesis(
        direction="short",
        invalidation={
            "warning": condition("31.00", operator="above"),
            "hard": condition("32.00", operator="above"),
            "time_limit": date(2026, 11, 1),
        },
    )
    assert made.direction == "short"


def test_previous_version_must_be_a_parent() -> None:
    with pytest.raises(ValidationError, match="previous"):
        thesis(previous_id=uuid4())
    earlier = uuid4()
    evidence = uuid4()
    later = thesis(previous_id=earlier, parents=[earlier, evidence], evidence=[evidence])
    assert later.previous_id == earlier


def test_conviction_range() -> None:
    with pytest.raises(ValidationError):
        thesis(conviction=6)


def candidate(**overrides: object) -> LaneCandidate:
    trigger = uuid4()
    fields: dict[str, object] = {
        "produced_by": "idea.lanes",
        "runtime_ms": 0,
        "parents": [trigger],
        "lane": "screen",
        "instrument": "NVDA",
        "driver": "NVDA broke above its 20-day range",
        "score": 0.62,
        "ingredients": [
            {"name": "trigger importance", "value": 0.5, "source_ref": f"trigger:{trigger}"}
        ],
    }
    fields.update(overrides)
    return LaneCandidate.model_validate(fields)


def test_candidate_score_range_and_lane() -> None:
    assert candidate().lane == "screen"
    with pytest.raises(ValidationError):
        candidate(score=1.5)
    with pytest.raises(ValidationError):
        candidate(lane="book_maintenance")
    assert ScoreIngredient(name="x", value=0.1, source_ref="r").value == 0.1


def test_selection_references_parents() -> None:
    chosen, dropped = uuid4(), uuid4()
    selection = IdeaSelection(
        produced_by="idea.select",
        runtime_ms=0,
        parents=(chosen, dropped),
        selected=(chosen,),
        dropped=(DroppedCandidate(candidate_id=dropped, reason="held"),),
    )
    assert selection.selected == (chosen,)
    with pytest.raises(ValidationError, match="parents"):
        IdeaSelection(
            produced_by="idea.select",
            runtime_ms=0,
            parents=(chosen,),
            selected=(chosen,),
            dropped=(DroppedCandidate(candidate_id=dropped, reason="held"),),
        )


def test_intake_message() -> None:
    message = IntakeMessage(produced_by="jon", runtime_ms=0, text="Long SLV, stop 28")
    assert isinstance(message.id, UUID)
    with pytest.raises(ValidationError):
        IntakeMessage(produced_by="jon", runtime_ms=0, text="")
