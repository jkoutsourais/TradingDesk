"""Trader and risk desk artifacts: TradePlan and RiskDecision.

Every price in a plan is copied from a quote, a stored bar or a code-computed level and
names that source in its `*_ref`; the trader model only chooses among code-built
options. A RiskDecision records every check the risk desk ran and why a plan was sized
down or vetoed. Nothing here places orders.
"""

from datetime import date
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact

Structure = Literal[
    "shares", "etf", "levered_etf", "long_call", "long_put", "debit_spread", "future"
]
OPTION_STRUCTURES = ("long_call", "long_put", "debit_spread")


class Leg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["buy", "sell"]
    kind: Literal["shares", "option", "future"]
    symbol: str = Field(min_length=1)
    expiry: date | None = None
    strike: Decimal | None = None
    right: Literal["call", "put"] | None = None
    ratio: int = Field(default=1, ge=1)
    price: Decimal = Field(ge=0)
    price_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def _option_fields(self) -> Self:
        if self.kind == "option" and None in (self.expiry, self.strike, self.right):
            raise ValueError("an option leg needs expiry, strike and right")
        return self


@register_artifact
class TradePlan(ArtifactBase):
    kind: Literal["trade_plan"] = "trade_plan"
    schema_version: Literal[1] = 1

    # A plan comes from a pursued thesis or a Buy/Add holding rating.
    thesis_id: UUID | None = None
    rating_id: UUID | None = None
    account_ref: str = Field(min_length=1)
    subject: str = Field(min_length=1)  # the thesis or holding instrument
    instrument: str = Field(min_length=1)  # what is traded
    structure: Structure
    direction: Literal["long", "short"]
    legs: tuple[Leg, ...] = Field(min_length=1)
    # entry, stop and target are prices of the subject.
    entry: Decimal = Field(gt=0)
    entry_ref: str = Field(min_length=1)
    stop: Decimal | None = None
    stop_ref: str | None = None
    target: Decimal | None = None
    target_ref: str | None = None
    expiry: date | None = None
    unit_cost: Decimal = Field(ge=0)  # cash per unit including the contract multiplier
    unit_max_loss: Decimal = Field(gt=0)
    leverage: Decimal = Decimal(1)
    spread_pct: float | None = None
    open_interest: int | None = None
    conviction: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1, max_length=800)
    alternatives_rejected: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.thesis_id is None) == (self.rating_id is None):
            raise ValueError("a plan comes from exactly one thesis or a holding rating")
        source = self.thesis_id or self.rating_id
        if source not in self.parents:
            raise ValueError("the plan's thesis or rating must be listed in parents")
        if self.structure in OPTION_STRUCTURES and self.expiry is None:
            raise ValueError("an option plan needs an expiry")
        return self


class CheckRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    result: Literal["pass", "warn", "fail"]
    detail: str = Field(min_length=1)


@register_artifact
class RiskDecision(ArtifactBase):
    kind: Literal["risk_decision"] = "risk_decision"
    schema_version: Literal[1] = 1

    plan_id: UUID
    decision: Literal["approved", "resized", "vetoed"]
    tier_pct: Decimal
    cap: Decimal  # dollars at risk allowed at this conviction
    requested_size: int = Field(ge=0)
    size: int = Field(ge=0)
    max_loss: Decimal = Field(ge=0)
    cost: Decimal = Field(ge=0)
    funding_needed: Decimal = Field(ge=0)
    checks: tuple[CheckRecord, ...] = Field(min_length=1)
    veto_reasons: tuple[str, ...] = ()
    # Plain-English explanation for anything beyond shares or a single long option.
    risk_note: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.plan_id not in self.parents:
            raise ValueError("the plan must be listed in parents")
        failed = any(check.result == "fail" for check in self.checks)
        if self.decision == "vetoed":
            if not (failed and self.veto_reasons and self.size == 0):
                raise ValueError("a veto needs a failed check, veto reasons and size 0")
        else:
            if failed:
                raise ValueError("an approved or resized plan cannot have failed checks")
            if self.size < 1:
                raise ValueError("an approved or resized plan needs a size")
            if self.decision == "resized" and self.size >= self.requested_size:
                raise ValueError("a resized plan is smaller than requested")
        return self
