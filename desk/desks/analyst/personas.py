"""Analyst personas from personas/*.yaml and the rules for calling them."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from desk.settings import REPO_ROOT

PERSONAS_DIR = REPO_ROOT / "personas"
FactSet = Literal["claims", "levels", "trend", "macro", "cot", "eia", "grid", "policy"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Called(_Frozen):
    always: bool = False
    groups: tuple[str, ...] = ()  # Tier 1 groups in config/tiers.yaml
    instruments: tuple[str, ...] = ()
    lanes: tuple[str, ...] = ()
    asset_classes: tuple[Literal["stock"], ...] = ()


class Persona(_Frozen):
    name: str = Field(pattern=r"^[a-z_]+$")
    title: str
    specialty: str
    version: str
    called: Called
    facts: tuple[FactSet, ...] = Field(min_length=1)
    default_keeper: bool = False
    prompt: str = Field(min_length=1)


def load_personas(directory: Path = PERSONAS_DIR) -> dict[str, Persona]:
    personas = {}
    for path in sorted(directory.glob("*.yaml")):
        with path.open(encoding="utf-8") as handle:
            persona = Persona.model_validate(yaml.safe_load(handle))
        if persona.name != path.stem:
            raise ValueError(f"{path.name} names persona {persona.name!r}")
        personas[persona.name] = persona
    return personas


def _matches(
    persona: Persona, instrument: str, lane: str | None, group: str | None, stock: bool
) -> bool:
    called = persona.called
    return (
        instrument in called.instruments
        or (group is not None and group in called.groups)
        or (lane is not None and lane in called.lanes)
        or (stock and "stock" in called.asset_classes)
    )


def select_personas(
    personas: dict[str, Persona],
    instrument: str,
    lane: str | None,
    group: str | None,
    stock: bool,
) -> list[Persona]:
    """Every-debate personas first, then the specialists this subject calls for."""
    always = [p for p in personas.values() if p.called.always]
    specialists = [
        p
        for p in personas.values()
        if not p.called.always and _matches(p, instrument, lane, group, stock)
    ]
    return always + specialists


def keeper_for(
    personas: dict[str, Persona], instrument: str, group: str | None, stock: bool
) -> Persona:
    """The persona that argues keep in a holding debate: its specialist, else the default."""
    for persona in personas.values():
        if not persona.called.always and _matches(persona, instrument, None, group, stock):
            return persona
    return next(p for p in personas.values() if p.default_keeper)
