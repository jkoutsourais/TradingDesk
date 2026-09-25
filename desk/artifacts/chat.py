"""Chat with the chief of staff: Jon's messages and the desk's replies.

A reply's text is rendered by code from the model's placeholders, and every fact it
cites names its source; artifacts it cites are listed in parents, so a reply traces back
like any other artifact.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from desk.artifacts.base import ArtifactBase
from desk.artifacts.registry import register_artifact


@register_artifact
class ChatMessage(ArtifactBase):
    kind: Literal["chat_message"] = "chat_message"
    schema_version: Literal[1] = 1

    thread_id: UUID
    text: str = Field(min_length=1, max_length=4000)
    # ask: a question about the desk; explain: a plain-English explanation of one artifact.
    mode: Literal["ask", "explain"] = "ask"
    subject_id: UUID | None = None

    @model_validator(mode="after")
    def _subject(self) -> Self:
        if self.mode == "explain" and self.subject_id is None:
            raise ValueError("an explain message names the artifact to explain")
        if self.subject_id is not None and self.subject_id not in self.parents:
            raise ValueError("the explained artifact must be listed in parents")
        return self


@register_artifact
class ChatReply(ArtifactBase):
    kind: Literal["chat_reply"] = "chat_reply"
    schema_version: Literal[1] = 1

    thread_id: UUID
    message_id: UUID
    text: str = Field(min_length=1, max_length=6000)
    cited_artifacts: tuple[UUID, ...] = ()
    fact_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _linked(self) -> Self:
        if self.message_id not in self.parents:
            raise ValueError("the message answered must be listed in parents")
        if set(self.cited_artifacts) - set(self.parents):
            raise ValueError("every cited artifact must be listed in parents")
        return self
