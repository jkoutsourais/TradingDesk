from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from desk.config import Window, load_schedule, load_sources, load_tiers

NY = ZoneInfo("America/New_York")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")


def ny(*args: int) -> datetime:
    return datetime(*args, tzinfo=NY).astimezone(UTC)  # type: ignore[misc]


def edgar_window() -> Window:
    return Window(days=WEEKDAYS, start=time(6), end=time(20))


def test_repo_config_files_load() -> None:
    schedule = load_schedule()
    tiers = load_tiers()
    sources = load_sources()
    assert schedule.collectors["edgar_latest_filings"].window is not None
    assert "NVDA" in tiers.tier_1_stocks()
    assert "GLD" not in tiers.tier_1_stocks()
    assert "/GC" in tiers.tier_1_symbols()
    assert "DGS10" in sources.fred.series


def test_window_contains_respects_days_and_hours() -> None:
    window = edgar_window()
    assert window.contains(ny(2026, 9, 22, 6, 0), NY)  # Tuesday 06:00
    assert not window.contains(ny(2026, 9, 22, 20, 0), NY)  # end is exclusive
    assert not window.contains(ny(2026, 9, 22, 5, 59), NY)
    assert not window.contains(ny(2026, 9, 26, 12, 0), NY)  # Saturday


def test_active_seconds_skips_nights_and_weekends() -> None:
    window = edgar_window()
    # Friday 19:00 to Monday 07:00: one hour Friday evening, one hour Monday morning.
    active = window.active_seconds(ny(2026, 9, 25, 19, 0), ny(2026, 9, 28, 7, 0), NY)
    assert active == 2 * 3600


def test_active_seconds_across_dst_change() -> None:
    window = Window(days=("sun", "mon"), start=time(0), end=time(23, 59))
    # 2026-11-01 is the fall-back Sunday; the local day is 25 hours long.
    active = window.active_seconds(ny(2026, 11, 1, 0, 0), ny(2026, 11, 1, 23, 59), NY)
    assert active == timedelta(hours=24, minutes=59).total_seconds()


def test_next_open() -> None:
    window = edgar_window()
    assert window.next_open(ny(2026, 9, 25, 21, 0), NY) == ny(2026, 9, 28, 6, 0)
    inside = ny(2026, 9, 22, 12, 0)
    assert window.next_open(inside, NY) == inside


def test_window_end_must_follow_start() -> None:
    with pytest.raises(ValidationError):
        Window(days=WEEKDAYS, start=time(20), end=time(6))
