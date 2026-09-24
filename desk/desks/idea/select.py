"""Idea selection: merge lane candidates into a shortlist, then keep those with evidence.

Candidates are taken best score first. Held instruments go to the holdings desk, and an
instrument already taken by a higher-scoring lane is a duplicate. After research and
fact-check, the first shortlisted candidates with at least one verified claim become
theses; every other candidate is dropped with its reason.
"""

from dataclasses import dataclass, field
from uuid import UUID

from desk.artifacts.idea import DroppedCandidate, IdeaSelection, LaneCandidate


@dataclass
class Shortlist:
    chosen: list[LaneCandidate] = field(default_factory=list)
    dropped: dict[UUID, str] = field(default_factory=dict)


def shortlist(
    candidates: list[LaneCandidate],
    held: set[str],
    limit: int,
    covered: frozenset[str] = frozenset(),
) -> Shortlist:
    """`covered` holds instruments that already have an open thesis."""
    result = Shortlist()
    taken: dict[str, LaneCandidate] = {}
    for candidate in sorted(candidates, key=lambda c: (-c.score, c.lane, c.instrument)):
        if candidate.instrument in held:
            result.dropped[candidate.id] = "held: rated by the holdings desk"
        elif candidate.instrument in covered:
            result.dropped[candidate.id] = "an open thesis already covers it"
        elif candidate.instrument in taken:
            best = taken[candidate.instrument]
            result.dropped[candidate.id] = f"duplicate of {best.instrument} ({best.lane})"
        elif len(result.chosen) >= limit:
            result.dropped[candidate.id] = "below the research cut"
        else:
            taken[candidate.instrument] = candidate
            result.chosen.append(candidate)
    return result


def finalize(
    candidates: list[LaneCandidate],
    listed: Shortlist,
    evidence_counts: dict[str, int],
    keep: int,
    shift_id: UUID | None = None,
) -> IdeaSelection:
    """Keep the first `keep` shortlisted candidates that have verified evidence."""
    dropped = dict(listed.dropped)
    selected: list[UUID] = []
    for candidate in listed.chosen:
        if evidence_counts.get(candidate.instrument, 0) == 0:
            dropped[candidate.id] = "no verified evidence from research"
        elif len(selected) >= keep:
            dropped[candidate.id] = "below the thesis cut"
        else:
            selected.append(candidate.id)
    return IdeaSelection(
        produced_by="idea.select",
        runtime_ms=0,
        shift_id=shift_id,
        parents=tuple(c.id for c in candidates),
        selected=tuple(selected),
        dropped=tuple(
            DroppedCandidate(candidate_id=cid, reason=reason) for cid, reason in dropped.items()
        ),
    )
