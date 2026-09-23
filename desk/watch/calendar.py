"""Market calendar: NYSE sessions, market phase, futures hours and weekly release dates.

NYSE sessions (holidays and half-days) come from pandas_market_calendars and are cached
for a window around today, rebuilt when a date falls outside it. The futures check is an
approximation of CME Globex hours (Sunday 18:00 to Friday 17:00 ET with a daily
17:00-18:00 break, closed on NYSE full holidays), which is enough to decide whether to
scan futures.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import yaml
from pydantic import BaseModel, ConfigDict

from desk.config import CONFIG_DIR, WEEKDAY_INDEX, Weekday

CACHE_DAYS_BACK = 400
CACHE_DAYS_AHEAD = 400
FUTURES_DAILY_BREAK = (time(17, 0), time(18, 0))


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExtendedHours(_Frozen):
    pre_open: time
    post_close: time


class WeeklyRelease(_Frozen):
    name: str
    weekday: Weekday
    time: time
    holiday_shift: Literal["next_trading_day", "next_monday"]


class FredRelease(_Frozen):
    kind: str
    name: str
    time: time


class FomcSource(_Frozen):
    url: str
    statement_time: time


class CalendarConfig(_Frozen):
    timezone: str
    extended_hours: ExtendedHours
    weekly_releases: dict[str, WeeklyRelease]
    fred_releases: dict[int, FredRelease]
    fomc: FomcSource


def load_calendar_config(config_dir: Path = CONFIG_DIR) -> CalendarConfig:
    with (config_dir / "calendar.yaml").open(encoding="utf-8") as handle:
        return CalendarConfig.model_validate(yaml.safe_load(handle))


class MarketPhase(StrEnum):
    CLOSED = "closed"
    PRE = "pre"
    REGULAR = "regular"
    POST = "post"


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    open: datetime  # UTC
    close: datetime  # UTC

    @property
    def early_close(self) -> bool:
        return self.close.astimezone(ZoneInfo("America/New_York")).time() < time(16, 0)


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    key: str  # stable id, e.g. "eia_petroleum:2026-09-23"
    kind: str
    name: str
    at: datetime  # UTC
    source: str


class MarketCalendar:
    def __init__(self, config: CalendarConfig) -> None:
        self._config = config
        self._tz = ZoneInfo(config.timezone)
        self._nyse = mcal.get_calendar("XNYS")
        self._sessions: dict[date, Session] = {}
        self._range: tuple[date, date] | None = None

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    def _ensure(self, day: date) -> None:
        if self._range is not None and self._range[0] <= day <= self._range[1]:
            return
        start = day - timedelta(days=CACHE_DAYS_BACK)
        end = day + timedelta(days=CACHE_DAYS_AHEAD)
        schedule = self._nyse.schedule(start_date=start.isoformat(), end_date=end.isoformat())
        self._sessions = {
            index.date(): Session(
                day=index.date(),
                open=row["market_open"].to_pydatetime().astimezone(UTC),
                close=row["market_close"].to_pydatetime().astimezone(UTC),
            )
            for index, row in schedule.iterrows()
        }
        self._range = (start, end)

    def session(self, day: date) -> Session | None:
        self._ensure(day)
        return self._sessions.get(day)

    def is_trading_day(self, day: date) -> bool:
        return self.session(day) is not None

    def next_trading_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def previous_trading_day(self, day: date) -> date:
        candidate = day - timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate -= timedelta(days=1)
        return candidate

    def add_trading_days(self, day: date, count: int) -> date:
        for _ in range(count):
            day = self.next_trading_day(day)
        return day

    def phase(self, moment: datetime) -> MarketPhase:
        local = moment.astimezone(self._tz)
        session = self.session(local.date())
        if session is None:
            return MarketPhase.CLOSED
        pre_open = datetime.combine(local.date(), self._config.extended_hours.pre_open, self._tz)
        post_close = datetime.combine(
            local.date(), self._config.extended_hours.post_close, self._tz
        )
        if session.open <= moment < session.close:
            return MarketPhase.REGULAR
        if pre_open <= moment < session.open:
            return MarketPhase.PRE
        if session.close <= moment < post_close:
            return MarketPhase.POST
        return MarketPhase.CLOSED

    def futures_open(self, moment: datetime) -> bool:
        local = moment.astimezone(self._tz)
        weekday, clock = local.weekday(), local.time()
        if weekday == 5:  # Saturday
            return False
        if weekday == 6:  # Sunday opens at 18:00
            return clock >= FUTURES_DAILY_BREAK[1]
        if weekday == 4 and clock >= FUTURES_DAILY_BREAK[0]:  # Friday close
            return False
        if FUTURES_DAILY_BREAK[0] <= clock < FUTURES_DAILY_BREAK[1]:
            return False
        # The session trading at this moment belongs to today (before 17:00) or to the
        # next weekday (after 18:00); a full exchange holiday closes it.
        session_day = (
            local.date() if clock < FUTURES_DAILY_BREAK[0] else local.date() + timedelta(days=1)
        )
        if session_day.weekday() >= 5:
            session_day += timedelta(days=7 - session_day.weekday())
        return self.is_trading_day(session_day)

    def weekly_release_events(self, start: date, end: date) -> list[CalendarEvent]:
        """EIA and COT dates in [start, end], shifted for holidays earlier in the week."""
        events = []
        for key, release in self._config.weekly_releases.items():
            target = WEEKDAY_INDEX[release.weekday]
            day = start - timedelta(days=start.weekday())  # Monday of the first week
            while day <= end:
                scheduled = day + timedelta(days=target)
                holiday_this_week = any(
                    not self.is_trading_day(day + timedelta(days=offset))
                    for offset in range(target + 1)
                )
                if holiday_this_week:
                    if release.holiday_shift == "next_monday":
                        scheduled = day + timedelta(days=7)
                        while not self.is_trading_day(scheduled):
                            scheduled += timedelta(days=1)
                    else:
                        scheduled = self.next_trading_day(scheduled)
                if start <= scheduled <= end:
                    events.append(
                        CalendarEvent(
                            key=f"{key}:{scheduled.isoformat()}",
                            kind=key,
                            name=release.name,
                            at=datetime.combine(scheduled, release.time, self._tz).astimezone(UTC),
                            source="computed",
                        )
                    )
                day += timedelta(days=7)
        return sorted(events, key=lambda event: event.at)
