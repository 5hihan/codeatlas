// Real-time layer: one Server-Sent Events connection shared by the whole app.
// Pages call useLive(["index", "snipe"]) and get a counter that bumps whenever
// one of those events arrives, which they use as a dependency to refetch.
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api, type LiveEvent, type Status } from "./api";

interface LiveCtx {
  connected: boolean;
  status: Status | null;
  events: LiveEvent[];
  counters: Record<string, number>;
  toast: (msg: string, kind?: "info" | "ok" | "bad") => void;
  refreshStatus: () => void;
}

const Ctx = createContext<LiveCtx>(null as any);

export function LiveProvider({ children }: { children: React.ReactNode }) {
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [counters, setCounters] = useState<Record<string, number>>({});
  const [toasts, setToasts] = useState<{ id: number; msg: string; kind: string }[]>([]);
  const toastId = useRef(0);

  const toast = useCallback((msg: string, kind: "info" | "ok" | "bad" = "info") => {
    const id = ++toastId.current;
    setToasts((t) => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }, []);

  const refreshStatus = useCallback(() => {
    api<Status>("/api/status").then((s) => { setStatus(s); setEvents(s.events || []); }).catch(() => {});
  }, []);

  useEffect(() => {
    refreshStatus();
    let es: EventSource | null = null;
    let retry = 1000;
    let closed = false;
    const connect = () => {
      es = new EventSource("/api/events");
      es.addEventListener("hello", () => { setConnected(true); retry = 1000; refreshStatus(); });
      const bump = (type: string) => setCounters((c) => ({ ...c, [type]: (c[type] || 0) + 1, any: (c.any || 0) + 1 }));
      for (const type of ["index", "watch", "snipe", "tool"]) {
        es.addEventListener(type, (e: MessageEvent) => {
          let data: LiveEvent = { type, t: Date.now() / 1000 };
          try { data = { ...JSON.parse(e.data), type }; } catch { /* ignore */ }
          if (type !== "snipe") setEvents((ev) => [data, ...ev].slice(0, 200));
          if (type === "snipe" && data.created?.length) toast(`SNIPE found ${data.created.length} new issue(s): ${data.created.join(", ")}`, "info");
          bump(type);
          if (type === "watch" || type === "index") refreshStatus();
          if (type === "watch" && (data.parsed || data.removed)) toast(`index updated: ${data.parsed} parsed, ${data.removed} removed (${data.seconds}s)`, "ok");
        });
      }
      es.onerror = () => {
        setConnected(false);
        es?.close();
        if (!closed) setTimeout(connect, retry = Math.min(retry * 2, 15000));
      };
    };
    connect();
    const iv = setInterval(refreshStatus, 15000);
    return () => { closed = true; es?.close(); clearInterval(iv); };
  }, [refreshStatus, toast]);

  const value = useMemo(() => ({ connected, status, events, counters, toast, refreshStatus }), [connected, status, events, counters, toast, refreshStatus]);
  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toasts">{toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.msg}</div>)}</div>
    </Ctx.Provider>
  );
}

export const useLiveCtx = () => useContext(Ctx);

/** Returns a number that changes when any of the given event types arrive. Use as an effect dependency. */
export function useLive(types: string[]): number {
  const { counters } = useContext(Ctx);
  return types.reduce((a, t) => a + (counters[t] || 0), 0);
}

/** Fetch helper that re-runs on deps and on live events of the given types (debounced). */
export function useData<T>(fn: () => Promise<T>, deps: unknown[], liveTypes: string[] = ["index"]) {
  const tick = useLive(liveTypes);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  useEffect(() => {
    let live = true;
    const h = setTimeout(() => {
      setLoading(true);
      fnRef.current().then((d) => { if (live) { setData(d); setError(null); } })
        .catch((e) => { if (live) setError(String(e.message || e)); })
        .finally(() => { if (live) setLoading(false); });
    }, tick ? 250 : 0);
    return () => { live = false; clearTimeout(h); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, nonce]);
  return { data, error, loading, reload };
}
