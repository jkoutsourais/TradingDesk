"""Thesis intake: Jon's chat messages become a draft Thesis, then an active one.

1. Jon posts a message (`submit_message`); it is stored verbatim as an IntakeMessage.
2. The scheduler's triage tick drafts a Thesis from it with the small model, outside
   shifts (`process_pending`). Every number in the draft must appear in Jon's messages,
   and each level cites the message it came from.
3. Code lists the required fields still missing and the questions to ask. Jon answers
   with another message against the draft, which drafts a new version.
4. Jon confirms (`confirm_draft`); a complete draft becomes an active thesis with origin
   `jon`, researched and fact-checked at the next post-market shift.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Connection, Engine, text

from desk.artifacts.base import ArtifactStatus
from desk.artifacts.intake import IntakeMessage
from desk.artifacts.store import append_artifact, get_artifact
from desk.artifacts.thesis import Condition, Driver, Invalidation, Thesis
from desk.config import ChatModel
from desk.desks.idea.status import new_version
from desk.desks.idea.writer import DraftDriver, review_by
from desk.llm.client import OllamaChat, StructuredResult, Usage, request_failure
from desk.llm.prompts import load_prompt

logger = logging.getLogger(__name__)

NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
MAX_PENDING = 5
REQUIRED = ("invalidation", "horizon", "conviction")
QUESTIONS = {
    "invalidation": (
        "At what price is the idea wrong (the hard line, checked on the daily close), and "
        "where should the desk warn you first?"
    ),
    "invalidation_order": (
        "The warning level must sit between the current price and the hard line; which "
        "levels did you mean?"
    ),
    "horizon": "Over what horizon: days, weeks or months?",
    "conviction": "How confident are you, from 1 to 5?",
}


def number_tokens(value: str) -> set[str]:
    return {token.replace(",", "") for token in NUMBER.findall(value)}


def parse_level(value: str) -> Decimal | None:
    """The one number in a level text: "28", "$29.50" and "close below 28" all parse."""
    tokens = number_tokens(value)
    if len(tokens) != 1:
        return None
    try:
        level = Decimal(tokens.pop())
    except InvalidOperation:
        return None
    return level if level > 0 else None


def date_mentioned(day: date, text_value: str) -> bool:
    """The day number and the month (by number or name) both appear in Jon's text."""
    tokens = number_tokens(text_value)
    lowered = text_value.casefold()
    month_named = day.strftime("%B").casefold()[:3] in lowered
    month_numbered = str(day.month) in tokens or f"{day.month:02d}" in tokens
    return str(day.day) in tokens and (month_named or month_numbered)


class IntakeReply(BaseModel):
    statement: str = Field(min_length=1, max_length=400)
    instruments: list[str] = Field(min_length=1, max_length=4)
    direction: Literal["long", "short", "relative"]
    drivers: list[DraftDriver] = Field(min_length=1, max_length=4)
    hard_level: str | None = Field(
        default=None, description="The price only, as Jon wrote it, e.g. '28'"
    )
    warning_level: str | None = Field(
        default=None, description="The price only, as Jon wrote it, e.g. '29.50'"
    )
    horizon: Literal["days", "weeks", "months"] | None = None
    time_limit: str | None = None
    conviction: int | None = Field(default=None, ge=1, le=5)
    entry_conditions: list[str] = Field(default_factory=list, max_length=3)


def check_reply(messages_text: str, futures: frozenset[str] = frozenset()) -> Any:
    """`futures` lists the futures symbols the desk tracks; others must not take a slash."""
    allowed = number_tokens(messages_text)

    def check(reply: IntakeReply) -> list[str]:
        problems = []
        for symbol in reply.instruments:
            if futures and symbol.startswith("/") and symbol not in futures:
                problems.append(
                    f"{symbol} is not a tracked future; write stocks and ETFs without a slash"
                )
        for name in ("hard_level", "warning_level"):
            value = getattr(reply, name)
            if value is None:
                continue
            level = parse_level(value)
            if level is None:
                problems.append(f"{name} {value!r} must be one price, for example '28'")
            elif not number_tokens(value) <= allowed:
                problems.append(f"{name} {value!r} is not in Jon's messages")
        if reply.conviction is not None and str(reply.conviction) not in allowed:
            problems.append("conviction must be a rating Jon gave; otherwise leave it null")
        if reply.time_limit is not None:
            try:
                deadline = date.fromisoformat(reply.time_limit)
            except ValueError:
                problems.append("time_limit must be YYYY-MM-DD")
            else:
                if not date_mentioned(deadline, messages_text):
                    problems.append("time_limit must be a date Jon gave; otherwise leave it null")
        texts = [reply.statement, *(d.statement for d in reply.drivers), *reply.entry_conditions]
        for value in texts:
            extra = number_tokens(value) - allowed
            if extra:
                problems.append(f"numbers {sorted(extra)} are not in Jon's messages: {value!r}")
        return problems

    return check


# --- Drafts -------------------------------------------------------------------------------


@dataclass
class DraftReview:
    thesis: Thesis
    missing: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def _source_message(token: str, messages: list[IntakeMessage]) -> UUID:
    for message in reversed(messages):
        if token in number_tokens(message.text):
            return message.id
    return messages[-1].id


def build_draft(
    reply: IntakeReply,
    messages: list[IntakeMessage],
    today: date,
    usage: Usage,
    prompt_version: str,
    runtime_ms: int,
    previous: Thesis | None,
) -> DraftReview:
    operator: Literal["below", "above"] = "above" if reply.direction == "short" else "below"
    primary = reply.instruments[0].strip().upper()
    problems: list[str] = []

    def condition(value: str | None, measure: Literal["intraday_price", "daily_close"]) -> Any:
        level = parse_level(value) if value else None
        if level is None:
            return None
        token = next(iter(number_tokens(value or "")), "")
        return Condition(
            instrument=primary,
            measure=measure,
            operator=operator,
            level=level,
            level_ref=f"intake_message:{_source_message(token, messages)}",
        )

    hard = condition(reply.hard_level, "daily_close")
    warning = condition(reply.warning_level, "intraday_price")
    deadline = date.fromisoformat(reply.time_limit) if reply.time_limit else None
    invalidation = None
    if hard is not None and warning is not None:
        try:
            invalidation = Invalidation(warning=warning, hard=hard, time_limit=deadline)
        except ValidationError:
            problems.append("invalidation_order")
    due = review_by(reply.horizon, today) if reply.horizon else deadline
    parents = [m.id for m in messages]
    if previous is not None:
        parents.insert(0, previous.id)
    thesis = Thesis(
        produced_by="front_office.intake",
        runtime_ms=runtime_ms,
        parents=tuple(dict.fromkeys(parents)),
        model=usage.model,
        prompt_version=prompt_version,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        statement=reply.statement,
        origin="jon",
        instruments=tuple(dict.fromkeys(s.strip().upper() for s in reply.instruments)),
        direction=reply.direction,
        horizon=reply.horizon,
        review_by=due,
        drivers=tuple(
            Driver(statement=d.statement, metric=d.metric, source=d.source) for d in reply.drivers
        ),
        invalidation=invalidation,
        entry_conditions=tuple(reply.entry_conditions),
        conviction=reply.conviction,
        state="draft",
        previous_id=previous.id if previous else None,
        change_note="drafted from Jon's messages",
    )
    return review(thesis, problems)


def review(thesis: Thesis, problems: list[str] | None = None) -> DraftReview:
    """Required fields still missing, with the questions code asks Jon for them."""
    missing = list(problems or [])
    if thesis.invalidation is None and "invalidation_order" not in missing:
        missing.append("invalidation")
    missing += [name for name in REQUIRED[1:] if getattr(thesis, name) is None]
    return DraftReview(thesis, missing, [QUESTIONS[name] for name in missing])


# --- Storage and running ------------------------------------------------------------------


def submit_message(engine: Engine, text_value: str, draft_id: UUID | None = None) -> IntakeMessage:
    parents: tuple[UUID, ...] = ()
    if draft_id is not None:
        with engine.connect() as conn:
            if not isinstance(get_artifact(conn, draft_id), Thesis):
                raise ValueError(f"{draft_id} is not a thesis draft")
        parents = (draft_id,)
    message = IntakeMessage(
        produced_by="jon", runtime_ms=0, text=text_value.strip(), draft_id=draft_id, parents=parents
    )
    with engine.begin() as conn:
        append_artifact(conn, message)
    return message


def pending_messages(conn: Connection) -> list[IntakeMessage]:
    ids = conn.execute(
        text(
            "SELECT m.id FROM artifacts m WHERE m.kind = 'intake_message' "
            "AND NOT EXISTS (SELECT 1 FROM artifact_parents p JOIN artifacts t "
            "  ON t.id = p.child_id WHERE p.parent_id = m.id AND t.kind = 'thesis') "
            "ORDER BY m.created_at LIMIT :n"
        ),
        {"n": MAX_PENDING},
    ).scalars()
    messages = []
    for message_id in ids:
        message = get_artifact(conn, message_id)
        assert isinstance(message, IntakeMessage)
        messages.append(message)
    return messages


def conversation(
    conn: Connection, message: IntakeMessage
) -> tuple[list[IntakeMessage], Thesis | None]:
    """All messages behind the draft this message answers, plus that draft."""
    if message.draft_id is None:
        return [message], None
    previous = get_artifact(conn, message.draft_id)
    assert isinstance(previous, Thesis)
    earlier = [get_artifact(conn, pid) for pid in previous.parents]
    messages = [m for m in earlier if isinstance(m, IntakeMessage)]
    return [*messages, message], previous


@dataclass
class IntakeOutcome:
    drafted: int = 0
    failed: int = 0
    usage: Usage | None = None


async def process_pending(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    today: date,
    futures: frozenset[str] = frozenset(),
) -> IntakeOutcome:
    outcome = IntakeOutcome()
    with engine.connect() as conn:
        pending = [(m, *conversation(conn, m)) for m in pending_messages(conn)]
    for message, messages, previous in pending:
        listing = "\n\n".join(f"[{i}] {m.text}" for i, m in enumerate(messages, start=1))
        prompt = load_prompt("intake", {"messages": listing})
        joined = "\n".join(m.text for m in messages)
        started = datetime.now(UTC)
        try:
            result = await chat.structured(
                model, prompt, IntakeReply, check=check_reply(joined, futures)
            )
        except httpx.HTTPError as exc:
            logger.warning("intake request failed: %s", request_failure(exc))
            result = StructuredResult(None, Usage(model=model.model), 0, error=request_failure(exc))
        outcome.usage = result.usage
        runtime_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        error = result.error or "intake reply failed"
        thesis: Thesis | None = None
        if result.value is not None:
            try:
                thesis = build_draft(
                    result.value,
                    messages,
                    today,
                    result.usage,
                    prompt.version,
                    runtime_ms,
                    previous,
                ).thesis
            except ValidationError as exc:
                # The reply parsed but does not make a valid thesis (e.g. a relative idea
                # with one instrument); Jon sees the reason and can restate it.
                error = f"draft is not a valid thesis: {exc.errors()[0]['msg']}"
        if thesis is None:
            thesis = _failed_draft(
                message, previous, error, result.usage, prompt.version, runtime_ms
            )
            outcome.failed += 1
        else:
            outcome.drafted += 1
        with engine.begin() as conn:
            append_artifact(conn, thesis)
    return outcome


def _failed_draft(
    message: IntakeMessage,
    previous: Thesis | None,
    error: str,
    usage: Usage,
    prompt_version: str,
    runtime_ms: int,
) -> Thesis:
    # Records that the message was read; its fields carry Jon's text, not a judgment.
    parents = (message.id,) if previous is None else (previous.id, message.id)
    return Thesis(
        status=ArtifactStatus.FAILED,
        error=error,
        produced_by="front_office.intake",
        runtime_ms=runtime_ms,
        parents=parents,
        model=usage.model,
        prompt_version=prompt_version,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        statement=message.text[:400],
        origin="jon",
        instruments=("UNKNOWN",),
        direction="long",
        drivers=(Driver(statement=message.text[:300], metric="unparsed", source="yahoo"),),
        state="draft",
    )


def draft_for_message(conn: Connection, message_id: UUID) -> Thesis | None:
    thesis_id = conn.execute(
        text(
            "SELECT t.id FROM artifact_parents p JOIN artifacts t ON t.id = p.child_id "
            "WHERE p.parent_id = :m AND t.kind = 'thesis' ORDER BY t.created_at DESC LIMIT 1"
        ),
        {"m": message_id},
    ).scalar()
    if thesis_id is None:
        return None
    thesis = get_artifact(conn, thesis_id)
    assert isinstance(thesis, Thesis)
    return thesis


class IncompleteDraftError(ValueError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"draft is missing {', '.join(missing)}")
        self.missing = missing


def confirm_draft(engine: Engine, draft_id: UUID) -> Thesis:
    with engine.connect() as conn:
        draft = get_artifact(conn, draft_id)
    if not isinstance(draft, Thesis) or draft.origin != "jon" or draft.state != "draft":
        raise ValueError(f"{draft_id} is not one of Jon's thesis drafts")
    if draft.status is not ArtifactStatus.OK:
        raise ValueError(f"{draft_id} failed to draft: {draft.error}")
    checked = review(draft)
    if checked.missing:
        raise IncompleteDraftError(checked.missing)
    active = new_version(draft, "front_office.intake", "confirmed by Jon", state="active")
    with engine.begin() as conn:
        append_artifact(conn, active)
    return active
