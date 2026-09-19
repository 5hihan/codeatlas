// Thin client for the CodeAtlas dashboard API (same origin, served by the Python server).

export type Params = Record<string, string | number | boolean | undefined | null>;

export async function api<T = any>(path: string, params?: Params): Promise<T> {
  const u = new URL(path, location.origin);
  if (params) for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, String(v));
  const r = await fetch(u.toString());
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  const ct = r.headers.get("content-type") || "";
  return (ct.includes("json") ? r.json() : r.text()) as Promise<T>;
}

export async function post<T = any>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body ?? {}) });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// ---- types (subset of what the server returns) ----
export interface Status {
  version: string; repo: string; files: number; symbols: number; refs: number; last_update: string | null;
  watcher: { backend: string; events: number; pending: number; last_event: number; rescan_every: number; alive: boolean } | null;
  dashboard: string | null; uptime_s: number; snipe: Record<string, number>; events: LiveEvent[];
}
export interface LiveEvent { type: string; t: number; [k: string]: any }
export interface Sym {
  id: number; name: string; kind: string; qualified: string; path: string; start_line: number; end_line: number;
  signature?: string; doc?: string; complexity?: number; exported?: boolean | number; ref_files?: number; parent_id?: number | null;
  decorators?: string[];
}
export interface SnipeCandidate { symbol: string; path: string; line: number; confidence: number; evidence: string[]; counter: string[] }
export interface SnipeItem {
  id: string; kind: "issue" | "opportunity"; state: string; problem: string; evidence: string[]; suspect: string | null;
  suspect_path: string | null; suspect_line: number | null; confidence: number | null; candidates: SnipeCandidate[];
  related: string[]; verification: string[]; priority: string; score: number; risk: string; owner: string | null;
  source: string; symbols: number; tokens: number; notes: { t: string; text: string }[]; updated: string;
}
export interface SnipeBoard {
  project: string; ready: SnipeItem[]; investigating: SnipeItem[]; current: SnipeItem[]; deferred: number;
  opportunities: SnipeItem[]; opportunities_total: number; done: SnipeItem[]; done_total: number; hot: number;
  config: Record<string, any>; test_cmd: string | null; log: { t: string; msg: string }[]; watcher_alive?: boolean;
}

export const fmtN = (n: number | undefined | null) => Number(n || 0).toLocaleString();
export const age = (t?: number | string | null) => {
  if (!t) return "never";
  const d = Date.now() / 1000 - Number(t);
  return d < 60 ? `${d | 0}s ago` : d < 3600 ? `${(d / 60) | 0}m ago` : `${(d / 3600).toFixed(1)}h ago`;
};
export const P = (p: number) => `P${5 - p}`;
export const isTest = (p: string) => /(^|\/)(tests?|__tests__|spec|e2e)(\/|$)|\.(test|spec)\.[a-z]+$/i.test(p);
