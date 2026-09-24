"""Idea desk artifacts: LaneCandidate and IdeaSelection.

A lane turns watch hits and verified claims into candidates with a code-computed score;
the selection keeps the best few and records why every other candidate was dropped, so
the Lanes view can replay each shift's funnel.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact
from desk.artifacts.thesis import Lane


class ScoreIngredient(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    value: float
    source_ref: str = Field(min_length=1)


@register_artifact
class LaneCandidate(ArtifactBase):
    kind: Literal["lane_candidate"] = "lane_candidate"
    schema_version: Literal[1] = 1

    lane: Lane
    instrument: str = Field(min_length=1)
    driver: str = Field(min_length=1, max_length=300)
    score: float = Field(ge=0.0, le=1.0)
    ingredients: tuple[ScoreIngredient, ...] = Field(min_length=1)


class DroppedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: UUID
    # e.g. "held", "duplicate of NVDA (screen)", "below the cut", "research failed"
    reason: str = Field(min_length=1)


@register_artifact
class IdeaSelection(ArtifactBase):
    kind: Literal["idea_selection"] = "idea_selection"
    schema_version: Literal[1] = 1

    selected: tuple[UUID, ...]
    dropped: tuple[DroppedCandidate, ...] = ()

    @model_validator(mode="after")
    def _candidates_are_parents(self) -> Self:
        named = set(self.selected) | {d.candidate_id for d in self.dropped}
        if named - set(self.parents):
            raise ValueError("every selected or dropped candidate must be listed in parents")
        return self
