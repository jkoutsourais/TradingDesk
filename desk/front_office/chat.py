"""Chief-of-staff chat: questions answered from the desk's own records.

Code gathers the context (holdings and ratings, open theses and verdicts, recent plans
and vetoes, the latest briefing, watch hits, score rollups, facts for any ticker named,
and the lineage of an artifact being explained) as a fact table. The deep model answers
with placeholders and cites fact ids; code checks both, renders the text and records
the artifacts the answer rests on.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.artifacts.base import ArtifactBase, ArtifactStatus
from desk.artifacts.chat import ChatMessage, ChatReply
from desk.artifacts.store import append_artifact, get_artifact, get_lineage
from desk.collectors.holdings import latest_snapshots
from desk.config import ChatModel
from desk.desks.analyst.debate import ask, chain_ids, latest_verdicts
from desk.desks.analyst.facts import build_book
from desk.desks.idea.status import open_theses
from desk.llm.client import OllamaChat, Usage
from desk.llm.facts import Fact, FactTable
from desk.llm.prompts import load_prompt
from desk.scoring.report import rollups

logger = logging.getLogger(__name__)

HISTORY_TURNS = 4
MAX_TICKERS = 2
MAX_PENDING = 3
CLAIM_WINDOW = timedelta(days=7)
TICKER = re.compile(r"(?<![A-Za-z])/?[A-Z]{1,5}(?:\.[A-Z])?(?![A-Za-z])")
RATING = {"buy_add": "Buy/Add", "hold": "Hold", "trim": "Trim", "sell": "Sell"}


@dataclass
class Context:
    facts: list[Fact] = field(default_factory=list)
    artifacts: dict[str, UUID] = field(default_factory=dict)  # fact id -> artifact it shows

    def add(self, fact: Fact, artifact_id: UUID | None = None) -> None:
        if any(existing.id == fact.id for existing in self.facts):
            return
        self.facts.append(fact)
        if artifact_id is not None:
            self.artifacts[fact.id] = artifact_id

    def text(
        self, fact_id: str, value: str, label: str, ref: str, artifact_id: UUID | None = None
    ) -> None:
        self.add(Fact(fact_id, value, "", value, label, ref), artifact_id)

    def money(self, fact_id: str, value: Decimal, label: str, ref: str) -> None:
        self.add(Fact(fact_id, value, "USD", f"${value:,.2f}", label, ref))

    def pct(self, fact_id: str, value: float, label: str, ref: str) -> None:
        self.add(Fact(fact_id, Decimal(f"{value:.2f}"), "%", f"{value:+.1f}%", label, ref))


def _safe_id(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", symbol)


def add_holdings(ctx: Context, conn: Connection) -> set[str]:
    ratings = {
        row.subject: row
        for row in conn.execute(
            text(
                "SELECT DISTINCT ON (payload->>'subject') id, payload->>'subject' AS subject, "
                "payload->>'rating' AS rating, payload->'reasons'->0->>'text' AS reason, "
                "payload->>'suggested_action' AS action FROM artifacts "
                "WHERE kind = 'holding_rating' AND status = 'ok' "
                "ORDER BY payload->>'subject', created_at DESC"
            )
        )
    }
    held: set[str] = set()
    for snapshot in latest_snapshots(conn):
        ref = f"account_snapshots:{snapshot['id']}"
        if snapshot["net_liquidation"] is not None:
            broker = snapshot["account_ref"].split(":", 1)[0]
            ctx.money(
                f"acct_{broker}_nl",
                Decimal(snapshot["net_liquidation"]),
                f"{broker} account net liquidation",
                ref,
            )
        for position in snapshot["positions"]:
            symbol = position["symbol"]
            key = _safe_id(symbol)
            held.add(symbol)
            if position["market_value"] is not None:
                ctx.money(
                    f"hold_{key}_value",
                    Decimal(position["market_value"]),
                    f"{symbol} holding value",
                    ref,
                )
            if position["market_value"] and position["cost_basis"]:
                gain = (
                    Decimal(position["market_value"]) / Decimal(position["cost_basis"]) - 1
                ) * 100
                ctx.pct(f"hold_{key}_pnl", float(gain), f"{symbol} gain or loss on cost", ref)
            rating = ratings.get(symbol)
            if rating is not None:
                label = RATING.get(rating.rating, rating.rating)
                line = f"{label}: {rating.reason}. Action: {rating.action}"
                ctx.text(
                    f"rt_{key}",
                    line,
                    f"{symbol} holding rating",
                    f"holding_rating:{rating.id}",
                    rating.id,
                )
    return held


def add_theses(ctx: Context, conn: Connection) -> None:
    verdicts = latest_verdicts(conn)
    for index, thesis in enumerate(open_theses(conn)[:12], start=1):
        verdict = next((verdicts[v] for v in chain_ids(conn, thesis) if v in verdicts), None)
        summary = (
            f"{thesis.primary_instrument} {thesis.direction} ({thesis.origin}, {thesis.state}"
            + (f", verdict {verdict[1]}" if verdict else ", not debated")
            + f"): {thesis.statement[:240]}"
        )
        ctx.text(f"th_{index}", summary, "thesis", f"thesis:{thesis.id}", thesis.id)
        if thesis.invalidation is not None:
            hard, warning = thesis.invalidation.hard, thesis.invalidation.warning
            ctx.money(
                f"th_{index}_hard",
                hard.level,
                f"thesis {index} hard line ({hard.instrument})",
                hard.level_ref,
            )
            ctx.money(
                f"th_{index}_warn",
                warning.level,
                f"thesis {index} warning level ({warning.instrument})",
                warning.level_ref,
            )


def add_plans(ctx: Context, conn: Connection, now: datetime) -> None:
    rows = conn.execute(
        text(
            "SELECT p.id, p.payload->>'subject' AS subject, p.payload->>'structure' AS structure, "
            "d.payload->>'decision' AS decision, (d.payload->>'size')::int AS size, "
            "(d.payload->>'max_loss')::numeric AS max_loss, d.payload->'veto_reasons'->>0 AS veto "
            "FROM artifacts p JOIN artifacts d ON d.kind = 'risk_decision' "
            "AND d.payload->>'plan_id' = p.id::text WHERE p.kind = 'trade_plan' "
            "AND p.created_at >= :since ORDER BY p.created_at DESC LIMIT 10"
        ),
        {"since": now - timedelta(days=5)},
    ).all()
    for index, row in enumerate(rows, start=1):
        line = f"{row.subject} {row.structure.replace('_', ' ')}: {row.decision}"
        line += f", {row.veto}" if row.veto else f", size {row.size}"
        ctx.text(f"plan_{index}", line, "trade plan", f"trade_plan:{row.id}", row.id)
        if row.decision != "vetoed":
            ctx.money(
                f"plan_{index}_loss",
                Decimal(row.max_loss),
                f"plan {index} maximum loss",
                f"trade_plan:{row.id}",
            )


def add_brief(ctx: Context, conn: Connection) -> None:
    row = conn.execute(
        text("SELECT id FROM artifacts WHERE kind = 'brief' ORDER BY created_at DESC LIMIT 1")
    ).scalar()
    if row is None:
        return
    brief = get_artifact(conn, row)
    index = 0
    for section in getattr(brief, "sections", ()):
        for line in section.lines[:6]:
            index += 1
            ctx.text(
                f"br_{index}",
                line,
                f"latest briefing, {section.title}",
                f"brief:{brief.id}",
                brief.id,
            )


def add_hits(ctx: Context, conn: Connection, now: datetime) -> None:
    rows = conn.execute(
        text(
            "SELECT id, payload->>'summary' AS summary FROM artifacts WHERE kind = 'trigger' "
            "AND created_at >= :since ORDER BY (payload->>'importance')::float DESC LIMIT 10"
        ),
        {"since": now - timedelta(hours=24)},
    ).all()
    for index, row in enumerate(rows, start=1):
        ctx.text(
            f"hit_{index}", row.summary, "watch hit, last 24 hours", f"trigger:{row.id}", row.id
        )


def add_scores(ctx: Context, conn: Connection) -> None:
    report = rollups(conn, "5d")
    for dimension in ("lane", "persona", "decision"):
        for group in report["dimensions"][dimension][:6]:
            if group["hit_rate"] is None:
                continue
            key = f"sc_{dimension}_{_safe_id(group['value'])}"
            ref = f"scores:5d:{dimension}:{group['value']}"
            ctx.pct(
                f"{key}_hit",
                group["hit_rate"] * 100,
                f"{group['value']} 5-day hit rate ({group['count']} scored)",
                ref,
            )


def add_tickers(
    ctx: Context, conn: Connection, question: str, known: set[str], now: datetime, tz: ZoneInfo
) -> None:
    named = [t for t in dict.fromkeys(TICKER.findall(question)) if t in known][:MAX_TICKERS]
    for symbol in named:
        book = build_book(conn, symbol, {"levels", "trend"}, now, tz, now - CLAIM_WINDOW)
        prefix = _safe_id(symbol)
        for fact in book.facts:
            fact_id = f"{prefix}_{fact.id}"
            ctx.add(Fact(fact_id, fact.value, fact.unit, fact.display, fact.label, fact.source_ref))


def add_subject(ctx: Context, conn: Connection, subject_id: UUID) -> None:
    for entry in get_lineage(conn, subject_id)[:15]:
        artifact = entry.artifact
        fields = artifact.model_dump(
            mode="json",
            exclude={
                "id",
                "parents",
                "shift_id",
                "model",
                "prompt_version",
                "runtime_ms",
                "tokens_in",
                "tokens_out",
            },
        )
        summary = _summary(fields)
        label = (
            "the artifact to explain"
            if entry.depth == 0
            else f"its source, {entry.depth} step back"
        )
        ctx.text(
            f"art_{len(ctx.artifacts) + 1}",
            f"{artifact.kind}: {summary}",
            label,
            f"{artifact.kind}:{artifact.id}",
            artifact.id,
        )


def _summary(fields: dict[str, Any]) -> str:
    parts = []
    for key in (
        "subject",
        "instrument",
        "statement",
        "summary",
        "driver",
        "verdict",
        "rating",
        "decision",
        "rationale",
        "text",
        "reason",
        "dissent",
    ):
        value = fields.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key}: {value[:300]}")
    payload = fields.get("payload")
    if isinstance(payload, dict):
        for key in ("headline", "title", "summary"):
            if isinstance(payload.get(key), str):
                parts.append(f"{key}: {payload[key][:300]}")
    return "; ".join(parts) or str(fields.get("kind", "artifact"))


def build_context(
    conn: Connection, message: ChatMessage, known_symbols: set[str], now: datetime, tz: ZoneInfo
) -> Context:
    ctx = Context()
    held = add_holdings(ctx, conn)
    add_theses(ctx, conn)
    add_plans(ctx, conn, now)
    add_brief(ctx, conn)
    add_hits(ctx, conn, now)
    add_scores(ctx, conn)
    add_tickers(ctx, conn, message.text, known_symbols | held, now, tz)
    if message.subject_id is not None:
        add_subject(ctx, conn, message.subject_id)
    return ctx


# --- Model reply ----------------------------------------------------------------------


class ChatDraft(BaseModel):
    answer: str = Field(min_length=1, max_length=3000)
    cites: list[str] = Field(default_factory=list, max_length=20)


def check_draft(table: FactTable, known: set[str]) -> Any:
    def check(draft: ChatDraft) -> list[str]:
        problems = table.violations(draft.answer)
        unknown = [c for c in draft.cites if c.strip("{}") not in known]
        if unknown:
            problems.append(f"unknown fact ids in cites: {unknown}")
        return problems

    return check


def history(conn: Connection, thread_id: UUID, before: datetime) -> str:
    rows = conn.execute(
        text(
            "SELECT kind, payload->>'text' AS text FROM artifacts "
            "WHERE kind IN ('chat_message', 'chat_reply') AND payload->>'thread_id' = :t "
            "AND created_at < :before ORDER BY created_at DESC LIMIT :n"
        ),
        {"t": str(thread_id), "before": before, "n": HISTORY_TURNS * 2},
    ).all()
    if not rows:
        return ""
    lines = [
        f"{'Jon' if row.kind == 'chat_message' else 'Desk'}: {row.text[:600]}"
        for row in reversed(rows)
    ]
    # Earlier replies are shown for continuity; their numbers are not facts to repeat.
    return (
        "Earlier in this conversation (use the facts below, not numbers from here):\n"
        + "\n".join(lines)
        + "\n\n"
    )


async def answer(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    message: ChatMessage,
    known_symbols: set[str],
    tz: ZoneInfo,
) -> tuple[ChatReply, Usage]:
    now = datetime.now(UTC)
    with engine.connect() as conn:
        ctx = build_context(conn, message, known_symbols, now, tz)
        past = history(conn, message.thread_id, message.created_at)
    table = FactTable(ctx.facts)
    prompt = load_prompt(
        "chat",
        {
            "history": past,
            "mode": "explain this artifact" if message.mode == "explain" else "question",
            "question": message.text,
            "facts": table.prompt_listing() or "(no facts)",
        },
    )
    started = datetime.now(UTC)
    known = {fact.id for fact in ctx.facts}
    result = await ask(chat, model, prompt, ChatDraft, check_draft(table, known))
    runtime_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    envelope = {
        "produced_by": "front_office.chat",
        "runtime_ms": runtime_ms,
        "model": model.model,
        "prompt_version": prompt.version,
        "tokens_in": result.usage.tokens_in,
        "tokens_out": result.usage.tokens_out,
        "thread_id": message.thread_id,
        "message_id": message.id,
    }
    if result.value is None:
        reply = ChatReply(
            **envelope,
            status=ArtifactStatus.FAILED,
            error=(result.error or "chat reply failed")[:1000],
            parents=(message.id,),
            text=(
                "No answer this time: the model's reply did not pass the fact checks. "
                "Try rephrasing."
            ),
        )
        return reply, result.usage
    cites = [c.strip("{}") for c in result.value.cites]
    cited = tuple(dict.fromkeys(ctx.artifacts[c] for c in cites if c in ctx.artifacts))
    refs = tuple(dict.fromkeys(f.source_ref for f in ctx.facts if f.id in cites))
    reply = ChatReply(
        **envelope,
        parents=(message.id, *(c for c in cited if c != message.id)),
        text=table.render(result.value.answer)[:6000],
        cited_artifacts=tuple(c for c in cited if c != message.id),
        fact_refs=refs[:40],
    )
    return reply, result.usage


# --- Storage --------------------------------------------------------------------------


def submit(
    engine: Engine, text_value: str, thread_id: UUID | None, subject_id: UUID | None
) -> ChatMessage:
    if subject_id is not None:
        with engine.connect() as conn:
            get_artifact(conn, subject_id)  # raises ArtifactNotFoundError
    message = ChatMessage(
        produced_by="jon",
        runtime_ms=0,
        thread_id=thread_id or uuid4(),
        text=text_value.strip(),
        mode="explain" if subject_id else "ask",
        subject_id=subject_id,
        parents=(subject_id,) if subject_id else (),
    )
    with engine.begin() as conn:
        append_artifact(conn, message)
    return message


def pending(conn: Connection) -> list[ChatMessage]:
    ids = conn.execute(
        text(
            "SELECT m.id FROM artifacts m WHERE m.kind = 'chat_message' AND NOT EXISTS "
            "(SELECT 1 FROM artifacts r WHERE r.kind = 'chat_reply' "
            " AND r.payload->>'message_id' = m.id::text) ORDER BY m.created_at LIMIT :n"
        ),
        {"n": MAX_PENDING},
    ).scalars()
    messages = []
    for message_id in ids:
        message = get_artifact(conn, message_id)
        assert isinstance(message, ChatMessage)
        messages.append(message)
    return messages


async def answer_pending(
    engine: Engine, chat: OllamaChat, model: ChatModel, known_symbols: set[str], tz: ZoneInfo
) -> int:
    with engine.connect() as conn:
        messages = pending(conn)
    for message in messages:
        reply, _ = await answer(engine, chat, model, message, known_symbols, tz)
        with engine.begin() as conn:
            append_artifact(conn, reply)
    return len(messages)


def thread(conn: Connection, thread_id: UUID) -> list[ArtifactBase]:
    ids = conn.execute(
        text(
            "SELECT id FROM artifacts WHERE kind IN ('chat_message', 'chat_reply') "
            "AND payload->>'thread_id' = :t ORDER BY created_at"
        ),
        {"t": str(thread_id)},
    ).scalars()
    return [get_artifact(conn, i) for i in ids]
