from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from desk.artifacts.raw_record import RawRecord, content_hash
from desk.artifacts.registry import artifact_class

FETCHED = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def make_record(**overrides: object) -> RawRecord:
    fields: dict[str, object] = {
        "produced_by": "data.finnhub_company_news",
        "runtime_ms": 0,
        "source": "finnhub.company_news",
        "source_id": "142342933",
        "url": "https://example.com/story",
        "fetched_at": FETCHED,
        "published_at": datetime(2026, 9, 22, 11, 0, tzinfo=UTC),
        "tickers": ["nvda", "AMD"],
        "tags": ["company_news"],
        "payload": {"headline": "Chip demand rises", "summary": "..."},
        "content_hash": content_hash("finnhub.company_news", "142342933"),
    }
    fields.update(overrides)
    return RawRecord.model_validate(fields)


def test_registered_under_raw_record_kind() -> None:
    assert artifact_class("raw_record") is RawRecord
    assert make_record().kind == "raw_record"


def test_tickers_are_uppercased_deduped_and_sorted() -> None:
    record = make_record(tickers=["nvda", "AMD", "NVDA", " msft "])
    assert record.tickers == ("AMD", "MSFT", "NVDA")


def test_empty_ticker_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_record(tickers=["AAPL", "  "])


def test_tags_are_deduped_and_sorted() -> None:
    assert make_record(tags=["policy", "fed", "policy"]).tags == ("fed", "policy")


def test_content_hash_is_stable_and_order_sensitive() -> None:
    first = content_hash("edgar.filing", "0000320193-26-000123")
    assert first == content_hash("edgar.filing", "0000320193-26-000123")
    assert len(first) == 64
    assert first != content_hash("0000320193-26-000123", "edgar.filing")


def test_content_hash_parts_cannot_collide_by_concatenation() -> None:
    assert content_hash("ab", "c") != content_hash("a", "bc")


def test_malformed_content_hash_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_record(content_hash="not-a-hash")


def test_naive_published_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_record(published_at=datetime(2026, 9, 22, 11, 0))  # noqa: DTZ001


def test_published_at_is_optional() -> None:
    assert make_record(published_at=None).published_at is None


def test_source_and_source_id_are_required() -> None:
    with pytest.raises(ValidationError):
        make_record(source="")
    with pytest.raises(ValidationError):
        make_record(source_id="")


def test_raw_record_cannot_have_parents() -> None:
    from uuid import uuid4

    with pytest.raises(ValidationError, match="parents"):
        make_record(parents=[uuid4()])
