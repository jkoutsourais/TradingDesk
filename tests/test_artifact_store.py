from uuid import uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from desk.artifacts.base import ArtifactStatus
from desk.artifacts.store import (
    ArtifactNotFoundError,
    append_artifact,
    get_artifact,
    get_lineage,
)
from tests.probe import ProbeArtifact


def probe(note: str, parents: tuple[ProbeArtifact, ...] = ()) -> ProbeArtifact:
    return ProbeArtifact(
        produced_by="test.probe",
        runtime_ms=12,
        note=note,
        parents=tuple(parent.id for parent in parents),
        model="test-model",
        prompt_version="probe.v1",
        tokens_in=100,
        tokens_out=20,
        shift_id=uuid4(),
    )


def test_round_trip_preserves_every_field(db_conn: Connection) -> None:
    original = probe("round trip")
    append_artifact(db_conn, original)
    loaded = get_artifact(db_conn, original.id)
    assert isinstance(loaded, ProbeArtifact)
    assert loaded == original


def test_failed_artifact_round_trips(db_conn: Connection) -> None:
    failed = ProbeArtifact(
        produced_by="test.probe",
        runtime_ms=40,
        note="",
        status=ArtifactStatus.FAILED,
        error="structured output parse failed twice",
    )
    append_artifact(db_conn, failed)
    assert get_artifact(db_conn, failed.id) == failed


def test_missing_artifact_raises(db_conn: Connection) -> None:
    with pytest.raises(ArtifactNotFoundError):
        get_artifact(db_conn, uuid4())


def test_parent_must_exist(db_conn: Connection) -> None:
    orphan = ProbeArtifact(produced_by="test.probe", runtime_ms=1, note="x", parents=(uuid4(),))
    with pytest.raises(IntegrityError):
        append_artifact(db_conn, orphan)


def test_duplicate_id_is_rejected(db_conn: Connection) -> None:
    original = probe("once")
    append_artifact(db_conn, original)
    with pytest.raises(IntegrityError):
        append_artifact(db_conn, original)


def test_lineage_walks_all_ancestors(db_conn: Connection) -> None:
    # Diamond: raw_a and raw_b feed claim; claim and raw_b feed verdict.
    raw_a = probe("raw a")
    raw_b = probe("raw b")
    claim = probe("claim", parents=(raw_a, raw_b))
    verdict = probe("verdict", parents=(claim, raw_b))
    for artifact in (raw_a, raw_b, claim, verdict):
        append_artifact(db_conn, artifact)

    lineage = get_lineage(db_conn, verdict.id)
    depth_by_id = {entry.artifact.id: entry.depth for entry in lineage}

    assert depth_by_id == {verdict.id: 0, claim.id: 1, raw_b.id: 1, raw_a.id: 2}
    assert [entry.depth for entry in lineage] == sorted(entry.depth for entry in lineage)
    assert next(e for e in lineage if e.artifact.id == claim.id).artifact == claim


def test_lineage_of_missing_artifact_raises(db_conn: Connection) -> None:
    with pytest.raises(ArtifactNotFoundError):
        get_lineage(db_conn, uuid4())


def test_update_is_rejected_by_database(db_conn: Connection) -> None:
    original = probe("immutable")
    append_artifact(db_conn, original)
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(
            text("UPDATE artifacts SET produced_by = 'tampered' WHERE id = :id"),
            {"id": original.id},
        )


def test_delete_is_rejected_by_database(db_conn: Connection) -> None:
    original = probe("immutable")
    append_artifact(db_conn, original)
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("DELETE FROM artifacts WHERE id = :id"), {"id": original.id})


def test_edge_update_is_rejected_by_database(db_conn: Connection) -> None:
    parent = probe("parent")
    child = probe("child", parents=(parent,))
    append_artifact(db_conn, parent)
    append_artifact(db_conn, child)
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("DELETE FROM artifact_parents WHERE child_id = :id"), {"id": child.id})


def test_truncate_is_rejected_by_database(db_conn: Connection) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        db_conn.execute(text("TRUNCATE artifacts CASCADE"))
