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
from desk.artifacts.raw_record import RawRecord
from desk.artifacts.research import Claim, Dossier, VerifiedClaim
from desk.artifacts.store import ArtifactNotFoundError, get_artifact
from desk.artifacts.thesis import Thesis
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

    @router.get("/dossiers/{dossier_id}", response_class=HTMLResponse)
    def dossier_page(dossier_id: UUID) -> str:
        dossier = load(dossier_id)
        if not isinstance(dossier, Dossier):
            raise HTTPException(status_code=404, detail="not a dossier")
        with engine.connect() as conn:
            verdict_ids = {
                claim_id: verdict_id
                for claim_id, verdict_id in conn.execute(
                    text(
                        "SELECT DISTINCT ON (payload->>'claim_id') payload->>'claim_id', id "
                        "FROM artifacts WHERE kind = 'verified_claim' AND status = 'ok' "
                        "AND payload->>'claim_id' = ANY(:ids) "
                        "ORDER BY payload->>'claim_id', created_at DESC"
                    ),
                    {"ids": [str(c) for c in dossier.claim_ids]},
                )
            }
        when = dossier.created_at.astimezone(tz)
        kind = "Holding" if dossier.subject_kind == "holding" else "Play"
        body = [
            _header(f"{dossier.subject} research", f"{when:%a %b} {when.day}, {when:%H:%M} ET"),
            "<main>",
        ]
        if dossier.status.value == "failed":
            body.append(f'<div class="banner">Research failed: {_esc(dossier.error)}</div>')
        score = "" if dossier.selection_score is None else f", score {dossier.selection_score:.2f}"
        body.append(f'<p class="muted">{kind}{score}. Claims marked [cN] are listed below.</p>')
        for section in dossier.sections:
            body.append(
                f"<section><h2>{_esc(section.title)}</h2><ul><li>{_esc(section.text)}</li></ul>"
                "</section>"
            )
        rows = []
        for index, claim_id in enumerate(dossier.claim_ids, start=1):
            claim = load(claim_id)
            assert isinstance(claim, Claim)
            source = load(claim.source_record_id)
            verdict_id = verdict_ids.get(str(claim_id))
            verified = load(verdict_id) if verdict_id else None
            if isinstance(verified, VerifiedClaim):
                support = "-" if verified.entailment is None else f"{verified.entailment:.1f}"
                recomputed = "; ".join(
                    f"{r.stated} recomputed {r.value}{r.unit}" for r in verified.recomputed
                )
                check = (
                    f'<span class="pill">{_esc(verified.verdict)}</span> '
                    f'<span class="num">{support}</span> {_esc(recomputed)}'
                    f'<div class="muted">{_esc(verified.reason)}</div>'
                )
            else:
                check = '<span class="muted">pending</span>'
            link = (
                f'<a href="{_esc(source.url)}">{_esc(source.source)}</a>'
                if isinstance(source, RawRecord) and source.url
                else _esc(getattr(source, "source", "source"))
            )
            rows.append(
                f'<tr><td>c{index}</td><td class="wrap">{_esc(claim.statement)}'
                f'<div class="muted">&ldquo;{_esc(claim.quoted_span)}&rdquo; ({link})</div></td>'
                f'<td class="wrap">{check}</td></tr>'
            )
        if rows:
            body.append(
                '<section><h2>Claims</h2><div class="scroll"><table><tr><th>Ref</th>'
                "<th>Claim and quote</th><th>Fact-check</th></tr>"
                + "".join(rows)
                + "</table></div></section>"
            )
        body.append(
            f'<p class="muted">Model {_esc(dossier.model)}, prompt '
            f"{_esc(dossier.prompt_version)}.</p></main>"
        )
        return _page(f"{dossier.subject} research", "".join(body))

    @router.get("/theses/{thesis_id}", response_class=HTMLResponse)
    def thesis_page(thesis_id: UUID) -> str:
        thesis = load(thesis_id)
        if not isinstance(thesis, Thesis):
            raise HTTPException(status_code=404, detail="not a thesis")
        history = [thesis]
        while history[-1].previous_id is not None and len(history) < 50:
            earlier = load(history[-1].previous_id)
            assert isinstance(earlier, Thesis)
            history.append(earlier)
        when = thesis.created_at.astimezone(tz)
        title = f"{thesis.primary_instrument} {thesis.direction}"
        body = [_header(title, f"{when:%a %b} {when.day}, {when:%H:%M} ET"), "<main>"]
        if thesis.status.value == "failed":
            body.append(f'<div class="banner">Thesis writing failed: {_esc(thesis.error)}</div>')
        facts = [
            ("State", thesis.state),
            ("Origin", thesis.origin),
            ("Instruments", ", ".join(thesis.instruments)),
            ("Horizon", thesis.horizon or "-"),
            ("Review by", thesis.review_by or "-"),
            ("Conviction", f"{thesis.conviction}/5" if thesis.conviction else "-"),
        ]
        if thesis.invalidation is not None:
            inv = thesis.invalidation
            facts += [
                (
                    "Hard line",
                    f"{inv.hard.instrument} daily close {inv.hard.operator} {inv.hard.level} "
                    f"({inv.hard.level_ref})",
                ),
                (
                    "Warning",
                    f"{inv.warning.instrument} price {inv.warning.operator} "
                    f"{inv.warning.level} ({inv.warning.level_ref})",
                ),
                ("Time limit", inv.time_limit or "-"),
            ]
        rows = "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in facts)
        body.append(
            f"<section><h2>Thesis</h2><ul><li>{_esc(thesis.statement)}</li></ul>"
            f'<div class="scroll"><table>{rows}</table></div></section>'
        )
        drivers = "".join(
            f'<li>{_esc(d.statement)} <span class="muted">({_esc(d.metric)}, '
            f"{_esc(d.source)})</span></li>"
            for d in thesis.drivers
        )
        body.append(f"<section><h2>Drivers</h2><ul>{drivers}</ul></section>")
        if thesis.entry_conditions or thesis.catalysts:
            items = "".join(f"<li>{_esc(e)}</li>" for e in thesis.entry_conditions)
            items += "".join(
                f'<li>{_esc(c.name)} <span class="muted">{_esc(c.on)}</span></li>'
                for c in thesis.catalysts
            )
            body.append(f"<section><h2>Entry and catalysts</h2><ul>{items}</ul></section>")
        evidence = []
        for verified_id in thesis.evidence:
            verified = load(verified_id)
            if not isinstance(verified, VerifiedClaim):
                continue
            claim = load(verified.claim_id)
            statement = claim.statement if isinstance(claim, Claim) else "-"
            evidence.append(
                f'<li><span class="pill">{_esc(verified.verdict)}</span> {_esc(statement)}</li>'
            )
        body.append(
            "<section><h2>Evidence</h2><ul>"
            + ("".join(evidence) or '<li class="muted">No verified claims yet.</li>')
            + "</ul></section>"
        )
        versions = "".join(
            f'<tr><td><a href="/theses/{v.id}">{v.created_at.astimezone(tz):%b %d %H:%M}</a>'
            f"</td><td>{_esc(v.state)}</td><td>{_esc(v.change_note or v.produced_by)}</td></tr>"
            for v in history
        )
        body.append(
            '<section><h2>History</h2><div class="scroll"><table><tr><th>Version</th>'
            f"<th>State</th><th>Change</th></tr>{versions}</table></div></section>"
        )
        body.append(
            f'<p class="muted">Model {_esc(thesis.model)}, prompt '
            f"{_esc(thesis.prompt_version)}.</p></main>"
        )
        return _page(title, "".join(body))

    return router
