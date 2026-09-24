"""Thesis intake API; the Phase 9 Chat tab calls these.

POST /intake/messages               {"text": ..., "draft_id": null | <draft id>}
GET  /intake/messages/{id}          pending, or the draft with missing fields and questions
POST /intake/drafts/{id}/confirm    make a complete draft active
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from desk.artifacts.base import ArtifactStatus
from desk.artifacts.intake import IntakeMessage
from desk.artifacts.store import ArtifactNotFoundError, get_artifact
from desk.front_office.intake import (
    IncompleteDraftError,
    confirm_draft,
    draft_for_message,
    review,
    submit_message,
)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    draft_id: UUID | None = None


def intake_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/intake", tags=["intake"])

    @router.post("/messages", status_code=201)
    def post_message(body: MessageIn) -> dict[str, Any]:
        try:
            message = submit_message(engine, body.text, body.draft_id)
        except (ValueError, ArtifactNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"message_id": str(message.id), "status": "pending"}

    @router.get("/messages/{message_id}")
    def get_message(message_id: UUID) -> dict[str, Any]:
        with engine.connect() as conn:
            try:
                message = get_artifact(conn, message_id)
            except ArtifactNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if not isinstance(message, IntakeMessage):
                raise HTTPException(status_code=404, detail="not an intake message")
            draft = draft_for_message(conn, message_id)
        if draft is None:
            return {"message_id": str(message_id), "status": "pending"}
        if draft.status is ArtifactStatus.FAILED:
            return {"message_id": str(message_id), "status": "failed", "error": draft.error}
        checked = review(draft)
        return {
            "message_id": str(message_id),
            "status": "drafted",
            "draft_id": str(draft.id),
            "draft": draft.model_dump(mode="json"),
            "missing": checked.missing,
            "questions": checked.questions,
        }

    @router.post("/drafts/{draft_id}/confirm")
    def confirm(draft_id: UUID) -> dict[str, Any]:
        try:
            thesis = confirm_draft(engine, draft_id)
        except IncompleteDraftError as exc:
            raise HTTPException(status_code=409, detail={"missing": exc.missing}) from exc
        except (ValueError, ArtifactNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"thesis_id": str(thesis.id), "state": thesis.state}

    return router
