"""Federal Reserve RSS feeds: speeches and all press releases (FOMC statements included)."""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.collectors.base import CollectResult, make_client

FEEDS = {
    "fed_speeches": ("https://www.federalreserve.gov/feeds/speeches.xml", "fed.speech"),
    "fed_press_releases": ("https://www.federalreserve.gov/feeds/press_all.xml", "fed.press"),
}


def _text(item: ET.Element, tag: str) -> str | None:
    value = item.findtext(tag)
    return value.strip() if value else None


def parse_feed(xml_bytes: bytes, source: str, collector: str) -> list[RawRecord]:
    # The Fed's feeds are trusted, plain RSS; ElementTree does not resolve external entities.
    root = ET.fromstring(xml_bytes)  # noqa: S314
    records = []
    fetched_at = datetime.now(UTC)
    for item in root.iter("item"):
        guid = _text(item, "guid") or _text(item, "link")
        if not guid:
            continue
        pub_date = _text(item, "pubDate")
        category = _text(item, "category")
        records.append(
            RawRecord(
                produced_by=f"data.{collector}",
                runtime_ms=0,
                source=source,
                source_id=guid,
                url=_text(item, "link"),
                fetched_at=fetched_at,
                published_at=parsedate_to_datetime(pub_date).astimezone(UTC) if pub_date else None,
                tags=("fed", "policy", *([category.lower().replace(" ", "_")] if category else [])),
                payload={
                    "title": _text(item, "title"),
                    "description": _text(item, "description"),
                    "category": category,
                },
                content_hash=content_hash(source, guid),
            )
        )
    return records


class FedRssCollector:
    def __init__(self, name: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = name
        self._url, self._source = FEEDS[name]
        self._http = make_client(transport=transport)

    async def collect(self) -> CollectResult:
        response = await self._http.get(self._url)
        response.raise_for_status()
        return CollectResult(records=parse_feed(response.content, self._source, self.name))

    async def aclose(self) -> None:
        await self._http.aclose()
