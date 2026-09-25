"""Dashboard API for Theses, Book, Scores and Chat.

GET  /api/theses                  every thesis chain, latest version, with outcomes
GET  /api/theses/{id}             one thesis: versions, evidence, debates, plans, scores
GET  /api/plans/{id}              one plan with its risk decision
GET  /api/book                    broker holdings, positions from fills, recent fills
GET  /api/scores?horizon=5d       rollups plus the latest scores
POST /api/positions/{id}/link     Jon confirms a position's plan or thesis
POST /api/chat                    {"text", "thread_id"?, "subject_id"?}
GET  /api/chat/threads            recent conversations
GET  /api/chat/threads/{id}       one conversation
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from desk.api.v1 import _jsonable, _rows, ratings
from desk.artifacts.analyst import AnalystView, DebateVerdict
from desk.artifacts.chat import ChatMessage, ChatReply
from desk.artifacts.research import Claim, VerifiedClaim
from desk.artifacts.scoring import Position
from desk.artifacts.store import ArtifactNotFoundError, append_artifact, get_artifact
from desk.artifacts.thesis import Thesis
from desk.artifacts.trade import RiskDecision, TradePlan
from desk.collectors.holdings import latest_snapshots
from desk.front_office import chat as chat_desk
from desk.scoring.report import rollups
from desk.scoring.run import latest_positions, load_kind, relink, thesis_roots
from desk.watch import queue


def _load(conn: Connection, artifact_id: UUID) -> Any:
    try:
        return get_artifact(conn, artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _dump(artifact: Any) -> dict[str, Any]:
    return artifact.model_dump(mode="json")  # type: ignore[no-any-return]


def _chain(conn: Connection, thesis: Thesis) -> list[Thesis]:
    """Every version of this thesis, oldest first, found through previous_id both ways."""
    versions = {thesis.id: thesis}
    current = thesis
    while current.previous_id is not None:
        previous = get_artifact(conn, current.previous_id)
        assert isinstance(previous, Thesis)
        versions[previous.id] = previous
        current = previous
    frontier = [thesis.id]
    while frontier:
        found = conn.execute(
            text(
                "SELECT id FROM artifacts WHERE kind = 'thesis' AND status = 'ok' "
                "AND payload->>'previous_id' = ANY(:ids)"
            ),
            {"ids": [str(i) for i in frontier]},
        ).scalars()
        frontier = []
        for version_id in found:
            if version_id not in versions:
                later = get_artifact(conn, version_id)
                assert isinstance(later, Thesis)
                versions[version_id] = later
                frontier.append(version_id)
    return sorted(versions.values(), key=lambda t: t.created_at)


def _scores_for(conn: Connection, ids: list[UUID]) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT payload->>'subject_id' AS subject_id, payload->>'subject_kind' AS kind, "
        "payload->>'horizon' AS horizon, (payload->>'return_pct')::float AS return_pct, "
        "(payload->>'r_multiple')::float AS r_multiple, payload->>'first_hit' AS first_hit, "
        "(payload->>'hit')::boolean AS hit, payload->>'exit_day' AS exit_day "
        "FROM artifacts WHERE kind = 'score' AND payload->>'subject_id' = ANY(:ids) "
        "ORDER BY payload->>'horizon'",
        ids=[str(i) for i in ids],
    )


def _claim_rows(conn: Connection, verified_ids: tuple[UUID, ...]) -> list[dict[str, Any]]:
    rows = []
    for verified_id in verified_ids:
        verified = get_artifact(conn, verified_id)
        if not isinstance(verified, VerifiedClaim):
            continue
        claim = get_artifact(conn, verified.claim_id)
        assert isinstance(claim, Claim)
        source = get_artifact(conn, claim.source_record_id)
        rows.append(
            {
                "id": str(verified.id),
                "statement": claim.statement,
                "quote": claim.quoted_span,
                "verdict": verified.verdict,
                "entailment": verified.entailment,
                "source": getattr(source, "source", ""),
                "url": getattr(source, "url", None),
            }
        )
    return rows


def _plan_detail(conn: Connection, plan: TradePlan) -> dict[str, Any]:
    decision_id = conn.execute(
        text(
            "SELECT id FROM artifacts WHERE kind = 'risk_decision' AND payload->>'plan_id' = :p "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"p": str(plan.id)},
    ).scalar()
    decision = get_artifact(conn, decision_id) if decision_id else None
    return {
        "plan": _dump(plan),
        "decision": _dump(decision) if isinstance(decision, RiskDecision) else None,
        "scores": _scores_for(conn, [plan.id]),
    }


class LinkIn(BaseModel):
    plan_id: UUID | None = None
    thesis_id: UUID | None = None


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    thread_id: UUID | None = None
    subject_id: UUID | None = None


def views_router(engine: Engine) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["dashboard"])

    @router.get("/theses")
    def theses() -> list[dict[str, Any]]:
        with engine.connect() as conn:
            all_versions = [t for t in load_kind(conn, "thesis") if isinstance(t, Thesis)]
            superseded = {t.previous_id for t in all_versions if t.previous_id}
            roots = thesis_roots(all_versions)
            latest = [t for t in all_versions if t.id not in superseded]
            scores = {
                (row["subject_id"], row["horizon"]): row
                for row in _scores_for(conn, [r.id for r in roots.values()])
            }
        result = []
        for thesis in sorted(latest, key=lambda t: t.created_at, reverse=True):
            root = roots.get(thesis.id)
            outcome = {
                h: scores.get((str(root.id), h), {}).get("return_pct") if root else None
                for h in ("1d", "5d", "20d")
            }
            result.append(
                {
                    "id": str(thesis.id),
                    "instrument": thesis.primary_instrument,
                    "direction": thesis.direction,
                    "statement": thesis.statement,
                    "origin": thesis.origin,
                    "state": thesis.state,
                    "status": thesis.status.value,
                    "conviction": thesis.conviction,
                    "created_at": thesis.created_at.isoformat(),
                    "review_by": thesis.review_by.isoformat() if thesis.review_by else None,
                    "change_note": thesis.change_note,
                    "returns": outcome,
                }
            )
        return result

    @router.get("/theses/{thesis_id}")
    def thesis_detail(thesis_id: UUID) -> dict[str, Any]:
        with engine.connect() as conn:
            thesis = _load(conn, thesis_id)
            if not isinstance(thesis, Thesis):
                raise HTTPException(status_code=404, detail="not a thesis")
            versions = _chain(conn, thesis)
            ids = [str(v.id) for v in versions]
            verdicts = []
            for verdict_id in conn.execute(
                text(
                    "SELECT id FROM artifacts WHERE kind = 'debate_verdict' "
                    "AND payload->>'thesis_id' = ANY(:ids) ORDER BY created_at"
                ),
                {"ids": ids},
            ).scalars():
                verdict = get_artifact(conn, verdict_id)
                assert isinstance(verdict, DebateVerdict)
                views = [get_artifact(conn, v) for v in verdict.view_ids]
                verdicts.append(
                    {
                        "verdict": _dump(verdict),
                        "views": [_dump(v) for v in views if isinstance(v, AnalystView)],
                    }
                )
            plans = [
                _plan_detail(conn, plan)
                for plan in (
                    get_artifact(conn, plan_id)
                    for plan_id in conn.execute(
                        text(
                            "SELECT id FROM artifacts WHERE kind = 'trade_plan' "
                            "AND payload->>'thesis_id' = ANY(:ids) ORDER BY created_at"
                        ),
                        {"ids": ids},
                    ).scalars()
                )
                if isinstance(plan, TradePlan)
            ]
            latest = versions[-1]
            return {
                "thesis": _dump(latest),
                "versions": [
                    {
                        "id": str(v.id),
                        "created_at": v.created_at.isoformat(),
                        "state": v.state,
                        "conviction": v.conviction,
                        "change_note": v.change_note,
                        "produced_by": v.produced_by,
                    }
                    for v in versions
                ],
                "evidence": _claim_rows(conn, latest.evidence),
                "verdicts": verdicts,
                "plans": plans,
                "scores": _scores_for(conn, [v.id for v in versions]),
            }

    @router.get("/plans/{plan_id}")
    def plan_detail(plan_id: UUID) -> dict[str, Any]:
        with engine.connect() as conn:
            plan = _load(conn, plan_id)
            if not isinstance(plan, TradePlan):
                raise HTTPException(status_code=404, detail="not a trade plan")
            return _plan_detail(conn, plan)

    @router.get("/book")
    def book() -> dict[str, Any]:
        with engine.connect() as conn:
            positions: list[Position] = list(latest_positions(conn).values())
            exit_scores = {
                row["subject_id"]: row
                for row in _rows(
                    conn,
                    "SELECT payload->>'subject_id' AS subject_id, "
                    "(payload->>'return_pct')::float AS return_pct, "
                    "(payload->>'r_multiple')::float AS r_multiple, "
                    "payload->'adherence' AS adherence "
                    "FROM artifacts WHERE kind = 'score' AND payload->>'horizon' = 'exit'",
                )
            }
            fills = _rows(
                conn,
                "SELECT id, payload->>'account_ref' AS account_ref, payload->>'symbol' AS symbol, "
                "payload->>'contract' AS contract, payload->>'side' AS side, "
                "(payload->>'quantity')::numeric AS quantity, "
                "(payload->>'price')::numeric AS price, "
                "(payload->>'fees')::numeric AS fees, payload->>'executed_at' AS executed_at "
                "FROM artifacts WHERE kind = 'fill' ORDER BY payload->>'executed_at' DESC LIMIT 50",
            )
            holdings = [
                {
                    "account_ref": s["account_ref"],
                    "source": s["source"],
                    "as_of": s["as_of"],
                    "net_liquidation": s["net_liquidation"],
                    "cash": s["cash"],
                    "positions": s["positions"],
                }
                for s in latest_snapshots(conn)
            ]
            rating_rows = ratings(conn)
        return _jsonable(
            {
                "holdings": holdings,
                "ratings": rating_rows,
                "positions": [
                    {**_dump(p), "exit": exit_scores.get(str(p.id))}
                    for p in sorted(positions, key=lambda p: p.opened_at, reverse=True)
                ],
                "fills": fills,
            }
        )

    @router.get("/scores")
    def scores(horizon: Literal["1d", "5d", "20d", "exit"] = "5d") -> dict[str, Any]:
        with engine.connect() as conn:
            report = rollups(conn, horizon)
            report["recent"] = _rows(
                conn,
                "SELECT s.payload->>'subject_id' AS subject_id, "
                "s.payload->>'subject_kind' AS kind, s.payload->'attribution' AS attribution, "
                "(s.payload->>'return_pct')::float AS return_pct, "
                "(s.payload->>'r_multiple')::float AS r_multiple, "
                "(s.payload->>'hit')::boolean AS hit, s.payload->>'first_hit' AS first_hit, "
                "s.created_at FROM artifacts s WHERE s.kind = 'score' "
                "AND s.payload->>'horizon' = :h ORDER BY s.created_at DESC LIMIT 60",
                h=horizon,
            )
        return report

    @router.post("/positions/{position_id}/link")
    def link(position_id: UUID, body: LinkIn) -> dict[str, Any]:
        try:
            with engine.begin() as conn:
                position = relink(conn, position_id, body.plan_id, body.thesis_id)
                append_artifact(conn, position)
        except (ValueError, ArtifactNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"position_id": str(position.id), "link_status": position.link_status}

    @router.post("/chat", status_code=201)
    def post_chat(body: ChatIn) -> dict[str, Any]:
        try:
            message = chat_desk.submit(engine, body.text, body.thread_id, body.subject_id)
        except ArtifactNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with engine.begin() as conn:
            queue.enqueue(conn, "chat", {"message_id": str(message.id)})
        return {"message_id": str(message.id), "thread_id": str(message.thread_id)}

    @router.get("/chat/threads")
    def threads() -> list[dict[str, Any]]:
        with engine.connect() as conn:
            return _rows(
                conn,
                "SELECT payload->>'thread_id' AS thread_id, min(created_at) AS started_at, "
                "max(created_at) AS last_at, "
                "(array_agg(payload->>'text' ORDER BY created_at))[1] AS first_text, "
                "count(*) FILTER (WHERE kind = 'chat_message') AS messages, "
                "count(*) FILTER (WHERE kind = 'chat_reply') AS replies FROM artifacts "
                "WHERE kind IN ('chat_message', 'chat_reply') GROUP BY 1 "
                "ORDER BY max(created_at) DESC LIMIT 30",
            )

    @router.get("/chat/threads/{thread_id}")
    def thread(thread_id: UUID) -> dict[str, Any]:
        with engine.connect() as conn:
            items = chat_desk.thread(conn, thread_id)
        answered = {i.message_id for i in items if isinstance(i, ChatReply)}
        entries = []
        for item in items:
            if isinstance(item, ChatMessage):
                entries.append(
                    {
                        "id": str(item.id),
                        "role": "jon",
                        "text": item.text,
                        "mode": item.mode,
                        "subject_id": str(item.subject_id) if item.subject_id else None,
                        "created_at": item.created_at.isoformat(),
                        "answered": item.id in answered,
                    }
                )
            elif isinstance(item, ChatReply):
                entries.append(
                    {
                        "id": str(item.id),
                        "role": "desk",
                        "text": item.text,
                        "status": item.status.value,
                        "error": item.error,
                        "cited": [str(c) for c in item.cited_artifacts],
                        "created_at": item.created_at.isoformat(),
                    }
                )
        return {
            "thread_id": str(thread_id),
            "items": entries,
            "pending": any(e["role"] == "jon" and not e["answered"] for e in entries),
        }

    return router
