import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useData } from "../live";
import { Badge, Card, FileLink, Loading } from "../ui";

type Pattern = { pattern: string; title: string; path: string; line: number; explanation: string; evidence: string; confidence: number };
type Relationship = { source_path: string; target_path: string; kind: string; line: number; evidence: string; confidence: string; resolved: boolean };
type Page<T> = { items: T[]; total: number; truncated: boolean; next_offset?: number | null; error?: string };

export function PatternHints({ data }: { data?: Page<Pattern> }) {
  if (!data?.items?.length) return null;
  return <section className="evidence-section"><h3>Patterns in context</h3>
    {data.items.map((p, i) => <details key={`${p.pattern}:${p.line}:${i}`} className="evidence-item">
      <summary>{p.title} <span className="muted mono">:{p.line}</span></summary>
      <p>{p.explanation}</p><pre>{p.evidence}</pre>
      <span className="muted">{p.confidence} confidence · syntax evidence</span>
    </details>)}
    {data.truncated && <Link to={`/patterns?path=${encodeURIComponent(data.items[0].path)}`}>Explore all occurrences</Link>}
  </section>;
}

export function RelationshipHints({ data }: { data?: Page<Relationship> }) {
  if (!data?.items?.length) return null;
  return <section className="evidence-section"><h3>Code / config / schema relationships</h3>
    {data.items.map((r, i) => <div className="evidence-item" key={i}>
      <Link className="mono" to={`/relationships?path=${encodeURIComponent(r.source_path)}`}>{r.source_path}:{r.line}</Link>
      <span className="relationship-kind">{r.kind.replaceAll("_", " ")} →</span>
      <Link className="mono" to={`/relationships?path=${encodeURIComponent(r.target_path)}`}>{r.target_path}</Link>
      {!r.resolved && <Badge>target missing</Badge>}
    </div>)}
  </section>;
}

function Pagination({ offset, data, change }: { offset: number; data: Page<unknown>; change: (n: number) => void }) {
  return <div className="row evidence-pagination">
    <span className="muted">{data.total ? `${offset + 1}–${offset + data.items.length} of ${data.total}` : "0 results"}</span>
    <span className="spacer" />
    <button disabled={!offset} onClick={() => change(Math.max(0, offset - 40))}>Previous</button>
    <button disabled={data.next_offset == null} onClick={() => change(data.next_offset!)}>Next</button>
  </div>;
}

export function Relationships() {
  const [sp, setSp] = useSearchParams();
  const path = sp.get("path") || "", direction = sp.get("direction") || "both", offset = Number(sp.get("offset") || 0);
  const [draft, setDraft] = useState(path);
  useEffect(() => setDraft(path), [path]);
  const { data, error, loading, reload } = useData(() => api<Page<Relationship>>("/api/relationships", { path, direction, offset, limit: 40 }), [path, direction, offset]);
  const page = (n: number) => setSp({ path, direction, offset: String(n) });
  return <div className="intelligence"><h1>Relationships</h1>
    <p className="page-intro">Follow dependencies between code, configuration, and schemas. Every connection includes its source evidence.</p>
    <form className="query-toolbar" onSubmit={(e) => { e.preventDefault(); setSp({ path: draft, direction }); }}>
      <label>File or directory<input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="All files" /></label>
      <label>Direction<select value={direction} onChange={(e) => setSp({ path, direction: e.target.value })}>
        <option value="both">Both directions</option><option value="in">Consumers of this file</option><option value="out">Dependencies of this file</option>
      </select></label><button className="primary" type="submit">Find relationships</button>
    </form>
    {(error || data?.error) && <p role="alert">{error || data?.error} <button onClick={reload}>Retry</button></p>}
    {loading && !data ? <Loading /> : data && !data.error && <Card>
      <div className="row"><h2>{data.total} relationships</h2>{loading && <span role="status">Refreshing…</span>}</div>
      {!data.items.length && <p className="muted">No explicit relationships found for this selection. Try a parent directory or both directions. Dynamic paths may not be resolvable.</p>}
      {data.items.map((r, i) => <article className="evidence-item" key={`${r.source_path}:${r.line}:${i}`}>
        <div className="relationship-path"><Link to={`/relationships?path=${encodeURIComponent(r.source_path)}`}>{r.source_path}:{r.line}</Link>
          <span className="relationship-kind">{r.kind.replaceAll("_", " ")} →</span>
          <Link to={`/relationships?path=${encodeURIComponent(r.target_path)}`}>{r.target_path}</Link></div>
        <p className="muted">{r.evidence}</p><Badge>{r.confidence} confidence</Badge> {!r.resolved && <Badge>target missing</Badge>}
      </article>)}<Pagination offset={offset} data={data} change={page} />
    </Card>}
  </div>;
}

export function Patterns() {
  const [sp, setSp] = useSearchParams();
  const path = sp.get("path") || "", pattern = sp.get("pattern") || "", offset = Number(sp.get("offset") || 0);
  const [draft, setDraft] = useState(path);
  useEffect(() => setDraft(path), [path]);
  const { data: catalogue } = useData(() => api<any[]>("/api/patterns/catalogue"), [], []);
  const { data, error, loading, reload } = useData(() => api<Page<Pattern>>("/api/patterns", { path, pattern, offset, limit: 40 }), [path, pattern, offset]);
  return <div className="intelligence"><h1>Programming patterns</h1>
    <p className="page-intro">Twelve concepts, explained where they occur in your code. Expand an occurrence to see the evidence and what it means here.</p>
    <form className="query-toolbar" onSubmit={(e) => { e.preventDefault(); setSp({ path: draft, pattern }); }}>
      <label>File or directory<input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="All source files" /></label>
      <label>Concept<select value={pattern} onChange={(e) => setSp({ path, pattern: e.target.value })}>
        <option value="">All 12 patterns</option>{catalogue?.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
      </select></label><button className="primary" type="submit">Find patterns</button>
    </form>
    {(error || data?.error) && <p role="alert">{error || data?.error} <button onClick={reload}>Retry</button></p>}
    {loading && !data ? <Loading /> : data && !data.error && <Card>
      <div className="row"><h2>{data.total} occurrences</h2>{loading && <span role="status">Refreshing…</span>}</div>
      {!data.items.length && <p className="muted">No supported pattern occurrences in this selection. Choose a different concept or broaden the path.</p>}
      {data.items.map((p, i) => <article className="evidence-item" key={`${p.path}:${p.line}:${i}`}>
        <div className="row"><h3>{p.title}</h3><FileLink path={p.path} line={p.line} /><Badge>{p.confidence}</Badge></div>
        <p>{p.explanation}</p><details><summary>Source evidence</summary><pre>{p.evidence}</pre></details>
      </article>)}<Pagination offset={offset} data={data} change={(n) => setSp({ path, pattern, offset: String(n) })} />
    </Card>}
  </div>;
}

export function Changes() {
  const [sp, setSp] = useSearchParams();
  const base = sp.get("base") || "HEAD", comparison = sp.get("comparison") || "working";
  const depth = Number(sp.get("depth") || 3), mergeBase = sp.get("merge_base") === "1";
  const [draft, setDraft] = useState(base);
  useEffect(() => setDraft(base), [base]);
  const { data, error, loading, reload } = useData(() => api<any>("/api/changes", { base, comparison, depth, merge_base: mergeBase ? "1" : "0" }), [base, comparison, depth, mergeBase]);
  return <div className="intelligence"><h1>Git diff blast radius</h1>
    <p className="page-intro">Trace a change through code, configuration, and schemas to the consumers and tests worth checking.</p>
    <form className="query-toolbar" onSubmit={(e) => { e.preventDefault(); setSp({ base: draft, comparison, depth: String(depth), merge_base: mergeBase ? "1" : "0" }); reload(); }}>
      <label>Base ref<input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="HEAD or main" required /></label>
      <label>Compare with<select value={comparison} onChange={(e) => setSp({ base, comparison: e.target.value, depth: String(depth), merge_base: mergeBase ? "1" : "0" })}>
        <option value="working">Working tree</option><option value="staged">Staged changes</option><option value="branch">Branch HEAD</option>
      </select></label>
      <label>Dependency depth<select value={depth} onChange={(e) => setSp({ base, comparison, depth: e.target.value, merge_base: mergeBase ? "1" : "0" })}>
        {[1, 2, 3, 4, 5].map((n) => <option key={n}>{n}</option>)}
      </select></label>
      <label className="checkbox-label"><input type="checkbox" checked={mergeBase} onChange={(e) => setSp({ base, comparison, depth: String(depth), merge_base: e.target.checked ? "1" : "0" })} /> Use common ancestor</label>
      <button className="primary" type="submit" disabled={loading}>Analyze changes</button>
    </form>
    {(error || data?.error) && <p role="alert">{error || data?.error} <button onClick={reload}>Retry</button></p>}
    {loading && !data ? <Loading /> : data && !data.error && <>
      <div className="change-summary"><strong>{data.files?.length || 0}</strong> changed files <span>· {data.changed_symbols?.length || 0} symbols · {data.tests?.length || 0} candidate tests</span>{loading && <span role="status"> · Refreshing…</span>}</div>
      {data.budget_plan && <details className="evidence-item"><summary>Context budget · ~{data.budget_plan.budget.toLocaleString()} tokens · {data.budget_plan.policy}</summary>
        <p className="muted">The agent response budget adapts to repository scope, change size, dependency fanout, complexity, and verification needs.</p>
        <ul>{data.budget_plan.reasons.map((reason: string, i: number) => <li key={i}>{reason}</li>)}</ul>
      </details>}
      {data.truncated && <p className="notice">Analysis reached a limit. Narrow the comparison or increase the limits with the CLI or MCP tool.</p>}
      {data.limitations?.length > 0 && <details className="evidence-item"><summary>Coverage and limits</summary><ul>{data.limitations.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></details>}
      {!data.files?.length ? <Card>No changes in this comparison. Choose a base ref or make a local edit, then analyze again.</Card> : <div className="grid g2">
        <Card><h2>Changed files</h2>{data.files.map((f: any) => <div className="evidence-item" key={f.path}><Badge>{f.status}</Badge> <span className="mono">{f.path}</span>{f.old_path && <div className="muted">from {f.old_path}</div>}</div>)}</Card>
        <Card><h2>Candidate tests</h2>{data.tests?.length ? data.tests.map((t: any) => <div className="evidence-item" key={t.path}><FileLink path={t.path} /><p className="muted">{t.reasons?.join("; ")}</p></div>) : <p className="muted">No tests reached by the known relationships. This does not establish test coverage.</p>}</Card>
        <Card><h2>Affected files</h2>{(data.blast_radius?.files || []).map((f: any, i: number) => <div className="evidence-item" key={`${f.path}:${i}`}><FileLink path={f.path} /> <Badge>{f.depth} hops</Badge><p className="muted">{f.reasons?.join("; ")} · {f.confidence}</p></div>)}</Card>
        <Card><h2>Consumers to check</h2>{data.consumers?.length ? data.consumers.map((c: any, i: number) => <div className="evidence-item" key={i}><b>{c.qualified}</b> <FileLink path={c.path} line={c.line} /><p className="muted">Uses {c.uses?.join(", ")} · {c.confidence}</p></div>) : <p className="muted">No additional resolved symbol callers found.</p>}</Card>
      </div>}
    </>}
  </div>;
}
