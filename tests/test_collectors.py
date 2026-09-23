"""Collector parsing against mocked HTTP. Payloads are invented but shaped like the live APIs."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from desk.collectors.edgar import (
    EdgarClient,
    EdgarCompanyFilings,
    EdgarLatestFilings,
    parse_submissions,
)
from desk.collectors.fed_rss import FedRssCollector
from desk.collectors.federal_register import (
    FederalRegisterDocuments,
    FederalRegisterPublicInspection,
)
from desk.collectors.finnhub import FinnhubClient, FinnhubCompanyNews, FinnhubEarningsCalendar
from desk.collectors.numeric import CftcCotCollector, EiaCollector, FredCollector
from desk.collectors.truth_social import TruthSocialCollector, parse_array_prefix
from desk.config import load_sources

TODAY = datetime.now(UTC).date()


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# --- Finnhub -------------------------------------------------------------------------


def finnhub_news_item(story_id: int, related: str) -> dict[str, object]:
    return {
        "category": "company",
        "datetime": 1790113544,
        "headline": f"Story {story_id}",
        "id": story_id,
        "image": "",
        "related": related,
        "source": "Example Wire",
        "summary": "Summary text",
        "url": f"https://example.com/{story_id}",
    }


def test_finnhub_company_news_uses_header_token_and_dedupes_by_story_id() -> None:
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        symbol = request.url.params["symbol"]
        if symbol == "BAD":
            return httpx.Response(500)
        return httpx.Response(200, json=[finnhub_news_item(1, "NVDA,AMD")])

    client = FinnhubClient("KEY123", transport=httpx.MockTransport(handler))
    collector = FinnhubCompanyNews(client, lambda: ("NVDA", "AMD", "BAD"))
    result = run(collector.collect())

    assert all(r.headers["X-Finnhub-Token"] == "KEY123" for r in seen_requests)
    assert all("KEY123" not in str(r.url) for r in seen_requests)
    assert len(result.records) == 2
    assert len({record.content_hash for record in result.records}) == 1
    assert result.records[0].tickers == ("AMD", "NVDA")
    assert result.records[0].published_at == datetime.fromtimestamp(1790113544, UTC)
    assert len(result.errors) == 1 and result.errors[0].startswith("BAD:")


def test_finnhub_retries_once_after_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from desk.collectors import finnhub

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(finnhub.asyncio, "sleep", fake_sleep)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"x-ratelimit-reset": "0"})
        return httpx.Response(200, json=[])

    client = FinnhubClient("KEY", transport=httpx.MockTransport(handler))
    assert run(client.get("/company-news", {"symbol": "NVDA"})) == []
    assert len(calls) == 2 and waits == [1.0]


def test_finnhub_earnings_hash_changes_when_actuals_arrive() -> None:
    entry = {
        "symbol": "ORCL",
        "date": "2026-09-29",
        "hour": "amc",
        "quarter": 1,
        "year": 2027,
        "epsEstimate": 1.5,
        "epsActual": None,
        "revenueEstimate": 1.0e10,
        "revenueActual": None,
    }
    responses = [entry, {**entry, "epsActual": 1.62}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"earningsCalendar": [responses.pop(0)]})

    client = FinnhubClient("KEY", transport=httpx.MockTransport(handler))
    collector = FinnhubEarningsCalendar(client)
    before = run(collector.collect()).records[0]
    after = run(collector.collect()).records[0]
    assert before.source_id == after.source_id == "ORCL:2027Q1"
    assert before.content_hash != after.content_hash


# --- Federal Register and Fed RSS ------------------------------------------------------


def test_federal_register_documents() -> None:
    body = {
        "results": [
            {
                "document_number": "2026-19417",
                "title": "An Executive Order",
                "type": "Presidential Document",
                "subtype": "Executive Order",
                "abstract": None,
                "html_url": "https://www.federalregister.gov/d/2026-19417",
                "publication_date": "2026-09-22",
                "signing_date": "2026-09-18",
                "executive_order_number": 14999,
                "agencies": [{"name": "Executive Office of the President"}],
            }
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    record = run(FederalRegisterDocuments(transport=transport).collect()).records[0]
    assert record.source == "federal_register.document"
    assert record.tags == ("policy", "presidential_document")
    assert record.payload["executive_order_number"] == 14999


def test_federal_register_public_inspection() -> None:
    body = {
        "results": [
            {
                "document_number": "2026-19456",
                "title": "Tariff adjustment",
                "type": "Rule",
                "filed_at": "2026-09-21T16:15:00.000-04:00",
                "publication_date": "2026-09-23",
                "subject_1": "Tariffs:",
                "subject_2": "Steel",
                "subject_3": None,
                "filing_type": "regular",
                "html_url": "https://example.gov/pi/2026-19456",
                "agencies": [{"raw_name": "OFFICE OF THE U.S. TRADE REPRESENTATIVE"}],
            }
        ]
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    record = run(FederalRegisterPublicInspection(transport=transport).collect()).records[0]
    assert record.published_at == datetime(2026, 9, 21, 20, 15, tzinfo=UTC)
    assert record.payload["subjects"] == ["Tariffs:", "Steel"]


FED_RSS = """﻿<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0"><channel><title>FRB: Speeches</title>
<item>
  <title>Jefferson, Treasury Market Functioning</title>
  <link><![CDATA[https://www.federalreserve.gov/newsevents/speech/jefferson20260922a.htm]]></link>
  <guid><![CDATA[https://www.federalreserve.gov/newsevents/speech/jefferson20260922a.htm]]></guid>
  <description><![CDATA[Speech at a conference]]></description>
  <category>Speech</category>
  <pubDate><![CDATA[Tue, 22 Sep 2026 14:20:00 GMT]]></pubDate>
</item></channel></rss>""".encode()


def test_fed_rss_parses_bom_prefixed_feed() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=FED_RSS))
    record = run(FedRssCollector("fed_speeches", transport=transport).collect()).records[0]
    assert record.source == "fed.speech"
    assert record.published_at == datetime(2026, 9, 22, 14, 20, tzinfo=UTC)
    assert record.tags == ("fed", "policy", "speech")


# --- Truth Social -----------------------------------------------------------------------


def truth_post(post_id: str, content: str, favourites: int = 1) -> dict[str, object]:
    return {
        "id": post_id,
        "created_at": "2026-09-22T22:13:52.016Z",
        "content": content,
        "url": f"https://truthsocial.com/@x/{post_id}",
        "media": [],
        "replies_count": 1,
        "reblogs_count": 1,
        "favourites_count": favourites,
    }


def test_parse_array_prefix_drops_truncated_tail() -> None:
    full = json.dumps([truth_post("2", "newest"), truth_post("1", "older")], indent=2)
    truncated = full[: full.index('"older"') + 3]
    assert [item["id"] for item in parse_array_prefix(truncated)] == ["2"]
    assert [item["id"] for item in parse_array_prefix(full)] == ["2", "1"]


def test_truth_social_conditional_range_fetch() -> None:
    requests: list[httpx.Request] = []
    archive = json.dumps([truth_post("2", "tariffs on steel"), truth_post("1", "hello")])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(206, text=archive, headers={"ETag": '"v1"'})

    collector = TruthSocialCollector(transport=httpx.MockTransport(handler))
    first = run(collector.collect())
    second = run(collector.collect())
    assert requests[0].headers["Range"].startswith("bytes=0-")
    assert len(first.records) == 2
    assert second.records == []


def test_truth_social_hash_ignores_engagement_counts() -> None:
    posts = [[truth_post("7", "same text", favourites=1)], [truth_post("7", "same text", 999)]]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(posts.pop(0)))

    collector = TruthSocialCollector(transport=httpx.MockTransport(handler))
    assert (
        run(collector.collect()).records[0].content_hash
        == run(collector.collect()).records[0].content_hash
    )


# --- EDGAR ------------------------------------------------------------------------------

TICKER_MAP = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
}

EDGAR_FEED = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Latest Filings</title>
<entry>
  <title>4 - Doe Jane (0001999999) (Reporting)</title>
  <link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1999999/000104581026000123/0001045810-26-000123-index.htm"/>
  <summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-09-22</summary>
  <updated>2026-09-22T17:30:05-04:00</updated>
  <category scheme="https://www.sec.gov/" label="form type" term="4"/>
  <id>urn:tag:sec.gov,2008:accession-number=0001045810-26-000123</id>
</entry>
<entry>
  <title>4 - NVIDIA CORP (0001045810) (Issuer)</title>
  <link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1045810/000104581026000123/0001045810-26-000123-index.htm"/>
  <updated>2026-09-22T17:30:05-04:00</updated>
  <category scheme="https://www.sec.gov/" label="form type" term="4"/>
  <id>urn:tag:sec.gov,2008:accession-number=0001045810-26-000123</id>
</entry>
<entry>
  <title>D - Private Fund LP (0001888888) (Filer)</title>
  <link rel="alternate" type="text/html" href="https://www.sec.gov/x"/>
  <updated>2026-09-22T17:29:00-04:00</updated>
  <category scheme="https://www.sec.gov/" label="form type" term="D"/>
  <id>urn:tag:sec.gov,2008:accession-number=0001888888-26-000001</id>
</entry>
</feed>"""


def edgar_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["User-Agent"] == "Test Desk test@example.com"
    if request.url.path.endswith("company_tickers.json"):
        return httpx.Response(200, json=TICKER_MAP)
    if request.url.path.startswith("/submissions/"):
        return httpx.Response(
            200,
            json={
                "name": "Apple Inc.",
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-26-000100", "0000320193-25-000001"],
                        "filingDate": [TODAY.isoformat(), "2025-01-01"],
                        "acceptanceDateTime": [
                            f"{TODAY.isoformat()}T16:30:00.000Z",
                            "2025-01-01T16:30:00.000Z",
                        ],
                        "form": ["8-K", "10-K"],
                        "primaryDocument": ["aapl-8k.htm", "aapl-10k.htm"],
                    }
                },
            },
        )
    return httpx.Response(200, content=EDGAR_FEED)


def test_edgar_latest_groups_by_accession_and_keeps_listed_only() -> None:
    client = EdgarClient("Test Desk test@example.com", transport=httpx.MockTransport(edgar_handler))
    records = run(EdgarLatestFilings(client).collect()).records
    assert len(records) == 1
    record = records[0]
    assert record.source_id == "0001045810-26-000123"
    assert record.tickers == ("NVDA",)
    assert {c["role"] for c in record.payload["companies"]} == {"Reporting", "Issuer"}  # type: ignore[union-attr, index]
    assert record.published_at == datetime(2026, 9, 22, 21, 30, 5, tzinfo=UTC)


def test_edgar_company_filings_within_lookback() -> None:
    client = EdgarClient("Test Desk test@example.com", transport=httpx.MockTransport(edgar_handler))
    result = run(EdgarCompanyFilings(client, lambda: ("AAPL", "ZZZZ")).collect())
    assert [r.source_id for r in result.records] == ["0000320193-26-000100"]
    assert result.records[0].url is not None
    assert result.records[0].url.endswith("/320193/000032019326000100/aapl-8k.htm")
    # ZZZZ (like an ETF) has no CIK: skipped, not a failed run.
    assert result.errors == []


def test_feed_and_submissions_share_content_hash() -> None:
    client = EdgarClient("Test Desk test@example.com", transport=httpx.MockTransport(edgar_handler))
    run(client.ticker_maps())
    filings = parse_submissions(
        {
            "name": "NVIDIA",
            "filings": {
                "recent": {
                    "accessionNumber": ["0001045810-26-000123"],
                    "filingDate": [TODAY.isoformat()],
                    "acceptanceDateTime": [None],
                    "form": ["4"],
                    "primaryDocument": [""],
                }
            },
        },
        1045810,
        TODAY - timedelta(days=1),
    )
    from desk.collectors.edgar import _filing_record

    submitted = _filing_record(filings[0], {1045810: ("NVDA",)}, "edgar_company_filings")
    from_feed = run(EdgarLatestFilings(client).collect()).records[0]
    assert submitted is not None
    assert submitted.content_hash == from_feed.content_hash


# --- Numeric ----------------------------------------------------------------------------


def test_fred_skips_missing_values_and_keeps_key_out_of_errors() -> None:
    sources = load_sources().fred.model_copy(update={"series": {"DGS10": "10y", "BROKEN": "x"}})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "FREDKEY"
        if request.url.params["series_id"] == "BROKEN":
            return httpx.Response(400)
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2026-09-18", "value": "4.12"},
                    {"date": "2026-09-21", "value": "."},
                ]
            },
        )

    result = run(
        FredCollector("FREDKEY", sources, transport=httpx.MockTransport(handler)).collect()
    )
    assert [(o.series_id, o.period, o.value) for o in result.observations] == [
        ("DGS10", date(2026, 9, 18), Decimal("4.12"))
    ]
    assert len(result.errors) == 1
    assert "FREDKEY" not in result.errors[0]


def test_eia_filters_to_lookback() -> None:
    sources = load_sources().eia.model_copy(update={"series": {"PET.WCESTUS1.W": "crude"}})
    recent = (TODAY - timedelta(days=5)).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "response": {
                    "data": [
                        {"period": recent, "value": 415000, "units": "MBBL"},
                        {"period": "2020-01-03", "value": 430000, "units": "MBBL"},
                    ]
                }
            },
        )

    result = run(EiaCollector("K", sources, transport=httpx.MockTransport(handler)).collect())
    assert [(o.value, o.unit) for o in result.observations] == [(Decimal("415000"), "MBBL")]


def test_cftc_cot_fields_become_series() -> None:
    sources = load_sources().cftc_cot

    def handler(request: httpx.Request) -> httpx.Response:
        assert "088691" in request.url.params["$where"]
        return httpx.Response(
            200,
            json=[
                {
                    "report_date_as_yyyy_mm_dd": "2026-09-15T00:00:00.000",
                    "cftc_contract_market_code": "088691",
                    "open_interest_all": "409899",
                    "m_money_positions_long_all": "142394",
                    "m_money_positions_short_all": "9278",
                }
            ],
        )

    result = run(CftcCotCollector(sources, transport=httpx.MockTransport(handler)).collect())
    values = {o.series_id: o.value for o in result.observations}
    assert values["088691.m_money_positions_long_all"] == Decimal("142394")
    assert all(o.period == date(2026, 9, 15) for o in result.observations)
    assert len(values) == 3


@pytest.mark.parametrize("status", [429, 503])
def test_whole_collector_http_failure_raises(status: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        run(FederalRegisterDocuments(transport=transport).collect())
