from datetime import UTC, datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from desk.artifacts.base import ArtifactBase, ArtifactStatus
from desk.artifacts.registry import (
    UnknownArtifactKindError,
    artifact_class,
    register_artifact,
)
from tests.probe import ProbeArtifact


def make_probe(**overrides: object) -> ProbeArtifact:
    fields: dict[str, object] = {"produced_by": "test.probe", "runtime_ms": 5, "note": "hello"}
    fields.update(overrides)
    return ProbeArtifact.model_validate(fields)


def test_envelope_defaults_are_filled() -> None:
    probe = make_probe()
    assert probe.kind == "probe"
    assert probe.schema_version == 1
    assert probe.status is ArtifactStatus.OK
    assert probe.error is None
    assert probe.parents == ()
    assert probe.shift_id is None
    assert probe.created_at.tzinfo is not None


def test_artifact_is_frozen() -> None:
    probe = make_probe()
    with pytest.raises(ValidationError):
        probe.note = "changed"  # type: ignore[misc]


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        make_probe(unexpected_field=1)


def test_naive_created_at_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        make_probe(created_at=datetime(2026, 9, 22, 12, 0))  # noqa: DTZ001


def test_created_at_is_normalized_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))
    probe = make_probe(created_at=datetime(2026, 9, 22, 8, 0, tzinfo=eastern))
    assert probe.created_at == datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    assert probe.created_at.utcoffset() == timedelta(0)


def test_kind_cannot_be_overridden() -> None:
    with pytest.raises(ValidationError):
        make_probe(kind="thesis")


def test_schema_version_cannot_be_overridden() -> None:
    with pytest.raises(ValidationError):
        make_probe(schema_version=2)


def test_failed_status_requires_error() -> None:
    with pytest.raises(ValidationError, match="error"):
        make_probe(status=ArtifactStatus.FAILED)
    failed = make_probe(status=ArtifactStatus.FAILED, error="second parse failed")
    assert failed.error == "second parse failed"


def test_ok_status_forbids_error() -> None:
    with pytest.raises(ValidationError, match="error"):
        make_probe(error="should not be here")


def test_negative_runtime_and_tokens_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_probe(runtime_ms=-1)
    with pytest.raises(ValidationError):
        make_probe(tokens_in=-1)


def test_parent_rules() -> None:
    parent_id = uuid4()
    with pytest.raises(ValidationError, match="duplicate"):
        make_probe(parents=[parent_id, parent_id])
    own_id = uuid4()
    with pytest.raises(ValidationError, match="own parent"):
        make_probe(id=own_id, parents=[own_id])


def test_registry_resolves_kind() -> None:
    assert artifact_class("probe") is ProbeArtifact


def test_registry_rejects_unknown_kind() -> None:
    with pytest.raises(UnknownArtifactKindError):
        artifact_class("no_such_kind")


def test_registry_rejects_duplicate_kind() -> None:
    with pytest.raises(ValueError, match="already registered"):

        @register_artifact
        class DuplicateProbe(ArtifactBase):
            kind: Literal["probe"] = "probe"
            schema_version: Literal[1] = 1


def test_registry_rejects_class_without_literal_kind() -> None:
    with pytest.raises(TypeError, match="kind"):

        @register_artifact
        class MissingKind(ArtifactBase):
            schema_version: Literal[1] = 1
