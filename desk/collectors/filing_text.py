"""Text of recent 8-K filings for Tier 0-1 names, for the research desk.

An 8-K's main document often only lists items; the substance is usually in exhibit 99.x
(the press release). For each new 8-K the filing's index.json is read, the main document
and any 99.x exhibits are fetched, their HTML is reduced to text, and the result is stored
as an edgar.filing_text raw record keyed on the accession number.
"""

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, ClassVar

import httpx
from sqlalchemy import Engine, text

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.collectors.base import CollectResult, describe_http_error
from desk.collectors.edgar import EdgarClient

FORMS = ("8-K", "8-K/A")
LOOKBACK = timedelta(days=3)
MAX_FILINGS_PER_RUN = 15
MAX_TEXT_CHARS = 30_000
_EXHIBIT = re.compile(r"ex[-_]?99", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    SKIP: ClassVar[frozenset[str]] = frozenset({"script", "style", "head", "title"})
    BLOCK: ClassVar[frozenset[str]] = frozenset(
        {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}
    )

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(page: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(page)
    joined = "".join(extractor.parts).replace("\xa0", " ")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in joined.splitlines())
    return "\n".join(line for line in lines if line)


def filing_folder(url: str) -> str:
    """Archive folder of a filing from either its index page or a document URL."""
    return url.rsplit("/", 1)[0]


def pick_documents(index: dict[str, Any], primary_name: str | None) -> list[str]:
    """Main document plus exhibit 99.x, by name, from a filing's index.json."""
    names = [item["name"] for item in index.get("directory", {}).get("item", [])]
    html_docs = [n for n in names if n.lower().endswith((".htm", ".html")) and "index" not in n]
    chosen = []
    if primary_name and primary_name in html_docs:
        chosen.append(primary_name)
    elif html_docs:
        # Without a known primary document, the first non-exhibit page is the filing body.
        main = [n for n in html_docs if not _EXHIBIT.search(n)]
        if main:
            chosen.append(main[0])
    chosen += [n for n in html_docs if _EXHIBIT.search(n) and n not in chosen]
    return chosen


class FilingText:
    name = "edgar_filing_text"

    def __init__(
        self,
        engine: Engine,
        client: EdgarClient,
        symbols: Callable[[], tuple[str, ...]],
    ) -> None:
        self._engine = engine
        self._client = client
        self._symbols = symbols

    def _pending(self) -> list[Any]:
        with self._engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT f.payload->>'source_id' AS accession, f.payload->>'url' AS url, "
                    "f.payload->'tickers' AS tickers, f.payload->'payload'->>'form' AS form, "
                    "f.payload->>'published_at' AS published "
                    "FROM artifacts f WHERE f.kind = 'raw_record' "
                    "AND f.payload->>'source' = 'edgar.filing' "
                    "AND f.payload->'payload'->>'form' = ANY(:forms) AND f.created_at >= :since "
                    "AND f.payload->'tickers' ?| CAST(:symbols AS text[]) "
                    "AND NOT EXISTS (SELECT 1 FROM artifacts t WHERE t.kind = 'raw_record' "
                    "  AND t.payload->>'source' = 'edgar.filing_text' "
                    "  AND t.payload->>'source_id' = f.payload->>'source_id') "
                    "ORDER BY f.created_at DESC LIMIT :n"
                ),
                {
                    "forms": list(FORMS),
                    "since": datetime.now(UTC) - LOOKBACK,
                    "symbols": list(self._symbols()),
                    "n": MAX_FILINGS_PER_RUN,
                },
            ).all()

    async def collect(self) -> CollectResult:
        result = CollectResult()
        for filing in await asyncio.to_thread(self._pending):
            if not filing.url:
                continue
            folder = filing_folder(filing.url)
            primary = None if filing.url.endswith("-index.htm") else filing.url.rsplit("/", 1)[1]
            try:
                index = (await self._client.get(f"{folder}/index.json")).json()
                texts = []
                for name in pick_documents(index, primary):
                    page = (await self._client.get(f"{folder}/{name}")).text
                    texts.append(f"[{name}]\n{html_to_text(page)}")
            except httpx.HTTPError as exc:
                result.errors.append(f"{filing.accession}: {describe_http_error(exc)}")
                continue
            body = "\n\n".join(texts)[:MAX_TEXT_CHARS]
            if not body:
                continue
            result.records.append(
                RawRecord(
                    produced_by="data.edgar_filing_text",
                    runtime_ms=0,
                    source="edgar.filing_text",
                    source_id=filing.accession,
                    url=filing.url,
                    fetched_at=datetime.now(UTC),
                    published_at=datetime.fromisoformat(filing.published)
                    if filing.published
                    else None,
                    tickers=tuple(filing.tickers or ()),
                    tags=("filing", "filing_text", filing.form.lower()),
                    payload={"form": filing.form, "accession": filing.accession, "text": body},
                    content_hash=content_hash("edgar.filing_text", filing.accession),
                )
            )
        return result

    async def aclose(self) -> None:
        return None
