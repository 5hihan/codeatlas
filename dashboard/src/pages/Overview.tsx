import { Link } from "react-router-dom";
import { api, fmtN, isTest } from "../api";
import { useData } from "../live";
import { Card, FileLink, Kind, Loading, SymLink, Badge } from "../ui";

const COLORS = ["#7aa2ff", "#5ce0b8", "#ffb454", "#c792ea", "#ff6b6b", "#82aaff", "#f78c6c", "#89ddff"];

export default function Overview() {
  const { data: o } = useData(() => api("/api/overview"), []);
  const { data: top } = useData(() => api("/api/top", { limit: 40 }), []);
  if (!o) return <Loading />;
  const total = o.languages.reduce((a: number, l: any) => a + l.lines, 0) || 1;
  const key = (top?.symbols || []).filter((s: any) => !isTest(s.path)).slice(0, 14);
  return (
    <>
      <h1>Overview</h1>
      <div className="grid g2">
        <Card title="Languages">
          <div className="bar">{o.languages.map((l: any, i: number) => <i key={l.lang} style={{ width: `${(100 * l.lines) / total}%`, background: COLORS[i % 8] }} title={l.lang} />)}</div>
          <table><thead><tr><th>Language</th><th className="num">Files</th><th className="num">Lines</th></tr></thead>
            <tbody>{o.languages.map((l: any, i: number) => <tr key={l.lang}><td><span className="dot" style={{ background: COLORS[i % 8], marginRight: 6 }} />{l.lang}</td><td className="num">{l.files}</td><td className="num">{fmtN(l.lines)}</td></tr>)}</tbody></table>
          <p className="muted small">{fmtN(o.files)} files, {fmtN(o.lines)} lines and {fmtN(o.symbols)} symbols: {o.kinds.map((k: any) => `${k.kind} ${k.count}`).join(", ")}</p>
        </Card>
        <Card title="Top directories">
          <table><thead><tr><th>Directory</th><th className="num">Files</th><th className="num">Lines</th></tr></thead>
            <tbody>{o.top_dirs.slice(0, 14).map((d: any) => <tr key={d.dir}><td><FileLink path={d.dir} /></td><td className="num">{d.files}</td><td className="num">{fmtN(d.lines)}</td></tr>)}</tbody></table>
        </Card>
        <Card title={<>Hub files <span className="muted small">imported by N files</span></>}>
          <table><tbody>{o.hubs.length ? o.hubs.map((h: any) => <tr key={h.path}><td className="num">{h.imported_by}</td><td><FileLink path={h.path} /></td></tr>) : <tr><td className="muted">no resolved imports</td></tr>}</tbody></table>
        </Card>
        <Card title={<>Key symbols <span className="muted small">PageRank</span></>}>
          <table><tbody>{key.map((s: any) => <tr key={s.id}><td><Kind k={s.kind} /><SymLink name={s.qualified} path={s.path} /></td><td className="muted small mono">{s.path}:{s.start_line}</td><td className="num">{s.in_deg}</td></tr>)}</tbody></table>
        </Card>
        <Card title="Entry-point candidates">
          <table><tbody>{o.entry_points.length ? o.entry_points.map((e: any, i: number) => <tr key={i}><td><Kind k={e.kind} /><SymLink name={e.symbol} path={e.path} /></td><td className="muted small mono">{e.path}:{e.line}</td></tr>) : <tr><td className="muted">none detected</td></tr>}</tbody></table>
        </Card>
        <Card title="Findings">
          {o.findings.length ? o.findings.map((f: any) => <Link key={f.code} to={`/findings?code=${f.code}`} style={{ margin: 3, display: "inline-block" }}><Badge>{f.code} <b>{f.count}</b></Badge></Link>) : <span className="muted">none</span>}
          <p className="muted small">Static findings feed SNIPE as dormant opportunities; the <code>snipe_scan</code> tool and every index delta turn concrete defects into ready-to-snipe issues.</p>
        </Card>
      </div>
    </>
  );
}
