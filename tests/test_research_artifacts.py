from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from desk.artifacts.registry import artifact_class
from desk.artifacts.research import (
    Claim,
    ClaimNumber,
    Dossier,
    DossierSection,
    RecomputedNumber,
    VerifiedClaim,
)


def claim(**overrides: object) -> Claim:
    source = uuid4()
    fields: dict[str, object] = {
        "produced_by": "research",
        "runtime_ms": 0,
        "parents": [source],
        "subject": "AEP",
        "statement": "AEP raised its five-year capital plan to $54 billion.",
        "source_record_id": source,
        "quoted_span": "raised its five-year capital plan to $54 billion",
        "numbers": [{"name": "capital plan", "text": "$54 billion", "kind": "reported"}],
    }
    fields.update(overrides)
    return Claim.model_validate(fields)


def test_kinds_registered() -> None:
    assert artifact_class("claim") is Claim
    assert artifact_class("verified_claim") is VerifiedClaim
    assert artifact_class("dossier") is Dossier


def test_claim_cites_its_source() -> None:
    c = claim()
    assert c.source_record_id in c.parents
    with pytest.raises(ValidationError, match="source"):
        claim(parents=[uuid4()])


def test_claim_needs_a_quote() -> None:
    with pytest.raises(ValidationError):
        claim(quoted_span="")


def test_market_move_number_needs_symbol_and_date() -> None:
    with pytest.raises(ValidationError, match="market_move"):
        ClaimNumber(name="move", text="2.1%", kind="market_move")
    number = ClaimNumber(
        name="move", text="2.1%", kind="market_move", symbol="AEP", as_of=date(2026, 9, 22)
    )
    assert number.symbol == "AEP"


def test_verified_claim_statuses() -> None:
    c = claim()
    verified = VerifiedClaim(
        produced_by="factcheck",
        runtime_ms=0,
        parents=(c.id,),
        claim_id=c.id,
        verdict="verified",
        entailment=0.92,
        reason="quote found; numbers present; supported",
    )
    assert verified.claim_id in verified.parents
    with pytest.raises(ValidationError):
        VerifiedClaim(
            produced_by="f",
            runtime_ms=0,
            parents=(c.id,),
            claim_id=c.id,
            verdict="maybe",
            reason="x",
        )


def test_corrected_claim_needs_recomputed_numbers() -> None:
    c = claim()
    with pytest.raises(ValidationError, match="corrected"):
        VerifiedClaim(
            produced_by="f",
            runtime_ms=0,
            parents=(c.id,),
            claim_id=c.id,
            verdict="corrected",
            reason="price move differs",
        )
    fixed = VerifiedClaim(
        produced_by="f",
        runtime_ms=0,
        parents=(c.id,),
        claim_id=c.id,
        verdict="corrected",
        reason="price move differs",
        recomputed=[
            RecomputedNumber(
                name="move",
                stated="2.1%",
                value=Decimal("-1.30"),
                unit="%",
                source_ref="price_bars:yahoo:AEP:1d:2026-09-22",
            )
        ],
    )
    assert fixed.recomputed[0].value == Decimal("-1.30")


def test_entailment_bounds() -> None:
    c = claim()
    with pytest.raises(ValidationError):
        VerifiedClaim(
            produced_by="f",
            runtime_ms=0,
            parents=(c.id,),
            claim_id=c.id,
            verdict="verified",
            entailment=1.5,
            reason="x",
        )


def test_dossier_lists_claims_as_parents() -> None:
    c = claim()
    dossier = Dossier(
        produced_by="research",
        runtime_ms=0,
        parents=(c.id,),
        subject="AEP",
        subject_kind="holding",
        selection_score=None,
        sections=(DossierSection(title="What happened", text="Capital plan raised."),),
        claim_ids=(c.id,),
    )
    assert dossier.claim_ids == (c.id,)
    with pytest.raises(ValidationError, match="claim"):
        Dossier(
            produced_by="r",
            runtime_ms=0,
            parents=(),
            subject="AEP",
            subject_kind="holding",
            sections=(),
            claim_ids=(c.id,),
        )
