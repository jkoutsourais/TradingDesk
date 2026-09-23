"""Truth Social posts via CNN's public archive (updated about every 5 minutes).

The archive is one ~20 MB JSON array, newest post first. Each poll sends a conditional
request with the last ETag, and when the file has changed it fetches only the first
HEAD_BYTES bytes and parses the complete posts in that prefix.
"""

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.collectors.base import CollectResult, make_client

ARCHIVE_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"
HEAD_BYTES = 262_144


def parse_array_prefix(text: str) -> list[dict[str, Any]]:
    """Parse the complete objects at the start of a possibly truncated JSON array."""
    decoder = json.JSONDecoder()
    start = text.find("[")
    if start < 0:
        raise ValueError("archive does not start with a JSON array")
    position = start + 1
    items: list[dict[str, Any]] = []
    while True:
        while position < len(text) and text[position] in " \t\r\n,":
            position += 1
        if position >= len(text) or text[position] == "]":
            return items
        try:
            item, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            return items  # truncated final object
        items.append(item)


def _post_record(post: dict[str, Any], fetched_at: datetime) -> RawRecord:
    post_id = str(post["id"])
    body = post.get("content") or ""
    return RawRecord(
        produced_by="data.truth_social",
        runtime_ms=0,
        source="truth_social.post",
        source_id=post_id,
        url=post.get("url"),
        fetched_at=fetched_at,
        published_at=datetime.fromisoformat(post["created_at"]).astimezone(UTC),
        tags=("policy", "officials"),
        payload={"content": body, "media": post.get("media") or []},
        # Engagement counts change constantly and are left out of the hash; an edited
        # post body produces a new record.
        content_hash=content_hash("truth_social.post", post_id, body),
    )


class TruthSocialCollector:
    name = "truth_social"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = make_client(transport=transport)
        self._etag: str | None = None

    async def collect(self) -> CollectResult:
        # Identity encoding keeps the byte range meaningful as a prefix of the JSON text.
        headers = {"Range": f"bytes=0-{HEAD_BYTES - 1}", "Accept-Encoding": "identity"}
        if self._etag:
            headers["If-None-Match"] = self._etag
        response = await self._http.get(ARCHIVE_URL, headers=headers)
        if response.status_code == 304:
            return CollectResult()
        response.raise_for_status()
        self._etag = response.headers.get("ETag")
        fetched_at = datetime.now(UTC)
        posts = parse_array_prefix(response.text)
        if not posts:
            raise ValueError("no complete posts in archive prefix")
        return CollectResult(records=[_post_record(post, fetched_at) for post in posts])

    async def aclose(self) -> None:
        await self._http.aclose()
