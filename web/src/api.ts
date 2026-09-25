// Types mirror desk/api/v1.py. Numbers arrive already computed by the backend.

export interface ShiftRef {
  kind: string;
  scheduled_for: string;
  started_at?: string | null;
  status?: string;
  finished_at?: string | null;
}

export interface Status {
  now: string;
  timezone: string;
  shift: { running: ShiftRef | null; next: ShiftRef | null; last: ShiftRef | null };
  models: string[] | null;
  last_job: { job: string; status: string; finished_at: string } | null;
  collectors: { ok: number; failing: number; stale: number; disabled: number };
}

export interface BriefSection {
  title: string;
  lines: string[];
}

export interface Brief {
  id: string;
  created_at: string;
  status: string;
  kind: string;
  sections: BriefSection[];
}

export interface ThesisRow {
  id: string;
  instrument: string;
  direction: string;
  statement: string;
  origin: string;
  state: string;
  conviction: number | null;
  review_by: string | null;
  hard: number | null;
  warning: number | null;
  evidence: number;
  verdict: string | null;
  verdict_id: string | null;
  in_warning_zone: boolean;
  created_at: string;
}

export interface PlanRow {
  id: string;
  created_at: string;
  subject: string;
  structure: string;
  instrument: string;
  direction: string;
  conviction: number;
  thesis_id: string | null;
  decision_id: string | null;
  decision: string | null;
  size: number | null;
  max_loss: number | null;
  cost: number | null;
  funding_needed: number | null;
  veto: string | null;
  risk_note: string | null;
}

export interface RatingRow {
  id: string;
  created_at: string;
  status: string;
  error: string | null;
  subject: string;
  rating: string;
  previous: string | null;
  judged: string | null;
  confidence: string;
  action: string;
  reason: string | null;
  flags: { code: string; detail: string; forces: string | null }[] | null;
}

export interface Today {
  brief: Brief | null;
  theses: ThesisRow[];
  plans: PlanRow[];
  vetoes: PlanRow[];
  ratings: RatingRow[];
}

export interface DeskRow {
  id: string;
  title: string;
  about: string;
  summary: string[];
  problems: number;
  last_at: string | null;
  failures: { job: string; finished_at: string; error: string | null }[];
}

export interface CollectorRow {
  collector: string;
  state: string;
  enabled: boolean;
  last_success_at: string | null;
  consecutive_failures: number;
  records_added_last: number | null;
  last_error: string | null;
  disabled_reason: string | null;
}

export interface Desks {
  desks: DeskRow[];
  collectors: CollectorRow[];
}

export interface ArtifactEvent {
  id: string;
  kind: string;
  status: string;
  produced_by: string;
  created_at: string;
}

export async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${response.status} ${path}: ${body.slice(0, 200)}`);
  }
  return (await response.json()) as T;
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  return getJson<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
