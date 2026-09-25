"""Scores and positions: rollups, the position book and Jon's link confirmations.

GET  /scores?horizon=5d            rollups page (1d, 5d, 20d or exit)
GET  /scores.json?horizon=5d       the same data as JSON
GET  /positions.json               latest version of every position
POST /positions/{id}/link          {"plan_id": ..., "thesis_id": ...} confirms a link
"""

import html
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import Engine

from desk.api.pages import STYLE
from desk.artifacts.store import ArtifactNotFoundError, append_artifact
from desk.scoring.report import DIMENSIONS, rollups
from desk.scoring.run import latest_positions, relink

Horizon = Literal["1d", "5d", "20d", "exit"]


class LinkIn(BaseModel):
    plan_id: UUID | None = None
    thesis_id: UUID | None = None


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    return "-" if value is None else f"{value:,.{digits}f}{suffix}"


def _pct(share: float | None) -> float | None:
    return None if share is None else share * 100


def scores_router(engine: Engine) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/scores.json")
    def scores_json(horizon: Horizon = "5d") -> dict[str, Any]:
        with engine.connect() as conn:
            return rollups(conn, horizon)

    @router.get("/scores", response_class=HTMLResponse)
    def scores_page(horizon: Horizon = "5d") -> str:
        with engine.connect() as conn:
            data = rollups(conn, horizon)
        tabs = " ".join(
            f'<a href="/scores?horizon={h}">{"<b>" + h + "</b>" if h == horizon else h}</a>'
            for h in ("1d", "5d", "20d", "exit")
        )
        parts = [
            "<header><h1>Scores</h1>"
            f'<span class="muted">{data["scored"]} scores at {horizon}</span>'
            f'{tabs}<a href="/dev">dev status</a></header><main>'
        ]
        for dimension in DIMENSIONS:
            groups = data["dimensions"][dimension]
            if not groups:
                continue
            rows = "".join(
                f'<tr><td>{html.escape(g["value"])}</td><td class="num">{g["count"]}</td>'
                f'<td class="num">{_fmt(_pct(g["hit_rate"]), 0, "%")}</td>'
                f'<td class="num">{_fmt(g["avg_return"], 2, "%")}</td>'
                f'<td class="num">{_fmt(g["avg_r"])}</td></tr>'
                for g in groups
            )
            parts.append(
                f"<section><h2>By {html.escape(dimension.replace('_', ' '))}</h2>"
                '<div class="scroll"><table><tr><th>Value</th><th class="num">Count</th>'
                '<th class="num">Hit rate</th><th class="num">Avg return</th>'
                f'<th class="num">Avg R</th></tr>{rows}</table></div></section>'
            )
        if len(parts) == 1:
            parts.append('<p class="muted">Nothing scored at this horizon yet.</p>')
        parts.append("</main>")
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Scores</title><style>{STYLE}</style></head><body>{''.join(parts)}</body></html>"
        )

    @router.get("/positions.json")
    def positions_json() -> list[dict[str, Any]]:
        with engine.connect() as conn:
            return [p.model_dump(mode="json") for p in latest_positions(conn).values()]

    @router.post("/positions/{position_id}/link")
    def link(position_id: UUID, body: LinkIn) -> dict[str, Any]:
        try:
            with engine.begin() as conn:
                position = relink(conn, position_id, body.plan_id, body.thesis_id)
                append_artifact(conn, position)
        except (ValueError, ArtifactNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"position_id": str(position.id), "link_status": position.link_status}

    return router
