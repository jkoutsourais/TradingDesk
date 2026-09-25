import { useCallback, useEffect, useRef, useState } from "react";

import { type ArtifactEvent, getJson } from "./api";

type Listener = (event: ArtifactEvent) => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;

// One EventSource for the whole app; views subscribe to the artifact kinds they show.
function ensureSource(): void {
  if (source !== null) return;
  source = new EventSource("/api/events");
  source.addEventListener("artifact", (message) => {
    const event = JSON.parse((message as MessageEvent<string>).data) as ArtifactEvent;
    for (const listener of listeners) listener(event);
  });
}

export function useArtifactEvents(listener: Listener): void {
  const latest = useRef(listener);
  latest.current = listener;
  useEffect(() => {
    ensureSource();
    const wrapped: Listener = (event) => latest.current(event);
    listeners.add(wrapped);
    return () => {
      listeners.delete(wrapped);
    };
  }, []);
}

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Fetches `path`, refetching when an artifact of one of `kinds` arrives (debounced) and
 * every `intervalMs` as a fallback.
 */
export function useApi<T>(path: string, kinds: string[] = [], intervalMs = 60_000): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const timer = useRef<number | null>(null);

  const reload = useCallback(() => {
    getJson<T>(path)
      .then((value) => {
        setData(value);
        setError(null);
      })
      .catch((reason: unknown) => setError(String(reason)))
      .finally(() => setLoading(false));
  }, [path]);

  useEffect(() => {
    setLoading(true);
    reload();
    const id = window.setInterval(reload, intervalMs);
    return () => window.clearInterval(id);
  }, [reload, intervalMs]);

  const kindKey = kinds.join(",");
  useArtifactEvents((event) => {
    if (kindKey === "" || !kindKey.split(",").includes(event.kind)) return;
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(reload, 1500);
  });

  return { data, error, loading, reload };
}

export function useHashRoute(): string[] {
  const read = () => window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const [parts, setParts] = useState<string[]>(read);
  useEffect(() => {
    const onChange = () => setParts(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return parts;
}
