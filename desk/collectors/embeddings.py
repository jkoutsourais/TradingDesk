"""Embed news and policy raw records shortly after they arrive.

Runs every minute, newest records first, so fresh stories never queue behind the backlog.
Filings and the earnings calendar carry no prose and are not embedded.
"""

import asyncio
import html
import re
from typing import Any

from sqlalchemy import Engine, text

from desk.collectors.base import CollectResult, RecordEmbedding
from desk.llm.embeddings import OllamaEmbedder

EMBEDDED_SOURCES = (
    "finnhub.company_news",
    "finnhub.general_news",
    "truth_social.post",
    "fed.speech",
    "fed.press",
    "federal_register.document",
    "federal_register.public_inspection",
    "edgar.filing_text",
)
MAX_RECORDS_PER_RUN = 512
MAX_TEXT_CHARS = 4000
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def record_text(source: str, body: dict[str, Any], url: str | None) -> str:
    """The prose to embed for one record; falls back to the URL so every record resolves."""
    if source.startswith("finnhub."):
        parts = [body.get("headline"), body.get("summary")]
    elif source == "truth_social.post":
        parts = [body.get("content")]
    elif source.startswith("fed."):
        parts = [body.get("title"), body.get("description")]
    elif source == "federal_register.public_inspection":
        parts = [body.get("title"), *(body.get("subjects") or [])]
    elif source == "federal_register.document":
        parts = [body.get("type"), body.get("title"), body.get("abstract")]
    elif source == "edgar.filing_text":
        parts = [body.get("text")]
    else:
        parts = []
    joined = " \n".join(str(part) for part in parts if part)
    cleaned = _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", joined))).strip()
    return (cleaned or url or source)[:MAX_TEXT_CHARS]


class NewsEmbeddings:
    name = "news_embeddings"

    def __init__(self, engine: Engine, embedder: OllamaEmbedder, batch_size: int) -> None:
        self._engine = engine
        self._embedder = embedder
        self._batch_size = batch_size

    def _pending(self) -> list[tuple[Any, str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.id, a.payload->>'source' AS source, a.payload->'payload' AS body, "
                    "a.payload->>'url' AS url FROM artifacts a "
                    "WHERE a.kind = 'raw_record' AND a.payload->>'source' = ANY(:sources) "
                    "AND NOT EXISTS (SELECT 1 FROM raw_record_embeddings e "
                    "WHERE e.artifact_id = a.id AND e.model = :model) "
                    "ORDER BY a.created_at DESC LIMIT :limit"
                ),
                {
                    "sources": list(EMBEDDED_SOURCES),
                    "model": self._embedder.model,
                    "limit": MAX_RECORDS_PER_RUN,
                },
            ).all()
        return [(row.id, record_text(row.source, row.body or {}, row.url)) for row in rows]

    async def collect(self) -> CollectResult:
        pending = await asyncio.to_thread(self._pending)
        result = CollectResult()
        for start in range(0, len(pending), self._batch_size):
            batch = pending[start : start + self._batch_size]
            vectors = await self._embedder.embed_documents([body for _, body in batch])
            result.embeddings += [
                RecordEmbedding(record_id, self._embedder.model, tuple(vector))
                for (record_id, _), vector in zip(batch, vectors, strict=True)
            ]
        return result

    async def aclose(self) -> None:
        await self._embedder.aclose()
