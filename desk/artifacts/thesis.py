"""Thesis: one tradeable idea, from an idea lane or from Jon.

Every level in `invalidation` names where it came from in `level_ref`: a code-computed
level (e.g. "levels:SLV:low_20d") or Jon's own message ("intake_message:<id>"). A change
to a thesis, including a state change, is a new Thesis with the previous one in `parents`
and `previous_id`.
"""

from datetime import date
from decimal import Decimal
from typing import Literal, Self, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact

Lane = Literal[
    "catalyst",
    "screen",
    "macro",
    "commodity",
    "power_grid",
    "policy",
    "event_additions",
]
LANES: tuple[str, ...] = get_args(Lane)
Origin = Literal[
    "jon",
    "catalyst",
    "screen",
    "macro",
    "commodity",
    "power_grid",
    "policy",
    "event_additions",
]
ThesisState = Literal["draft", "active", "triggered", "in_position", "closed", "invalidated"]
# States a thesis never leaves.
FINAL_STATES: frozenset[str] = frozenset({"closed", "invalidated"})


class Condition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    # intraday_price checks the live quote; daily_close checks the stored daily bar.
    measure: Literal["intraday_price", "daily_close"]
    operator: Literal["below", "above"]
    level: Decimal = Field(gt=0)
    level_ref: str = Field(min_length=1)

    def crossed(self, value: Decimal) -> bool:
        return value < self.level if self.operator == "below" else value > self.level


class Invalidation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Crossing the warning flags the thesis; only the hard line or the time limit
    # invalidates it.
    warning: Condition
    hard: Condition
    time_limit: date | None = None

    @model_validator(mode="after")
    def _warning_before_hard(self) -> Self:
        if self.warning.operator != self.hard.operator:
            raise ValueError("warning and hard conditions must point the same way")
        if self.warning.instrument == self.hard.instrument:
            if self.hard.operator == "below":
                wrong_side = self.warning.level < self.hard.level
            else:
                wrong_side = self.warning.level > self.hard.level
            if wrong_side:
                raise ValueError("the warning level must be reached before the hard line")
        return self


class Driver(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1, max_length=300)
    metric: str = Field(min_length=1)  # e.g. "DFII10", "SLV relative strength"
    source: str = Field(min_length=1)  # the feed that measures it, e.g. "fred"


class Catalyst(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    on: date
    ref: str | None = None  # e.g. "calendar_events:<event_key>"


@register_artifact
class Thesis(ArtifactBase):
    kind: Literal["thesis"] = "thesis"
    schema_version: Literal[1] = 1

    statement: str = Field(min_length=1, max_length=400)
    origin: Origin
    # Primary first, then proxies; a relative thesis reads instruments[0] over [1].
    instruments: tuple[str, ...] = Field(min_length=1)
    direction: Literal["long", "short", "relative"]
    horizon: Literal["days", "weeks", "months"] | None = None
    review_by: date | None = None
    drivers: tuple[Driver, ...] = Field(min_length=1)
    catalysts: tuple[Catalyst, ...] = ()
    evidence: tuple[UUID, ...] = ()  # VerifiedClaim ids
    invalidation: Invalidation | None = None
    entry_conditions: tuple[str, ...] = ()
    conviction: int | None = Field(default=None, ge=1, le=5)
    # "state", not "status": the envelope's status (ok/failed) says whether writing it ran.
    state: ThesisState
    previous_id: UUID | None = None
    # Why this version exists, e.g. "hard line crossed: SLV daily close 27.80".
    change_note: str | None = None

    @property
    def primary_instrument(self) -> str:
        return self.instruments[0]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if set(self.evidence) - set(self.parents):
            raise ValueError("every evidence claim must be listed in parents")
        if self.previous_id is not None and self.previous_id not in self.parents:
            raise ValueError("the previous version must be listed in parents")
        if self.origin != "jon" and not self.evidence:
            raise ValueError("a desk thesis needs at least one verified claim as evidence")
        if self.direction == "relative" and len(self.instruments) < 2:
            raise ValueError("a relative thesis needs two instruments")
        if self.state != "draft":
            for name in ("invalidation", "horizon", "review_by", "conviction"):
                if getattr(self, name) is None:
                    raise ValueError(f"{name} is required once a thesis leaves draft")
        if self.invalidation is not None and self.direction != "relative":
            expected = "below" if self.direction == "long" else "above"
            if self.invalidation.hard.operator != expected:
                raise ValueError(f"a {self.direction} thesis is invalidated {expected} its levels")
        return self
