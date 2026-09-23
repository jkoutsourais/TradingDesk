from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from desk.watch.calendar import MarketCalendar, MarketPhase, load_calendar_config

NY = ZoneInfo("America/New_York")


@pytest.fixture(scope="module")
def cal() -> MarketCalendar:
    return MarketCalendar(load_calendar_config())


def ny(*args: int) -> datetime:
    return datetime(*args, tzinfo=NY).astimezone(UTC)  # type: ignore[misc]


def test_holidays_and_half_days(cal: MarketCalendar) -> None:
    assert not cal.is_trading_day(date(2026, 11, 26))  # Thanksgiving
    friday_after = cal.session(date(2026, 11, 27))
    assert friday_after is not None and friday_after.early_close
    regular = cal.session(date(2026, 9, 23))
    assert regular is not None and not regular.early_close
    assert cal.next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)
    assert cal.previous_trading_day(date(2026, 9, 28)) == date(2026, 9, 25)
    assert cal.add_trading_days(date(2026, 9, 23), 5) == date(2026, 9, 30)


@pytest.mark.parametrize(
    ("moment", "phase"),
    [
        (ny(2026, 9, 23, 3, 59), MarketPhase.CLOSED),
        (ny(2026, 9, 23, 4, 0), MarketPhase.PRE),
        (ny(2026, 9, 23, 9, 30), MarketPhase.REGULAR),
        (ny(2026, 9, 23, 16, 0), MarketPhase.POST),
        (ny(2026, 9, 23, 20, 0), MarketPhase.CLOSED),
        (ny(2026, 11, 27, 13, 30), MarketPhase.POST),  # half-day closes at 13:00
        (ny(2026, 9, 26, 12, 0), MarketPhase.CLOSED),  # Saturday
    ],
)
def test_market_phase(cal: MarketCalendar, moment: datetime, phase: MarketPhase) -> None:
    assert cal.phase(moment) is phase


@pytest.mark.parametrize(
    ("moment", "is_open"),
    [
        (ny(2026, 9, 27, 17, 59), False),  # Sunday before open
        (ny(2026, 9, 27, 18, 0), True),  # Sunday open
        (ny(2026, 9, 23, 17, 30), False),  # daily break
        (ny(2026, 9, 23, 21, 0), True),  # evening session
        (ny(2026, 9, 25, 17, 0), False),  # Friday close
        (ny(2026, 9, 26, 12, 0), False),  # Saturday
        (ny(2026, 11, 25, 20, 0), False),  # evening before Thanksgiving belongs to a holiday
    ],
)
def test_futures_open(cal: MarketCalendar, moment: datetime, is_open: bool) -> None:
    assert cal.futures_open(moment) is is_open


def test_weekly_releases_shift_for_holidays(cal: MarketCalendar) -> None:
    # Week of Labor Day 2026 (Mon Sep 7): EIA petroleum Wed -> Thu, gas Thu -> Fri,
    # COT Fri -> following Monday.
    events = {
        e.kind: e.at.astimezone(NY)
        for e in cal.weekly_release_events(date(2026, 9, 7), date(2026, 9, 14))
    }
    assert events["eia_petroleum"].date() == date(2026, 9, 10)
    assert events["eia_natural_gas"].date() == date(2026, 9, 11)
    assert events["cftc_cot"].date() == date(2026, 9, 14)
    normal = {
        e.kind: e.at.astimezone(NY)
        for e in cal.weekly_release_events(date(2026, 9, 21), date(2026, 9, 27))
    }
    assert normal["eia_petroleum"] == datetime(2026, 9, 23, 10, 30, tzinfo=NY)
    assert normal["cftc_cot"].date() == date(2026, 9, 25)
