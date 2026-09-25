"""Fact-check desk: code checks every claim before anything downstream may use it.

1. The quoted span must appear in the cited source (spacing, case and quote-mark style
   are normalized; nothing else is).
2. Every reported number must appear inside that quote.
3. Every market-move number is recomputed from stored daily closes; a wrong size or
   direction makes the claim "corrected" with the recomputed value.
4. The small model scores how well the quote supports the statement (entailment); a low
   score rejects the claim.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.artifacts.base import ArtifactStatus
from desk.artifacts.research import Claim, ClaimNumber, RecomputedNumber, VerifiedClaim
from desk.artifacts.store import append_artifact, get_artifact
from desk.collectors.embeddings import record_text
from desk.config import ChatModel
from desk.llm.client import OllamaChat, StructuredResult, Usage, request_failure
from desk.llm.prompts import load_prompt

logger = logging.getLogger(__name__)

NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# A stated move may differ from the recomputed one by this much before it is corrected:
# sources round, and "2%" for a 2.08% move is fair.
MOVE_ABSOLUTE_TOLERANCE = Decimal("0.35")
MOVE_RELATIVE_TOLERANCE = Decimal("0.15")
DOWN_WORDS = (
    "fell",
    "fall",
    "falls",
    "dropped",
    "drop",
    "drops",
    "declined",
    "declines",
    "slid",
    "slides",
    "slumped",
    "sank",
    "lost",
    "down",
    "lower",
    "plunged",
    "tumbled",
)
UP_WORDS = (
    "rose",
    "rise",
    "rises",
    "gained",
    "gains",
    "jumped",
    "jumps",
    "climbed",
    "climbs",
    "rallied",
    "rallies",
    "surged",
    "soared",
    "up",
    "higher",
    "advanced",
)
# Curly double and single quotes map to their straight forms.
_QUOTES = str.maketrans({chr(0x201C): '"', chr(0x201D): '"', chr(0x2018): "'", chr(0x2019): "'"})


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).translate(_QUOTES).casefold()
    return re.sub(r"\s+", " ", folded).strip()


WORD = re.compile(r"[^\W_]+(?:[.,]\d+)*")
ELLIPSIS = re.compile(r"\[?(?:\.\.\.|…)\]?")
# Share of words a copied passage must share, in order, with a same-length span of the
# source. Small local models drop or swap a word when copying a passage.
QUOTE_MATCH_RATIO = 0.85
# Shorter passages must match exactly; a few words match too much text loosely.
MIN_FUZZY_WORDS = 6


def _words(value: str) -> list[str]:
    return WORD.findall(_normalize(value))


def _fragment_in(fragment: list[str], source: list[str]) -> bool:
    size = len(fragment)
    if f" {' '.join(fragment)} " in f" {' '.join(source)} ":
        return True
    if size < MIN_FUZZY_WORDS:
        return False
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(fragment)
    for start in range(max(1, len(source) - size + 1)):
        matcher.set_seq1(source[start : start + size])
        if matcher.real_quick_ratio() < QUOTE_MATCH_RATIO:
            continue
        if matcher.quick_ratio() >= QUOTE_MATCH_RATIO and matcher.ratio() >= QUOTE_MATCH_RATIO:
            return True
    return False


def quote_in_source(quote: str, source: str) -> bool:
    """The quote, allowing for case, punctuation, "..." gaps and a few changed words.

    Every number in the quote must still appear in the source, so a loose match never
    carries a figure the source does not contain.
    """
    fragments = [words for part in ELLIPSIS.split(quote) if (words := _words(part))]
    if not fragments or not set(_digits(quote)) <= set(_digits(source)):
        return False
    source_words = _words(source)
    return all(_fragment_in(fragment, source_words) for fragment in fragments)


def _digits(value: str) -> list[str]:
    return [token.replace(",", "") for token in NUMBER.findall(value)]


def number_in_quote(number: ClaimNumber, quote: str) -> bool:
    """Every numeric token of the stated number must appear as a token in the quote."""
    stated = _digits(number.text)
    return bool(stated) and set(stated) <= set(_digits(quote))


def stated_direction(statement: str) -> int:
    words = set(re.findall(r"[a-z]+", statement.casefold()))
    down, up = words & set(DOWN_WORDS), words & set(UP_WORDS)
    if down and not up:
        return -1
    if up and not down:
        return 1
    return 0


@dataclass(frozen=True, slots=True)
class MoveCheck:
    ok: bool
    reason: str
    recomputed: RecomputedNumber | None


def check_market_move(
    number: ClaimNumber,
    statement: str,
    closes: tuple[Decimal, Decimal] | None,
    source_ref: str,
) -> MoveCheck:
    """Compare a stated percentage move with (prior close, close) for its date."""
    if closes is None:
        return MoveCheck(False, f"no stored prices for {number.symbol} on {number.as_of}", None)
    prior, close = closes
    actual = ((close / prior - 1) * 100).quantize(Decimal("0.01"))
    recomputed = RecomputedNumber(
        name=number.name, stated=number.text, value=actual, unit="%", source_ref=source_ref
    )
    stated_values = _digits(number.text)
    if not stated_values:
        return MoveCheck(False, f"stated move {number.text!r} has no number", recomputed)
    stated = Decimal(stated_values[0])
    direction = stated_direction(statement)
    if direction and (actual > 0) != (direction > 0) and actual != 0:
        return MoveCheck(
            False, f"direction differs: stated {number.text}, recomputed {actual}%", recomputed
        )
    gap = abs(abs(actual) - stated)
    if gap > max(MOVE_ABSOLUTE_TOLERANCE, abs(actual) * MOVE_RELATIVE_TOLERANCE):
        return MoveCheck(
            False, f"size differs: stated {number.text}, recomputed {actual}%", recomputed
        )
    return MoveCheck(True, f"matches recomputed {actual}%", recomputed)


# --- Running the checks --------------------------------------------------------------------

# Code maps the small model's support label to the stored score, so the model never
# produces the number itself.
SUPPORT_SCORE = {"strong": 0.9, "partial": 0.6, "none": 0.1}
MAX_CLAIMS_PER_RUN = 60
ENTAILMENT_BATCH = 15


class SupportEntry(BaseModel):
    index: int
    support: Literal["strong", "partial", "none"]
    reason: str = Field(min_length=1, max_length=300)


class SupportReply(BaseModel):
    labels: list[SupportEntry]


def closes_for(
    conn: Connection, symbol: str, as_of: date, tz: ZoneInfo
) -> tuple[tuple[Decimal, Decimal], str] | None:
    """(prior close, close) for the trading day `as_of`, from stored daily bars."""
    day_start = datetime.combine(as_of, time(0), tz)
    rows = conn.execute(
        text(
            "SELECT ts, close FROM price_bars WHERE source = 'yahoo' AND interval = '1d' "
            "AND symbol = :s AND ts <= :day ORDER BY ts DESC LIMIT 2"
        ),
        {"s": symbol, "day": day_start},
    ).all()
    if len(rows) < 2 or rows[0].ts.astimezone(tz).date() != as_of:
        return None
    ref = f"price_bars:yahoo:{symbol}:1d:{as_of.isoformat()}"
    return (Decimal(rows[1].close), Decimal(rows[0].close)), ref


@dataclass
class HardCheck:
    claim: Claim
    passed: bool
    reasons: list[str] = field(default_factory=list)
    recomputed: list[RecomputedNumber] = field(default_factory=list)
    corrected: bool = False


def hard_check(conn: Connection, claim: Claim, tz: ZoneInfo) -> HardCheck:
    result = HardCheck(claim, passed=True)
    source = get_artifact(conn, claim.source_record_id)
    payload = source.model_dump()
    body = (
        payload["payload"].get("text", "")
        if payload["source"] == "edgar.filing_text"
        else record_text(payload["source"], payload["payload"], payload.get("url"))
    )
    if not quote_in_source(claim.quoted_span, body):
        return HardCheck(claim, False, ["quote not found in the cited source"])
    result.reasons.append("quote found in source")
    for number in claim.numbers:
        if number.kind == "reported":
            if not number_in_quote(number, claim.quoted_span):
                return HardCheck(claim, False, [f"number {number.text!r} not in the quote"])
            result.reasons.append(f"{number.text} present in quote")
        else:
            assert number.symbol is not None and number.as_of is not None
            found = closes_for(conn, number.symbol, number.as_of, tz)
            move = check_market_move(
                number,
                claim.statement,
                found[0] if found else None,
                found[1] if found else "none",
            )
            if move.recomputed is None:
                return HardCheck(claim, False, [move.reason])
            result.recomputed.append(move.recomputed)
            result.reasons.append(move.reason)
            if not move.ok:
                result.corrected = True
    return result


@dataclass
class FactCheckOutcome:
    verified: int = 0
    corrected: int = 0
    rejected: int = 0
    failed: int = 0
    usage: Usage | None = None
    error: str | None = None


async def run_factcheck(
    engine: Engine,
    chat: OllamaChat,
    model: ChatModel,
    tz: ZoneInfo,
    shift_id: UUID | None = None,
    since: timedelta = timedelta(days=2),
) -> FactCheckOutcome:
    outcome = FactCheckOutcome()
    with engine.connect() as conn:
        ids = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT c.id FROM artifacts c WHERE c.kind = 'claim' "
                    "AND c.created_at >= :since AND NOT EXISTS (SELECT 1 FROM artifacts v "
                    "WHERE v.kind = 'verified_claim' AND v.status = 'ok' "
                    "AND v.payload->>'claim_id' = c.id::text) "
                    "ORDER BY c.created_at LIMIT :n"
                ),
                {"since": datetime.now(UTC) - since, "n": MAX_CLAIMS_PER_RUN},
            )
        ]
        checks = []
        for claim_id in ids:
            claim = get_artifact(conn, claim_id)
            assert isinstance(claim, Claim)
            checks.append(hard_check(conn, claim, tz))

    verdicts: list[VerifiedClaim] = []
    failed_verdicts: list[VerifiedClaim] = []
    needs_support = [c for c in checks if c.passed]
    for check in checks:
        if not check.passed:
            verdicts.append(_verdict(check, "rejected", None, shift_id, model, None, 0))

    for start in range(0, len(needs_support), ENTAILMENT_BATCH):
        batch = needs_support[start : start + ENTAILMENT_BATCH]
        listing = "\n\n".join(
            f"[{i}] statement: {c.claim.statement}\n    quote: {c.claim.quoted_span}"
            for i, c in enumerate(batch, start=1)
        )
        prompt = load_prompt("entailment", {"items": listing})
        started = datetime.now(UTC)
        try:
            result = await chat.structured(model, prompt, SupportReply, check=_support_check(batch))
        except httpx.HTTPError as exc:
            logger.warning("entailment request failed: %s", request_failure(exc))
            result = StructuredResult(None, Usage(model=model.model), 0, error=request_failure(exc))
        runtime_ms = int((datetime.now(UTC) - started).total_seconds() * 1000) // len(batch)
        outcome.usage = result.usage
        if result.value is None:
            # Failed checks are recorded; only ok verdicts close a claim, so the next run
            # retries these.
            outcome.error = result.error
            failed_verdicts += [
                _verdict(
                    c,
                    "rejected",
                    None,
                    shift_id,
                    model,
                    prompt.version,
                    runtime_ms,
                    error=result.error or "entailment reply failed",
                )
                for c in batch
            ]
            continue
        for entry in result.value.labels:
            check = batch[entry.index - 1]
            score = SUPPORT_SCORE[entry.support]
            check.reasons.append(f"support {entry.support}: {entry.reason}")
            if entry.support == "none":
                verdict: Literal["verified", "corrected", "rejected"] = "rejected"
            elif check.corrected:
                verdict = "corrected"
            else:
                verdict = "verified"
            verdicts.append(
                _verdict(check, verdict, score, shift_id, model, prompt.version, runtime_ms)
            )

    with engine.begin() as conn:
        for verified in verdicts + failed_verdicts:
            append_artifact(conn, verified)
    outcome.failed = len(failed_verdicts)
    for verified in verdicts:
        setattr(outcome, verified.verdict, getattr(outcome, verified.verdict) + 1)
    return outcome


def _support_check(batch: list[HardCheck]) -> Any:
    def check(reply: SupportReply) -> list[str]:
        problems = []
        if sorted(e.index for e in reply.labels) != list(range(1, len(batch) + 1)):
            problems.append(f"return exactly one entry for each index 1 to {len(batch)}")
        for entry in reply.labels:
            if 1 <= entry.index <= len(batch):
                allowed = set(NUMBER.findall(batch[entry.index - 1].claim.statement))
                allowed |= set(NUMBER.findall(batch[entry.index - 1].claim.quoted_span))
                extra = set(NUMBER.findall(entry.reason)) - allowed
                if extra:
                    problems.append(f"item {entry.index}: reason adds numbers {sorted(extra)}")
        return problems

    return check


def _verdict(
    check: HardCheck,
    verdict: Literal["verified", "corrected", "rejected"],
    score: float | None,
    shift_id: UUID | None,
    model: ChatModel,
    prompt_version: str | None,
    runtime_ms: int,
    error: str | None = None,
) -> VerifiedClaim:
    return VerifiedClaim(
        status=ArtifactStatus.FAILED if error else ArtifactStatus.OK,
        error=error,
        produced_by="factcheck",
        runtime_ms=runtime_ms,
        shift_id=shift_id,
        parents=(check.claim.id,),
        model=model.model if score is not None else None,
        prompt_version=prompt_version,
        claim_id=check.claim.id,
        verdict=verdict,
        entailment=score,
        recomputed=tuple(check.recomputed) if verdict == "corrected" else (),
        reason="; ".join(check.reasons)[:1000],
    )
