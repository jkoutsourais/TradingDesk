import { GraphIcon } from "@primer/octicons-react";

import { ExplainButton } from "../components/Explain";
import { money, num, time } from "../format";
import { useApi } from "../hooks";

interface PlanDetail {
  plan: {
    id: string;
    created_at: string;
    thesis_id: string | null;
    rating_id: string | null;
    account_ref: string;
    subject: string;
    instrument: string;
    structure: string;
    direction: string;
    legs: { action: string; kind: string; symbol: string; price: string; price_ref: string }[];
    entry: string;
    entry_ref: string;
    stop: string | null;
    stop_ref: string | null;
    target: string | null;
    target_ref: string | null;
    expiry: string | null;
    unit_cost: string;
    unit_max_loss: string;
    conviction: number;
    rationale: string;
    alternatives_rejected: string[];
  };
  decision: {
    decision: string;
    tier_pct: string;
    cap: string;
    requested_size: number;
    size: number;
    max_loss: string;
    cost: string;
    funding_needed: string;
    checks: { name: string; result: string; detail: string }[];
    veto_reasons: string[];
    risk_note: string | null;
  } | null;
}

export function PlanView({ id }: { id: string }) {
  const { data, error } = useApi<PlanDetail>(`/api/plans/${id}`, ["risk_decision"]);
  if (error && !data) return <div className="content"><div className="empty">{error}</div></div>;
  if (!data) return <div className="content"><div className="empty">Loading</div></div>;
  const { plan, decision } = data;
  const rows: [string, string][] = [
    ["Account", plan.account_ref],
    ["Entry", `${plan.subject} ${plan.entry}  (${plan.entry_ref})`],
    ["Stop", plan.stop ? `${plan.stop}  (${plan.stop_ref})` : "-"],
    ["Target", plan.target ? `${plan.target}  (${plan.target_ref})` : "-"],
    ["Expiry", plan.expiry ?? "-"],
    ["Cost per unit", money(Number(plan.unit_cost))],
    ["Loss per unit at the stop", money(Number(plan.unit_max_loss))],
    ["Conviction", `${plan.conviction}/5`],
  ];
  return (
    <div className="content">
      <div className="box">
        <div className="box-header">
          <b>{plan.subject}</b> {plan.structure.replace("_", " ")} <span className="mono muted">{plan.instrument}</span>
          {decision && <span className={`label ${decision.decision === "vetoed" ? "danger" : "success"}`}>{decision.decision}</span>}
          <span className="spacer" />
          <ExplainButton id={plan.id} />
          <a className="btn" href={`#/trace/${plan.id}`}><GraphIcon size={14} /> Trace</a>
        </div>
        <div className="box-body">
          {plan.rationale}
          <div className="muted">
            A plan for Jon to review; nothing places orders. {time(plan.created_at)} ·{" "}
            {plan.thesis_id ? <a href={`#/theses/${plan.thesis_id}`}>thesis</a> : "Buy/Add holding rating"}
          </div>
        </div>
        <div className="scroll">
          <table>
            <tbody>
              {rows.map(([k, v]) => (
                <tr key={k}><td className="muted" style={{ width: 200 }}>{k}</td><td className="mono">{v}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="box">
        <div className="box-header">Legs</div>
        <div className="scroll">
          <table>
            <thead><tr><th>Action</th><th>Kind</th><th>Symbol</th><th className="num">Price</th><th>Source</th></tr></thead>
            <tbody>
              {plan.legs.map((leg, i) => (
                <tr key={i}>
                  <td>{leg.action}</td><td>{leg.kind}</td><td className="mono">{leg.symbol}</td>
                  <td className="num">{num(Number(leg.price))}</td><td className="muted mono">{leg.price_ref}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {decision && (
        <div className="box">
          <div className="box-header">
            Risk decision
            <span className="muted">
              size {decision.size} of {decision.requested_size} requested · max loss {money(Number(decision.max_loss))} of a {money(Number(decision.cap))} cap ({decision.tier_pct}% tier) · cost {money(Number(decision.cost))}
              {Number(decision.funding_needed) > 0 && ` · needs ${money(Number(decision.funding_needed))} more settled cash`}
            </span>
          </div>
          {decision.risk_note && <div className="box-body">{decision.risk_note}</div>}
          {decision.veto_reasons.map((r) => <div key={r} className="box-body neg">Veto: {r}</div>)}
          <div className="scroll">
            <table>
              <thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
              <tbody>
                {decision.checks.map((c) => (
                  <tr key={c.name}>
                    <td className="mono">{c.name}</td>
                    <td><span className={`dot ${c.result === "pass" ? "ok" : c.result === "fail" ? "fail" : "warn"}`} /> {c.result}</td>
                    <td>{c.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {plan.alternatives_rejected.length > 0 && (
        <div className="box">
          <div className="box-header">Alternatives rejected</div>
          <ul className="lines">{plan.alternatives_rejected.map((a) => <li key={a}>{a}</li>)}</ul>
        </div>
      )}
    </div>
  );
}
