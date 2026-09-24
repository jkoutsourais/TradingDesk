"""Research desk: pick subjects, gather sources, have the deep model write a dossier of
claims, and store Claims and the Dossier.

Subjects are every holding plus the top plays: symbols not held whose recent watch hits
and relevant headlines score highest. The play score is computed here, never by a model.
Claims are checked for quote and number problems before they are stored (one retry with
the problems listed); the fact-check desk then decides each claim's verdict.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.artifacts.base import ArtifactStatus
from desk.artifacts.research import Claim, ClaimNumber, Dossier, DossierSection
from desk.artifacts.store import append_artifact
from desk.collectors.embeddings import record_text
from desk.collectors.holdings import held_symbols
from desk.config import ChatModel, EmbeddingModel
from desk.desks.factcheck import number_in_quote, quote_in_source
from desk.llm.client import OllamaChat, StructuredResult, Usage, request_failure
from desk.llm.embeddings import OllamaEmbedder
from desk.llm.prompts import load_prompt

logger = logging.getLogger(__name__)

TRIGGER_WINDOW = timedelta(hours=24)
SOURCE_WINDOW = timedelta(days=3)
MAX_SOURCES = 12
MAX_SOURCE_CHARS = 1500
MAX_FILING_CHARS = 6000
SIMILAR_SOURCES = 5
DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
CLAIM_REF = re.compile(r"\[c(\d+)\]")


# --- Subject selection ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Subject:
    symbol: str
    kind: Literal["holding", "play"]
    score: float | None = None
    reasons: tuple[str, ...] = ()
    # Artifacts that put the subject on the list (lane candidates, a thesis), kept as
    # dossier parents for lineage.
    origins: tuple[UUID, ...] = ()


def combine_importance(values: list[float]) -> float:
    """1 - prod(1 - v): several hits add up with diminishing returns, capped at 1."""
    remaining = 1.0
    for value in values:
        remaining *= 1.0 - max(0.0, min(value, 1.0))
    return 1.0 - remaining


def holding_subjects(conn: Connection) -> list[Subject]:
    return [Subject(symbol, "holding") for symbol in held_symbols(conn)]


# --- Sources -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    record_id: UUID
    label: str
    text: str


async def gather_sources(
    engine: Engine,
    embedder: OllamaEmbedder | None,
    config: EmbeddingModel,
    subject: Subject,
    now: datetime,
) -> tuple[list[Source], list[str]]:
    since = now - SOURCE_WINDOW
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, payload FROM artifacts WHERE kind = 'raw_record' "
                "AND created_at >= :since AND payload->'tickers' ? :symbol "
                # Filing index rows carry only a link; their text arrives as edgar.filing_text.
                "AND payload->>'source' NOT IN ('finnhub.earnings_calendar', 'edgar.filing') "
                "ORDER BY (payload->>'source' = 'edgar.filing_text') DESC, created_at DESC "
                "LIMIT :n"
            ),
            {"since": since, "symbol": subject.symbol, "n": MAX_SOURCES},
        ).all()
        chosen = {row.id: row for row in rows}
        if embedder is not None and len(chosen) < MAX_SOURCES:
            query = f"{subject.symbol}: " + "; ".join(subject.reasons or ("latest news",))
            vector = await embedder.embed_query(query)
            similar = conn.execute(
                text(
                    "SELECT a.id, a.payload FROM raw_record_embeddings e "
                    "JOIN artifacts a ON a.id = e.artifact_id WHERE e.model = :model "
                    "AND a.created_at >= :since ORDER BY e.embedding <=> CAST(:v AS vector) "
                    "LIMIT :n"
                ),
                {
                    "model": config.model,
                    "since": since,
                    "n": SIMILAR_SOURCES,
                    "v": "[" + ",".join(repr(x) for x in vector) + "]",
                },
            ).all()
            for row in similar:
                if len(chosen) >= MAX_SOURCES:
                    break
                chosen.setdefault(row.id, row)
        context = [
            f"- {row.summary}"
            for row in conn.execute(
                text(
                    "SELECT payload->>'summary' AS summary FROM artifacts WHERE kind = 'trigger' "
                    "AND payload->>'instrument' = :s AND created_at >= :since "
                    "ORDER BY created_at DESC LIMIT 5"
                ),
                {"s": subject.symbol, "since": now - TRIGGER_WINDOW},
            )
        ]
    sources = []
    for row in chosen.values():
        payload = row.payload
        if payload["source"] == "edgar.filing_text":
            body = payload["payload"].get("text", "")[:MAX_FILING_CHARS]
        else:
            body = record_text(payload["source"], payload["payload"], payload.get("url"))
            body = body[:MAX_SOURCE_CHARS]
        published = (payload.get("published_at") or "")[:10]
        sources.append(Source(row.id, f"{payload['source']} {published}".strip(), body))
    return sources, context


# --- Model output and checks ---------------------------------------------------------------


class DraftNumber(BaseModel):
    name: str
    text: str
    kind: Literal["reported", "market_move"]
    symbol: str | None = None
    as_of: str | None = None


class DraftClaim(BaseModel):
    statement: str = Field(min_length=1, max_length=500)
    source_index: int
    quote: str = Field(min_length=1, max_length=1000)
    numbers: list[DraftNumber] = Field(default_factory=list)


class DraftSection(BaseModel):
    title: str
    text: str


class ResearchDraft(BaseModel):
    sections: list[DraftSection] = Field(max_length=4)
    claims: list[DraftClaim] = Field(max_length=12)


def check_draft(sources: list[Source]) -> Any:
    def check(draft: ResearchDraft) -> list[str]:
        problems: list[str] = []
        quote_digits: dict[int, set[str]] = {}
        for i, claim in enumerate(draft.claims, start=1):
            if not 1 <= claim.source_index <= len(sources):
                problems.append(f"claim c{i}: source_index {claim.source_index} does not exist")
                continue
            source = sources[claim.source_index - 1]
            if not quote_in_source(claim.quote, source.text):
                problems.append(
                    f"claim c{i}: quote is not word for word in source [{claim.source_index}]"
                )
            quote_digits[i] = set(DIGITS.findall(claim.quote))
            for number in claim.numbers:
                if number.kind == "reported" and not number_in_quote(
                    ClaimNumber(name=number.name, text=number.text, kind="reported"), claim.quote
                ):
                    problems.append(f"claim c{i}: number {number.text!r} is not in its quote")
                if number.kind == "market_move" and not (number.symbol and number.as_of):
                    problems.append(f"claim c{i}: market_move needs symbol and as_of")
            stated = set(DIGITS.findall(claim.statement))
            listed = {d for n in claim.numbers for d in DIGITS.findall(n.text)}
            if stated - listed:
                missing = sorted(stated - listed)
                problems.append(f"claim c{i}: statement numbers {missing} are not in numbers")
        for section in draft.sections:
            cited = {int(n) for n in CLAIM_REF.findall(section.text)}
            allowed = set().union(*(quote_digits.get(c, set()) for c in cited)) if cited else set()
            prose = CLAIM_REF.sub(" ", section.text)
            extra = set(DIGITS.findall(prose)) - allowed
            if extra:
                problems.append(
                    f"section {section.title!r}: numbers {sorted(extra)} are not in the quotes "
                    "of the claims it cites"
                )
        return problems

    return check


def _as_claim_number(number: DraftNumber) -> ClaimNumber:
    as_of = date.fromisoformat(number.as_of[:10]) if number.as_of else None
    return ClaimNumber(
        name=number.name, text=number.text, kind=number.kind, symbol=number.symbol, as_of=as_of
    )


@dataclass
class ResearchOutcome:
    dossiers: list[Dossier] = field(default_factory=list)
    claims: int = 0
    failed: list[str] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)


async def research_subject(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    embedder: OllamaEmbedder | None,
    embed_config: EmbeddingModel,
    subject: Subject,
    now: datetime,
    shift_id: UUID | None,
) -> tuple[Dossier | None, list[Claim], Usage | None, str | None]:
    sources, context = await gather_sources(engine, embedder, embed_config, subject, now)
    if not sources:
        return None, [], None, f"{subject.symbol}: no sources in the last three days"
    listing = "\n\n".join(f"[{i}] ({s.label})\n{s.text}" for i, s in enumerate(sources, start=1))
    prompt = load_prompt(
        "research",
        {
            "subject": subject.symbol,
            "subject_kind": subject.kind,
            "context": "\n".join(context) or "- none",
            "sources": listing,
        },
    )
    started = datetime.now(UTC)
    try:
        result = await chat.structured(model, prompt, ResearchDraft, check=check_draft(sources))
    except httpx.HTTPError as exc:
        # One subject's failed request is recorded and the shift moves on to the next.
        logger.warning("research request for %s failed: %s", subject.symbol, request_failure(exc))
        result = StructuredResult(None, Usage(model=model.model), 0, error=request_failure(exc))
    if result.value is None:
        error = result.error or "research reply failed"
        failed = Dossier(
            status=ArtifactStatus.FAILED,
            error=error,
            produced_by="research",
            runtime_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
            shift_id=shift_id,
            parents=tuple(dict.fromkeys(s.record_id for s in sources)),
            model=model.model,
            prompt_version=prompt.version,
            tokens_in=result.usage.tokens_in,
            tokens_out=result.usage.tokens_out,
            subject=subject.symbol,
            subject_kind=subject.kind,
            selection_score=subject.score,
            sections=(),
            claim_ids=(),
        )
        with engine.begin() as conn:
            append_artifact(conn, failed)
        return None, [], result.usage, f"{subject.symbol}: {error}"
    draft = result.value
    claims = []
    id_by_ref: dict[int, UUID] = {}
    for i, draft_claim in enumerate(draft.claims, start=1):
        source = sources[draft_claim.source_index - 1]
        claim = Claim(
            produced_by="research",
            runtime_ms=0,
            shift_id=shift_id,
            parents=(source.record_id,),
            model=model.model,
            prompt_version=prompt.version,
            subject=subject.symbol,
            statement=draft_claim.statement,
            source_record_id=source.record_id,
            quoted_span=draft_claim.quote,
            numbers=tuple(_as_claim_number(n) for n in draft_claim.numbers),
        )
        claims.append(claim)
        id_by_ref[i] = claim.id
    sections = [
        DossierSection(
            title=section.title,
            text=CLAIM_REF.sub(lambda m: f"[c{m.group(1)}]", section.text),
        )
        for section in draft.sections
    ]
    if subject.kind == "play" and subject.reasons:
        sections.insert(0, DossierSection(title="Why selected", text="; ".join(subject.reasons)))
    dossier = Dossier(
        produced_by="research",
        runtime_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        shift_id=shift_id,
        parents=(*(c.id for c in claims), *subject.origins),
        model=model.model,
        prompt_version=prompt.version,
        tokens_in=result.usage.tokens_in,
        tokens_out=result.usage.tokens_out,
        subject=subject.symbol,
        subject_kind=subject.kind,
        selection_score=subject.score,
        sections=tuple(sections),
        claim_ids=tuple(c.id for c in claims),
    )
    with engine.begin() as conn:
        for claim in claims:
            append_artifact(conn, claim)
        append_artifact(conn, dossier)
    return dossier, claims, result.usage, None


async def run_research(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    embedder: OllamaEmbedder | None,
    embed_config: EmbeddingModel,
    subjects: list[Subject],
    now: datetime,
    shift_id: UUID | None = None,
) -> ResearchOutcome:
    outcome = ResearchOutcome()
    for subject in subjects:
        dossier, claims, usage, error = await research_subject(
            engine, chat, model, embedder, embed_config, subject, now, shift_id
        )
        if usage is not None:
            outcome.usage.append(usage)
        if error:
            outcome.failed.append(error)
        if dossier is not None:
            outcome.dossiers.append(dossier)
            outcome.claims += len(claims)
    return outcome
