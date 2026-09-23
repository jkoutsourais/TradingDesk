"""Finnhub: company news for Tier 0-1 stocks, general market news, earnings calendar.

The key travels in the X-Finnhub-Token header rather than the query string so it never
appears in logged or stored URLs. All three collectors share one client and one rate
limiter, since the free tier's 60 calls/min is per key.
"""

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.collectors.base import CollectResult, RateLimiter, describe_http_error, make_client

BASE_URL = "https://finnhub.io/api/v1"
# Stay under the documented 60/min so a clock skew never trips a 429.
CALLS_PER_MINUTE = 55
CALLS_PER_SECOND = 10
MAX_RATE_LIMIT_WAIT_S = 60.0
NEWS_LOOKBACK_DAYS = 1
EARNINGS_LOOKBACK_DAYS = 1
EARNINGS_LOOKAHEAD_DAYS = 14
GENERAL_CATEGORIES = ("general", "merger")


def _seconds_until_reset(response: httpx.Response, now: float | None = None) -> float:
    """Wait implied by Finnhub's X-Ratelimit-Reset (epoch seconds), capped at a minute."""
    now = time.time() if now is None else now
    try:
        reset = float(response.headers.get("x-ratelimit-reset", ""))
    except ValueError:
        return MAX_RATE_LIMIT_WAIT_S
    return min(max(reset - now, 1.0), MAX_RATE_LIMIT_WAIT_S)


class FinnhubClient:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = make_client(
            base_url=BASE_URL, headers={"X-Finnhub-Token": api_key}, transport=transport
        )
        self._limiter = RateLimiter(CALLS_PER_MINUTE, 60.0)
        # Finnhub also caps bursts at 30 calls/second; a 40-symbol news pass trips it
        # without this even though the per-minute budget is fine.
        self._burst_limiter = RateLimiter(CALLS_PER_SECOND, 1.0)

    async def get(self, path: str, params: dict[str, str]) -> Any:
        for attempt in range(2):
            await self._limiter.acquire()
            await self._burst_limiter.acquire()
            response = await self._http.get(path, params=params)
            if response.status_code == 429 and attempt == 0:
                # The local limiter restarts empty with the process, while Finnhub still
                # counts the previous process's calls; wait out its window once.
                await asyncio.sleep(_seconds_until_reset(response))
                continue
            response.raise_for_status()
            return response.json()
        raise AssertionError("unreachable: the second attempt returns or raises")

    async def aclose(self) -> None:
        await self._http.aclose()


def _news_record(item: dict[str, Any], source: str, extra_tickers: tuple[str, ...]) -> RawRecord:
    related = tuple(t for t in str(item.get("related") or "").split(",") if t.strip())
    published = item.get("datetime")
    return RawRecord(
        produced_by=f"data.{source.replace('.', '_')}",
        runtime_ms=0,
        source=source,
        source_id=str(item["id"]),
        url=item.get("url") or None,
        fetched_at=datetime.now(UTC),
        published_at=datetime.fromtimestamp(published, UTC) if published else None,
        tickers=(*related, *extra_tickers),
        tags=(str(item.get("category") or "news"),),
        payload={
            "headline": item.get("headline"),
            "summary": item.get("summary"),
            "publisher": item.get("source"),
            "category": item.get("category"),
        },
        # Finnhub ids are stable per story; the same story returned under several tickers
        # hashes identically and is stored once.
        content_hash=content_hash("finnhub.news", str(item["id"])),
    )


class FinnhubCompanyNews:
    name = "finnhub_company_news"

    def __init__(self, client: FinnhubClient, symbols: Callable[[], tuple[str, ...]]) -> None:
        self._client = client
        self._symbols = symbols

    async def collect(self) -> CollectResult:
        result = CollectResult()
        today = datetime.now(UTC).date()
        params_window = {
            "from": (today - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat(),
            "to": today.isoformat(),
        }
        for symbol in self._symbols():
            try:
                items = await self._client.get("/company-news", {"symbol": symbol, **params_window})
            except httpx.HTTPError as exc:
                result.errors.append(f"{symbol}: {describe_http_error(exc)}")
                continue
            result.records += [
                _news_record(item, "finnhub.company_news", (symbol,)) for item in items
            ]
        return result

    async def aclose(self) -> None:
        return None


class FinnhubGeneralNews:
    name = "finnhub_general_news"

    def __init__(self, client: FinnhubClient) -> None:
        self._client = client

    async def collect(self) -> CollectResult:
        result = CollectResult()
        for category in GENERAL_CATEGORIES:
            try:
                items = await self._client.get("/news", {"category": category})
            except httpx.HTTPError as exc:
                result.errors.append(f"{category}: {describe_http_error(exc)}")
                continue
            result.records += [_news_record(item, "finnhub.general_news", ()) for item in items]
        return result

    async def aclose(self) -> None:
        return None


class FinnhubEarningsCalendar:
    name = "finnhub_earnings_calendar"

    def __init__(self, client: FinnhubClient) -> None:
        self._client = client

    async def collect(self) -> CollectResult:
        today = datetime.now(UTC).date()
        data = await self._client.get(
            "/calendar/earnings",
            {
                "from": (today - timedelta(days=EARNINGS_LOOKBACK_DAYS)).isoformat(),
                "to": (today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)).isoformat(),
            },
        )
        records = []
        for item in data.get("earningsCalendar") or []:
            source_id = f"{item['symbol']}:{item['year']}Q{item['quarter']}"
            # The whole entry is hashed: estimates and actuals filling in produce a new
            # record, which is how a reported result reaches the buffer.
            canonical = json.dumps(item, sort_keys=True)
            records.append(
                RawRecord(
                    produced_by="data.finnhub_earnings_calendar",
                    runtime_ms=0,
                    source="finnhub.earnings_calendar",
                    source_id=source_id,
                    fetched_at=datetime.now(UTC),
                    tickers=(item["symbol"],),
                    tags=("earnings",),
                    payload=item,
                    content_hash=content_hash("finnhub.earnings_calendar", canonical),
                )
            )
        return CollectResult(records=records)

    async def aclose(self) -> None:
        return None
