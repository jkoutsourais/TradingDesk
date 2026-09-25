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
