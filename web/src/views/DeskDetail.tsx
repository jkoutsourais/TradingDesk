import { count, shiftTitle, time } from "../format";
import { useApi } from "../hooks";

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
  const quotes = problems.filter((p) => p.includes("word for word")).length;
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

export function DeskDetail({ desk }: { desk: string }) {
  const { data } = useApi<Record<string, unknown>>(`/api/desks/${desk}`, ["trigger", "dossier", "verified_claim"], 30_000);
  if (!data || Object.keys(data).length === 0) return null;
  if (desk === "watch") return <WatchView data={data as unknown as WatchDetail} />;
  if (desk === "research") return <ResearchView data={data as unknown as ResearchDetail} />;
  return null;
}
