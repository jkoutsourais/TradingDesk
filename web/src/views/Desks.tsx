import type { CollectorRow, Desks as DesksData, DeskRow } from "../api";
import { ago, jobTitle, plainError, SOURCES, time } from "../format";
import { useApi } from "../hooks";
import { DeskDetail } from "./DeskDetail";

const SOURCE_STATES: Record<string, [string, string]> = {
  ok: ["ok", "Working"],
  idle: ["ok", "Outside its hours"],
  stale: ["warn", "Behind schedule"],
  failing: ["fail", "Failing"],
  disabled: ["", "Off"],
};

/** Failed runs grouped by job and cause, since most repeat a transient provider error. */
function Problems({ desk }: { desk: DeskRow }) {
  if (!desk.failures.length) return <div className="empty">No failed runs in the last 24 hours.</div>;
  const groups = new Map<string, { job: string; reason: string; times: number; last: string }>();
  for (const f of desk.failures) {
    const reason = plainError(f.error);
    const key = `${f.job}|${reason}`;
    const group = groups.get(key);
    if (group) group.times += 1;
    else groups.set(key, { job: f.job, reason, times: 1, last: f.finished_at });
  }
  return (
    <div className="scroll">
      <table>
        <thead><tr><th>Run</th><th>Why it failed</th><th className="num">Times</th><th>Last</th></tr></thead>
        <tbody>
          {[...groups.values()].map((g) => (
            <tr key={`${g.job}-${g.reason}`}>
              <td>{jobTitle(g.job)}</td>
              <td>{g.reason}</td>
              <td className="num">{g.times}</td>
              <td className="muted" style={{ whiteSpace: "nowrap" }}>{time(g.last)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Sources({ collectors }: { collectors: CollectorRow[] }) {
  const order = ["failing", "stale", "ok", "idle", "disabled"];
  const sorted = [...collectors].sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state));
  return (
    <div className="box">
      <div className="box-header">Sources</div>
      <div className="scroll">
        <table>
          <thead>
            <tr><th>Source</th><th>Status</th><th>Last update</th><th>Problem</th></tr>
          </thead>
          <tbody>
            {sorted.map((c) => {
              const [dot, label] = SOURCE_STATES[c.state] ?? ["", c.state];
              const troubled = c.state === "failing" || c.state === "stale";
              return (
                <tr key={c.collector}>
                  <td>{SOURCES[c.collector] ?? c.collector}</td>
                  <td><span className={`dot ${dot}`} /> {label}</td>
                  <td>{c.enabled ? ago(c.last_success_at) : c.disabled_reason}</td>
                  <td className="muted">{troubled ? plainError(c.last_error) : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function troubledSources(collectors: CollectorRow[]): number {
  return collectors.filter((c) => c.state === "failing" || c.state === "stale").length;
}

function Health({ problems, noun }: { problems: number; noun: string }) {
  if (!problems) return <><span className="dot ok" /> OK</>;
  return <><span className="dot warn" /> {problems} {noun}{problems > 1 ? "s" : ""}</>;
}

function sourceSummary(collectors: CollectorRow[]): string {
  const enabled = collectors.filter((c) => c.state !== "disabled");
  const healthy = enabled.filter((c) => c.state === "ok" || c.state === "idle").length;
  return `${healthy} of ${enabled.length} sources healthy`;
}

export function Desks({ selected }: { selected?: string }) {
  const { data, error } = useApi<DesksData>("/api/desks", [], 20_000);
  if (error && !data) return <div className="content"><div className="empty">{error}</div></div>;
  if (!data) return <div className="content"><div className="empty">Loading</div></div>;
  const desk = data.desks.find((d) => d.id === selected);
  return (
    <div className="content">
      <div className="box">
        <div className="box-header">Desks, last 24 hours</div>
        <div className="scroll">
          <table>
            <thead>
              <tr><th>Desk</th><th>Produced</th><th>Health</th><th>Last activity</th></tr>
            </thead>
            <tbody>
              {data.desks.map((d) => {
                const summary = d.id === "data" ? [sourceSummary(data.collectors)] : d.summary;
                return (
                  <tr key={d.id} className={d.id === selected ? "selected" : ""}>
                    <td>
                      <a href={`#/desks/${d.id}`}><b>{d.title}</b></a>
                      <div className="muted">{d.about}</div>
                    </td>
                    <td>{summary.length ? summary.join(", ") : <span className="muted">Nothing yet</span>}</td>
                    <td>
                      {d.id === "data" ? (
                        <Health problems={troubledSources(data.collectors)} noun="source down" />
                      ) : (
                        <Health problems={d.problems} noun="failure" />
                      )}
                    </td>
                    <td>{d.last_at ? ago(d.last_at) : <span className="muted">-</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
      {selected && <DeskDetail desk={selected} />}
      {selected === "data" && <Sources collectors={data.collectors} />}
      {desk && (desk.failures.length > 0 || selected !== "data") && (
        <div className="box">
          <div className="box-header">
            {desk.title} desk: failed runs, last 24 hours
            {selected === "data" && <span className="muted">Most clear on the next run; the Sources table shows what is down now.</span>}
          </div>
          <Problems desk={desk} />
        </div>
      )}
    </div>
  );
}
