import { useState } from "react";
import { PatternHints } from "./Intelligence";
import { useSearchParams } from "react-router-dom";
import { api, type Sym } from "../api";
import { useData } from "../live";
import { Badge, Card, Empty, FileLink, Kind, SymLink } from "../ui";

const KINDS = ["function", "method", "class", "interface", "type", "enum", "struct", "module"];

export default function Explore() {
  const [sp, setSp] = useSearchParams();
  const q = sp.get("q") || "";
  const kind = sp.get("kind") || "";
  const name = sp.get("name") || "";
  const path = sp.get("path") || "";
  const [text, setText] = useState(q);
  const { data: results } = useData(() => (q ? api<Sym[]>("/api/search", { q, kind, limit: 60 }) : Promise.resolve([])), [q, kind]);
  const { data: d } = useData(() => (name ? api("/api/symbol", { name, path }) : Promise.resolve(null)), [name, path]);
  const search = () => setSp({ q: text, kind, name, path });
  return (
    <>
      <h1>Explore symbols</h1>
      <div className="split">
        <Card>
          <div className="row">
            <input value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && search()} placeholder="name, Class.method, or CamelCase words" style={{ flex: 1 }} />
            <select value={kind} onChange={(e) => setSp({ q, kind: e.target.value, name, path })}><option value="">any</option>{KINDS.map((k) => <option key={k}>{k}</option>)}</select>
            <button className="primary" onClick={search}>Find</button>
          </div>
          <div className="list" style={{ marginTop: 10 }}>
            {results?.length ? results.map((s) => (
              <div key={s.id} className="sym" style={{ padding: "4px 0" }} onClick={() => setSp({ q, kind, name: s.qualified, path: s.path })}>
                <Kind k={s.kind} /><b>{s.qualified}</b> <span className="muted small">←{s.ref_files}f</span>
                <div className="muted small mono">{s.path}:{s.start_line}</div>
              </div>
            )) : q ? <div className="muted">no matches</div> : <div className="muted small">Type a name and press Enter. Exact, prefix, substring and fuzzy CamelCase matches are tried in order.</div>}
          </div>
        </Card>
        <Card>{name ? <Detail d={d} /> : <Empty>Search or pick a symbol</Empty>}</Card>
      </div>
    </>
  );
}

function Detail({ d }: { d: any }) {
  if (!d) return <Empty>Loading…</Empty>;
  if (!d.found) return <Empty>not found{d.similar?.length ? <><br />similar: {d.similar.map((s: Sym, i: number) => <span key={i}>{i ? ", " : ""}<SymLink name={s.qualified} path={s.path} /></span>)}</> : null}</Empty>;
  const im = d.impact || {}, tr = d.triggers || {};
  return (
    <>
      <div className="row">
        <h2><Kind k={d.kind} />{d.qualified}</h2>
        <Badge>{d.lines} lines</Badge><Badge>cx {d.complexity}</Badge><Badge>{d.exported ? "exported" : "private"}</Badge><Badge>←{d.ref_files} files</Badge>
        <span className="spacer" /><FileLink path={d.path} line={d.start_line} /><span className="mono muted">-{d.end_line}</span>
      </div>
      <pre style={{ margin: "10px 0" }}>{d.signature}</pre>
      {d.doc ? <p className="muted">{d.doc}</p> : null}
      {d.decorators?.length ? <p>{d.decorators.map((x: string, i: number) => <span key={i} className="tag" style={{ marginRight: 6 }}>{x}</span>)}</p> : null}
      {d.ambiguous?.length ? <p className="small muted">also defined: {d.ambiguous.map((a: any, i: number) => <span key={i}>{i ? "; " : ""}<SymLink name={a.qualified} path={a.path} /> <span className="mono">{a.path}</span></span>)}</p> : null}
      <div className="grid g3">
        <div><h3>Called by ({d.callers.length})</h3>
          {d.callers.length ? d.callers.map((c: any, i: number) => <div key={i} className="small"><SymLink name={c.qualified} path={c.path} /> <span className="muted mono">{c.path.split("/").pop()}:{c.line}{c.via !== "call" ? ` [${c.via}]` : ""}</span></div>) : <span className="muted small">none found</span>}</div>
        <div><h3>Calls ({d.callees.length})</h3>
          {d.callees.length ? d.callees.map((c: any, i: number) => <div key={i} className="small">{c.target?.path ? <SymLink name={c.target.qualified} path={c.target.path} /> : <span className="muted">{(c.qualifier ? c.qualifier + "." : "") + c.name}{c.target ? ` (${c.target.resolution})` : ""}</span>}{c.count > 1 ? <span className="muted"> ×{c.count}</span> : null}</div>) : <span className="muted small">none</span>}</div>
        <div><h3>Triggers</h3>
          {(tr.trigger_decorators || []).map((x: string, i: number) => <div key={i} className="tag">{x}</div>)}
          {(tr.registrations || []).length ? (tr.registrations || []).map((r: any, i: number) => <div key={i} className="small">{r.event_like ? "⚡ " : ""}<b>{r.via || "?"}</b>(…) <span className="muted mono">{r.path.split("/").pop()}:{r.line}</span></div>) : <span className="muted small">no registrations</span>}
          <h3>Impact (2 hops)</h3><div className="small">{im.affected_symbols || 0} symbols in {im.affected_files || 0} files{im.tests?.length ? `; tests: ${im.tests.map((t: string) => t.split("/").pop()).join(", ")}` : ""}</div></div>
      </div>
      {d.children?.length ? <><h3>Members ({d.children.length})</h3><div className="list" style={{ maxHeight: 220 }}>{d.children.map((c: any) => <div key={c.id} className="small"><Kind k={c.kind} /><SymLink name={`${d.qualified}.${c.name}`} path={d.path} /> <span className="muted mono">:{c.start_line}</span> <span className="muted">{(c.signature || "").slice(0, 90)}</span></div>)}</div></> : null}
      <PatternHints data={d.patterns} />
      <h3>Source</h3><pre>{d.source?.text || ""}</pre>
    </>
  );
}
