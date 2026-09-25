import { count, num, time } from "../format";
import { useApi } from "../hooks";

interface DataDetail {
  volumes: {
    raw_records_24h: { source: string; n: number }[];
    raw_records_total: number;
    embeddings_total: number;
    daily_bars_total: number;
    daily_bar_symbols: number;
    minute_bars_24h: number;
    series_observations_24h: number;
    grid_observations_24h: number;
  };
  failures: { job: string; finished_at: string; error: string }[];
}

interface WatchDetail {
  triggers: { created_at: string; rule: string; instrument: string; tier: string | null; importance: number; urgent: boolean; summary: string }[];
  shifts: { kind: string; scheduled_for: string; status: string; note: string | null }[];
  queue_last_hour: Record<string, number>;
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

function DataView({ data }: { data: DataDetail }) {
  const v = data.volumes;
  const totals: [string, number][] = [
    ["Raw records stored", v.raw_records_total],
    ["Embeddings", v.embeddings_total],
    ["Daily bars", v.daily_bars_total],
    ["Symbols with daily bars", v.daily_bar_symbols],
    ["Minute bars, 24h", v.minute_bars_24h],
    ["Series observations, 24h", v.series_observations_24h],
    ["Grid observations, 24h", v.grid_observations_24h],
  ];
  return (
    <div className="grid-2">
      <div className="box">
        <div className="box-header">Volumes</div>
        <table>
          <tbody>
            {totals.map(([label, value]) => (
              <tr key={label}><td>{label}</td><td className="num">{count(value)}</td></tr>
            ))}
            {v.raw_records_24h.map((row) => (
              <tr key={row.source}><td className="muted mono">{row.source}, 24h</td><td className="num">{count(row.n)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="box">
        <div className="box-header">Failed runs, last 6 hours</div>
        {data.failures.length === 0 ? <div className="empty">None.</div> : (
          <table>
            <tbody>
              {data.failures.map((f, i) => (
                <tr key={i}><td className="mono">{f.job}</td><td>{time(f.finished_at)}</td><td className="muted">{f.error}</td></tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function WatchView({ data }: { data: WatchDetail }) {
  return (
    <>
      <div className="box">
        <div className="box-header">Triggers, last 24 hours</div>
        {data.triggers.length === 0 ? <div className="empty">None.</div> : (
          <div className="scroll">
            <table>
              <thead><tr><th>When</th><th className="num">Score</th><th>Tier</th><th>Rule</th><th>Trigger</th></tr></thead>
              <tbody>
                {data.triggers.map((t, i) => (
                  <tr key={i}>
                    <td>{time(t.created_at)}</td>
                    <td className="num">{t.urgent && <span className="dot fail" />} {num(t.importance)}</td>
                    <td>{t.tier ?? "-"}</td>
                    <td className="mono muted">{t.rule}</td>
                    <td>{t.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="grid-2">
        <div className="box">
          <div className="box-header">
            Shifts
            <span className="muted">
              jobs last hour: {count(data.queue_last_hour.ok)} ok, {count(data.queue_last_hour.failed)} failed, {count(data.queue_last_hour.queued)} queued
            </span>
          </div>
          <table>
            <tbody>
              {data.shifts.map((s) => (
                <tr key={`${s.kind}-${s.scheduled_for}`}>
                  <td>{s.kind}</td>
                  <td>{time(s.scheduled_for)}</td>
                  <td><span className={`dot ${s.status === "ok" ? "ok" : s.status === "failed" ? "fail" : s.status === "running" ? "run" : ""}`} /> {s.status}</td>
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

function ResearchView({ data }: { data: ResearchDetail }) {
  const failed = data.dossiers.filter((d) => d.status === "failed").length;
  return (
    <div className="box">
      <div className="box-header">
        Dossiers, last 36 hours
        <span className="muted">
          {data.dossiers.length - failed} written, {failed} rejected by the fact checks
        </span>
      </div>
      {data.dossiers.length === 0 ? <div className="empty">None yet.</div> : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Subject</th>
                <th>Kind</th>
                <th className="num">Score</th>
                <th className="num">Claims</th>
                <th className="num">Verified</th>
                <th className="num">Corrected</th>
                <th className="num">Rejected</th>
                <th className="num">Pending</th>
                <th className="num">Evidence</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {data.dossiers.map((d) => (
                <tr key={d.id}>
                  <td style={{ maxWidth: 420 }}>
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
                  <td>{d.status === "failed" ? <span className="label danger">failed</span> : <span className="label">{d.subject_kind}</span>}</td>
                  <td className="num">{d.score === null ? "" : num(d.score)}</td>
                  <td className="num">{d.status === "failed" ? "" : count(d.claims)}</td>
                  <td className="num">{d.verified ? count(d.verified) : ""}</td>
                  <td className="num">{d.corrected ? count(d.corrected) : ""}</td>
                  <td className="num">{d.rejected ? count(d.rejected) : ""}</td>
                  <td className="num">{d.pending ? count(d.pending) : ""}</td>
                  <td className="num">{d.evidence === null ? "" : num(d.evidence)}</td>
                  <td className="muted">{time(d.created_at)}</td>
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
  if (desk === "data") return <DataView data={data as unknown as DataDetail} />;
  if (desk === "watch") return <WatchView data={data as unknown as WatchDetail} />;
  if (desk === "research") return <ResearchView data={data as unknown as ResearchDetail} />;
  return null;
}
