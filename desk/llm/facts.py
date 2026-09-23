"""Fact tables: how numbers reach LLM-written text without coming from the model.

Code computes every number and gives it an id, a display string and a source. The model
writes prose that refers to facts by placeholder, "{AEP.change_pct}", and code renders the
display strings in. violations() rejects model text that invents a number: any digit
outside a placeholder must be part of some fact's label (so "10-year yield" is allowed
when a fact is labelled "10-year Treasury yield").
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_.:/^\-]+)\}")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


class UnknownFactError(KeyError):
    pass


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    value: Decimal | str
    unit: str
    display: str  # code-formatted, e.g. "-1.23%", "$1,200.30", "Oct 14 08:30 ET"
    label: str  # human description, e.g. "AEP day change"
    source_ref: str

    def __post_init__(self) -> None:
        if not self.source_ref:
            raise ValueError(f"fact {self.id} has no source reference")
        if not PLACEHOLDER.fullmatch("{" + self.id + "}"):
            raise ValueError(f"fact id {self.id!r} cannot be used as a placeholder")


class FactTable:
    def __init__(self, facts: Iterable[Fact]) -> None:
        self._facts: dict[str, Fact] = {}
        for fact in facts:
            if fact.id in self._facts:
                raise ValueError(f"duplicate fact id {fact.id}")
            self._facts[fact.id] = fact
        self._label_numbers = {n for f in self._facts.values() for n in NUMBER.findall(f.label)}

    def __len__(self) -> int:
        return len(self._facts)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._facts.values())

    def get(self, fact_id: str) -> Fact:
        try:
            return self._facts[fact_id]
        except KeyError:
            raise UnknownFactError(fact_id) from None

    def render(self, text: str) -> str:
        return PLACEHOLDER.sub(lambda m: self.get(m.group(1)).display, text)

    def violations(self, text: str) -> list[str]:
        problems = [
            f"unknown fact {{{fid}}}" for fid in PLACEHOLDER.findall(text) if fid not in self._facts
        ]
        prose = PLACEHOLDER.sub(" ", text)
        for number in NUMBER.findall(prose):
            if number not in self._label_numbers:
                problems.append(
                    f"number {number!r} is not from a fact; use a {{fact_id}} placeholder"
                )
        return problems

    def prompt_listing(self) -> str:
        """One line per fact for the prompt: id, rendered value and what it means."""
        return "\n".join(f"{{{f.id}}} = {f.display}  ({f.label})" for f in self._facts.values())
