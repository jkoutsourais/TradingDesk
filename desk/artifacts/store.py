"""Append-only persistence for artifacts.

There is deliberately no update or delete path; the database triggers from migration 0001
reject both. Callers own the transaction: these functions never commit.
"""

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, RowMapping, text

from desk.artifacts.base import ENVELOPE_FIELDS, ArtifactBase
from desk.artifacts.registry import artifact_class

# Envelope columns in the order they are selected and inserted; payload holds the rest.
_ENVELOPE_COLUMNS = (
    "id",
    "kind",
    "schema_version",
    "status",
    "error",
    "shift_id",
    "produced_by",
    "parents",
    "created_at",
    "model",
    "prompt_version",
    "runtime_ms",
    "tokens_in",
    "tokens_out",
)
if set(_ENVELOPE_COLUMNS) != ENVELOPE_FIELDS:
    raise RuntimeError("artifacts table columns drifted from the ArtifactBase envelope")

# Statements are assembled once from the fixed column tuple above; no caller input is
# interpolated, so the S608 injection warning does not apply.
_SELECT_COLUMNS = ", ".join(f"a.{column}" for column in _ENVELOPE_COLUMNS) + ", a.payload"
_INSERT_ARTIFACT_SQL = (
    "INSERT INTO artifacts ({}, payload) VALUES ({}, CAST(:payload AS jsonb))".format(  # noqa: S608
        ", ".join(_ENVELOPE_COLUMNS), ", ".join(f":{column}" for column in _ENVELOPE_COLUMNS)
    )
)
_INSERT_EDGE_SQL = (
    "INSERT INTO artifact_parents (child_id, parent_id, ordinal) "
    "VALUES (:child_id, :parent_id, :ordinal)"
)
_SELECT_ARTIFACT_SQL = f"SELECT {_SELECT_COLUMNS} FROM artifacts a WHERE a.id = :id"  # noqa: S608
_SELECT_LINEAGE_SQL = f"""
WITH RECURSIVE walk(id, depth) AS (
    SELECT CAST(:id AS uuid), 0
    UNION
    SELECT e.parent_id, w.depth + 1
    FROM artifact_parents e JOIN walk w ON e.child_id = w.id
), nearest AS (
    SELECT id, min(depth) AS depth FROM walk GROUP BY id
)
SELECT n.depth, {_SELECT_COLUMNS}
FROM nearest n JOIN artifacts a ON a.id = n.id
ORDER BY n.depth, a.created_at, a.id
"""  # noqa: S608


class ArtifactNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class LineageEntry:
    """An ancestor of the requested artifact; depth 0 is the artifact itself."""

    depth: int
    artifact: ArtifactBase


def append_artifact(conn: Connection, artifact: ArtifactBase) -> None:
    envelope = {column: getattr(artifact, column) for column in _ENVELOPE_COLUMNS}
    envelope["status"] = artifact.status.value
    envelope["parents"] = list(artifact.parents)
    payload = artifact.model_dump(mode="json", exclude=set(ENVELOPE_FIELDS))

    conn.execute(text(_INSERT_ARTIFACT_SQL), {**envelope, "payload": json.dumps(payload)})
    if artifact.parents:
        conn.execute(
            text(_INSERT_EDGE_SQL),
            [
                {"child_id": artifact.id, "parent_id": parent_id, "ordinal": ordinal}
                for ordinal, parent_id in enumerate(artifact.parents)
            ],
        )


def _row_to_artifact(row: RowMapping) -> ArtifactBase:
    fields = {column: row[column] for column in _ENVELOPE_COLUMNS}
    fields["parents"] = tuple(row["parents"])
    fields.update(row["payload"])
    return artifact_class(row["kind"]).model_validate(fields)


def get_artifact(conn: Connection, artifact_id: UUID) -> ArtifactBase:
    row = conn.execute(text(_SELECT_ARTIFACT_SQL), {"id": artifact_id}).mappings().one_or_none()
    if row is None:
        raise ArtifactNotFoundError(f"artifact {artifact_id} does not exist")
    return _row_to_artifact(row)


def get_lineage(conn: Connection, artifact_id: UUID) -> list[LineageEntry]:
    """Return the artifact and all its ancestors, nearest first.

    An ancestor reachable by several paths appears once, at its shortest distance. The graph
    is acyclic by construction: a parent must exist before its child is inserted, and rows
    are never updated.
    """
    rows = conn.execute(text(_SELECT_LINEAGE_SQL), {"id": artifact_id}).mappings().all()
    if not rows:
        raise ArtifactNotFoundError(f"artifact {artifact_id} does not exist")
    return [LineageEntry(depth=row["depth"], artifact=_row_to_artifact(row)) for row in rows]
