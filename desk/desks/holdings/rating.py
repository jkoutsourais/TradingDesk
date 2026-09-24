"""Holdings desk: a daily Buy/Add, Hold, Trim or Sell rating for every holding.

1 Refresh    facts from research, prices and the position (code)
2 Debate     the holding's specialist argues keep, the bear argues exit (deep model)
3 Rate       the judge rates with confidence and cited reasons (deep model)
4 Risk check code flags (concentration, levered-fund days, option expiry) can force a
             more defensive rating; the judge's own rating is kept alongside
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.artifacts.analyst import (
    RATING_SEVERITY,
    AnalystView,
    ConfidenceLabel,
    HoldingRating,
    Point,
    Rating,
    RiskFlag,
)
from desk.artifacts.base import ArtifactStatus
from desk.artifacts.store import append_artifact
from desk.artifacts.thesis import Thesis
from desk.config import ChatModel
from desk.desks.analyst.debate import (
    ArgumentDraft,
    DraftPoint,
    ask,
    check_points,
    claim_parents,
    point_problems,
    text_problems,
    to_points,
    transcript,
)
from desk.desks.analyst.facts import FactBook, build_book, listing
from desk.desks.analyst.personas import Persona
from desk.desks.holdings.flags import Holding, describe
from desk.llm.client import OllamaChat, Usage
from desk.llm.facts import Fact
from desk.llm.prompts import load_prompt

logger = logging.getLogger(__name__)


class RatingDraft(BaseModel):
    rating: Rating
    confidence: ConfidenceLabel
    reasons: list[DraftPoint] = Field(min_length=1, max_length=3)
    what_would_change_it: str = Field(min_length=1, max_length=400)
    suggested_action: str = Field(min_length=1, max_length=400)


def check_rating(book: FactBook) -> Any:
    def check(reply: RatingDraft) -> list[str]:
        problems = point_problems(book, reply.reasons, "reason")
        problems += text_problems(book, reply.what_would_change_it, "what_would_change_it")
        problems += text_problems(book, reply.suggested_action, "suggested_action")
        return problems

    return check


def apply_flags(judged: Rating, flags: list[RiskFlag]) -> tuple[Rating, Rating | None]:
    """The final rating and, when a flag overrode it, the judge's own rating."""
    forced = [f.forces for f in flags if f.forces is not None]
    if not forced:
        return judged, None
    floor = max(forced, key=lambda r: RATING_SEVERITY[r])
    if RATING_SEVERITY[judged] >= RATING_SEVERITY[floor]:
        return judged, None
    return floor, judged


def position_facts(book: FactBook, holding: Holding, today: date) -> str:
    """Adds the position's own numbers as facts; returns the position line for prompts."""
    numbers = describe(holding, today)
    ref = f"account_snapshots:latest:{holding.symbol}"
    parts = [f"Held in {len(holding.accounts)} account(s)."]
    if numbers["share_pct"] is not None:
        value = numbers["share_pct"]
        book.add(
            Fact(
                "pos_share",
                Decimal(f"{value:.2f}"),
                "%",
                f"{value:.1f}%",
                "position share of its account",
                ref,
            )
        )
        parts.append("Share of account: {pos_share}.")
    if numbers["pnl_pct"] is not None:
        value = numbers["pnl_pct"]
        book.add(
            Fact(
                "pos_pnl",
                Decimal(f"{value:.2f}"),
                "%",
                f"{value:+.1f}%",
                "position gain or loss on cost",
                ref,
            )
        )
        parts.append("Gain or loss on cost: {pos_pnl}.")
    if numbers["days"] is not None:
        value = numbers["days"]
        book.add(
            Fact(
                "pos_days",
                Decimal(value),
                "days",
                f"at least {value} days",
                "days held (from broker snapshots)",
                ref,
            )
        )
        parts.append("Held: {pos_days}.")
    return " ".join(parts)


def previous_rating(conn: Connection, symbol: str) -> Rating | None:
    value = conn.execute(
        text(
            "SELECT payload->>'rating' FROM artifacts WHERE kind = 'holding_rating' "
            "AND status = 'ok' AND payload->>'subject' = :s ORDER BY created_at DESC LIMIT 1"
        ),
        {"s": symbol},
    ).scalar()
    return value if value in RATING_SEVERITY else None


@dataclass
class RatingOutcome:
    rating: HoldingRating | None = None
    views: list[AnalystView] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    error: str | None = None


def _view(
    role: Literal["keep", "exit"],
    persona: str,
    stance: Literal["for", "against"],
    points: tuple[Point, ...],
    symbol: str,
    thesis: Thesis | None,
    model: ChatModel,
    version: str,
    usage: Usage,
    shift_id: UUID | None,
    confidence: ConfidenceLabel,
) -> AnalystView:
    thesis_parents = (thesis.id,) if thesis else ()
    return AnalystView(
        produced_by=f"holdings.{persona}",
        runtime_ms=usage.total_ms,
        shift_id=shift_id,
        parents=(*thesis_parents, *dict.fromkeys(claim_parents(points))),
        model=model.model,
        prompt_version=version,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        thesis_id=thesis.id if thesis else None,
        subject=symbol,
        persona=persona,
        role=role,
        stance=stance,
        points=points,
        confidence_label=confidence,
    )


async def rate_holding(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    keeper: Persona,
    holding: Holding,
    flags: list[RiskFlag],
    thesis: Thesis | None,
    now: datetime,
    tz: ZoneInfo,
    claims_since: datetime,
    shift_id: UUID | None = None,
) -> RatingOutcome:
    outcome = RatingOutcome()
    symbol = holding.symbol
    today = now.astimezone(tz).date()
    evidence = thesis.evidence if thesis else ()
    with engine.connect() as conn:
        book = build_book(
            conn, symbol, {*keeper.facts, "levels", "trend"}, now, tz, claims_since, evidence
        )
        previous = previous_rating(conn, symbol)
    position = position_facts(book, holding, today)
    for index, flag in enumerate(flags, start=1):
        book.add(
            Fact(
                f"flag_{index}", flag.detail, "", flag.detail, "risk flag", f"holdings:{flag.code}"
            )
        )
    if flags:
        position += (
            " Risk flags: " + ", ".join(f"{{flag_{i}}}" for i in range(1, len(flags) + 1)) + "."
        )
    facts = listing(book)
    started = datetime.now(UTC)

    keep_prompt = load_prompt(
        "keep",
        {
            "title": keeper.title,
            "persona_prompt": keeper.prompt.strip(),
            "subject": symbol,
            "position": position,
            "facts": facts,
        },
    )
    keep = await ask(
        chat,
        model,
        keep_prompt,
        ArgumentDraft,
        check_points(book),
    )
    outcome.usage.append(keep.usage)
    exit_ = None
    exit_version = "exit"
    if keep.value is not None:
        exit_prompt = load_prompt(
            "exit",
            {
                "subject": symbol,
                "position": position,
                "keep": transcript(keep.value.points),
                "facts": facts,
            },
        )
        exit_version = exit_prompt.version
        exit_ = await ask(
            chat,
            model,
            exit_prompt,
            ArgumentDraft,
            check_points(book),
        )
        outcome.usage.append(exit_.usage)
    judged = None
    rating_prompt = load_prompt(
        "rating",
        {
            "subject": symbol,
            "position": position,
            "previous": previous or "none",
            "keep": transcript(keep.value.points) if keep.value else "- none",
            "exit": transcript(exit_.value.points) if exit_ and exit_.value else "- none",
            "facts": facts,
        },
    )
    if keep.value is not None and exit_ is not None and exit_.value is not None:
        judged = await ask(chat, model, rating_prompt, RatingDraft, check_rating(book))
        outcome.usage.append(judged.usage)

    tokens_in = sum(u.tokens_in for u in outcome.usage)
    tokens_out = sum(u.tokens_out for u in outcome.usage)
    runtime_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    thesis_parents = (thesis.id,) if thesis else ()
    if judged is None or judged.value is None:
        failures = [r.error for r in (keep, exit_, judged) if r is not None and r.error]
        outcome.error = f"{symbol}: " + ("; ".join(failures) or "rating failed")
        rating, overridden = apply_flags(previous or "hold", flags)
        outcome.rating = HoldingRating(
            status=ArtifactStatus.FAILED,
            error=outcome.error[:1000],
            produced_by="holdings.judge",
            runtime_ms=runtime_ms,
            shift_id=shift_id,
            parents=thesis_parents,
            model=model.model,
            prompt_version=rating_prompt.version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            subject=symbol,
            account_refs=tuple(holding.accounts),
            thesis_id=thesis.id if thesis else None,
            rating=rating,
            previous_rating=previous,
            judged_rating=overridden,
            confidence_label="low",
            reasons=(
                Point(
                    text="No rating from the judge; the previous rating stands.",
                    fact_refs=("holdings:rating_failed",),
                ),
            ),
            what_would_change_it="A successful rating run.",
            risk_flags=tuple(flags),
            suggested_action="Review manually.",
        )
    else:
        draft = judged.value
        rating, overridden = apply_flags(draft.rating, flags)
        table = book.table()
        action = table.render(draft.suggested_action)
        if overridden is not None:
            forcing = next(f for f in flags if f.forces == rating)
            label = rating.replace("_", "/").title()
            action = f"{label} forced by risk rule ({forcing.detail}). {action}"
        assert keep.value is not None and exit_ is not None and exit_.value is not None
        keep_points = to_points(book, keep.value.points)
        exit_points = to_points(book, exit_.value.points)
        outcome.views = [
            _view(
                "keep",
                keeper.name,
                "for",
                keep_points,
                symbol,
                thesis,
                model,
                f"{keep_prompt.version}+{keeper.version}",
                keep.usage,
                shift_id,
                "medium",
            ),
            _view(
                "exit",
                "bear",
                "against",
                exit_points,
                symbol,
                thesis,
                model,
                exit_version,
                exit_.usage,
                shift_id,
                "medium",
            ),
        ]
        reasons = to_points(book, draft.reasons)
        outcome.rating = HoldingRating(
            produced_by="holdings.judge",
            runtime_ms=runtime_ms,
            shift_id=shift_id,
            parents=(
                *thesis_parents,
                *(v.id for v in outcome.views),
                *dict.fromkeys(claim_parents(reasons)),
            ),
            model=model.model,
            prompt_version=rating_prompt.version,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            subject=symbol,
            account_refs=tuple(holding.accounts),
            thesis_id=thesis.id if thesis else None,
            rating=rating,
            previous_rating=previous,
            judged_rating=overridden,
            confidence_label=draft.confidence,
            reasons=reasons,
            what_would_change_it=table.render(draft.what_would_change_it),
            risk_flags=tuple(flags),
            suggested_action=action[:400],
        )
    with engine.begin() as conn:
        for view in outcome.views:
            append_artifact(conn, view)
        append_artifact(conn, outcome.rating)
    return outcome
