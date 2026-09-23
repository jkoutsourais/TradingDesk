"""RawRecord: one item fetched by a Data desk collector (news story, filing, post, release)."""

import hashlib
from typing import Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact


def content_hash(*parts: str) -> str:
    """SHA-256 over the identifying parts of a record.

    Each collector chooses parts that identify the content and ignores volatile fields
    (engagement counts, fetch time), so a re-fetch of unchanged content dedupes. Parts are
    length-prefixed so ("ab", "c") and ("a", "bc") hash differently.
    """
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@register_artifact
class RawRecord(ArtifactBase):
    kind: Literal["raw_record"] = "raw_record"
    schema_version: Literal[1] = 1

    source: str = Field(min_length=1)  # e.g. "finnhub.company_news", "edgar.filing"
    source_id: str = Field(min_length=1)
    url: str | None = None
    fetched_at: AwareDatetime
    published_at: AwareDatetime | None = None
    tickers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    payload: dict[str, JsonValue]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("tickers")
    @classmethod
    def _normalize_tickers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = {ticker.strip().upper() for ticker in value}
        if "" in normalized:
            raise ValueError("tickers must not contain empty values")
        return tuple(sorted(normalized))

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _no_parents(self) -> Self:
        # Raw records are lineage roots; the buffer purge relies on them having no parents.
        if self.parents:
            raise ValueError("a raw record cannot have parents")
        return self
