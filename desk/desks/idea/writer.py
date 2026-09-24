"""Thesis writer: the deep model turns a selected candidate into a Thesis.

The model chooses direction, horizon, drivers, evidence and invalidation levels by id;
code supplies every value. Levels come from `levels.level_menu`, evidence from verified
claims on the instrument, catalysts from the release calendar. Conviction and the
review-by date are set by code until the analyst desk's judge takes conviction over.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.artifacts.base import ArtifactStatus
from desk.artifacts.idea import LaneCandidate
from desk.artifacts.store import append_artifact
from desk.artifacts.thesis import Catalyst, Condition, Driver, Invalidation, Thesis
from desk.config import ChatModel
from desk.desks.idea.levels import Level, level_menu
from desk.llm.client import OllamaChat, StructuredResult, Usage, request_failure
from desk.llm.facts import Fact, FactTable
from desk.llm.prompts import load_prompt
from desk.watch.scan import load_daily_bars

logger = logging.getLogger(__name__)

CALENDAR_DAYS_AHEAD = 60
MAX_EVENTS = 12
MAX_CLAIMS = 12
REVIEW_DAYS = {"days": 10, "weeks": 35, "months": 100}
Feed = Literal[
    "yahoo",
    "tastytrade",
    "fred",
    "eia",
    "cftc_cot",
    "edgar",
    "finnhub",
    "gridstatus",
    "federal_register",
    "truth_social",
    "fed",
]


def conviction_from_score(score: float) -> int:
    """1 to 4 from the lane score; 5 is left to the judge once debates exist."""
    if score >= 0.8:
        return 4
    if score >= 0.6:
        return 3
    if score >= 0.4:
        return 2
    return 1


def review_by(horizon: str, today: date) -> date:
    return today + timedelta(days=REVIEW_DAYS[horizon])


# --- Context ------------------------------------------------------------------------------


@dataclass
class WriterContext:
    candidate: LaneCandidate
    last_close: Decimal | None = None
    claims: dict[str, UUID] = field(default_factory=dict)  # claim_N -> VerifiedClaim id
    levels: dict[str, Level] = field(default_factory=dict)
    events: dict[str, Catalyst] = field(default_factory=dict)
    dossier_id: UUID | None = None
    facts: list[Fact] = field(default_factory=list)

    def table(self) -> FactTable:
        return FactTable(self.facts)


def accepted_claims(conn: Connection, symbol: str, since: datetime) -> list[Any]:
    """Verified or corrected claims on `symbol`, strongest support first."""
    return list(
        conn.execute(
            text(
                "SELECT v.id, c.payload->>'statement' AS statement, "
                "v.payload->>'verdict' AS verdict, v.payload->'recomputed' AS recomputed "
                "FROM artifacts v JOIN artifacts c ON c.id = (v.payload->>'claim_id')::uuid "
                "WHERE v.kind = 'verified_claim' AND v.status = 'ok' "
                "AND v.payload->>'verdict' IN ('verified', 'corrected') "
                "AND v.created_at >= :since AND c.payload->>'subject' = :symbol "
                "ORDER BY (v.payload->>'entailment')::float DESC NULLS LAST, v.created_at DESC "
                "LIMIT :n"
            ),
            {"since": since, "symbol": symbol, "n": MAX_CLAIMS},
        ).all()
    )


def claim_display(statement: str, verdict: str, recomputed: list[dict[str, Any]]) -> str:
    if verdict != "corrected":
        return statement
    moves = ", ".join(f"{r['stated']} was {r['value']}%" for r in recomputed)
    return f"{statement} [recomputed from stored closes: {moves}]"


def gather_context(
    conn: Connection, candidate: LaneCandidate, now: datetime, tz: ZoneInfo, claims_since: datetime
) -> WriterContext:
    context = WriterContext(candidate)
    symbol = candidate.instrument
    for index, row in enumerate(accepted_claims(conn, symbol, claims_since), start=1):
        fact_id = f"claim_{index}"
        context.claims[fact_id] = row.id
        display = claim_display(row.statement, row.verdict, row.recomputed or [])
        context.facts.append(
            Fact(
                fact_id,
                display,
                "",
                display,
                f"verified claim on {symbol}",
                f"verified_claim:{row.id}",
            )
        )

    today = now.astimezone(tz).date()
    bars = load_daily_bars(conn, [symbol], today + timedelta(days=1), tz).get(symbol, [])
    for level in level_menu(symbol, bars):
        context.levels[level.id] = level
        if level.name == "last_close":
            context.last_close = level.value
        context.facts.append(
            Fact(level.id, level.value, "USD", f"${level.value:,}", level.label, level.ref)
        )

    events = conn.execute(
        text(
            "SELECT event_key, name, at FROM calendar_events WHERE at >= :start AND at < :end "
            "ORDER BY at LIMIT :n"
        ),
        {"start": now, "end": now + timedelta(days=CALENDAR_DAYS_AHEAD), "n": MAX_EVENTS},
    ).all()
    for index, row in enumerate(events, start=1):
        fact_id = f"cal_{index}"
        day = row.at.astimezone(tz).date()
        context.events[fact_id] = Catalyst(
            name=row.name, on=day, ref=f"calendar_events:{row.event_key}"
        )
        context.facts.append(
            Fact(
                fact_id,
                row.name,
                "",
                f"{day:%b} {day.day} {row.name}",
                "scheduled release",
                f"calendar_events:{row.event_key}",
            )
        )
    context.dossier_id = conn.execute(
        text(
            "SELECT id FROM artifacts WHERE kind = 'dossier' AND status = 'ok' "
            "AND payload->>'subject' = :s ORDER BY created_at DESC LIMIT 1"
        ),
        {"s": symbol},
    ).scalar()
    return context


# --- Model output and checks ---------------------------------------------------------------


class DraftDriver(BaseModel):
    statement: str = Field(min_length=1, max_length=300)
    metric: str = Field(min_length=1, max_length=80)
    source: Feed


class ThesisDraft(BaseModel):
    statement: str = Field(min_length=1, max_length=400)
    direction: Literal["long", "short"]
    horizon: Literal["days", "weeks", "months"]
    drivers: list[DraftDriver] = Field(min_length=1, max_length=4)
    evidence_ids: list[str] = Field(min_length=1)
    catalyst_ids: list[str] = Field(default_factory=list)
    warning_level_id: str
    hard_level_id: str
    entry_conditions: list[str] = Field(default_factory=list, max_length=3)


def check_draft(context: WriterContext) -> Any:
    table = context.table()

    def check(draft: ThesisDraft) -> list[str]:
        problems = [
            f"unknown evidence id {e}" for e in draft.evidence_ids if e not in context.claims
        ]
        problems += [
            f"unknown catalyst id {c}" for c in draft.catalyst_ids if c not in context.events
        ]
        warning = context.levels.get(draft.warning_level_id)
        hard = context.levels.get(draft.hard_level_id)
        if warning is None or hard is None:
            problems.append("warning_level_id and hard_level_id must be level ids (lvl_N)")
        elif context.last_close is not None:
            last = context.last_close
            if draft.direction == "long" and not (hard.value < warning.value < last):
                problems.append(
                    "for a long, the hard level must be below the warning level, and both "
                    "below the last close"
                )
            if draft.direction == "short" and not (last < warning.value < hard.value):
                problems.append(
                    "for a short, the hard level must be above the warning level, and both "
                    "above the last close"
                )
        texts = [draft.statement, *(d.statement for d in draft.drivers), *draft.entry_conditions]
        for value in texts:
            problems += table.violations(value)
        return problems

    return check


def build_thesis(
    draft: ThesisDraft,
    context: WriterContext,
    today: date,
    usage: Usage,
    prompt_version: str,
    runtime_ms: int,
    shift_id: UUID | None,
) -> Thesis:
    table = context.table()
    candidate = context.candidate
    operator: Literal["below", "above"] = "below" if draft.direction == "long" else "above"
    warning = context.levels[draft.warning_level_id]
    hard = context.levels[draft.hard_level_id]
    evidence = tuple(dict.fromkeys(context.claims[e] for e in draft.evidence_ids))
    due = review_by(draft.horizon, today)
    origins = [candidate.id, *([context.dossier_id] if context.dossier_id else [])]
    return Thesis(
        produced_by="idea.writer",
        runtime_ms=runtime_ms,
        shift_id=shift_id,
        parents=(*origins, *evidence),
        model=usage.model,
        prompt_version=prompt_version,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        statement=table.render(draft.statement),
        origin=candidate.lane,
        instruments=(candidate.instrument,),
        direction=draft.direction,
        horizon=draft.horizon,
        review_by=due,
        drivers=tuple(
            Driver(statement=table.render(d.statement), metric=d.metric, source=d.source)
            for d in draft.drivers
        ),
        catalysts=tuple(context.events[c] for c in dict.fromkeys(draft.catalyst_ids)),
        evidence=evidence,
        invalidation=Invalidation(
            warning=Condition(
                instrument=candidate.instrument,
                measure="intraday_price",
                operator=operator,
                level=warning.value,
                level_ref=warning.ref,
            ),
            hard=Condition(
                instrument=candidate.instrument,
                measure="daily_close",
                operator=operator,
                level=hard.value,
                level_ref=hard.ref,
            ),
            time_limit=due,
        ),
        entry_conditions=tuple(table.render(e) for e in draft.entry_conditions),
        conviction=conviction_from_score(candidate.score),
        state="active",
    )


# --- Runner -------------------------------------------------------------------------------


@dataclass
class WriterOutcome:
    theses: list[Thesis] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)


async def write_thesis(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    candidate: LaneCandidate,
    now: datetime,
    tz: ZoneInfo,
    claims_since: datetime,
    shift_id: UUID | None = None,
) -> tuple[Thesis | None, Usage, str | None]:
    with engine.connect() as conn:
        context = gather_context(conn, candidate, now, tz, claims_since)
    if not context.claims:
        return None, Usage(model=model.model), f"{candidate.instrument}: no verified claims"
    if context.last_close is None:
        return None, Usage(model=model.model), f"{candidate.instrument}: no stored daily closes"
    prompt = load_prompt(
        "thesis",
        {
            "instrument": candidate.instrument,
            "lane": candidate.lane,
            "score": f"{candidate.score:.2f}",
            "driver": candidate.driver,
            "facts": context.table().prompt_listing(),
        },
    )
    started = datetime.now(UTC)
    try:
        result = await chat.structured(model, prompt, ThesisDraft, check=check_draft(context))
    except httpx.HTTPError as exc:
        logger.warning(
            "thesis request for %s failed: %s", candidate.instrument, request_failure(exc)
        )
        result = StructuredResult(None, Usage(model=model.model), 0, error=request_failure(exc))
    runtime_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    if result.value is None:
        error = result.error or "thesis reply failed"
        failed = _failed_thesis(
            candidate, context, error, result.usage, prompt.version, runtime_ms, shift_id
        )
        with engine.begin() as conn:
            append_artifact(conn, failed)
        return None, result.usage, f"{candidate.instrument}: {error}"
    thesis = build_thesis(
        result.value,
        context,
        now.astimezone(tz).date(),
        result.usage,
        prompt.version,
        runtime_ms,
        shift_id,
    )
    with engine.begin() as conn:
        append_artifact(conn, thesis)
    return thesis, result.usage, None


def _failed_thesis(
    candidate: LaneCandidate,
    context: WriterContext,
    error: str,
    usage: Usage,
    prompt_version: str,
    runtime_ms: int,
    shift_id: UUID | None,
) -> Thesis:
    # A failed thesis records the candidate and its evidence so the run is visible; its
    # direction and driver are the schema's minimum, not a judgment, and it stays a draft.
    evidence = tuple(context.claims.values())
    return Thesis(
        status=ArtifactStatus.FAILED,
        error=error,
        produced_by="idea.writer",
        runtime_ms=runtime_ms,
        shift_id=shift_id,
        parents=(candidate.id, *evidence),
        model=usage.model,
        prompt_version=prompt_version,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        statement=candidate.driver[:400],
        origin=candidate.lane,
        instruments=(candidate.instrument,),
        direction="long",
        drivers=(Driver(statement=candidate.driver[:300], metric="lane score", source="yahoo"),),
        evidence=evidence,
        state="draft",
    )


async def write_theses(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    candidates: list[LaneCandidate],
    now: datetime,
    tz: ZoneInfo,
    claims_since: datetime,
    shift_id: UUID | None = None,
) -> WriterOutcome:
    outcome = WriterOutcome()
    for candidate in candidates:
        thesis, usage, error = await write_thesis(
            engine, chat, model, candidate, now, tz, claims_since, shift_id
        )
        outcome.usage.append(usage)
        if error:
            outcome.failed.append(error)
        if thesis is not None:
            outcome.theses.append(thesis)
    return outcome
