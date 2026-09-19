import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useData } from "../live";
import { Badge, Card, FileLink, Loading } from "../ui";

export default function Findings() {
  const [sp] = useSearchParams();
  const code = sp.get("code") || "";
  const { data: rows } = useData(() => api("/api/findings", { code, limit: 500 }), [code]);
  const { data: all } = useData(() => api("/api/overview"), []);
  if (!rows) return <Loading />;
  const codes: string[] = all?.findings?.map((f: any) => f.code) || [];
  return (
    <>
      <h1>Findings</h1>
      <div className="row" style={{ marginBottom: 10 }}>
        <Link to="/findings"><Badge cls={!code ? "p3" : ""}>all</Badge></Link>
        {codes.map((c) => <Link key={c} to={`/findings?code=${c}`}><Badge cls={code === c ? "p3" : ""}>{c}</Badge></Link>)}
        <span className="muted small">{rows.length} shown</span>
      </div>
      <Card>
        <table><thead><tr><th>Code</th><th>Location</th><th>Message</th></tr></thead>
          <tbody>{rows.length ? rows.map((r: any, i: number) => <tr key={i}><td><span className="tag">{r.code}</span></td><td><FileLink path={r.path} line={r.line} /></td><td>{r.message}</td></tr>) : <tr><td colSpan={3} className="muted">no findings</td></tr>}</tbody></table>
      </Card>
    </>
  );
}
