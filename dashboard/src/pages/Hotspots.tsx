import { api, fmtN } from "../api";
import { useData } from "../live";
import { Card, FileLink, Kind, Loading, SymLink } from "../ui";

export default function Hotspots() {
  const { data: h } = useData(() => api("/api/hotspots", { limit: 20 }), []);
  const { data: u } = useData(() => api("/api/unreferenced", { limit: 40, exported: "1" }), []);
  if (!h) return <Loading />;
  const loc = (r: any) => <div className="muted small mono">{r.path}:{r.start_line}</div>;
  return (
    <>
      <h1>Hotspots</h1>
      <div className="grid g2">
        <Card title="Most complex functions">
          <table><thead><tr><th className="num">cx</th><th className="num">lines</th><th>symbol</th></tr></thead>
            <tbody>{h.most_complex.map((r: any, i: number) => <tr key={i}><td className="num">{r.complexity}</td><td className="num">{r.lines}</td><td><SymLink name={r.qualified} path={r.path} />{loc(r)}</td></tr>)}</tbody></table>
        </Card>
        <Card title="Longest functions">
          <table><thead><tr><th className="num">lines</th><th className="num">cx</th><th>symbol</th></tr></thead>
            <tbody>{h.longest.map((r: any, i: number) => <tr key={i}><td className="num">{r.lines}</td><td className="num">{r.complexity}</td><td><SymLink name={r.qualified} path={r.path} />{loc(r)}</td></tr>)}</tbody></table>
        </Card>
        <Card title="Largest files">
          <table><thead><tr><th className="num">lines</th><th className="num">sym</th><th>file</th></tr></thead>
            <tbody>{h.largest_files.map((r: any) => <tr key={r.path}><td className="num">{fmtN(r.lines)}</td><td className="num">{r.symbols}</td><td><FileLink path={r.path} /></td></tr>)}</tbody></table>
        </Card>
        <Card title="Key symbols (PageRank)">
          <table><thead><tr><th className="num">in</th><th>symbol</th></tr></thead>
            <tbody>{h.key_symbols.map((r: any, i: number) => <tr key={i}><td className="num">{r.in_deg}</td><td><SymLink name={r.qualified} path={r.path} /><div className="muted small mono">{r.path}</div></td></tr>)}</tbody></table>
        </Card>
        <Card title="Import cycles">
          {h.cycles.length ? h.cycles.map((c: string[], i: number) => <div key={i} className="small mono" style={{ marginBottom: 6 }}>{c.map((p, j) => <span key={p}>{j ? " → " : ""}<FileLink path={p} /></span>)}</div>) : <span className="muted">none</span>}
        </Card>
        <Card title="Unreferenced (dead-code candidates)">
          <table><thead><tr><th className="num">lines</th><th>symbol</th></tr></thead>
            <tbody>{(u || []).map((r: any) => <tr key={r.id}><td className="num">{r.lines}</td><td><Kind k={r.kind} /><SymLink name={r.qualified} path={r.path} />{loc(r)}</td></tr>)}</tbody></table>
        </Card>
      </div>
    </>
  );
}
