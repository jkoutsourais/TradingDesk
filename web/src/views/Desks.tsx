import { useState } from "react";

import type { Desks as DesksData, DeskRow } from "../api";
import { ago, count, duration, num, time } from "../format";
import { useApi } from "../hooks";

function DeskJobs({ desk }: { desk: DeskRow }) {
  if (!desk.jobs.length) {
    return <div className="empty">No scheduler or collector jobs recorded for this desk in 24 hours.</div>;
  }
  return (
    <div className="scroll">
      <table>
        <thead>
          <tr>
            <th>Job</th>
            <th>Status</th>
            <th>Started</th>
            <th className="num">Runtime</th>
            <th>Model</th>
            <th className="num">Tokens out</th>
            <th className="num">tok/s</th>
            <th className="num">Load</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          {desk.jobs.map((job, i) => (
            <tr key={`${job.job}-${job.started_at}-${i}`}>
              <td className="mono">{job.job}</td>
              <td>
                <span className={`dot ${job.status === "ok" ? "ok" : job.status === "failed" ? "fail" : "run"}`} />{" "}
                {job.status}
              </td>
              <td>{time(job.started_at)}</td>
              <td className="num">{duration(job.runtime_ms)}</td>
              <td className="mono">{job.model ?? "-"}</td>
              <td className="num">{job.tokens_out ? count(job.tokens_out) : "-"}</td>
              <td className="num">{job.tokens_per_s ? num(job.tokens_per_s, 1) : "-"}</td>
              <td className="num">{job.load_ms ? duration(job.load_ms) : "-"}</td>
              <td className="muted">{job.error ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Desks({ selected }: { selected?: string }) {
  const { data, error } = useApi<DesksData>("/api/desks", [], 20_000);
  const [showCollectors, setShowCollectors] = useState(selected === "data");
  if (error) return <div className="content"><div className="empty">{error}</div></div>;
  if (!data) return <div className="content"><div className="empty">Loading</div></div>;
  const desks = selected ? data.desks.filter((d) => d.id === selected) : data.desks;
  return (
    <div className="content">
      <div className="box">
        <div className="box-header">Desks, last 24 hours</div>
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Desk</th>
                <th className="num">Artifacts</th>
                <th className="num">Failed</th>
                <th className="num">Tokens in</th>
                <th className="num">Tokens out</th>
                <th className="num">tok/s</th>
                <th>Models</th>
                <th>Last output</th>
              </tr>
            </thead>
            <tbody>
              {data.desks.map((d) => (
                <tr key={d.id}>
                  <td>
                    <a href={`#/desks/${d.id}`}>{d.title}</a>
                  </td>
                  <td className="num">{count(d.artifacts)}</td>
                  <td className={`num ${d.failed ? "neg" : ""}`}>{count(d.failed)}</td>
                  <td className="num">{count(d.tokens_in)}</td>
                  <td className="num">{count(d.tokens_out)}</td>
                  <td className="num">{d.tokens_per_s ? num(d.tokens_per_s, 1) : "-"}</td>
                  <td className="mono">{d.models.join(", ") || "-"}</td>
                  <td>{ago(d.last_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {desks.map((desk) =>
        selected ? (
          <div className="box" key={desk.id}>
            <div className="box-header">
              {desk.title} jobs
              {desk.job_failures > 0 && <span className="label danger">{desk.job_failures} failed</span>}
            </div>
            <DeskJobs desk={desk} />
          </div>
        ) : null,
      )}
      <div className="box">
        <div className="box-header">
          Collectors
          <span className="spacer" />
          <button className="btn" onClick={() => setShowCollectors((v) => !v)}>
            {showCollectors ? "Hide" : "Show"}
          </button>
        </div>
        {showCollectors && (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>Collector</th>
                  <th>State</th>
                  <th>Last success</th>
                  <th className="num">Fails</th>
                  <th className="num">Last run added</th>
                  <th>Last error</th>
                </tr>
              </thead>
              <tbody>
                {data.collectors.map((c) => (
                  <tr key={c.collector}>
                    <td className="mono">{c.collector}</td>
                    <td>
                      <span className={`dot ${c.state === "ok" ? "ok" : c.state === "failing" ? "fail" : c.state === "stale" ? "warn" : ""}`} />{" "}
                      {c.state}
                    </td>
                    <td>{c.enabled ? ago(c.last_success_at) : c.disabled_reason}</td>
                    <td className="num">{count(c.consecutive_failures)}</td>
                    <td className="num">{count(c.records_added_last)}</td>
                    <td className="muted">{c.last_error ?? ""}</td>
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
