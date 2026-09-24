"""The post-market shift's desk sequence: lanes, research, fact-check, ideas, theses.

    lanes (code) -> shortlist (code) -> research (deep) -> fact-check (small)
    -> selection (code) -> thesis writer (deep) -> Jon's evidence and state checks (code)

The GPU holds one chat model at a time, so each model is unloaded before the next loads.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine

from desk.artifacts.store import append_artifact
from desk.artifacts.thesis import Thesis
from desk.collectors.holdings import held_symbols
from desk.config import ModelsConfig, TiersConfig, UniverseConfig
from desk.desks.factcheck import FactCheckOutcome, run_factcheck
from desk.desks.idea.lanes import LanesConfig, build_candidates
from desk.desks.idea.select import finalize, shortlist
from desk.desks.idea.status import check_theses, new_version, open_theses
from desk.desks.idea.writer import accepted_claims, write_theses
from desk.desks.research import ResearchOutcome, Subject, holding_subjects, run_research
from desk.llm.client import OllamaChat
from desk.llm.embeddings import OllamaEmbedder
from desk.watch.calendar import MarketCalendar
from desk.watch.rules import WatchConfig
from desk.watch.scan import load_tier_map


@dataclass
class PostMarketOutcome:
    notes: list[str] = field(default_factory=list)
    error: str | None = None


def research_subjects(
    holdings: list[Subject], shortlisted: list[Subject], jon_theses: list[Thesis]
) -> list[Subject]:
    """Holdings, then shortlisted candidates, then Jon's open theses, one per symbol."""
    subjects: dict[str, Subject] = {}
    for subject in [*holdings, *shortlisted]:
        subjects.setdefault(subject.symbol, subject)
    for thesis in jon_theses:
        symbol = thesis.primary_instrument
        if symbol not in subjects:
            subjects[symbol] = Subject(
                symbol, "play", reasons=(f"Jon's thesis: {thesis.statement}",), origins=(thesis.id,)
            )
    return list(subjects.values())


def attach_evidence(thesis: Thesis, claim_ids: list[UUID], shift_id: UUID | None) -> Thesis | None:
    """A new version of Jon's thesis carrying new verified claims, or None if none are new."""
    new = [c for c in claim_ids if c not in thesis.evidence]
    if not new:
        return None
    return new_version(
        thesis,
        "idea.evidence",
        f"added {len(new)} verified claims from research",
        shift_id,
        evidence=(*thesis.evidence, *new),
    )


async def run_post_market(
    engine: Engine,
    chat: OllamaChat,
    embedder: OllamaEmbedder,
    models: ModelsConfig,
    lanes: LanesConfig,
    watch: WatchConfig,
    tiers: TiersConfig,
    universe: UniverseConfig,
    calendar: MarketCalendar,
    now: datetime,
    shift_id: UUID | None = None,
) -> PostMarketOutcome:
    outcome = PostMarketOutcome()
    tz = calendar.tz
    today = now.astimezone(tz).date()
    claims_since = now - timedelta(days=lanes.claim_window_days)

    with engine.begin() as conn:
        tier_map = load_tier_map(conn, tiers, universe, today)
        candidates = build_candidates(
            conn, lanes, tier_map, tiers, watch.policy_groups(), now, tz, shift_id
        )
        for candidate in candidates:
            append_artifact(conn, candidate)
        held = set(held_symbols(conn))
        holdings = holding_subjects(conn)
        standing = [t for t in open_theses(conn) if t.state != "draft"]
    jon = [t for t in standing if t.origin == "jon"]
    covered = frozenset(t.primary_instrument for t in standing)
    listed = shortlist(candidates, held, lanes.selection.shortlist, covered)
    outcome.notes.append(
        f"lanes: {len(candidates)} candidates, shortlisted "
        f"{', '.join(c.instrument for c in listed.chosen) or 'none'}"
    )

    plays = [
        Subject(c.instrument, "play", c.score, (f"{c.lane}: {c.driver}",), (c.id,))
        for c in listed.chosen
    ]
    subjects = research_subjects(holdings, plays, jon)
    research: ResearchOutcome = await run_research(
        engine, chat, models.deep, embedder, models.embedding, subjects, now, shift_id
    )
    await chat.unload(models.deep.model)
    outcome.notes.append(
        f"research: {len(research.dossiers)} dossiers, {research.claims} claims, "
        f"{len(research.failed)} failed"
    )
    outcome.notes += research.failed[:5]

    check: FactCheckOutcome = await run_factcheck(engine, chat, models.small, tz, shift_id)
    await chat.unload(models.small.model)
    outcome.notes.append(
        f"fact-check: {check.verified} verified, {check.corrected} corrected, "
        f"{check.rejected} rejected, {check.failed} failed"
    )

    with engine.connect() as conn:
        evidence = {
            c.instrument: len(accepted_claims(conn, c.instrument, claims_since))
            for c in listed.chosen
        }
    selection = finalize(candidates, listed, evidence, lanes.selection.theses, shift_id)
    with engine.begin() as conn:
        append_artifact(conn, selection)
    chosen = [c for c in listed.chosen if c.id in selection.selected]

    written = await write_theses(engine, chat, models.deep, chosen, now, tz, claims_since, shift_id)
    await chat.unload(models.deep.model)
    outcome.notes.append(
        f"theses: {len(written.theses)} written"
        + (f", {len(written.failed)} failed" if written.failed else "")
    )
    outcome.notes += written.failed[:3]

    with engine.begin() as conn:
        updated = 0
        for thesis in jon:
            rows = accepted_claims(conn, thesis.primary_instrument, thesis.created_at)
            version = attach_evidence(thesis, [row.id for row in rows], shift_id)
            if version is not None:
                append_artifact(conn, version)
                updated += 1
        invalidated = check_theses(conn, tz, today, shift_id)
        for version in invalidated:
            append_artifact(conn, version)
    outcome.notes.append(
        f"Jon's theses: {updated} given new evidence; {len(invalidated)} invalidated"
    )

    if check.error:
        outcome.error = check.error
    elif research.failed and not research.dossiers:
        outcome.error = "research produced no dossiers"
    return outcome
