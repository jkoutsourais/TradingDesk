"""Thesis debates: specialist views, bull, bear, bull rebuttal, judge.

Every model reply cites fact ids from a code-built FactBook and writes values only as
placeholders; code checks both, renders the text and stores citations as verified
claim ids or fact source refs. Labels (confidence, grades, conviction) become numbers in
the artifacts, never in the model.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.artifacts.analyst import (
    AnalystView,
    ConfidenceLabel,
    ConvictionLabel,
    DebateVerdict,
    Grade,
    Point,
    RiskReward,
    Rubric,
)
from desk.artifacts.store import append_artifact
from desk.artifacts.thesis import Thesis
from desk.config import ChatModel
from desk.desks.analyst.facts import FactBook, build_book, listing
from desk.desks.analyst.personas import Persona, select_personas
from desk.desks.idea.status import new_version
from desk.llm.client import OllamaChat, StructuredResult, Usage, request_failure
from desk.llm.facts import Fact
from desk.llm.prompts import Prompt, load_prompt

logger = logging.getLogger(__name__)

REDEBATE_AFTER = timedelta(days=7)
# A thesis sitting in its warning zone is re-argued at most once a day.
WARNING_REDEBATE_AFTER = timedelta(hours=20)
BEAR_PUSH_JON = (
    "This is Jon's own thesis: push hardest. Find the weakest assumption and test it; do "
    "not go easy because it is his."
)


# --- Model replies and checks --------------------------------------------------------------


class DraftPoint(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    cites: list[str] = Field(min_length=1, max_length=6)


class ViewDraft(BaseModel):
    stance: Literal["for", "against", "neutral"]
    points: list[DraftPoint] = Field(min_length=1, max_length=4)
    confidence: ConfidenceLabel


class ArgumentDraft(BaseModel):
    points: list[DraftPoint] = Field(min_length=1, max_length=4)


class JudgeDraft(BaseModel):
    evidence_quality: Grade
    rebuttal: Grade
    risk_reward: RiskReward
    verdict: Literal["pursue", "watch", "reject"]
    confidence: ConfidenceLabel
    conviction: ConvictionLabel
    reasons: list[DraftPoint] = Field(min_length=1, max_length=3)
    dissent: str = Field(min_length=1, max_length=800)


def point_problems(book: FactBook, points: list[DraftPoint], where: str) -> list[str]:
    table = book.table()
    known = {fact.id for fact in book.facts}
    problems = []
    for index, point in enumerate(points, start=1):
        unknown = [c for c in point.cites if c.strip("{}") not in known]
        if unknown:
            problems.append(f"{where} point {index}: unknown fact ids {unknown}")
        problems += [f"{where} point {index}: {p}" for p in table.violations(point.text)]
    return problems


def text_problems(book: FactBook, value: str, where: str) -> list[str]:
    return [f"{where}: {p}" for p in book.table().violations(value)]


def check_points(book: FactBook) -> Any:
    def check(reply: ViewDraft | ArgumentDraft) -> list[str]:
        return point_problems(book, reply.points, "")

    return check


def check_judge(book: FactBook) -> Any:
    def check(reply: JudgeDraft) -> list[str]:
        problems = point_problems(book, reply.reasons, "reason")
        problems += text_problems(book, reply.dissent, "dissent")
        cites = {c.strip("{}") for p in reply.reasons for c in p.cites}
        if not cites & set(book.claims):
            problems.append("at least one reason must cite a verified claim (claim_N)")
        return problems

    return check


def to_points(book: FactBook, points: list[DraftPoint]) -> tuple[Point, ...]:
    table = book.table()
    result = []
    for point in points:
        ids = [c.strip("{}") for c in point.cites]
        result.append(
            Point(
                text=table.render(point.text),
                claim_ids=tuple(dict.fromkeys(book.claims[i] for i in ids if i in book.claims)),
                fact_refs=tuple(dict.fromkeys(book.ref(i) for i in ids if i not in book.claims)),
            )
        )
    return tuple(result)


def transcript(points: list[DraftPoint]) -> str:
    """Points as the next speaker sees them: placeholders kept, citations listed."""
    return "\n".join(f"- {p.text} [cites: {', '.join(p.cites)}]" for p in points) or "- none"


def claim_parents(points: tuple[Point, ...]) -> list[UUID]:
    return [claim for point in points for claim in point.claim_ids]


# --- Running a debate ------------------------------------------------------------------------


@dataclass
class DebateOutcome:
    verdict: DebateVerdict | None = None
    views: list[AnalystView] = field(default_factory=list)
    thesis_update: Thesis | None = None
    usage: list[Usage] = field(default_factory=list)
    error: str | None = None


async def ask[T: BaseModel](
    chat: OllamaChat, model: ChatModel, prompt: Prompt, schema: type[T], check: Any
) -> StructuredResult[T]:
    try:
        return await chat.structured(model, prompt, schema, check=check)
    except httpx.HTTPError as exc:
        logger.warning("debate request failed: %s", request_failure(exc))
        return StructuredResult(None, Usage(model=model.model), 0, error=request_failure(exc))


def thesis_summary(thesis: Thesis) -> str:
    parts = [f"{thesis.primary_instrument} {thesis.direction}: {thesis.statement}"]
    if thesis.invalidation is not None:
        parts.append(
            f"Wrong on a daily close {thesis.invalidation.hard.operator} {{th_hard}}; "
            f"warning {thesis.invalidation.warning.operator} {{th_warning}}."
        )
    if thesis.horizon:
        parts.append(f"Horizon: {thesis.horizon}.")
    parts += [f"Driver: {d.statement}" for d in thesis.drivers]
    return " ".join(parts)


def thesis_facts(book: FactBook, thesis: Thesis) -> None:
    """The thesis's own levels, citable like any other fact."""
    if thesis.invalidation is None:
        return
    for fact_id, condition, label in (
        ("th_hard", thesis.invalidation.hard, "hard line"),
        ("th_warning", thesis.invalidation.warning, "warning level"),
    ):
        book.add(
            Fact(
                fact_id,
                condition.level,
                "USD",
                f"${condition.level:,}",
                f"{condition.instrument} thesis {label}",
                condition.level_ref,
            )
        )


async def debate_thesis(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    personas: list[Persona],
    thesis: Thesis,
    now: datetime,
    tz: ZoneInfo,
    claims_since: datetime,
    shift_id: UUID | None = None,
) -> DebateOutcome:
    outcome = DebateOutcome()
    sets = {name for persona in personas for name in persona.facts} | {"levels", "trend"}
    with engine.connect() as conn:
        book = build_book(
            conn, thesis.primary_instrument, sets, now, tz, claims_since, thesis.evidence
        )
    thesis_facts(book, thesis)
    if not book.claims:
        outcome.error = f"{thesis.primary_instrument}: no verified claims to debate"
        return outcome
    summary = thesis_summary(thesis)
    base = {"thesis": summary, "origin": thesis.origin}
    started = datetime.now(UTC)

    view_drafts: list[tuple[Persona, ViewDraft]] = []
    for persona in personas:
        own = book.subset(persona.facts)
        thesis_facts(own, thesis)
        prompt = load_prompt(
            "view",
            {
                **base,
                "title": persona.title,
                "persona_prompt": persona.prompt.strip(),
                "facts": listing(own),
            },
        )
        result = await ask(chat, model, prompt, ViewDraft, check_points(own))
        outcome.usage.append(result.usage)
        if result.value is None:
            logger.warning(
                "%s view on %s failed: %s", persona.name, thesis.primary_instrument, result.error
            )
            continue
        view_drafts.append((persona, result.value))
        points = to_points(own, result.value.points)
        outcome.views.append(
            AnalystView(
                produced_by=f"analyst.{persona.name}",
                runtime_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
                shift_id=shift_id,
                parents=(thesis.id, *dict.fromkeys(claim_parents(points))),
                model=model.model,
                prompt_version=f"{prompt.version}+{persona.version}",
                tokens_in=result.usage.tokens_in,
                tokens_out=result.usage.tokens_out,
                thesis_id=thesis.id,
                subject=thesis.primary_instrument,
                persona=persona.name,
                role="view",
                stance=result.value.stance,
                points=points,
                confidence_label=result.value.confidence,
            )
        )
    views_text = (
        "\n".join(
            f"{persona.title} ({draft.stance}, {draft.confidence}):\n{transcript(draft.points)}"
            for persona, draft in view_drafts
        )
        or "(no specialist views)"
    )
    facts_text = listing(book)
    shared = {**base, "views": views_text, "facts": facts_text}

    bull = await ask(chat, model, load_prompt("bull", shared), ArgumentDraft, check_points(book))
    outcome.usage.append(bull.usage)
    if bull.value is None:
        outcome.error = f"{thesis.primary_instrument}: bull failed: {bull.error}"
        return outcome
    push = BEAR_PUSH_JON if thesis.origin == "jon" else ""
    bear = await ask(
        chat,
        model,
        load_prompt("bear", {**shared, "bull": transcript(bull.value.points), "push_note": push}),
        ArgumentDraft,
        check_points(book),
    )
    outcome.usage.append(bear.usage)
    if bear.value is None:
        outcome.error = f"{thesis.primary_instrument}: bear failed: {bear.error}"
        return outcome
    rebuttal = await ask(
        chat,
        model,
        load_prompt(
            "rebuttal",
            {
                **shared,
                "bull": transcript(bull.value.points),
                "bear": transcript(bear.value.points),
            },
        ),
        ArgumentDraft,
        check_points(book),
    )
    outcome.usage.append(rebuttal.usage)
    if rebuttal.value is None:
        outcome.error = f"{thesis.primary_instrument}: rebuttal failed: {rebuttal.error}"
        return outcome
    judge_prompt = load_prompt(
        "judge",
        {
            **shared,
            "bull": transcript(bull.value.points),
            "bear": transcript(bear.value.points),
            "rebuttal": transcript(rebuttal.value.points),
        },
    )
    judged = await ask(chat, model, judge_prompt, JudgeDraft, check_judge(book))
    outcome.usage.append(judged.usage)
    if judged.value is None:
        outcome.error = f"{thesis.primary_instrument}: judge failed: {judged.error}"
        return outcome

    draft = judged.value
    sides = {
        "bull": to_points(book, bull.value.points),
        "bear": to_points(book, bear.value.points),
        "rebuttal": to_points(book, rebuttal.value.points),
        "reasons": to_points(book, draft.reasons),
    }
    cited = [c for points in sides.values() for c in claim_parents(points)]
    tokens_in = sum(u.tokens_in for u in outcome.usage)
    tokens_out = sum(u.tokens_out for u in outcome.usage)
    verdict = DebateVerdict(
        produced_by="analyst.judge",
        runtime_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        shift_id=shift_id,
        parents=(thesis.id, *(v.id for v in outcome.views), *dict.fromkeys(cited)),
        model=model.model,
        prompt_version=judge_prompt.version,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        thesis_id=thesis.id,
        subject=thesis.primary_instrument,
        view_ids=tuple(v.id for v in outcome.views),
        bull=sides["bull"],
        bear=sides["bear"],
        rebuttal=sides["rebuttal"],
        rubric=Rubric(
            evidence_quality=draft.evidence_quality,
            rebuttal=draft.rebuttal,
            risk_reward=draft.risk_reward,
        ),
        verdict=draft.verdict,
        confidence_label=draft.confidence,
        conviction_label=None if thesis.origin == "jon" else draft.conviction,
        reasons=sides["reasons"],
        dissent=book.table().render(draft.dissent),
    )
    outcome.verdict = verdict
    if verdict.conviction is not None and verdict.conviction != thesis.conviction:
        outcome.thesis_update = new_version(
            thesis,
            "analyst.judge",
            f"conviction set by the judge ({verdict.verdict})",
            shift_id,
            extra_parents=(verdict.id,),
            conviction=verdict.conviction,
        )
    with engine.begin() as conn:
        for view in outcome.views:
            append_artifact(conn, view)
        append_artifact(conn, verdict)
        if outcome.thesis_update is not None:
            append_artifact(conn, outcome.thesis_update)
    return outcome


# --- Which theses are due ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DueDebate:
    thesis: Thesis
    reason: str
    priority: int


def latest_verdicts(conn: Connection) -> dict[UUID, tuple[UUID, str, datetime]]:
    """Latest verdict per debated thesis version: thesis id -> (verdict id, verdict, when)."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT ON (payload->>'thesis_id') (payload->>'thesis_id')::uuid AS thesis, "
            "id, payload->>'verdict' AS verdict, created_at FROM artifacts "
            "WHERE kind = 'debate_verdict' AND status = 'ok' "
            "ORDER BY payload->>'thesis_id', created_at DESC"
        )
    ).all()
    return {row.thesis: (row.id, row.verdict, row.created_at) for row in rows}


def chain_ids(conn: Connection, thesis: Thesis) -> list[UUID]:
    """This version and every earlier one, newest first."""
    ids = [thesis.id]
    previous = thesis.previous_id
    while previous is not None and len(ids) < 100:
        ids.append(previous)
        previous = conn.execute(
            text("SELECT (payload->>'previous_id')::uuid FROM artifacts WHERE id = :id"),
            {"id": previous},
        ).scalar()
    return ids


def due_debates(
    conn: Connection, theses: list[Thesis], warned: set[UUID], now: datetime
) -> list[DueDebate]:
    verdicts = latest_verdicts(conn)
    due = []
    for thesis in theses:
        if thesis.state in ("draft", "closed", "invalidated") or not thesis.evidence:
            continue
        chain = chain_ids(conn, thesis)
        debated = next(
            ((vid, verdict[1], verdict[2]) for vid in chain if (verdict := verdicts.get(vid))), None
        )
        jon = thesis.origin == "jon"
        if debated is None:
            due.append(DueDebate(thesis, "new", 0 if jon else 1))
            continue
        debated_version, last_verdict, when = debated
        if thesis.id in warned and now - when >= WARNING_REDEBATE_AFTER:
            due.append(DueDebate(thesis, "warning zone", 2))
        elif debated_version != thesis.id and "verified claims" in (thesis.change_note or ""):
            due.append(DueDebate(thesis, "new evidence", 3))
        elif now - when >= REDEBATE_AFTER and (jon or last_verdict != "reject"):
            due.append(DueDebate(thesis, "weekly", 4))
    due.sort(key=lambda d: (d.priority, d.thesis.created_at))
    return due


def personas_for(
    personas: dict[str, Persona], thesis: Thesis, group: str | None, stock: bool
) -> list[Persona]:
    lane = None if thesis.origin == "jon" else thesis.origin
    return select_personas(personas, thesis.primary_instrument, lane, group, stock)
