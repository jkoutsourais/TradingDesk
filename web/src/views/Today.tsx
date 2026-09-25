import { AlertIcon, FileIcon, LightBulbIcon, ShieldCheckIcon, ShieldXIcon } from "@primer/octicons-react";

import type { Brief, PlanRow, RatingRow, ThesisRow, Today as TodayData } from "../api";
import { money, num, RATING, time } from "../format";
import { useApi } from "../hooks";

const KINDS = ["brief", "thesis", "debate_verdict", "trade_plan", "risk_decision", "holding_rating"];

function verdictLabel(verdict: string | null) {
  if (!verdict) return <span className="label">not debated</span>;
  const tone = verdict === "pursue" ? "success" : verdict === "reject" ? "danger" : "attention";
  return <span className={`label ${tone}`}>{verdict}</span>;
}

function ThesisTable({ theses }: { theses: ThesisRow[] }) {
  if (!theses.length) return <div className="empty">No open theses.</div>;
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Thesis</th>
            <th>Origin</th>
            <th>Verdict</th>
            <th className="num">Conv.</th>
            <th className="num">Hard</th>
            <th className="num">Warning</th>
            <th>Review by</th>
          </tr>
        </thead>
        <tbody>
          {theses.map((t) => (
            <tr key={t.id}>
              <td>
                <a href={`#/theses/${t.id}`}>
                  <b>{t.instrument}</b> {t.direction}
                </a>
                {t.in_warning_zone && (
                  <span className="label attention" style={{ marginLeft: 6 }}>
                    <AlertIcon size={12} /> warning zone
                  </span>
                )}
                <div className="muted">{t.statement}</div>
              </td>
              <td>{t.origin}</td>
              <td>{verdictLabel(t.verdict)}</td>
              <td className="num">{t.conviction ?? "-"}</td>
              <td className="num">{num(t.hard)}</td>
              <td className="num">{num(t.warning)}</td>
              <td className="mono">{t.review_by ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PlanTable({ plans, vetoed }: { plans: PlanRow[]; vetoed: boolean }) {
  if (!plans.length) return <div className="empty">{vetoed ? "No vetoes." : "No pending plans."}</div>;
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Plan</th>
            <th>Decision</th>
            {vetoed ? <th>Reason</th> : <th className="num">Size</th>}
            {!vetoed && <th className="num">Max loss</th>}
            {!vetoed && <th className="num">Cost</th>}
            <th>When</th>
          </tr>
        </thead>
        <tbody>
          {plans.map((p) => (
            <tr key={p.id}>
              <td>
                <a href={`#/plans/${p.id}`}>
                  <b>{p.subject}</b> {p.structure.replace("_", " ")}
                </a>
                <div className="muted mono">{p.instrument}</div>
                {p.risk_note && <div className="muted">{p.risk_note}</div>}
              </td>
              <td>
                <span className={`label ${p.decision === "vetoed" ? "danger" : "success"}`}>
                  {p.decision ?? "pending"}
                </span>
              </td>
              {vetoed ? (
                <td>{p.veto}</td>
              ) : (
                <td className="num">{p.size ?? "-"}</td>
              )}
              {!vetoed && <td className="num">{money(p.max_loss)}</td>}
              {!vetoed && (
                <td className="num">
                  {money(p.cost)}
                  {p.funding_needed ? <div className="muted">needs {money(p.funding_needed)}</div> : null}
                </td>
              )}
              <td>{time(p.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RatingTable({ ratings }: { ratings: RatingRow[] }) {
  if (!ratings.length) return <div className="empty">No ratings yet.</div>;
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Holding</th>
            <th>Rating</th>
            <th>Confidence</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {ratings.map((r) => (
            <tr key={r.id}>
              <td>
                <b>{r.subject}</b>
              </td>
              <td>
                {r.status === "failed" ? <span className="dot fail" /> : null} {RATING[r.rating] ?? r.rating}
                {r.previous && r.previous !== r.rating && (
                  <div className="muted">was {RATING[r.previous] ?? r.previous}</div>
                )}
                {r.judged && <div className="muted">judge: {RATING[r.judged] ?? r.judged}</div>}
              </td>
              <td>{r.confidence}</td>
              <td>
                {r.status === "failed" ? <span className="muted">{r.error}</span> : r.reason}
                <div className="muted">{r.action}</div>
                {r.flags?.map((f) => (
                  <div key={f.code}>
                    <span className="label attention">{f.detail}</span>
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function BriefCard({ brief }: { brief: Brief | null }) {
  return (
    <div className="box">
      <div className="box-header">
        <FileIcon /> {brief ? (brief.kind === "weekend" ? "Weekend briefing" : "Morning briefing") : "Briefing"}
        <span className="spacer" />
        {brief && <span className="muted">{time(brief.created_at)}</span>}
        {brief?.status === "failed" && <span className="label attention">data-only</span>}
      </div>
      {brief ? (
        <div className="grid-2" style={{ gap: 0 }}>
          {brief.sections.map((section) => (
            <div key={section.title} style={{ borderBottom: "1px solid var(--borderColor-muted)" }}>
              <div className="box-body" style={{ fontWeight: 600 }}>{section.title}</div>
              <ul className="lines">
                {section.lines.map((line, i) => (
                  <li key={i}>{line}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty">No briefing yet.</div>
      )}
    </div>
  );
}

/** A briefing a push linked to; "latest" and unknown ids fall back to Today. */
export function BriefView({ id }: { id: string }) {
  const { data, error } = useApi<Brief>(`/api/briefs/${id}`);
  if (error) return <Today />;
  return <div className="content">{data ? <BriefCard brief={data} /> : <div className="empty">Loading</div>}</div>;
}

export function Today() {
  const { data, error } = useApi<TodayData>("/api/today", KINDS);
  if (error) return <div className="content"><div className="box"><div className="empty">{error}</div></div></div>;
  if (!data) return <div className="content"><div className="empty">Loading</div></div>;
  return (
    <div className="content">
      <BriefCard brief={data.brief} />
      <div className="box">
        <div className="box-header">
          <LightBulbIcon /> Open theses <span className="label">{data.theses.length}</span>
        </div>
        <ThesisTable theses={data.theses} />
      </div>
      <div className="grid-2">
        <div className="box">
          <div className="box-header">
            <ShieldCheckIcon /> Plans, last three days
          </div>
          <PlanTable plans={data.plans} vetoed={false} />
        </div>
        <div className="box">
          <div className="box-header">
            <ShieldXIcon /> Vetoed
          </div>
          <PlanTable plans={data.vetoes} vetoed />
        </div>
      </div>
      <div className="box">
        <div className="box-header">Holdings ratings</div>
        <RatingTable ratings={data.ratings} />
      </div>
    </div>
  );
}
