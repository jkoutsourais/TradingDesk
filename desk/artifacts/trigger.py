"""Trigger: a Watch desk hit worth attention, scored by code.

Every number in `observed` is computed by code from stored data and names where it came
from in `source_ref`, so a later fact-check can recompute it. News-driven triggers list
the raw records they came from in `parents`.
"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact


class ObservedValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    value: Decimal
    unit: str
    # e.g. "price_bars:yahoo:NVDA:1d:2026-09-22" or "computed:atr20(price_bars:...)"
    source_ref: str = Field(min_length=1)


@register_artifact
class Trigger(ArtifactBase):
    kind: Literal["trigger"] = "trigger"
    schema_version: Literal[1] = 1

    rule_id: str = Field(min_length=1)
    # Canonical symbol, or a topic such as "policy:tariffs" for non-instrument hits.
    instrument: str = Field(min_length=1)
    tier: int | None = Field(default=None, ge=0, le=3)
    importance: float = Field(ge=0.0, le=1.0)
    urgent: bool
    summary: str = Field(min_length=1)
    # Stable key for cooldowns: the same fingerprint within the cooldown is not re-fired.
    fingerprint: str = Field(min_length=1)
    observed: tuple[ObservedValue, ...] = ()
