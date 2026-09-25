from datetime import date
from decimal import Decimal

import pytest

from desk.desks.risk.config import load_risk_config
from desk.desks.risk.rules import (
    AccountState,
    OpenRisk,
    PlanInput,
    bucket_of,
    evaluate,
    holding_open_risk,
    tier_pct,
)

CONFIG = load_risk_config()
ROTH = AccountState(
    ref="ibkr:0000", account_type="roth", net_liq=Decimal(5000), settled_cash=Decimal(200)
)
TODAY = date(2026, 9, 24)


def plan(**overrides: object) -> PlanInput:
    fields: dict[str, object] = {
        "structure": "shares",
        "subject": "NVDA",
        "instrument": "NVDA",
        "direction": "long",
        "entry": Decimal("100"),
        "stop": Decimal("97.50"),
        "target": Decimal("110"),
        "thesis_hard": Decimal("97.50"),
        "unit_cost": Decimal("100"),
        "unit_max_loss": Decimal("2.50"),
        "conviction": 3,
        "today": TODAY,
        "review_by": date(2026, 10, 30),
    }
    fields.update(overrides)
    return PlanInput(**fields)  # type: ignore[arg-type]


def names(decision: object) -> dict[str, str]:
    return {c.name: c.result for c in decision.checks}  # type: ignore[attr-defined]


def test_tiers() -> None:
    assert tier_pct("shares", CONFIG) == Decimal(5)
    assert tier_pct("etf", CONFIG) == Decimal(5)
    for structure in ("levered_etf", "long_call", "long_put", "debit_spread", "future"):
        assert tier_pct(structure, CONFIG) == Decimal(2)


def test_conviction_scales_size_inside_the_cap() -> None:
    # 5% of 5000 = 250; conviction 3 uses half = 125; 125 / 2.50 = 50 shares.
    decision = evaluate(plan(), ROTH, [], Decimal(5000), CONFIG)
    assert decision.decision == "approved"
    assert decision.cap == Decimal(125) and decision.size == 50
    assert decision.max_loss == Decimal(125)
    full = evaluate(plan(conviction=5), ROTH, [], Decimal(5000), CONFIG)
    assert full.size == 100
    assert set(names(decision).values()) <= {"pass", "warn"}


def test_low_conviction_is_watch_only() -> None:
    decision = evaluate(plan(conviction=2), ROTH, [], Decimal(5000), CONFIG)
    assert decision.decision == "vetoed" and decision.size == 0
    assert any("watch-only" in r for r in decision.veto_reasons)


def test_levered_etf_uses_two_percent() -> None:
    decision = evaluate(
        plan(structure="levered_etf", instrument="AGQ", leverage=2.0),
        ROTH,
        [],
        Decimal(5000),
        CONFIG,
    )
    # 2% of 5000 = 100, half for conviction 3 = 50; 50 / 2.50 = 20.
    assert decision.cap == Decimal(50) and decision.size == 20


def test_roth_rejects_futures_and_short_shares() -> None:
    future = evaluate(plan(structure="future", instrument="/GC"), ROTH, [], Decimal(5000), CONFIG)
    assert future.decision == "vetoed" and names(future)["instrument_allowed"] == "fail"
    short = evaluate(
        plan(
            direction="short",
            stop=Decimal("102.50"),
            thesis_hard=Decimal("102.50"),
            target=Decimal("90"),
        ),
        ROTH,
        [],
        Decimal(5000),
        CONFIG,
    )
    assert short.decision == "vetoed" and names(short)["instrument_allowed"] == "fail"


def test_one_unit_over_the_cap_is_vetoed() -> None:
    decision = evaluate(plan(unit_max_loss=Decimal(200)), ROTH, [], Decimal(5000), CONFIG)
    assert decision.decision == "vetoed"
    assert names(decision)["max_loss"] == "fail"


def test_total_open_risk_resizes_then_vetoes() -> None:
    existing = [OpenRisk("AEP", None, Decimal(700), assumed=True)]
    # 15% of 5000 = 750; 50 of room / 2.50 = 20 shares.
    resized = evaluate(plan(), ROTH, existing, Decimal(5000), CONFIG)
    assert resized.decision == "resized" and resized.size == 20 and resized.requested_size == 50
    full = [OpenRisk("AEP", None, Decimal(749), assumed=True)]
    vetoed = evaluate(plan(), ROTH, full, Decimal(5000), CONFIG)
    assert vetoed.decision == "vetoed" and names(vetoed)["portfolio_open_risk"] == "fail"


def test_bucket_cap_counts_correlated_names_together() -> None:
    assert bucket_of("SLV", CONFIG) == bucket_of("NEM", CONFIG) == "metals"
    existing = [OpenRisk("GLD", "metals", Decimal(375), assumed=False)]
    # 8% of 5000 = 400; 25 of room / 2.50 = 10 shares.
    decision = evaluate(
        plan(subject="SLV", instrument="SLV"), ROTH, existing, Decimal(5000), CONFIG
    )
    assert decision.decision == "resized" and decision.size == 10
    assert "metals" in next(c.detail for c in decision.checks if c.name == "bucket_cap")


def test_option_liquidity() -> None:
    option = {
        "structure": "long_call",
        "instrument": "NVDA  261120C00105000",
        "unit_cost": Decimal(300),
        "unit_max_loss": Decimal(300),
        "conviction": 5,
    }
    wide = evaluate(
        plan(**option, spread_pct=25.0, open_interest=500), ROTH, [], Decimal(5000), CONFIG
    )
    assert wide.decision == "vetoed" and names(wide)["liquidity"] == "fail"
    thin = evaluate(
        plan(**option, spread_pct=4.0, open_interest=10), ROTH, [], Decimal(5000), CONFIG
    )
    assert names(thin)["liquidity"] == "fail"
    cheap = {**option, "unit_max_loss": Decimal(90), "unit_cost": Decimal(90)}
    ok = evaluate(plan(**cheap, spread_pct=4.0, open_interest=500), ROTH, [], Decimal(5000), CONFIG)
    assert names(ok)["liquidity"] == "pass" and ok.size == 1


def test_stop_must_match_thesis() -> None:
    wrong_side = evaluate(plan(stop=Decimal(101)), ROTH, [], Decimal(5000), CONFIG)
    assert wrong_side.decision == "vetoed" and names(wrong_side)["stop_consistent"] == "fail"
    off_thesis = evaluate(plan(stop=Decimal(90)), ROTH, [], Decimal(5000), CONFIG)
    assert names(off_thesis)["stop_consistent"] == "fail"


def test_earnings_inside_holding_period() -> None:
    blocked = evaluate(plan(earnings=[date(2026, 10, 20)]), ROTH, [], Decimal(5000), CONFIG)
    assert blocked.decision == "vetoed" and names(blocked)["events"] == "fail"
    catalyst = evaluate(
        plan(earnings=[date(2026, 10, 20)], catalyst_names=("NVDA earnings",)),
        ROTH,
        [],
        Decimal(5000),
        CONFIG,
    )
    assert names(catalyst)["events"] == "pass"
    macro = evaluate(
        plan(major_events=[(date(2026, 10, 14), "CPI")]), ROTH, [], Decimal(5000), CONFIG
    )
    assert names(macro)["events"] == "warn" and macro.decision == "approved"


def test_cash_shortfall_is_a_funding_note_not_a_veto() -> None:
    decision = evaluate(plan(), ROTH, [], Decimal(5000), CONFIG)
    # 50 shares x 100 = 5000 cost against 200 settled cash.
    assert decision.cost == Decimal(5000) and decision.funding_needed == Decimal(4800)
    assert names(decision)["settled_cash"] == "warn" and decision.decision == "approved"


def test_holding_open_risk_uses_thesis_line_or_two_atr() -> None:
    assumed = holding_open_risk(
        "AEP", Decimal(10), Decimal(100), atr=Decimal(2), thesis_hard=None, config=CONFIG
    )
    assert assumed.risk == Decimal(40) and assumed.assumed
    stopped = holding_open_risk(
        "AEP", Decimal(10), Decimal(100), atr=Decimal(2), thesis_hard=Decimal(95), config=CONFIG
    )
    assert stopped.risk == Decimal(50) and not stopped.assumed
    above = holding_open_risk(
        "AEP", Decimal(10), Decimal(100), atr=None, thesis_hard=None, config=CONFIG
    )
    assert above.risk == Decimal(1000) and above.assumed


def test_levered_holding_bucket_exposure() -> None:
    levered = holding_open_risk(
        "AGQ", Decimal(10), Decimal(50), atr=Decimal(3), thesis_hard=None, config=CONFIG
    )
    assert levered.bucket == "metals" and levered.exposure == Decimal(1000)


@pytest.mark.parametrize("structure", ["shares", "long_call"])
def test_every_decision_logs_all_checks(structure: str) -> None:
    decision = evaluate(
        plan(structure=structure, spread_pct=2.0, open_interest=900),
        ROTH,
        [],
        Decimal(5000),
        CONFIG,
    )
    assert set(names(decision)) == {
        "instrument_allowed",
        "max_loss",
        "portfolio_open_risk",
        "bucket_cap",
        "liquidity",
        "stop_consistent",
        "events",
        "settled_cash",
    }
