"""Small-model triage of new triggers and Tier 0-1 headlines, and the urgent push path.

Every minute (paused while a shift holds the GPU), up to MAX_ITEMS untriaged items are
labelled relevant or noise by the small model, and each label is stored as a TriageLabel
artifact citing its subject. An urgent trigger the model confirms as relevant is pushed,
subject to the push window and rate limits; otherwise the push is recorded as held.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.artifacts.brief import TriageLabel
from desk.artifacts.store import append_artifact
from desk.collectors.embeddings import record_text
from desk.collectors.holdings import held_symbols
from desk.config import ChatModel, TiersConfig
from desk.front_office.notify import (
    NotifyConfig,
    NtfyClient,
    decide_urgent,
    record_push,
    urgent_counts,
)
from desk.llm.client import OllamaChat
from desk.llm.prompts import load_prompt

MAX_ITEMS = 25
TRIGGER_LOOKBACK = timedelta(hours=24)
NEWS_LOOKBACK = timedelta(hours=12)
NEWS_SOURCES = (
    "finnhub.company_news",
    "truth_social.post",
    "fed.speech",
    "fed.press",
    "federal_register.public_inspection",
)
DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
TIER_REFERENCE = re.compile(r"\btier\s*[0-3]\b", re.IGNORECASE)


class TriageEntry(BaseModel):
    index: int
    label: Literal["relevant", "noise"]
    reason: str = Field(min_length=1, max_length=300)


class TriageReply(BaseModel):
    labels: list[TriageEntry]


@dataclass(frozen=True, slots=True)
class Item:
    subject_id: UUID
    kind: str  # "trigger" or "news"
    text: str
    urgent: bool = False
    created_at: datetime | None = None


def pending_items(conn: Connection, tiers: TiersConfig, now: datetime) -> list[Item]:
    items: list[Item] = []
    for row in conn.execute(
        text(
            "SELECT t.id, t.payload->>'summary' AS summary, t.payload->>'instrument' AS inst, "
            "coalesce(t.payload->>'tier', '-') AS tier, (t.payload->>'urgent')::boolean AS urgent, "
            "t.created_at "
            "FROM artifacts t WHERE t.kind = 'trigger' AND t.created_at >= :since "
            "AND NOT EXISTS (SELECT 1 FROM artifacts l WHERE l.kind = 'triage_label' "
            "  AND l.payload->>'subject_id' = t.id::text) "
            "ORDER BY (t.payload->>'urgent')::boolean DESC, t.created_at DESC LIMIT :n"
        ),
        {"since": now - TRIGGER_LOOKBACK, "n": MAX_ITEMS},
    ):
        items.append(
            Item(
                row.id,
                "trigger",
                f"watch hit, tier {row.tier}: {row.summary}",
                urgent=bool(row.urgent),
                created_at=row.created_at,
            )
        )
    watched = sorted({*held_symbols(conn), *tiers.tier_1_stocks()})
    room = MAX_ITEMS - len(items)
    if room > 0:
        for row in conn.execute(
            text(
                "SELECT r.id, r.payload AS payload FROM artifacts r WHERE r.kind = 'raw_record' "
                "AND r.payload->>'source' = ANY(:sources) AND r.created_at >= :since "
                "AND (r.payload->>'source' <> 'finnhub.company_news' "
                "     OR r.payload->'tickers' ?| CAST(:watched AS text[])) "
                "AND NOT EXISTS (SELECT 1 FROM artifacts l WHERE l.kind = 'triage_label' "
                "  AND l.payload->>'subject_id' = r.id::text) "
                "ORDER BY r.created_at DESC LIMIT :n"
            ),
            {
                "sources": list(NEWS_SOURCES),
                "since": now - NEWS_LOOKBACK,
                "watched": watched,
                "n": room,
            },
        ):
            payload = row.payload
            body = record_text(payload["source"], payload["payload"], payload.get("url"))
            tickers = ", ".join(payload.get("tickers", [])[:5]) or "no tickers"
            items.append(Item(row.id, "news", f"{payload['source']} ({tickers}): {body[:300]}"))
    return items


def _indexes_ok(items: list[Item], reply: TriageReply) -> bool:
    indexes = [entry.index for entry in reply.labels]
    return sorted(indexes) == list(range(1, len(items) + 1))


def _invented_numbers(item: Item, entry: TriageEntry) -> set[str]:
    allowed = set(DIGITS.findall(item.text))
    # "tier 1" names a category from the prompt, not a data value.
    reason = TIER_REFERENCE.sub("", entry.reason)
    return set(DIGITS.findall(reason)) - allowed


def check_reply(items: list[Item]) -> Any:
    def check(reply: TriageReply) -> list[str]:
        problems = []
        if not _indexes_ok(items, reply):
            problems.append(f"return exactly one entry for each index 1 to {len(items)}")
        for entry in reply.labels:
            if 1 <= entry.index <= len(items):
                extra = _invented_numbers(items[entry.index - 1], entry)
                if extra:
                    problems.append(f"item {entry.index}: reason adds numbers {sorted(extra)}")
        return problems

    return check


WITHHELD_REASON = "reason withheld: it cited a number not in the item"


def salvage_reply(items: list[Item], reply: TriageReply) -> TriageReply | None:
    """Keep every label and withhold only reasons that cite numbers not in their item.

    The label is the judgment; a stray number in its one-line reason should not cost the
    whole batch. Returns None when the reply does not label each item exactly once.
    """
    if not _indexes_ok(items, reply):
        return None
    return TriageReply(
        labels=[
            entry.model_copy(update={"reason": WITHHELD_REASON})
            if _invented_numbers(items[entry.index - 1], entry)
            else entry
            for entry in reply.labels
        ]
    )


@dataclass
class TriageOutcome:
    labelled: int = 0
    relevant: int = 0
    pushed: int = 0
    held: int = 0
    failed: bool = False
    error: str | None = None


async def run_triage(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    tiers: TiersConfig,
    ntfy: NtfyClient | None,
    notify: NotifyConfig,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> TriageOutcome:
    now = now or datetime.now(UTC)
    outcome = TriageOutcome()
    with engine.connect() as conn:
        items = pending_items(conn, tiers, now)
        held = held_symbols(conn)
        holdings = ", ".join(held) or "no positions"
        watchlist = ", ".join(s for s in tiers.tier_1_symbols() if s not in held)
    if not items:
        return outcome
    listing = "\n".join(f"[{i}] {item.text}" for i, item in enumerate(items, start=1))
    prompt = load_prompt("triage", {"holdings": holdings, "watchlist": watchlist, "items": listing})
    result = await chat.structured(model, prompt, TriageReply, check=check_reply(items))
    reply = result.value
    if reply is None and result.rejected is not None:
        reply = salvage_reply(items, result.rejected)
    if reply is None:
        outcome.failed, outcome.error = True, result.error
        return outcome

    to_push: list[Item] = []
    with engine.begin() as conn:
        for entry in reply.labels:
            item = items[entry.index - 1]
            append_artifact(
                conn,
                TriageLabel(
                    produced_by="watch.triage",
                    runtime_ms=result.usage.total_ms // max(len(items), 1),
                    parents=(item.subject_id,),
                    model=model.model,
                    prompt_version=prompt.version,
                    subject_id=item.subject_id,
                    label=entry.label,
                    reason=entry.reason,
                ),
            )
            outcome.labelled += 1
            if entry.label == "relevant":
                outcome.relevant += 1
                if item.kind == "trigger" and item.urgent:
                    to_push.append(item)

    for item in to_push:
        await _push_urgent(engine, ntfy, notify, tz, item, now, outcome)
    return outcome


async def _push_urgent(
    engine: Engine,
    ntfy: NtfyClient | None,
    notify: NotifyConfig,
    tz: ZoneInfo,
    item: Item,
    now: datetime,
    outcome: TriageOutcome,
) -> None:
    config = notify.urgent
    click = f"{notify.base_url}/#/alerts/{item.subject_id}"
    message = "Tap to see the alert."
    with engine.connect() as conn:
        hour, day = urgent_counts(conn, now, tz)
    decision = decide_urgent(config, now, tz, hour, day, alert_time=item.created_at)
    if decision.allowed and ntfy is not None:
        try:
            await ntfy.send(
                title=config.title, message=message, priority=config.priority, click_url=click
            )
        except Exception as exc:  # noqa: BLE001 - recorded as a failed push
            with engine.begin() as conn:
                record_push(
                    conn,
                    kind="urgent",
                    ref_id=item.subject_id,
                    title=config.title,
                    message=message,
                    click_url=click,
                    priority=config.priority,
                    status="failed",
                    reason=f"{type(exc).__name__}: {exc}"[:300],
                )
            return
        status, reason = "sent", decision.reason
        outcome.pushed += 1
    else:
        status = "held"
        reason = decision.reason if not decision.allowed else "ntfy is not configured"
        outcome.held += 1
    with engine.begin() as conn:
        record_push(
            conn,
            kind="urgent",
            ref_id=item.subject_id,
            title=config.title,
            message=message,
            click_url=click,
            priority=config.priority,
            status=status,
            reason=reason,
        )
