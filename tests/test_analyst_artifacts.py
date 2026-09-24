from uuid import uuid4

import pytest
from pydantic import ValidationError

from desk.artifacts.analyst import (
    CONFIDENCE,
    AnalystView,
    DebateVerdict,
    HoldingRating,
    Point,
    RiskFlag,
    Rubric,
)
from desk.artifacts.registry import artifact_class


def point(claim: object | None = None, fact: str | None = None) -> dict:
    return {
        "text": "Inflows keep rising.",
        "claim_ids": [claim] if claim else [],
        "fact_refs": [fact] if fact else [],
    }


def view(**overrides: object) -> AnalystView:
    claim, thesis = uuid4(), uuid4()
    fields: dict[str, object] = {
        "produced_by": "analyst.technician",
        "runtime_ms": 0,
        "parents": [thesis, claim],
        "thesis_id": thesis,
        "subject": "SLV",
        "persona": "technician",
        "role": "view",
        "stance": "for",
        "points": [point(claim)],
        "confidence_label": "medium",
    }
    fields.update(overrides)
    return AnalystView.model_validate(fields)


def test_kinds_registered() -> None:
    assert artifact_class("analyst_view") is AnalystView
    assert artifact_class("debate_verdict") is DebateVerdict
    assert artifact_class("holding_rating") is HoldingRating


def test_view_confidence_comes_from_label() -> None:
    made = view()
    assert made.confidence == CONFIDENCE["medium"]
    with pytest.raises(ValidationError):
        view(confidence_label="certain")


def test_points_need_a_citation_and_cited_claims_are_parents() -> None:
    with pytest.raises(ValidationError, match="cite"):
        view(points=[point()])
    assert view(points=[point(fact="levels:SLV:low_20d")]).points[0].fact_refs
    with pytest.raises(ValidationError, match="parents"):
        view(points=[point(uuid4())])


def test_view_subject_needs_thesis_or_holding() -> None:
    with pytest.raises(ValidationError, match="thesis"):
        view(thesis_id=None, parents=[])
    claim = uuid4()
    holding = view(thesis_id=None, parents=[claim], points=[point(claim)], role="keep")
    assert holding.role == "keep"


def verdict(**overrides: object) -> DebateVerdict:
    claim, thesis, bull_view = uuid4(), uuid4(), uuid4()
    argument = [Point.model_validate(point(claim))]
    fields: dict[str, object] = {
        "produced_by": "analyst.judge",
        "runtime_ms": 0,
        "parents": [thesis, bull_view, claim],
        "thesis_id": thesis,
        "subject": "SLV",
        "view_ids": [bull_view],
        "bull": argument,
        "bear": argument,
        "rebuttal": argument,
        "rubric": Rubric(evidence_quality="strong", rebuttal="adequate", risk_reward="fair"),
        "verdict": "watch",
        "confidence_label": "high",
        "conviction_label": "medium",
        "reasons": argument,
        "dissent": "The bear doubts the inflows last.",
    }
    fields.update(overrides)
    return DebateVerdict.model_validate(fields)


def test_verdict_scores_and_links() -> None:
    made = verdict()
    assert made.rubric.score == pytest.approx((3 + 2 + 2) / 9)
    assert made.conviction == 3 and made.confidence == CONFIDENCE["high"]
    with pytest.raises(ValidationError, match="parents"):
        verdict(view_ids=[uuid4()])
    with pytest.raises(ValidationError, match="thesis"):
        verdict(parents=[])


def test_verdict_must_cite_a_claim() -> None:
    fact_only = [Point.model_validate(point(fact="fred:DFII10"))]
    with pytest.raises(ValidationError, match="verified claim"):
        verdict(reasons=fact_only)


def rating(**overrides: object) -> HoldingRating:
    claim = uuid4()
    fields: dict[str, object] = {
        "produced_by": "holdings.judge",
        "runtime_ms": 0,
        "parents": [claim],
        "subject": "AEP",
        "account_refs": ["ibkr:0000"],
        "rating": "hold",
        "confidence_label": "medium",
        "reasons": [point(claim)],
        "what_would_change_it": "A daily close below the 20-day low.",
        "suggested_action": "Hold; no change.",
    }
    fields.update(overrides)
    return HoldingRating.model_validate(fields)


def test_rating_and_forced_override() -> None:
    assert rating().rating == "hold"
    flag = RiskFlag(code="levered_days", detail="3x fund held 12 days", forces="trim")
    forced = rating(risk_flags=[flag], rating="trim", judged_rating="hold")
    assert forced.judged_rating == "hold"
    with pytest.raises(ValidationError, match="forces"):
        rating(risk_flags=[flag], rating="hold")
    with pytest.raises(ValidationError, match="judged"):
        rating(rating="trim", judged_rating="hold")


def test_rating_without_claims_may_cite_facts() -> None:
    made = rating(parents=[], reasons=[point(fact="levels:AEP:low_20d")])
    assert made.reasons[0].fact_refs == ("levels:AEP:low_20d",)


def test_round_trip_through_payload() -> None:
    for made in (view(), verdict(), rating()):
        again = type(made).model_validate(made.model_dump(mode="json"))
        assert again == made
