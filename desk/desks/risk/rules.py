"""Risk desk rules: pure code with a hard veto (config/risk.yaml).

`evaluate` sizes a plan inside the per-trade cap (scaled by conviction), then shrinks it
to fit the portfolio and correlated-bucket open-risk limits. Every check is returned
with a pass, warn or fail result; any fail vetoes the plan, and a plan that fits only at
a smaller size is "resized".
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from desk.desks.risk.config import AccountType, RiskConfig, Structure

Result = Literal["pass", "warn", "fail"]
OPTION_STRUCTURES: frozenset[str] = frozenset({"long_call", "long_put", "debit_spread"})
SHARE_STRUCTURES: frozenset[str] = frozenset({"shares", "etf", "levered_etf"})
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class AccountState:
    ref: str  # "<broker>:<last four>"
    account_type: AccountType
    net_liq: Decimal
    settled_cash: Decimal


@dataclass(frozen=True, slots=True)
class OpenRisk:
    symbol: str
    bucket: str | None
    risk: Decimal  # dollars lost if stopped out
    assumed: bool  # True when the stop is the assumed ATR stop, not a thesis line
    exposure: Decimal = Decimal(0)  # notional x leverage


@dataclass(frozen=True, slots=True)
class PlanInput:
    structure: Structure
    subject: str  # the thesis instrument
    instrument: str  # what is traded (the subject, a proxy, or an option contract)
    direction: Literal["long", "short"]
    entry: Decimal  # the subject's price the stop and target are measured against
    stop: Decimal | None
    target: Decimal | None
    thesis_hard: Decimal | None  # the thesis hard line, in the same units as `stop`
    unit_cost: Decimal  # cash per unit (share, contract or spread) including multiplier
    unit_max_loss: Decimal  # loss per unit at the stop, or the premium for long options
    conviction: int
    today: date
    review_by: date | None
    leverage: float = 1.0
    spread_pct: float | None = None
    open_interest: int | None = None
    earnings: list[date] = field(default_factory=list)
    major_events: list[tuple[date, str]] = field(default_factory=list)
    catalyst_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    result: Result
    detail: str


@dataclass(frozen=True, slots=True)
class Decision:
    decision: Literal["approved", "resized", "vetoed"]
    size: int
    requested_size: int
    cap: Decimal
    tier_pct: Decimal
    max_loss: Decimal
    cost: Decimal
    funding_needed: Decimal
    checks: tuple[Check, ...]
    veto_reasons: tuple[str, ...]


def tier_pct(structure: Structure, config: RiskConfig) -> Decimal:
    return config.tier_pct[structure]


def bucket_of(symbol: str, config: RiskConfig) -> str | None:
    for name, members in config.buckets.items():
        if symbol in members:
            return name
    return None


def _units(budget: Decimal, unit_loss: Decimal) -> int:
    if budget <= 0 or unit_loss <= 0:
        return 0
    return int((budget / unit_loss).to_integral_value(rounding=ROUND_FLOOR))


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def holding_open_risk(
    symbol: str,
    quantity: Decimal,
    price: Decimal,
    atr: Decimal | None,
    thesis_hard: Decimal | None,
    config: RiskConfig,
) -> OpenRisk:
    """Open risk of an existing long position: to its thesis line, else an assumed ATR stop.

    With neither a thesis line nor an ATR, the whole position counts as at risk.
    """
    size = abs(quantity)
    if thesis_hard is not None:
        stop, assumed = thesis_hard, False
    elif atr is not None:
        stop, assumed = price - config.assumed_stop_atr * atr, True
    else:
        stop, assumed = Decimal(0), True
    risk = max(price - max(stop, Decimal(0)), Decimal(0)) * size
    leverage = config.levered_funds.get(symbol, Decimal(1))
    return OpenRisk(
        symbol=symbol,
        bucket=bucket_of(symbol, config),
        risk=risk,
        assumed=assumed,
        exposure=size * price * leverage,
    )


def _allowed(plan: PlanInput, account: AccountState, config: RiskConfig) -> Check:
    rules = config.account_types[account.account_type]
    if plan.structure not in rules.structures:
        return Check(
            "instrument_allowed",
            "fail",
            f"{plan.structure} is not allowed in a {account.account_type} account",
        )
    if (
        plan.direction == "short"
        and plan.structure in SHARE_STRUCTURES
        and not rules.allow_short_shares
    ):
        return Check(
            "instrument_allowed",
            "fail",
            f"short {plan.structure} is not allowed in a {account.account_type} account",
        )
    return Check("instrument_allowed", "pass", f"{plan.structure} allowed in {account.ref}")


def _liquidity(plan: PlanInput, config: RiskConfig) -> Check:
    if plan.structure in OPTION_STRUCTURES:
        limits = config.liquidity
        if plan.spread_pct is None or plan.open_interest is None:
            return Check("liquidity", "fail", "no option quote or open interest")
        problems = []
        if plan.spread_pct > limits.max_option_spread_pct:
            problems.append(
                f"bid-ask spread {plan.spread_pct:.1f}% over {limits.max_option_spread_pct:g}%"
            )
        if plan.open_interest < limits.min_open_interest:
            problems.append(f"open interest {plan.open_interest} under {limits.min_open_interest}")
        if problems:
            return Check("liquidity", "fail", "; ".join(problems))
        return Check(
            "liquidity",
            "pass",
            f"spread {plan.spread_pct:.1f}%, open interest {plan.open_interest}",
        )
    if plan.structure == "future":
        return Check("liquidity", "warn", "futures margin is not checked yet")
    return Check("liquidity", "pass", "exchange-listed shares")


def _stop(plan: PlanInput, config: RiskConfig) -> Check:
    if plan.stop is None:
        return Check("stop_consistent", "fail", "no stop")
    long = plan.direction == "long"
    if (plan.stop >= plan.entry) if long else (plan.stop <= plan.entry):
        side = "below" if long else "above"
        return Check(
            "stop_consistent", "fail", f"stop {plan.stop} is not {side} entry {plan.entry}"
        )
    if plan.target is not None and (
        (plan.target <= plan.entry) if long else (plan.target >= plan.entry)
    ):
        return Check(
            "stop_consistent", "fail", f"target {plan.target} is on the wrong side of entry"
        )
    if plan.thesis_hard is not None and plan.thesis_hard > 0:
        gap = abs(plan.stop - plan.thesis_hard) / plan.thesis_hard * HUNDRED
        if gap > config.stop_tolerance_pct:
            return Check(
                "stop_consistent",
                "fail",
                f"stop {plan.stop} is {gap:.1f}% from the thesis hard line {plan.thesis_hard}",
            )
    return Check("stop_consistent", "pass", f"stop {plan.stop} matches the thesis hard line")


def _events(plan: PlanInput) -> Check:
    end = plan.review_by or plan.today
    earnings = [day for day in plan.earnings if plan.today < day <= end]
    named = any("earnings" in name.casefold() for name in plan.catalyst_names)
    if earnings and not named:
        return Check(
            "events",
            "fail",
            f"earnings on {earnings[0]} inside the holding period and not a named catalyst",
        )
    majors = [(day, name) for day, name in plan.major_events if plan.today < day <= end]
    if majors:
        listed = ", ".join(f"{name} {day}" for day, name in majors[:3])
        return Check("events", "warn", f"major releases inside the holding period: {listed}")
    if earnings:
        return Check("events", "pass", f"earnings on {earnings[0]} is the named catalyst")
    return Check("events", "pass", "no earnings or major releases inside the holding period")


def evaluate(
    plan: PlanInput,
    account: AccountState,
    open_risks: list[OpenRisk],
    combined_equity: Decimal,
    config: RiskConfig,
) -> Decision:
    checks: list[Check] = [_allowed(plan, account, config)]

    tier = tier_pct(plan.structure, config)
    scale = config.conviction_scale.get(plan.conviction, Decimal(0))
    cap = account.net_liq * tier / HUNDRED * scale
    requested = _units(cap, plan.unit_max_loss)
    if scale == 0:
        checks.append(Check("max_loss", "fail", f"conviction {plan.conviction} is watch-only"))
    elif requested < 1:
        checks.append(
            Check(
                "max_loss",
                "fail",
                f"one unit loses {_money(plan.unit_max_loss)}, over the {tier}% cap of "
                f"{_money(cap)} at conviction {plan.conviction}",
            )
        )
    else:
        checks.append(
            Check(
                "max_loss",
                "pass",
                f"{requested} units lose {_money(requested * plan.unit_max_loss)} at the stop, "
                f"inside the {tier}% cap of {_money(cap)} at conviction {plan.conviction}",
            )
        )
    size = requested

    total_limit = combined_equity * config.total_open_risk_pct / HUNDRED
    existing = sum((r.risk for r in open_risks), Decimal(0))
    fits_total = _units(total_limit - existing, plan.unit_max_loss)
    if size >= 1 and fits_total < 1:
        checks.append(
            Check(
                "portfolio_open_risk",
                "fail",
                f"open risk {_money(existing)} leaves no room under {_money(total_limit)}",
            )
        )
    else:
        size = min(size, fits_total)
        checks.append(
            Check(
                "portfolio_open_risk",
                "pass",
                f"open risk {_money(existing + size * plan.unit_max_loss)} after the trade, "
                f"limit {_money(total_limit)}",
            )
        )

    bucket = bucket_of(plan.subject, config) or bucket_of(plan.instrument, config)
    if bucket is None:
        checks.append(Check("bucket_cap", "pass", "no correlated bucket"))
    else:
        bucket_limit = combined_equity * config.bucket_cap_pct / HUNDRED
        in_bucket = sum((r.risk for r in open_risks if r.bucket == bucket), Decimal(0))
        fits_bucket = _units(bucket_limit - in_bucket, plan.unit_max_loss)
        if size >= 1 and fits_bucket < 1:
            checks.append(
                Check(
                    "bucket_cap",
                    "fail",
                    f"{bucket} bucket risk {_money(in_bucket)} leaves no room under "
                    f"{_money(bucket_limit)}",
                )
            )
        else:
            size = min(size, fits_bucket)
            checks.append(
                Check(
                    "bucket_cap",
                    "pass",
                    f"{bucket} bucket risk {_money(in_bucket + size * plan.unit_max_loss)} "
                    f"after the trade, cap {_money(bucket_limit)}",
                )
            )

    checks += [_liquidity(plan, config), _stop(plan, config), _events(plan)]

    failed = [c for c in checks if c.result == "fail"]
    final = 0 if failed else max(size, 0)
    cost = final * plan.unit_cost
    rules = config.account_types[account.account_type]
    shortfall = (
        max(cost - account.settled_cash, Decimal(0)) if rules.settled_cash_only else Decimal(0)
    )
    if shortfall > 0:
        checks.append(
            Check(
                "settled_cash",
                "warn",
                f"cost {_money(cost)} exceeds settled cash {_money(account.settled_cash)}; "
                f"funding needed {_money(shortfall)}",
            )
        )
    else:
        checks.append(Check("settled_cash", "pass", f"cost {_money(cost)} is covered"))

    if failed:
        outcome: Literal["approved", "resized", "vetoed"] = "vetoed"
    elif final < requested:
        outcome = "resized"
    else:
        outcome = "approved"
    return Decision(
        decision=outcome,
        size=final,
        requested_size=requested,
        cap=cap,
        tier_pct=tier,
        max_loss=final * plan.unit_max_loss,
        cost=cost,
        funding_needed=shortfall,
        checks=tuple(checks),
        veto_reasons=tuple(c.detail for c in failed),
    )
