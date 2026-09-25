import { GraphIcon, HistoryIcon, LawIcon, ShieldCheckIcon } from "@primer/octicons-react";

import { ExplainButton } from "../components/Explain";
import { type Point, Points } from "../components/Points";
import { firstHit, num, origin, scoredTitle, time } from "../format";
import { useApi } from "../hooks";

interface ThesisListRow {
  id: string;
  instrument: string;
  direction: string;
  statement: string;
  origin: string;
  state: string;
  conviction: number | null;
  created_at: string;
  review_by: string | null;
  change_note: string | null;
  returns: Record<string, number | null>;
}

interface Condition {
  instrument: string;
  measure: string;
  operator: string;
  level: string;
  level_ref: string;
}

interface ThesisDetail {
  thesis: {
    id: string;
    statement: string;
    origin: string;
    instruments: string[];
    direction: string;
    horizon: string | null;
    review_by: string | null;
    state: string;
    conviction: number | null;
    drivers: { statement: string; metric: string; source: string }[];
    catalysts: { name: string; on: string }[];
    entry_conditions: string[];
    invalidation: { warning: Condition; hard: Condition; time_limit: string | null } | null;
    model: string | null;
    prompt_version: string | null;
  };
  versions: { id: string; created_at: string; state: string; conviction: number | null; change_note: string | null; produced_by: string }[];
  evidence: { id: string; statement: string; quote: string; verdict: string; entailment: number | null; source: string; url: string | null }[];
  verdicts: {
    verdict: {
      id: string;
      verdict: string;
      confidence_label: string;
      conviction_label: string | null;
      rubric: { evidence_quality: string; rebuttal: string; risk_reward: string };
      bull: Point[];
      bear: Point[];
      rebuttal: Point[];
      reasons: Point[];
      dissent: string;
      created_at: string;
    };
    views: { id: string; persona: string; stance: string; confidence_label: string; points: Point[] }[];
  }[];
  plans: { plan: { id: string; structure: string; instrument: string; created_at: string }; decision: { decision: string; size: number; max_loss: string; veto_reasons: string[] } | null }[];
  scores: { subject_id: string; kind: string; horizon: string; return_pct: number; r_multiple: number | null; first_hit: string | null; hit: boolean | null }[];
}

const KINDS = ["thesis", "debate_verdict", "trade_plan", "risk_decision", "score"];

function Ret({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="muted">-</span>;
  return <span className={value >= 0 ? "pos" : "neg"}>{num(value)}%</span>;
}

function stateLabel(state: string) {
  const tone = state === "active" ? "accent" : state === "invalidated" ? "danger" : state === "closed" ? "" : "attention";
  return <span className={`label ${tone}`}>{state}</span>;
}

export function ThesisList() {
  const { data, error } = useApi<ThesisListRow[]>("/api/theses", KINDS);
  if (error && !data) return <div className="content"><div className="empty">{error}</div></div>;
  if (!data) return <div className="content"><div className="empty">Loading</div></div>;
  return (
    <div className="content">
      <div className="box">
        <div className="box-header">
          Theses <span className="label">{data.length}</span>
        </div>
        {data.length === 0 ? (
          <div className="empty">No theses yet.</div>
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Thesis</th>
                  <th>Source</th>
                  <th>State</th>
                  <th className="num">Conviction</th>
                  <th className="num">1d</th>
                  <th className="num">5d</th>
                  <th className="num">20d</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {data.map((t) => (
                  <tr key={t.id}>
                    <td>
                      <a href={`#/theses/${t.id}`}>
                        <b>{t.instrument}</b> {t.direction}
                      </a>
                      <div className="muted">{t.statement}</div>
                    </td>
                    <td>{origin(t.origin)}</td>
                    <td>{stateLabel(t.state)}</td>
                    <td className="num">{t.conviction ? `${t.conviction}/5` : "-"}</td>
                    <td className="num"><Ret value={t.returns["1d"]} /></td>
                    <td className="num"><Ret value={t.returns["5d"]} /></td>
                    <td className="num"><Ret value={t.returns["20d"]} /></td>
                    <td>{time(t.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

export function ThesisView({ id }: { id: string }) {
  const { data, error } = useApi<ThesisDetail>(`/api/theses/${id}`, KINDS);
  if (error && !data) return <div className="content"><div className="empty">{error}</div></div>;
  if (!data) return <div className="content"><div className="empty">Loading</div></div>;
  const t = data.thesis;
  const inv = t.invalidation;
  return (
    <div className="content">
      <div className="box">
        <div className="box-header">
          <b>{t.instruments.join(", ")}</b> {t.direction} {stateLabel(t.state)}
          <span className="spacer" />
          <ExplainButton id={t.id} />
          <a className="btn" href={`#/trace/${t.id}`}>
            <GraphIcon size={14} /> Trace
          </a>
        </div>
        <div className="box-body">{t.statement}</div>
        <div className="scroll">
          <table>
            <tbody>
              <tr><td className="muted">Source</td><td>{origin(t.origin)}</td><td className="muted">Conviction</td><td className="num" style={{ textAlign: "left" }}>{t.conviction ? `${t.conviction}/5` : "-"}</td></tr>
              <tr><td className="muted">Horizon</td><td>{t.horizon ?? "-"}</td><td className="muted">Review by</td><td className="mono">{t.review_by ?? "-"}</td></tr>
              {inv && (
                <tr>
                  <td className="muted">Invalidated if</td>
                  <td className="mono">{inv.hard.instrument} daily close {inv.hard.operator} {inv.hard.level}</td>
                  <td className="muted">Warning if</td>
                  <td className="mono">{inv.warning.instrument} {inv.warning.operator} {inv.warning.level}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <ul className="lines">
          {t.drivers.map((d, i) => (
            <li key={i}>
              {d.statement} <span className="muted">({d.metric}, {d.source})</span>
            </li>
          ))}
          {t.entry_conditions.map((e, i) => <li key={`e${i}`}>Entry: {e}</li>)}
          {t.catalysts.map((c, i) => <li key={`c${i}`}>Catalyst: {c.name} <span className="mono muted">{c.on}</span></li>)}
        </ul>
      </div>
      <div className="box">
        <div className="box-header">Evidence <span className="label">{data.evidence.length}</span></div>
        {data.evidence.length === 0 ? (
          <div className="empty">No verified claims yet.</div>
        ) : (
          <ul className="lines">
            {data.evidence.map((e) => (
              <li key={e.id}>
                {e.statement}{" "}
                {e.verdict === "corrected" && <span className="label attention">corrected</span>}
                <div className="muted" style={{ fontSize: 12 }}>
                  "{e.quote}" · {e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.source}</a> : e.source}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
      {data.verdicts.map(({ verdict, views }) => (
        <div className="box" key={verdict.id}>
          <div className="box-header">
            <LawIcon /> Debate
            <span className={`label ${verdict.verdict === "pursue" ? "success" : verdict.verdict === "reject" ? "danger" : "attention"}`}>{verdict.verdict}</span>
            <span className="muted">
              {verdict.confidence_label} confidence · evidence {verdict.rubric.evidence_quality}, rebuttal {verdict.rubric.rebuttal}, risk/reward {verdict.rubric.risk_reward}
            </span>
            <span className="spacer" />
            <span className="muted">{time(verdict.created_at)}</span>
          </div>
          <div className="grid-2" style={{ gap: 0 }}>
            <div><div className="box-body"><b>Bull</b></div><Points points={verdict.bull} /></div>
            <div><div className="box-body"><b>Bear</b></div><Points points={verdict.bear} /></div>
            <div><div className="box-body"><b>Bull rebuttal</b></div><Points points={verdict.rebuttal} /></div>
            <div>
              <div className="box-body"><b>Judge</b></div>
              <Points points={verdict.reasons} />
              <div className="box-body muted">Dissent: {verdict.dissent}</div>
            </div>
          </div>
          {views.map((v) => (
            <details key={v.id} className="box-body">
              <summary>
                {v.persona} <span className="muted">({v.stance}, {v.confidence_label})</span>
              </summary>
              <Points points={v.points} />
            </details>
          ))}
        </div>
      ))}
      <div className="grid-2">
        <div className="box">
          <div className="box-header"><ShieldCheckIcon /> Plans</div>
          {data.plans.length === 0 ? (
            <div className="empty">No plans.</div>
          ) : (
            <ul className="lines">
              {data.plans.map(({ plan, decision }) => (
                <li key={plan.id}>
                  <a href={`#/plans/${plan.id}`}>{plan.structure.replace("_", " ")} {plan.instrument}</a>{" "}
                  <span className={`label ${decision?.decision === "vetoed" ? "danger" : "success"}`}>{decision?.decision ?? "pending"}</span>{" "}
                  <span className="muted">{decision?.decision === "vetoed" ? decision.veto_reasons[0] : decision ? `size ${decision.size}, max loss $${num(Number(decision.max_loss))}` : ""}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="box">
          <div className="box-header"><HistoryIcon /> History</div>
          <ul className="lines">
            {data.versions.map((v) => (
              <li key={v.id}>
                <a href={`#/trace/${v.id}`} className="mono">{time(v.created_at)}</a> {stateLabel(v.state)} <span className="muted">{v.change_note ?? "first version"}</span>
              </li>
            ))}
          </ul>
          {data.scores.length > 0 && (
            <div className="scroll">
              <table>
                <thead><tr><th>Call</th><th>After</th><th className="num">Return</th><th className="num">R</th><th>Reached</th></tr></thead>
                <tbody>
                  {data.scores.map((s) => (
                    <tr key={`${s.subject_id}-${s.horizon}`}>
                      <td>{scoredTitle(s.kind)}</td>
                      <td>{s.horizon}</td>
                      <td className="num"><Ret value={s.return_pct} /></td>
                      <td className="num">{num(s.r_multiple)}</td>
                      <td>{firstHit(s.first_hit)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
