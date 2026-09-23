"""Scheduled market-moving releases: FOMC (Fed website), CPI and jobs (FRED), EIA and COT
(computed from the weekly schedule with holiday shifts).

Agencies occasionally move dates, so every run replaces the upcoming events of each kind
it fetched successfully.
"""

import html
import re
from datetime import UTC, date, datetime, timedelta

import httpx

from desk.collectors.base import CollectResult, describe_http_error, make_client
from desk.watch.calendar import CalendarConfig, CalendarEvent, MarketCalendar

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
LOOKBACK_DAYS = 30
LOOKAHEAD_DAYS = 120
MONTHS = {
    name: index
    for index, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ],
        start=1,
    )
}
_TOKENS = re.compile(
    r"(?P<year>\d{4}) FOMC Meetings"
    r"|fomc-meeting__month[^>]*>\s*<strong>(?P<month>[^<]+)<"
    r"|fomc-meeting__date[^>]*>(?P<date>[^<]+)<"
)
_DAY_SPAN = re.compile(r"^(?P<first>\d{1,2})(?:-(?P<last>\d{1,2}))?\*?$")


def parse_fomc_meetings(page: str) -> list[date]:
    """Final day of each scheduled FOMC meeting (the statement day)."""
    meetings = []
    year: int | None = None
    month_label: str | None = None
    for match in _TOKENS.finditer(page):
        if match["year"]:
            year = int(match["year"])
        elif match["month"]:
            month_label = html.unescape(match["month"]).strip().lower()
        elif match["date"] and year is not None and month_label:
            span = _DAY_SPAN.match(match["date"].strip())
            if span is None:
                continue  # notation votes, cancelled or unscheduled meetings
            # "Apr/May" with "30-1": the meeting ends in the second month.
            months = [m for m in re.split(r"[/-]", month_label) if m]
            last_day = int(span["last"] or span["first"])
            wraps = span["last"] is not None and int(span["last"]) < int(span["first"])
            month_name = months[-1]
            month = next((v for k, v in MONTHS.items() if k.startswith(month_name[:3])), None)
            if month is None:
                continue
            if wraps and len(months) == 1:  # single label but the dates cross a month end
                month = month % 12 + 1
            meetings.append(date(year, month, last_day))
    return sorted(set(meetings))


class ReleaseCalendar:
    name = "release_calendar"

    def __init__(
        self,
        config: CalendarConfig,
        calendar: MarketCalendar,
        fred_api_key: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._calendar = calendar
        self._fred_api_key = fred_api_key
        self._http = make_client(transport=transport)

    async def _fomc(self) -> list[CalendarEvent]:
        response = await self._http.get(self._config.fomc.url)
        response.raise_for_status()
        tz = self._calendar.tz
        return [
            CalendarEvent(
                key=f"fomc:{day.isoformat()}",
                kind="fomc",
                name="FOMC statement",
                at=datetime.combine(day, self._config.fomc.statement_time, tz).astimezone(UTC),
                source="federalreserve.gov",
            )
            for day in parse_fomc_meetings(response.text)
        ]

    async def _fred(self, today: date) -> tuple[list[CalendarEvent], list[str]]:
        events: list[CalendarEvent] = []
        kinds: list[str] = []
        if not self._fred_api_key:
            return events, kinds
        tz = self._calendar.tz
        for release_id, release in self._config.fred_releases.items():
            response = await self._http.get(
                FRED_RELEASE_DATES_URL,
                params={
                    "release_id": str(release_id),
                    "api_key": self._fred_api_key,
                    "file_type": "json",
                    "include_release_dates_with_no_data": "true",
                    "realtime_start": (today - timedelta(days=LOOKBACK_DAYS)).isoformat(),
                    "sort_order": "asc",
                },
            )
            response.raise_for_status()
            kinds.append(release.kind)
            for item in response.json().get("release_dates", []):
                day = date.fromisoformat(item["date"])
                events.append(
                    CalendarEvent(
                        key=f"{release.kind}:{day.isoformat()}",
                        kind=release.kind,
                        name=release.name,
                        at=datetime.combine(day, release.time, tz).astimezone(UTC),
                        source="fred",
                    )
                )
        return events, kinds

    async def collect(self) -> CollectResult:
        today = datetime.now(self._calendar.tz).date()
        result = CollectResult()
        weekly = self._calendar.weekly_release_events(
            today - timedelta(days=LOOKBACK_DAYS), today + timedelta(days=LOOKAHEAD_DAYS)
        )
        result.calendar_events += weekly
        result.calendar_kinds += list(self._config.weekly_releases)
        try:
            result.calendar_events += await self._fomc()
            result.calendar_kinds.append("fomc")
        except httpx.HTTPError as exc:
            result.errors.append(f"fomc: {describe_http_error(exc)}")
        try:
            fred_events, fred_kinds = await self._fred(today)
            result.calendar_events += fred_events
            result.calendar_kinds += fred_kinds
        except httpx.HTTPError as exc:
            result.errors.append(f"fred release dates: {describe_http_error(exc)}")
        if not self._fred_api_key:
            result.errors.append("fred release dates: FRED_API_KEY is not set")
        return result

    async def aclose(self) -> None:
        await self._http.aclose()
