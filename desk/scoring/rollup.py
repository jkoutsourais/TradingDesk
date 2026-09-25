"""Score rollups: count, hit rate, average return and average R per attribution value."""

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScoreRow:
    attribution: dict[str, str]  # e.g. {"lane": "screen", "persona": "technician"}
    return_pct: float | None
    r_multiple: float | None
    hit: bool | None


@dataclass(frozen=True, slots=True)
class Group:
    dimension: str
    value: str
    count: int
    hit_rate: float | None
    avg_return: float | None
    avg_r: float | None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def rollup(rows: list[ScoreRow], dimension: str) -> list[Group]:
    grouped: dict[str, list[ScoreRow]] = defaultdict(list)
    for row in rows:
        value = row.attribution.get(dimension)
        if value:
            grouped[value].append(row)
    groups = []
    for value, members in grouped.items():
        hits = [m.hit for m in members if m.hit is not None]
        groups.append(
            Group(
                dimension=dimension,
                value=value,
                count=len(members),
                hit_rate=sum(hits) / len(hits) if hits else None,
                avg_return=_mean([m.return_pct for m in members if m.return_pct is not None]),
                avg_r=_mean([m.r_multiple for m in members if m.r_multiple is not None]),
            )
        )
    groups.sort(key=lambda g: (-g.count, g.value))
    return groups
