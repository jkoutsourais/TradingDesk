"""Front office artifacts: the fact snapshot a brief was written from, the brief itself,
and small-model triage labels.

A Brief's numbers come only from its FactSnapshot, which it lists as a parent; each fact
names the stored data it was computed from. Push delivery is tracked in the pushes table
rather than on the Brief, since artifacts never change after they are written.
"""

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact


class SnapshotFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    value: str
    unit: str
    display: str
    label: str
    source_ref: str = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> str:
        # Decimals are stored as their exact text so JSON never rounds them.
        return str(value) if isinstance(value, Decimal) else value  # type: ignore[return-value]


@register_artifact
class FactSnapshot(ArtifactBase):
    kind: Literal["fact_snapshot"] = "fact_snapshot"
    schema_version: Literal[1] = 1

    purpose: str = Field(min_length=1)  # e.g. "morning_brief"
    facts: tuple[SnapshotFact, ...]

    @field_validator("facts")
    @classmethod
    def _unique_ids(cls, facts: tuple[SnapshotFact, ...]) -> tuple[SnapshotFact, ...]:
        ids = [fact.id for fact in facts]
        if len(ids) != len(set(ids)):
            raise ValueError("fact snapshot has duplicate fact ids")
        return facts


class BriefSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    lines: tuple[str, ...]


@register_artifact
class Brief(ArtifactBase):
    kind: Literal["brief"] = "brief"
    schema_version: Literal[1] = 1

    brief_kind: Literal["morning", "weekend", "urgent"]
    covers_from: AwareDatetime
    covers_to: AwareDatetime
    fact_snapshot_id: UUID
    sections: tuple[BriefSection, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.fact_snapshot_id not in self.parents:
            raise ValueError("a brief must list its fact snapshot in parents")
        if self.covers_to <= self.covers_from:
            raise ValueError("covers_to must be after covers_from")
        return self


@register_artifact
class TriageLabel(ArtifactBase):
    kind: Literal["triage_label"] = "triage_label"
    schema_version: Literal[1] = 1

    subject_id: UUID  # the trigger or raw record being judged
    label: Literal["relevant", "noise"]
    reason: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def _cites_subject(self) -> Self:
        if self.subject_id not in self.parents:
            raise ValueError("a triage label must list its subject in parents")
        return self
