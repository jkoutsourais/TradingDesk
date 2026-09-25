"""Holdings risk flags, computed by code from broker snapshots (config/holdings.yaml)."""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from desk.artifacts.analyst import RiskFlag
from desk.collectors.holdings import latest_snapshots
from desk.config import CONFIG_DIR

OCC_EXPIRY = re.compile(r"(\d{6})[CP]\d{8}")
DATED_EXPIRY = re.compile(r"\b(\d{1,2})([A-Z]{3})(\d{2})\b")
MONTHS = {
    name: index
    for index, name in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Concentration(_Frozen):
    warn_pct: float = Field(gt=0)
    trim_pct: float = Field(gt=0)


class LeveredFunds(_Frozen):
    warn_days: int = Field(gt=0)
    trim_days: int = Field(gt=0)


class Options(_Frozen):
    warn_days: int = Field(gt=0)
    sell_days: int = Field(ge=0)


class HoldingsConfig(_Frozen):
    concentration: Concentration
    levered_funds: LeveredFunds
    options: Options


def load_holdings_config(config_dir: Path = CONFIG_DIR) -> HoldingsConfig:
    with (config_dir / "holdings.yaml").open(encoding="utf-8") as handle:
        return HoldingsConfig.model_validate(yaml.safe_load(handle))


def option_expiry(contract: str) -> date | None:
    """Expiry from an OCC symbol (SLV   261016C00030000) or a dated one (SLV 16OCT26 30 C)."""
    occ = OCC_EXPIRY.search(contract.replace(" ", ""))
    if occ:
        raw = occ.group(1)
        return date(2000 + int(raw[:2]), int(raw[2:4]), int(raw[4:]))
    dated = DATED_EXPIRY.search(contract.upper())
    if dated and dated.group(2) in MONTHS:
        return date(2000 + int(dated.group(3)), MONTHS[dated.group(2)], int(dated.group(1)))
    return None


@dataclass
class Holding:
    symbol: str
    accounts: list[str] = field(default_factory=list)
    asset_classes: set[str] = field(default_factory=set)
    market_value: Decimal = Decimal(0)
    cost_basis: Decimal = Decimal(0)
    # Largest share of any one account's net liquidation, in percent.
    max_share_pct: float | None = None
    first_seen: date | None = None
    expiries: list[date] = field(default_factory=list)


def load_holdings(conn: Connection, tz: ZoneInfo) -> list[Holding]:
    holdings: dict[str, Holding] = {}
    for snapshot in latest_snapshots(conn):
        net_liq = snapshot["net_liquidation"]
        for position in snapshot["positions"]:
            if not position["quantity"]:
                continue
            holding = holdings.setdefault(position["symbol"], Holding(position["symbol"]))
            if snapshot["account_ref"] not in holding.accounts:
                holding.accounts.append(snapshot["account_ref"])
            holding.asset_classes.add(position["asset_class"])
            value = Decimal(position["market_value"] or 0)
            holding.market_value += value
            holding.cost_basis += Decimal(position["cost_basis"] or 0)
            if net_liq:
                share = float(abs(value) / Decimal(net_liq) * 100)
                holding.max_share_pct = max(holding.max_share_pct or 0.0, share)
            if position["asset_class"] in ("option", "future_option"):
                expiry = option_expiry(position["contract"])
                if expiry is not None:
                    holding.expiries.append(expiry)
    for row in conn.execute(
        text(
            "SELECT p.symbol, min(s.as_of) AS first FROM position_snapshots p "
            "JOIN account_snapshots s ON s.id = p.snapshot_id WHERE p.quantity <> 0 "
            "AND p.symbol = ANY(:symbols) GROUP BY p.symbol"
        ),
        {"symbols": list(holdings)},
    ):
        holdings[row.symbol].first_seen = row.first.astimezone(tz).date()
    return sorted(holdings.values(), key=lambda h: h.symbol)


def risk_flags(
    holding: Holding, config: HoldingsConfig, leverage_by_fund: dict[str, Decimal], today: date
) -> list[RiskFlag]:
    flags = []
    share = holding.max_share_pct
    if share is not None and share >= config.concentration.warn_pct:
        forced = "trim" if share >= config.concentration.trim_pct else None
        flags.append(
            RiskFlag(
                code="concentration",
                detail=f"{share:.0f}% of an account's net liquidation",
                forces=forced,
            )
        )
    leverage = leverage_by_fund.get(holding.symbol)
    if leverage is not None and holding.first_seen is not None:
        days = (today - holding.first_seen).days
        if days >= config.levered_funds.warn_days:
            forced = "trim" if days >= config.levered_funds.trim_days else None
            flags.append(
                RiskFlag(
                    code="levered_days",
                    detail=f"{leverage:g}x daily-reset fund held at least {days} days",
                    forces=forced,
                )
            )
    for expiry in sorted(holding.expiries):
        days = (expiry - today).days
        if days <= config.options.warn_days:
            forced = "sell" if days <= config.options.sell_days else None
            flags.append(
                RiskFlag(
                    code="option_expiry",
                    detail=f"option expires {expiry} ({days} days); sell or roll",
                    forces=forced,
                )
            )
            break
    return flags


def describe(holding: Holding, today: date) -> dict[str, Any]:
    """Position numbers for the holding debate's fact table."""
    pnl_pct = (
        float((holding.market_value / holding.cost_basis - 1) * 100) if holding.cost_basis else None
    )
    days = (today - holding.first_seen).days if holding.first_seen else None
    return {"share_pct": holding.max_share_pct, "pnl_pct": pnl_pct, "days": days}
