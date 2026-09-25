import { CpuIcon, PulseIcon, ServerIcon, ClockIcon } from "@primer/octicons-react";

import type { Status } from "../api";
import { ago, jobTitle, shiftTitle, time } from "../format";
import { useApi } from "../hooks";

export function StatusBar() {
  const { data, error } = useApi<Status>("/api/status", [], 15_000);
  if (error) return <footer className="statusbar"><span className="dot fail" /> API unreachable</footer>;
  if (!data) return <footer className="statusbar">Loading</footer>;
  const { shift, models, last_job, collectors } = data;
  return (
    <footer className="statusbar">
      <span className="item">
        <PulseIcon size={12} />
        {shift.running ? (
          <>
            <span className="dot run" /> {shiftTitle(shift.running.kind)} running
          </>
        ) : shift.next ? (
          <>next: {shiftTitle(shift.next.kind)} {time(shift.next.scheduled_for)}</>
        ) : (
          "no shift scheduled"
        )}
      </span>
      <span className="item">
        <CpuIcon size={12} />
        {models === null ? "Ollama unreachable" : models.length ? models.join(", ") : "no model loaded"}
      </span>
      <span className="item">
        <ClockIcon size={12} />
        {last_job ? `last run: ${jobTitle(last_job.job)} ${ago(last_job.finished_at)}` : "no runs yet"}
      </span>
      <span className="item">
        <ServerIcon size={12} />
        <span className={`dot ${collectors.failing ? "fail" : collectors.stale ? "warn" : "ok"}`} />
        sources {collectors.ok} ok
        {collectors.failing ? `, ${collectors.failing} failing` : ""}
        {collectors.stale ? `, ${collectors.stale} stale` : ""}
      </span>
    </footer>
  );
}
