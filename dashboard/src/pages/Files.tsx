import { useState } from "react";
import { PatternHints, RelationshipHints } from "./Intelligence";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useData } from "../live";
import { Badge, Card, Empty, FileLink, Kind, SymLink } from "../ui";

interface TreeNode { name: string; files: number; lines: number; dirs: TreeNode[] }

function Dir({ n, prefix, onPick }: { n: TreeNode; prefix: string; onPick: (p: string) => void }) {
  const [open, setOpen] = useState(false);
  const p = prefix + n.name + "/";
  return (
    <li>
      <span className="d" onClick={() => setOpen(!open)}>{open ? "▾" : "▸"} {n.name}<span className="cnt">{n.files}</span></span>
      {open ? <ul>{n.dirs.map((d) => <Dir key={d.name} n={d} prefix={p} onPick={onPick} />)}<li><span className="f" onClick={() => onPick(p)}>… files in {n.name}</span></li></ul> : null}
    </li>
  );
}

export default function Files() {
  const [sp, setSp] = useSearchParams();
  const path = sp.get("path") || "";
  const [prefix, setPrefix] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const { data: tree } = useData(() => api<TreeNode>("/api/tree", { depth: 6 }), []);
  const { data: files } = useData(() => (prefix !== null ? api("/api/files", { prefix, limit: 500 }) : Promise.resolve(null)), [prefix]);
  const { data: o } = useData(() => (path ? api("/api/outline", { path }) : Promise.resolve(null)), [path]);
  const { data: dep } = useData(() => (path ? api("/api/dependencies", { path }) : Promise.resolve(null)), [path]);
  const pick = (p: string) => setSp({ path: p });
  return (
    <>
      <h1>Files</h1>
      <div className="split" style={{ gridTemplateColumns: "320px 1fr" }}>
        <Card className="tree">
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter path… (Enter)" style={{ width: "100%", marginBottom: 8 }} onKeyDown={(e) => e.key === "Enter" && setPrefix(filter)} />
          <div className="list">
            <ul>{tree?.dirs.map((d) => <Dir key={d.name} n={d} prefix="" onPick={setPrefix} />)}<li><span className="f" onClick={() => setPrefix("")}>… all files</span></li></ul>
            {files ? <><h3>{files.length} files</h3>{files.map((f: any) => <div key={f.path} className="f small mono" onClick={() => pick(f.path)}>{f.path} <span className="cnt">{f.lines}L</span></div>)}</> : null}
          </div>
        </Card>
        <Card>
          {!path ? <Empty>Pick a file to see its outline (what the agent gets from <code>file_outline</code>)</Empty> :
            !o ? <Empty>Loading…</Empty> : o.error ? <Empty>{path} is not in the index (unsupported type or ignored). Pick a source file.</Empty> : <Outline o={o} dep={dep} />}
        </Card>
      </div>
    </>
  );
}

function Outline({ o, dep }: { o: any; dep: any }) {
  const byP: Record<string, any[]> = {};
  o.symbols.forEach((s: any) => ((byP[s.parent_id ?? "root"] = byP[s.parent_id ?? "root"] || []).push(s)));
  const emit = (pid: string, depth: number): React.ReactNode => (byP[pid] || []).map((s: any) => (
    <div key={s.id}>
      <div className="small" style={{ paddingLeft: depth * 18 }}>
        <Kind k={s.kind} /><SymLink name={s.qualified} path={o.path} /> <span className="muted">{(s.signature || "").replace(s.name, "").slice(0, 80)}</span> <span className="muted mono">:{s.start_line}-{s.end_line}</span>
        {s.ref_files ? <> <Badge>←{s.ref_files}f</Badge></> : null}{s.complexity >= 15 ? <> <Badge cls="p2">cx{s.complexity}</Badge></> : null}
      </div>
      {emit(String(s.id), depth + 1)}
    </div>
  ));
  return (
    <>
      <div className="row"><h2 className="mono">{o.path}</h2><Badge>{o.lang}</Badge><Badge>{o.lines} lines</Badge><Badge>{o.symbols.length} symbols</Badge></div>
      <div className="list" style={{ marginTop: 10 }}>{o.symbols.length ? emit("root", 0) : <span className="muted">no symbols</span>}</div>
      <div className="grid g2" style={{ marginTop: 12 }}>
        <div><h3>Imports (repo)</h3>{dep?.imports_internal?.length ? dep.imports_internal.map((i: any) => <div key={i.path} className="small"><FileLink path={i.path} /> <span className="muted">{i.names.slice(0, 5).join(", ")}</span></div>) : <span className="muted small">none resolved</span>}
          {dep?.imports_external?.length ? <><h3>External</h3><div className="small muted">{dep.imports_external.join(", ")}</div></> : null}</div>
        <div><h3>Imported by ({dep?.imported_by?.length || 0})</h3>{dep?.imported_by?.length ? dep.imported_by.map((p: string) => <div key={p} className="small"><FileLink path={p} /></div>) : <span className="muted small">nobody</span>}</div>
      </div>
      {o.findings?.length ? <><h3>Findings</h3>{o.findings.map((f: any, i: number) => <div key={i} className="small"><span className="tag">{f.code}</span> :{f.line} {f.message}</div>)}</> : null}
      <PatternHints data={o.patterns} />
      <RelationshipHints data={o.relationships} />
    </>
  );
}
