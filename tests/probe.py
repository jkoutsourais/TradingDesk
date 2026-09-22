"""Artifact kind used only by the test suite to exercise the envelope, store and API."""

from typing import Literal

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact


@register_artifact
class ProbeArtifact(ArtifactBase):
    kind: Literal["probe"] = "probe"
    schema_version: Literal[1] = 1
    note: str
