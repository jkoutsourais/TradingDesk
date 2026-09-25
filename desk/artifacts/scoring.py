"""Trade capture and scoring artifacts: Fill, Position and Score.

Fills come from the brokers verbatim. A Position is rebuilt by code from its fills and
versioned when fills or its thesis link change. A Score is computed by code from stored
daily bars (shadow) or from a position's fills (real); every number is code-computed.
"""

from datetime import date
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact


@register_artifact
class Fill(ArtifactBase):
    kind: Literal["fill"] = "fill"
    schema_version: Literal[1] = 1

    broker: Literal["ibkr", "tastytrade"]
    account_ref: str = Field(min_length=1)  # "<broker>:<last four>"
    exec_id: str = Field(min_length=1)  # the broker's execution id; unique per broker
    symbol: str = Field(min_length=1)  # underlying
    contract: str = Field(min_length=1)  # what was traded
    asset_class: Literal["equity", "option", "future", "future_option", "crypto", "other"]
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(ge=0)
    multiplier: Decimal = Field(gt=0)
    fees: Decimal = Field(ge=0)
    executed_at: AwareDatetime


LinkStatus = Literal["none", "suggested", "auto", "confirmed"]


@register_artifact
class Position(ArtifactBase):
    kind: Literal["position"] = "position"
    schema_version: Literal[1] = 1

    account_ref: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    contract: str = Field(min_length=1)
    direction: Literal["long", "short"]
    state: Literal["open", "closed"]
    quantity: Decimal  # signed open quantity
    max_quantity: Decimal = Field(ge=0)
    avg_entry: Decimal | None = None
    avg_exit: Decimal | None = None
    realized_pnl: Decimal
    opened_at: AwareDatetime
    closed_at: AwareDatetime | None = None
    fill_ids: tuple[UUID, ...] = Field(min_length=1)
    # The plan or thesis this position trades, and how the link was made.
    plan_id: UUID | None = None
    thesis_id: UUID | None = None
    link_status: LinkStatus = "none"
    planned_max_loss: Decimal | None = None
    previous_id: UUID | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        linked = [i for i in (self.plan_id, self.thesis_id, self.previous_id) if i is not None]
        if set(self.fill_ids) - set(self.parents) or set(linked) - set(self.parents):
            raise ValueError(
                "fills, the linked plan or thesis and the previous version must be parents"
            )
        if (self.plan_id is None and self.thesis_id is None) != (self.link_status == "none"):
            raise ValueError("link_status is none exactly when nothing is linked")
        if (self.state == "closed") != (self.quantity == 0):
            raise ValueError("a closed position has zero quantity")
        return self


@register_artifact
class Score(ArtifactBase):
    kind: Literal["score"] = "score"
    schema_version: Literal[1] = 1

    subject_kind: Literal["thesis", "plan", "rating", "view", "verdict", "position"]
    subject_id: UUID
    horizon: Literal["1d", "5d", "20d", "exit"]
    shadow: bool  # True for price-only scores, False for real positions
    entry_day: date
    entry_price: Decimal
    entry_ref: str = Field(min_length=1)
    exit_day: date
    exit_price: Decimal
    return_pct: float  # direction-adjusted
    mae_pct: float | None = None
    mfe_pct: float | None = None
    first_hit: Literal["stop", "target", "none"] | None = None
    r_multiple: float | None = None
    hit: bool | None = None  # was the call right (rating, stance, verdict)
    realized_pnl: Decimal | None = None
    adherence: dict[str, bool | None] = Field(default_factory=dict)
    # Rollup dimensions: lane, persona, verdict, tier, instrument_class, origin, veto_reason.
    attribution: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.subject_id not in self.parents:
            raise ValueError("the scored subject must be listed in parents")
        if self.exit_day < self.entry_day:
            raise ValueError("exit_day is before entry_day")
        return self
