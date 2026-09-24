"""Research and fact-check artifacts: Claim, VerifiedClaim, Dossier.

A Claim is one sentence from the research desk that names the raw record it came from
and quotes the passage supporting it. Numbers inside a claim are carried as the text the
source used; the fact-check desk confirms each one against the quote or recomputes it
from stored market data. Only VerifiedClaims with verdict verified or corrected may be
used downstream.
"""

from datetime import date
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact


class ClaimNumber(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    text: str = Field(min_length=1)  # as written in the source, e.g. "$54 billion", "2.1%"
    # reported: a figure the source states (checked against the quote);
    # market_move: a price change the desk can recompute from stored bars.
    kind: Literal["reported", "market_move"]
    symbol: str | None = None
    as_of: date | None = None

    @model_validator(mode="after")
    def _market_move_is_addressable(self) -> Self:
        if self.kind == "market_move" and (self.symbol is None or self.as_of is None):
            raise ValueError("a market_move number needs a symbol and an as_of date")
        return self


@register_artifact
class Claim(ArtifactBase):
    kind: Literal["claim"] = "claim"
    schema_version: Literal[1] = 1

    subject: str = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=500)
    source_record_id: UUID
    quoted_span: str = Field(min_length=1, max_length=1000)
    numbers: tuple[ClaimNumber, ...] = ()

    @model_validator(mode="after")
    def _cites_source(self) -> Self:
        if self.source_record_id not in self.parents:
            raise ValueError("a claim must list its source record in parents")
        return self


class RecomputedNumber(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    stated: str
    value: Decimal
    unit: str
    source_ref: str = Field(min_length=1)


@register_artifact
class VerifiedClaim(ArtifactBase):
    kind: Literal["verified_claim"] = "verified_claim"
    schema_version: Literal[1] = 1

    claim_id: UUID
    # "verdict", not "status": the envelope's status (ok/failed) says whether the check ran.
    verdict: Literal["verified", "corrected", "rejected"]
    # How strongly the quote supports the statement, judged by the small model.
    entailment: float | None = Field(default=None, ge=0.0, le=1.0)
    recomputed: tuple[RecomputedNumber, ...] = ()
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.claim_id not in self.parents:
            raise ValueError("a verified claim must list its claim in parents")
        if self.verdict == "corrected" and not self.recomputed:
            raise ValueError("a corrected claim must carry the recomputed numbers")
        return self


class DossierSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    text: str


@register_artifact
class Dossier(ArtifactBase):
    kind: Literal["dossier"] = "dossier"
    schema_version: Literal[1] = 1

    subject: str = Field(min_length=1)
    subject_kind: Literal["holding", "play"]
    # Code-computed rank for plays (None for holdings, which are always researched).
    selection_score: float | None = None
    sections: tuple[DossierSection, ...]
    claim_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def _claims_are_parents(self) -> Self:
        missing = set(self.claim_ids) - set(self.parents)
        if missing:
            raise ValueError("every claim in a dossier must be listed in parents")
        return self
