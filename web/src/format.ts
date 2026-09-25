const TIMEZONE = "America/New_York";

export function time(iso: string | null | undefined): string {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("en-US", {
    timeZone: TIMEZONE,
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "-";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return `$${num(value, 2)}`;
}

export function count(value: number | null | undefined): string {
  return (value ?? 0).toLocaleString("en-US");
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "-";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 90_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

export const RATING: Record<string, string> = {
  buy_add: "Buy/Add",
  hold: "Hold",
  trim: "Trim",
  sell: "Sell",
};

export const LANES: Record<string, string> = {
  catalyst: "Catalyst",
  screen: "Screen",
  commodity: "Commodity",
  power_grid: "Power + grid",
  policy: "Policy",
  macro: "Macro",
  event_additions: "Event additions",
};

/** Thesis origin: a lane id, or "jon" for ideas entered through intake. */
export function origin(value: string): string {
  if (value === "jon") return "Jon";
  return LANES[value] ?? value.replace(/_/g, " ");
}

const SHIFTS: Record<string, string> = {
  pre_market: "Pre-market shift",
  post_market: "Post-market shift",
  briefing: "Morning briefing",
};

export function shiftTitle(kind: string): string {
  return SHIFTS[kind] ?? kind.replace(/_/g, " ");
}

const JOBS: Record<string, string> = {
  "scan:news": "News scan",
  "scan:intraday": "Intraday price scan",
  "scan:commodity": "Commodity scan",
  "scan:close": "Closing scan",
  "triage:run": "News triage",
  "chat:run": "Chat reply",
  "intake:run": "Thesis intake",
};

/** "shift:pre_market" and collector ids read as plain names. */
export function jobTitle(job: string): string {
  if (JOBS[job]) return JOBS[job];
  if (job.startsWith("shift:")) return shiftTitle(job.slice(6));
  return SOURCES[job] ?? job.replace(/[:_]/g, " ");
}

export const SOURCES: Record<string, string> = {
  cftc_cot: "CFTC futures positioning",
  edgar_company_filings: "SEC filings for followed companies",
  edgar_filing_text: "SEC filing text",
  edgar_latest_filings: "SEC latest filings",
  eia: "EIA energy data",
  federal_register_documents: "Federal Register",
  federal_register_public_inspection: "Federal Register, pre-publication",
  fed_press_releases: "Fed press releases",
  fed_speeches: "Fed speeches",
  finnhub_company_news: "Company news",
  finnhub_earnings_calendar: "Earnings calendar",
  finnhub_general_news: "Market news",
  fred: "FRED economic data",
  gridstatus: "Power grid data",
  ibkr_flex: "IBKR statements",
  news_embeddings: "News search index",
  raw_record_purge: "News buffer cleanup",
  release_calendar: "Economic release calendar",
  tastytrade_metrics: "tastytrade market metrics",
  tastytrade_positions: "tastytrade positions",
  tastytrade_stream: "Live quotes",
  truth_social: "Truth Social posts",
  yahoo_daily_bars: "Daily prices",
};

/** A short reading of a collector or job error for the dashboard. */
export function plainError(error: string | null | undefined): string {
  if (!error) return "";
  if (/HTTP 429/.test(error)) return "Rate limited by the provider; retrying on the next run.";
  if (/ReadTimeout|ConnectTimeout|timed out/i.test(error)) return "The provider did not respond in time.";
  if (/HTTP 404/.test(error)) return "The feed address returned not found.";
  if (/HTTP 5\d\d/.test(error)) return "The provider had a server error.";
  if (/HTTP 40[13]/.test(error)) return "Access was refused; check the API key.";
  if (/cancelled at shutdown/.test(error)) return "Stopped by a service restart.";
  if (/reason adds numbers/.test(error)) return "A news label contained a number not in the item; the batch is retried on the next run.";
  if (/unknown fact/.test(error)) return "The model cited a fact that does not exist, so that answer was not used.";
  if (/NotSupported|NoDataFound/.test(error)) return "The grid operator had no data for that request.";
  const text = error.replace(/^[A-Za-z]+Error: /, "");
  return text.length > 140 ? `${text.slice(0, 140)}...` : text;
}

const SCORED: Record<string, string> = {
  thesis: "Thesis",
  debate_verdict: "Debate call",
  trade_plan: "Plan",
  risk_decision: "Risk decision",
  holding_rating: "Holding rating",
  analyst_view: "Analyst view",
  position: "Position",
  plan: "Plan",
  rating: "Holding rating",
  view: "Analyst view",
  verdict: "Debate call",
};

export function scoredTitle(kind: string): string {
  return SCORED[kind] ?? kind.replace(/_/g, " ");
}

/** Which level the price reached first on a scored call. */
export function firstHit(value: string | null): string {
  if (value === "stop") return "stop first";
  if (value === "target") return "target first";
  return value === "none" ? "neither" : "-";
}
