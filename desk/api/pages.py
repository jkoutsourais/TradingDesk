"""Server-rendered pages that pushes link to: a brief and an alert.

Everything shown comes from stored artifacts; the pages compute nothing. Access control
is Cloudflare Access in front of the site.
"""

import html
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import Engine, text

from desk.artifacts.brief import Brief, FactSnapshot, TriageLabel
from desk.artifacts.store import ArtifactNotFoundError, get_artifact
from desk.artifacts.trigger import Trigger
from desk.config import load_schedule

STYLE = """
:root { --bg:#fff; --bg-subtle:#f6f8fa; --fg:#1f2328; --muted:#59636e; --border:#d1d9e0;
  --accent:#0969da; --success:#1a7f37; --danger:#d1242f; --attention:#9a6700; }
@media (prefers-color-scheme: dark) { :root { --bg:#0d1117; --bg-subtle:#151b23; --fg:#f0f6fc;
  --muted:#9198a1; --border:#3d444d; --accent:#4493f8; --success:#3fb950; --danger:#f85149;
  --attention:#d29922; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:13px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",
    Helvetica,Arial,sans-serif; }
header { padding:12px 16px; border-bottom:1px solid var(--border); background:var(--bg-subtle);
  display:flex; flex-wrap:wrap; gap:4px 16px; align-items:baseline; }
header h1 { font-size:14px; font-weight:600; margin:0; }
header a { color:var(--accent); text-decoration:none; }
.muted { color:var(--muted); }
main { padding:16px; display:grid; gap:16px; max-width:880px; }
section { border:1px solid var(--border); border-radius:6px; overflow:hidden; }
section h2 { font-size:13px; font-weight:600; margin:0; padding:8px 12px;
  background:var(--bg-subtle); border-bottom:1px solid var(--border); }
ul { margin:0; padding:8px 12px 8px 28px; }
li { margin:4px 0; }
.banner { padding:8px 12px; border:1px solid var(--attention); border-radius:6px;
  color:var(--attention); }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; }
th, td { padding:4px 12px; text-align:left; white-space:nowrap;
  border-top:1px solid var(--border); }
th { color:var(--muted); font-weight:500; }
td.num { text-align:right; font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; }
.pill { display:inline-block; padding:0 8px; border:1px solid var(--border); border-radius:12px; }
"""


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _header(title: str, meta: str) -> str:
    return (
        f'<header><h1>{_esc(title)}</h1><span class="muted">{meta}</span>'
        '<a href="/dev">dev status</a><a href="/briefs/latest">latest brief</a></header>'
    )


def pages_router(engine: Engine) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    tz = load_schedule().tz

    def load(artifact_id: UUID) -> Any:
        with engine.connect() as conn:
            try:
                return get_artifact(conn, artifact_id)
            except ArtifactNotFoundError as exc:
                raise HTTPException(status_code=404, detail="not found") from exc

    @router.get("/briefs/latest")
    def latest_brief() -> RedirectResponse:
        with engine.connect() as conn:
            brief_id = conn.execute(
                text(
                    "SELECT id FROM artifacts WHERE kind = 'brief' ORDER BY created_at DESC LIMIT 1"
                )
            ).scalar()
        if brief_id is None:
            raise HTTPException(status_code=404, detail="no briefs yet")
        return RedirectResponse(f"/briefs/{brief_id}", status_code=302)

    @router.get("/briefs/{brief_id}", response_class=HTMLResponse)
    def brief_page(brief_id: UUID) -> str:
        brief = load(brief_id)
        if not isinstance(brief, Brief):
            raise HTTPException(status_code=404, detail="not a brief")
        snapshot = load(brief.fact_snapshot_id)
        assert isinstance(snapshot, FactSnapshot)
        when = brief.created_at.astimezone(tz)
        title = "Weekend briefing" if brief.brief_kind == "weekend" else "Morning briefing"
        parts = [_header(title, f"{when:%A %b} {when.day}, {when:%H:%M} ET")]
        parts.append("<main>")
        if brief.status.value == "failed":
            parts.append(
                '<div class="banner">The writer model did not produce a valid brief, so this is '
                "the data-only version. Every figure below still comes from stored data.</div>"
            )
        for section in brief.sections:
            items = "".join(f"<li>{_esc(line)}</li>" for line in section.lines) or (
                '<li class="muted">Nothing to report.</li>'
            )
            parts.append(f"<section><h2>{_esc(section.title)}</h2><ul>{items}</ul></section>")
        facts = {fact.id: fact for fact in snapshot.facts}
        rows = []
        for fact in snapshot.facts:
            if fact.id.endswith(".value"):
                key = fact.id.rsplit(".", 1)[0]
                cells = [
                    facts.get(f"{key}.{name}")
                    for name in ("price", "change", "value", "pnl", "pnl_pct")
                ]
                rows.append(
                    f"<tr><td>{_esc(key)}</td>"
                    + "".join(
                        f'<td class="num">{_esc(c.display if c else "-")}</td>' for c in cells
                    )
                    + "</tr>"
                )
        if rows:
            parts.append(
                '<section><h2>Holdings detail</h2><div class="scroll"><table><tr><th>Symbol</th>'
                '<th class="num">Price</th><th class="num">Change</th><th class="num">Value</th>'
                '<th class="num">P&amp;L</th><th class="num">P&amp;L %</th></tr>'
                + "".join(rows)
                + "</table></div></section>"
            )
        parts.append(
            f'<p class="muted">Model {_esc(brief.model)}, prompt {_esc(brief.prompt_version)}, '
            f"{len(snapshot.facts)} facts, covers {brief.covers_from.astimezone(tz):%a %H:%M} "
            f"to {brief.covers_to.astimezone(tz):%a %H:%M} ET.</p></main>"
        )
        return _page(title, "".join(parts))

    @router.get("/alerts/{trigger_id}", response_class=HTMLResponse)
    def alert_page(trigger_id: UUID) -> str:
        trigger = load(trigger_id)
        if not isinstance(trigger, Trigger):
            raise HTTPException(status_code=404, detail="not an alert")
        with engine.connect() as conn:
            label_id = conn.execute(
                text(
                    "SELECT id FROM artifacts WHERE kind = 'triage_label' "
                    "AND payload->>'subject_id' = :id ORDER BY created_at DESC LIMIT 1"
                ),
                {"id": str(trigger_id)},
            ).scalar()
        label = load(label_id) if label_id else None
        when = trigger.created_at.astimezone(tz)
        observed = "".join(
            f'<tr><td>{_esc(o.name)}</td><td class="num">{_esc(o.value)}</td>'
            f'<td>{_esc(o.unit)}</td><td class="muted">{_esc(o.source_ref)}</td></tr>'
            for o in trigger.observed
        )
        body = [
            _header(
                "Urgent alert" if trigger.urgent else "Watch hit",
                f"{when:%a %b} {when.day}, {when:%H:%M} ET",
            ),
            "<main>",
            f"<section><h2>{_esc(trigger.instrument)}</h2><ul><li>{_esc(trigger.summary)}</li>"
            f'<li class="muted">rule {_esc(trigger.rule_id)}, tier {_esc(trigger.tier)}, '
            f"score {trigger.importance:.2f}</li></ul></section>",
        ]
        if isinstance(label, TriageLabel):
            body.append(
                f'<section><h2>Triage</h2><ul><li><span class="pill">{_esc(label.label)}'
                f"</span> {_esc(label.reason)}</li></ul></section>"
            )
        if observed:
            body.append(
                '<section><h2>Data</h2><div class="scroll"><table><tr><th>Measure</th>'
                '<th class="num">Value</th><th>Unit</th><th>Source</th></tr>'
                f"{observed}</table></div></section>"
            )
        body.append("</main>")
        return _page(trigger.summary, "".join(body))

    return router
