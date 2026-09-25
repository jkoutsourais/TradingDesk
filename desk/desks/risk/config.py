"""Typed loader for config/risk.yaml."""

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from desk.config import CONFIG_DIR

Structure = Literal[
    "shares", "etf", "levered_etf", "long_call", "long_put", "debit_spread", "future"
]
AccountType = Literal["roth", "individual"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AccountRules(_Frozen):
    structures: tuple[Structure, ...]
    allow_short_shares: bool
    settled_cash_only: bool


class Liquidity(_Frozen):
    max_option_spread_pct: float = Field(gt=0)
    min_open_interest: int = Field(ge=0)


class RiskConfig(_Frozen):
    accounts: dict[str, AccountType]
    account_types: dict[AccountType, AccountRules]
    tier_pct: dict[Structure, Decimal]
    conviction_scale: dict[int, Decimal]
    total_open_risk_pct: Decimal = Field(gt=0)
    bucket_cap_pct: Decimal = Field(gt=0)
    buckets: dict[str, tuple[str, ...]]
    levered_funds: dict[str, Decimal]
    liquidity: Liquidity
    assumed_stop_atr: Decimal = Field(gt=0)
    stop_tolerance_pct: Decimal = Field(ge=0)


def load_risk_config(config_dir: Path = CONFIG_DIR) -> RiskConfig:
    with (config_dir / "risk.yaml").open(encoding="utf-8") as handle:
        return RiskConfig.model_validate(yaml.safe_load(handle))
