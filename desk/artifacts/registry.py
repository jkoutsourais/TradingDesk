"""Maps stored `kind` strings back to their artifact classes."""

from typing import Literal, get_args, get_origin

from desk.artifacts.base import ArtifactBase

_REGISTRY: dict[str, type[ArtifactBase]] = {}


class UnknownArtifactKindError(LookupError):
    pass


def _literal_default(cls: type[ArtifactBase], field_name: str) -> object:
    field = cls.model_fields[field_name]
    if get_origin(field.annotation) is not Literal or len(get_args(field.annotation)) != 1:
        raise TypeError(f"{cls.__name__}.{field_name} must be annotated as a single Literal")
    literal_value = get_args(field.annotation)[0]
    if field.default != literal_value:
        raise TypeError(f"{cls.__name__}.{field_name} default must equal its Literal value")
    return literal_value


def register_artifact[ArtifactT: ArtifactBase](cls: type[ArtifactT]) -> type[ArtifactT]:
    """Class decorator that records an artifact class under its `kind`."""
    kind = _literal_default(cls, "kind")
    _literal_default(cls, "schema_version")
    if not isinstance(kind, str):
        raise TypeError(f"{cls.__name__}.kind must be a string Literal")
    if kind in _REGISTRY:
        raise ValueError(f"artifact kind {kind!r} is already registered")
    _REGISTRY[kind] = cls
    return cls


def artifact_class(kind: str) -> type[ArtifactBase]:
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise UnknownArtifactKindError(f"no artifact class registered for kind {kind!r}") from None
