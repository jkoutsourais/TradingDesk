"""SEC EDGAR: the latest-filings Atom feed plus per-company submissions for Tier 0-1.

SEC fair-access rules: a User-Agent naming the requester with a contact email, and under
10 requests/second across all EDGAR hosts. Both collectors share one limiter and one
client. Filings are keyed on accession number, so the feed and the per-company pull
dedupe against each other. Only filings tied to a listed ticker are kept.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.collectors.base import CollectResult, RateLimiter, describe_http_error, make_client

logger = logging.getLogger(__name__)

LATEST_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company=&dateb="
    "&owner=include&start=0&count=100&output=atom"
)
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
REQUESTS_PER_SECOND = 8
TICKER_MAP_MAX_AGE_S = 24 * 3600
SUBMISSIONS_LOOKBACK_DAYS = 7

ATOM = "{http://www.w3.org/2005/Atom}"
_TITLE_CIK = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)$")
_ACCESSION = re.compile(r"accession-number=(?P<accession>\d{10}-\d{2}-\d{6})")


class EdgarClient:
    def __init__(self, user_agent: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = make_client(headers={"User-Agent": user_agent}, transport=transport)
        self._limiter = RateLimiter(REQUESTS_PER_SECOND, 1.0)
        self._tickers_by_cik: dict[int, tuple[str, ...]] = {}
        self._cik_by_ticker: dict[str, int] = {}
        self._ticker_map_loaded_at = 0.0

    async def get(self, url: str) -> httpx.Response:
        await self._limiter.acquire()
        response = await self._http.get(url)
        response.raise_for_status()
        return response

    async def ticker_maps(self) -> tuple[dict[int, tuple[str, ...]], dict[str, int]]:
        if time.monotonic() - self._ticker_map_loaded_at > TICKER_MAP_MAX_AGE_S:
            data = (await self.get(TICKER_MAP_URL)).json()
            by_cik: dict[int, list[str]] = {}
            by_ticker: dict[str, int] = {}
            for entry in data.values():
                cik = int(entry["cik_str"])
                ticker = str(entry["ticker"]).upper()
                by_cik.setdefault(cik, []).append(ticker)
                by_ticker[ticker] = cik
            self._tickers_by_cik = {cik: tuple(tickers) for cik, tickers in by_cik.items()}
            self._cik_by_ticker = by_ticker
            self._ticker_map_loaded_at = time.monotonic()
        return self._tickers_by_cik, self._cik_by_ticker

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass
class _Filing:
    accession: str
    form: str
    url: str | None
    filed_at: datetime | None
    ciks: set[int] = field(default_factory=set)
    names: dict[int, str] = field(default_factory=dict)
    roles: dict[int, str] = field(default_factory=dict)


def _filing_record(
    filing: _Filing, tickers_by_cik: dict[int, tuple[str, ...]], collector: str
) -> RawRecord | None:
    tickers = tuple(t for cik in filing.ciks for t in tickers_by_cik.get(cik, ()))
    if not tickers:
        return None
    return RawRecord(
        produced_by=f"data.{collector}",
        runtime_ms=0,
        source="edgar.filing",
        source_id=filing.accession,
        url=filing.url,
        fetched_at=datetime.now(UTC),
        published_at=filing.filed_at,
        tickers=tickers,
        tags=("filing", filing.form.lower().replace(" ", "_")),
        payload={
            "form": filing.form,
            "accession": filing.accession,
            "companies": [
                {"cik": cik, "name": filing.names.get(cik), "role": filing.roles.get(cik)}
                for cik in sorted(filing.ciks)
            ],
        },
        content_hash=content_hash("edgar.filing", filing.accession),
    )


def parse_latest_feed(xml_bytes: bytes) -> list[_Filing]:
    """Group feed entries by accession; one filing lists filer, issuer and reporter."""
    root = ET.fromstring(xml_bytes)  # noqa: S314 - trusted SEC feed, no external entities
    filings: dict[str, _Filing] = {}
    for entry in root.iter(f"{ATOM}entry"):
        title_match = _TITLE_CIK.match((entry.findtext(f"{ATOM}title") or "").strip())
        accession_match = _ACCESSION.search(entry.findtext(f"{ATOM}id") or "")
        if title_match is None or accession_match is None:
            continue
        accession = accession_match["accession"]
        category = entry.find(f"{ATOM}category")
        link = entry.find(f"{ATOM}link")
        updated = entry.findtext(f"{ATOM}updated")
        filing = filings.setdefault(
            accession,
            _Filing(
                accession=accession,
                form=(category.get("term") if category is not None else None)
                or title_match["form"],
                url=link.get("href") if link is not None else None,
                filed_at=datetime.fromisoformat(updated).astimezone(UTC) if updated else None,
            ),
        )
        cik = int(title_match["cik"])
        filing.ciks.add(cik)
        filing.names[cik] = title_match["name"]
        filing.roles[cik] = title_match["role"]
    return list(filings.values())


def parse_submissions(data: dict[str, Any], cik: int, since: date) -> list[_Filing]:
    recent = data.get("filings", {}).get("recent", {})
    filings = []
    for index, accession in enumerate(recent.get("accessionNumber", [])):
        filing_date = date.fromisoformat(recent["filingDate"][index])
        if filing_date < since:
            continue
        accepted = recent.get("acceptanceDateTime", [None] * (index + 1))[index]
        document = recent.get("primaryDocument", [""] * (index + 1))[index]
        filing = _Filing(
            accession=accession,
            form=recent["form"][index],
            url=ARCHIVE_URL.format(cik=cik, accession=accession.replace("-", ""), document=document)
            if document
            else None,
            filed_at=datetime.fromisoformat(accepted.replace("Z", "+00:00")).astimezone(UTC)
            if accepted
            else None,
        )
        filing.ciks.add(cik)
        filing.names[cik] = data.get("name", "")
        filing.roles[cik] = "Filer"
        filings.append(filing)
    return filings


class EdgarLatestFilings:
    name = "edgar_latest_filings"

    def __init__(self, client: EdgarClient) -> None:
        self._client = client

    async def collect(self) -> CollectResult:
        tickers_by_cik, _ = await self._client.ticker_maps()
        feed = await self._client.get(LATEST_FEED_URL)
        records = [
            record
            for filing in parse_latest_feed(feed.content)
            if (record := _filing_record(filing, tickers_by_cik, self.name)) is not None
        ]
        return CollectResult(records=records)

    async def aclose(self) -> None:
        return None


class EdgarCompanyFilings:
    name = "edgar_company_filings"

    def __init__(self, client: EdgarClient, symbols: Callable[[], tuple[str, ...]]) -> None:
        self._client = client
        self._symbols = symbols

    async def collect(self) -> CollectResult:
        result = CollectResult()
        tickers_by_cik, cik_by_ticker = await self._client.ticker_maps()
        since = datetime.now(UTC).date() - timedelta(days=SUBMISSIONS_LOOKBACK_DAYS)
        for symbol in self._symbols():
            cik = cik_by_ticker.get(symbol)
            if cik is None:
                # ETFs and funds are not in the company ticker map and have no company
                # filings to follow; that is expected, not a failure.
                logger.debug("%s: no CIK in the SEC ticker map, skipped", symbol)
                continue
            try:
                data = (await self._client.get(SUBMISSIONS_URL.format(cik=cik))).json()
            except httpx.HTTPError as exc:
                result.errors.append(f"{symbol}: {describe_http_error(exc)}")
                continue
            for filing in parse_submissions(data, cik, since):
                record = _filing_record(filing, tickers_by_cik, self.name)
                if record is not None:
                    result.records.append(record)
        return result

    async def aclose(self) -> None:
        return None
