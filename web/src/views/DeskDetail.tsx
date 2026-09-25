import type { PlanRow, RatingRow, ThesisRow } from "../api";
import { count, num, scoredTitle, shiftTitle, time } from "../format";
import { useApi } from "../hooks";
import { PlanTable, RatingTable, ThesisTable } from "./Today";

interface WatchDetail {
  triggers: { created_at: string; rule: string; instrument: string; tier: string | null; importance: number; urgent: boolean; summary: string }[];
  shifts: { kind: string; scheduled_for: string; status: string; note: string | null }[];
  promotions: { symbol: string; expires_on: string; reason: string }[];
  calendar: { kind: string; name: string; at: string }[];
}

interface ResearchDetail {
  dossiers: {
    id: string;
    created_at: string;
    status: string;
    error: string | null;
    subject: string;
    subject_kind: string;
    score: number | null;
    why: string | null;
    claims: number;
    pending: number;
    verified: number;
    corrected: number;
    rejected: number;
    evidence: number | null;
  }[];
}

function WatchView({ data }: { data: WatchDetail }) {
  return (
    <>
      <div className="box">
        <div className="box-header">Top alerts, last 24 hours</div>
        {data.triggers.length === 0 ? <div className="empty">None.</div> : (
          <div className="scroll">
            <table>
              <thead><tr><th>Instrument</th><th>What happened</th><th>When</th></tr></thead>
              <tbody>
                {data.triggers.map((t, i) => (
                  <tr key={i}>
                    <td><b>{t.instrument}</b>{t.urgent && <span className="label attention" style={{ marginLeft: 6 }}>urgent</span>}</td>
                    <td>{t.summary}</td>
                    <td className="muted" style={{ whiteSpace: "nowrap" }}>{time(t.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="grid-2">
        <div className="box">
          <div className="box-header">Shifts</div>
          <table>
            <tbody>
              {data.shifts.map((s) => (
                <tr key={`${s.kind}-${s.scheduled_for}`}>
                  <td>{shiftTitle(s.kind)}</td>
                  <td>{time(s.scheduled_for)}</td>
                  <td><span className={`dot ${s.status === "ok" ? "ok" : s.status === "failed" ? "fail" : s.status === "running" ? "run" : ""}`} /> {s.status === "ok" ? "done" : s.status}</td>
                  <td className="muted">{s.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="box">
          <div className="box-header">Upcoming releases and promotions</div>
          <table>
            <tbody>
              {data.calendar.map((e) => (
                <tr key={`${e.name}-${e.at}`}><td>{e.name}</td><td>{time(e.at)}</td></tr>
              ))}
              {data.promotions.map((p) => (
                <tr key={p.symbol}><td><b>{p.symbol}</b> promoted to Tier 1</td><td className="muted">{p.reason} (until {p.expires_on})</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

/** "claim c2: quote is not word for word...; section ...: numbers ..." as a short count. */
function failureSummary(error: string | null): string {
  if (!error) return "failed";
  const problems = error.split("; ").filter(Boolean);
  const quotes = problems.filter((p) => p.includes("word for word") || p.includes("quote is not in source")).length;
  const numbers = problems.filter((p) => p.includes("numbers") || p.includes("number")).length;
  const parts = [];
  if (quotes) parts.push(`${quotes} quote${quotes > 1 ? "s" : ""} not found in the source`);
  if (numbers) parts.push(`${numbers} number check${numbers > 1 ? "s" : ""} failed`);
  const other = problems.length - quotes - numbers;
  if (other > 0) parts.push(`${other} other problem${other > 1 ? "s" : ""}`);
  return parts.length ? `Rejected after retry: ${parts.join(", ")}` : error.slice(0, 120);
}

function claimSummary(d: ResearchDetail["dossiers"][number]): string {
  const parts = [
    d.verified && `${count(d.verified)} verified`,
    d.corrected && `${count(d.corrected)} corrected`,
    d.rejected && `${count(d.rejected)} rejected`,
    d.pending && `${count(d.pending)} awaiting check`,
  ].filter(Boolean);
  return parts.length ? parts.join(", ") : "none";
}

function ResearchView({ data }: { data: ResearchDetail }) {
  const failed = data.dossiers.filter((d) => d.status === "failed").length;
  return (
    <div className="box">
      <div className="box-header">
        Dossiers, last 36 hours
        <span className="muted">
          {data.dossiers.length - failed} written, {failed} not written because they failed the source checks
        </span>
      </div>
      {data.dossiers.length === 0 ? <div className="empty">None yet.</div> : (
        <div className="scroll">
          <table>
            <thead>
              <tr><th>Subject</th><th>Type</th><th>Claims</th><th>When</th></tr>
            </thead>
            <tbody>
              {data.dossiers.map((d) => (
                <tr key={d.id}>
                  <td style={{ maxWidth: 520 }}>
                    <a href={`#/trace/${d.id}`}><b>{d.subject}</b></a>
                    {d.why && <div className="muted" style={{ fontSize: 12 }}>{d.why}</div>}
                    {d.status === "failed" && (
                      <details style={{ fontSize: 12 }}>
                        <summary className="muted">{failureSummary(d.error)}</summary>
                        <ul className="lines muted" style={{ padding: "4px 0 4px 16px" }}>
                          {(d.error ?? "").split("; ").map((problem, i) => <li key={i}>{problem}</li>)}
                        </ul>
                      </details>
                    )}
                  </td>
                  <td>{d.status === "failed" ? <span className="label">not written</span> : <span className="label">{d.subject_kind === "holding" ? "holding" : "idea"}</span>}</td>
                  <td>{d.status === "failed" ? "" : claimSummary(d)}</td>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>{time(d.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

interface OtherDetail {
  claims?: { id: string; created_at: string; status: string; verdict: string; reason: string; subject: string; statement: string }[];
  theses?: ThesisRow[];
  ratings?: RatingRow[];
  debates?: { id: string; created_at: string; status: string; error: string | null; subject: string; thesis_id: string; verdict: string; confidence: string; reason: string | null }[];
  plans?: PlanRow[];
  briefs?: { id: string; created_at: string; status: string; kind: string; headline: string | null }[];
  scores?: { id: string; created_at: string; subject_kind: string; subject_id: string; horizon: string; return_pct: number; r_multiple: number | null; hit: boolean | null }[];
}

function Box({ title, children, empty, count: n }: { title: string; children: React.ReactNode; empty: string; count: number }) {
  return (
    <div className="box">
      <div className="box-header">{title}</div>
      {n === 0 ? <div className="empty">{empty}</div> : children}
    </div>
  );
}

function verdictTone(verdict: string): string {
  return verdict === "verified" || verdict === "pursue" ? "success" : verdict === "rejected" || verdict === "reject" ? "danger" : "attention";
}

function OtherView({ desk, data }: { desk: string; data: OtherDetail }) {
  if (data.claims) {
    return (
      <Box title="Claims checked, last 3 days" count={data.claims.length} empty="No claims checked in the last 3 days.">
        <div className="scroll">
          <table>
            <thead><tr><th>Subject</th><th>Claim</th><th>Verdict</th><th>When</th></tr></thead>
            <tbody>
              {data.claims.map((c) => (
                <tr key={c.id}>
                  <td><b>{c.subject}</b></td>
                  <td>
                    <a href={`#/trace/${c.id}`}>{c.statement}</a>
                    {c.status === "failed" && <div className="muted" style={{ fontSize: 12 }}>The check did not run; it is retried at the next shift.</div>}
                  </td>
                  <td>{c.status === "failed" ? <span className="label">pending</span> : <span className={`label ${verdictTone(c.verdict)}`}>{c.verdict}</span>}</td>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>{time(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Box>
    );
  }
  if (data.theses) {
    return (
      <div className="box">
        <div className="box-header">
          Open theses <span className="spacer" /> <a className="btn" href="#/lanes">Lanes board</a>
        </div>
        <ThesisTable theses={data.theses} />
      </div>
    );
  }
  if (data.ratings) {
    return (
      <div className="box">
        <div className="box-header">This morning's holding ratings</div>
        <RatingTable ratings={data.ratings} />
      </div>
    );
  }
  if (data.debates) {
    return (
      <Box title="Debates, last 7 days" count={data.debates.length} empty="No debates in the last 7 days. Theses are debated in the pre-market shift.">
        <div className="scroll">
          <table>
            <thead><tr><th>Thesis</th><th>Verdict</th><th>Why</th><th>When</th></tr></thead>
            <tbody>
              {data.debates.map((d) => (
                <tr key={d.id}>
                  <td><a href={`#/theses/${d.thesis_id}`}><b>{d.subject}</b></a></td>
                  <td>{d.status === "failed" ? <span className="label">failed</span> : <><span className={`label ${verdictTone(d.verdict)}`}>{d.verdict}</span> <span className="muted">{d.confidence}</span></>}</td>
                  <td>{d.status === "failed" ? <span className="muted">The debate did not finish; it runs again next shift.</span> : d.reason}</td>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>{time(d.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Box>
    );
  }
  if (data.plans) {
    const title = desk === "risk" ? "Risk decisions, last 7 days" : "Trade plans, last 7 days";
    return (
      <div className="box">
        <div className="box-header">{title}</div>
        <PlanTable plans={data.plans} vetoed={false} />
      </div>
    );
  }
  if (data.briefs) {
    return (
      <Box title="Briefings, last 7 days" count={data.briefs.length} empty="No briefings in the last 7 days.">
        <ul className="lines">
          {data.briefs.map((b) => (
            <li key={b.id}>
              <a href={`#/briefs/${b.id}`}>{b.kind === "weekend" ? "Weekend briefing" : "Morning briefing"}, {time(b.created_at)}</a>
              {b.status === "failed" && <span className="label attention" style={{ marginLeft: 6 }}>data only</span>}
              {b.headline && <div className="muted">{b.headline}</div>}
            </li>
          ))}
        </ul>
      </Box>
    );
  }
  if (data.scores) {
    return (
      <Box title="Scores, last 7 days" count={data.scores.length} empty="Nothing scored yet. Calls are scored 1, 5 and 20 trading days after they are made.">
        <div className="scroll">
          <table>
            <thead><tr><th>Call</th><th>After</th><th className="num">Return</th><th className="num">R</th><th>Right?</th><th>Scored</th></tr></thead>
            <tbody>
              {data.scores.map((s) => (
                <tr key={s.id}>
                  <td><a href={`#/trace/${s.subject_id}`}>{scoredTitle(s.subject_kind)}</a></td>
                  <td>{s.horizon}</td>
                  <td className={`num ${s.return_pct >= 0 ? "pos" : "neg"}`}>{num(s.return_pct)}%</td>
                  <td className="num">{num(s.r_multiple)}</td>
                  <td>{s.hit === null ? "-" : s.hit ? "yes" : "no"}</td>
                  <td className="muted">{time(s.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Box>
    );
  }
  return null;
}

export function DeskDetail({ desk }: { desk: string }) {
  const { data } = useApi<Record<string, unknown>>(`/api/desks/${desk}`, [], 30_000);
  if (!data) return <div className="box"><div className="empty">Loading</div></div>;
  if (Object.keys(data).length === 0) return null;
  if (desk === "watch") return <WatchView data={data as unknown as WatchDetail} />;
  if (desk === "research") return <ResearchView data={data as unknown as ResearchDetail} />;
  return <OtherView desk={desk} data={data as OtherDetail} />;
}
