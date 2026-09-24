"""IntakeMessage: text Jon sends to the thesis intake, stored verbatim.

Numbers in a thesis drafted from chat must appear in one of these messages, which makes
Jon's own words the traceable source for his levels.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact


@register_artifact
class IntakeMessage(ArtifactBase):
    kind: Literal["intake_message"] = "intake_message"
    schema_version: Literal[1] = 1

    text: str = Field(min_length=1, max_length=4000)
    # The draft this message answers, when it fills in missing fields.
    draft_id: UUID | None = None
