"""Common envelope shared by every artifact passed between desks."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class ArtifactStatus(StrEnum):
    OK = "ok"
    # Written when structured output fails to parse twice, so the shift can continue.
    FAILED = "failed"


def utc_now() -> datetime:
    return datetime.now(UTC)


class ArtifactBase(BaseModel):
    """Immutable envelope. Subclasses pin `kind` and `schema_version` with Literal defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    kind: str
    schema_version: int
    status: ArtifactStatus = ArtifactStatus.OK
    error: str | None = None
    shift_id: UUID | None = None
    produced_by: str = Field(min_length=1)
    parents: tuple[UUID, ...] = ()
    created_at: AwareDatetime = Field(default_factory=utc_now)
    model: str | None = None
    prompt_version: str | None = None
    runtime_ms: int = Field(ge=0)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)

    @field_validator("created_at")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("parents")
    @classmethod
    def _parents_unique(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("parents contains duplicate ids")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.id in self.parents:
            raise ValueError("an artifact cannot be its own parent")
        if self.status is ArtifactStatus.FAILED and not self.error:
            raise ValueError("a failed artifact must carry an error message")
        if self.status is ArtifactStatus.OK and self.error is not None:
            raise ValueError("an ok artifact must not carry an error message")
        return self


ENVELOPE_FIELDS: frozenset[str] = frozenset(ArtifactBase.model_fields)
