"""Analyst desk artifacts: AnalystView, DebateVerdict and HoldingRating.

Mapped numbers (confidence, conviction, rubric score) are properties, not stored fields,
so the stored payload holds only the labels.

Models return labels (low/medium/high, weak/adequate/strong); code maps them to the
stored numbers, so no number in these artifacts comes from a model. Every point cites
verified claims (by id, listed in parents) or code-computed facts (by source ref).
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact

ConfidenceLabel = Literal["low", "medium", "high"]
CONFIDENCE: dict[str, float] = {"low": 0.35, "medium": 0.6, "high": 0.85}
Grade = Literal["weak", "adequate", "strong"]
GRADE: dict[str, int] = {"weak": 1, "adequate": 2, "strong": 3}
RiskReward = Literal["poor", "fair", "good"]
RISK_REWARD: dict[str, int] = {"poor": 1, "fair": 2, "good": 3}
ConvictionLabel = Literal["very_low", "low", "medium", "high", "very_high"]
CONVICTION: dict[str, int] = {"very_low": 1, "low": 2, "medium": 3, "high": 4, "very_high": 5}
Rating = Literal["buy_add", "hold", "trim", "sell"]
# Higher is more defensive; a risk flag can force a rating at least this far.
RATING_SEVERITY: dict[str, int] = {"buy_add": 0, "hold": 1, "trim": 2, "sell": 3}


class Point(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=600)
    claim_ids: tuple[UUID, ...] = ()  # VerifiedClaim ids
    fact_refs: tuple[str, ...] = ()  # source refs of code-computed facts

    @model_validator(mode="after")
    def _cited(self) -> Self:
        if not self.claim_ids and not self.fact_refs:
            raise ValueError("every point must cite a verified claim or a fact")
        return self


def _claims(points: tuple[Point, ...]) -> set[UUID]:
    return {claim for point in points for claim in point.claim_ids}


def _check_claims_are_parents(parents: tuple[UUID, ...], *groups: tuple[Point, ...]) -> None:
    cited = set().union(*(_claims(group) for group in groups))
    if cited - set(parents):
        raise ValueError("every cited claim must be listed in parents")


@register_artifact
class AnalystView(ArtifactBase):
    kind: Literal["analyst_view"] = "analyst_view"
    schema_version: Literal[1] = 1

    # A thesis debate names its thesis; a holding debate names only the subject.
    thesis_id: UUID | None = None
    subject: str = Field(min_length=1)
    persona: str = Field(min_length=1)
    # view: a specialist's filing; keep/exit: the two sides of a holding debate.
    role: Literal["view", "keep", "exit"]
    stance: Literal["for", "against", "neutral"]
    points: tuple[Point, ...] = Field(min_length=1)
    confidence_label: ConfidenceLabel

    @property
    def confidence(self) -> float:
        return CONFIDENCE[self.confidence_label]

    @model_validator(mode="after")
    def _linked(self) -> Self:
        if self.role == "view" and self.thesis_id is None:
            raise ValueError("a specialist view must name its thesis")
        if self.thesis_id is not None and self.thesis_id not in self.parents:
            raise ValueError("the thesis must be listed in parents")
        _check_claims_are_parents(self.parents, self.points)
        return self


class Rubric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_quality: Grade
    rebuttal: Grade  # how well the winning side answered the strongest counterpoint
    risk_reward: RiskReward

    @property
    def score(self) -> float:
        total = GRADE[self.evidence_quality] + GRADE[self.rebuttal] + RISK_REWARD[self.risk_reward]
        return total / 9


@register_artifact
class DebateVerdict(ArtifactBase):
    kind: Literal["debate_verdict"] = "debate_verdict"
    schema_version: Literal[1] = 1

    thesis_id: UUID
    subject: str = Field(min_length=1)
    view_ids: tuple[UUID, ...]
    bull: tuple[Point, ...] = Field(min_length=1)
    bear: tuple[Point, ...] = Field(min_length=1)
    rebuttal: tuple[Point, ...] = Field(min_length=1)
    rubric: Rubric
    verdict: Literal["pursue", "watch", "reject"]
    confidence_label: ConfidenceLabel
    # Set for desk theses; Jon sets conviction on his own.
    conviction_label: ConvictionLabel | None = None
    reasons: tuple[Point, ...] = Field(min_length=1)
    dissent: str = Field(min_length=1, max_length=800)

    @property
    def confidence(self) -> float:
        return CONFIDENCE[self.confidence_label]

    @property
    def conviction(self) -> int | None:
        return CONVICTION[self.conviction_label] if self.conviction_label else None

    @model_validator(mode="after")
    def _linked(self) -> Self:
        if self.thesis_id not in self.parents:
            raise ValueError("the thesis must be listed in parents")
        if set(self.view_ids) - set(self.parents):
            raise ValueError("every view must be listed in parents")
        _check_claims_are_parents(self.parents, self.bull, self.bear, self.rebuttal, self.reasons)
        if not _claims(self.reasons):
            raise ValueError("the judge's reasons must cite at least one verified claim")
        return self


class RiskFlag(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)  # e.g. "concentration", "levered_days", "option_expiry"
    detail: str = Field(min_length=1)
    # The rating this flag forces at minimum, if any.
    forces: Rating | None = None


@register_artifact
class HoldingRating(ArtifactBase):
    kind: Literal["holding_rating"] = "holding_rating"
    schema_version: Literal[1] = 1

    subject: str = Field(min_length=1)
    account_refs: tuple[str, ...] = Field(min_length=1)  # "<broker>:<last four>"
    thesis_id: UUID | None = None
    rating: Rating
    previous_rating: Rating | None = None
    # The judge's own rating when a risk flag forced a more defensive one.
    judged_rating: Rating | None = None
    confidence_label: ConfidenceLabel
    reasons: tuple[Point, ...] = Field(min_length=1)
    what_would_change_it: str = Field(min_length=1, max_length=400)
    risk_flags: tuple[RiskFlag, ...] = ()
    suggested_action: str = Field(min_length=1, max_length=400)

    @property
    def confidence(self) -> float:
        return CONFIDENCE[self.confidence_label]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_claims_are_parents(self.parents, self.reasons)
        if self.thesis_id is not None and self.thesis_id not in self.parents:
            raise ValueError("the linked thesis must be listed in parents")
        floor = max((RATING_SEVERITY[f.forces] for f in self.risk_flags if f.forces), default=-1)
        if RATING_SEVERITY[self.rating] < floor:
            raise ValueError("a risk flag forces a more defensive rating")
        if self.judged_rating is not None and (self.judged_rating == self.rating or floor < 0):
            raise ValueError("judged_rating is only set when a risk flag overrode it")
        return self
