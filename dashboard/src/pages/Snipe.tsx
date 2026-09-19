import { useState } from "react";
import { api, post, type SnipeBoard, type SnipeItem } from "../api";
import { useData, useLiveCtx } from "../live";
import { Badge, Card, FileLink, Icon, Loading, Modal, SymLink } from "../ui";

const STATES = ["FOUND", "TARGETED", "FIXING", "VERIFYING", "DONE", "BLOCKED"];
const PRIORITIES = ["SNIPE NOW", "INVESTIGATE", "DEFER", "IGNORE"];

function riskCls(r: string) { return r === "high" ? "p1" : r === "medium" ? "p2" : "p3"; }

export default function Snipe() {
  const { toast, status, connected } = useLiveCtx();
  const { data, reload } = useData(() => api<{ board: SnipeBoard; items: SnipeItem[] }>("/api/snipe"), [], ["snipe", "index"]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [reporting, setReporting] = useState(false);
  const [adding, setAdding] = useState(false);
  const [showOpps, setShowOpps] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  if (!data) return <Loading />;
  const b = data.board;
  const w = status?.watcher;
  const run = async (label: string, fn: () => Promise<any>) => {
    setBusy(label);
    try { const r = await fn(); return r; } catch (e: any) { toast(String(e.message || e), "bad"); } finally { setBusy(null); reload(); }
  };
  const scan = (runWhat?: string) => run("scan", async () => {
    toast(runWhat ? `running ${runWhat}…` : "scanning…");
    const r = await post("/api/snipe/scan", { run: runWhat });
    toast(`${r.created.length} new · ${r.issues_open} issues open · ${r.opportunities} opportunities` + (r.run?.error ? ` · ${r.run.error}` : ""), r.run?.error ? "bad" : "ok");
  });
  const next = () => run("next", async () => { const r = await post("/api/snipe/next", {}); if (!r.item) return toast("nothing ready to snipe"); setOpenId(r.item.id); });
  const verify = (id: string, deep = false) => run("verify", async () => { const r = await post("/api/snipe/verify", { id, deep }); toast(`${id}: ${r.status}`, r.status === "PASS" ? "ok" : r.status === "FAIL" || r.status === "ABORTED" ? "bad" : "info"); });
  const Row = ({ i, showState }: { i: SnipeItem; showState?: boolean }) => (
    <div className="item" style={{ cursor: "pointer" }} onClick={() => setOpenId(i.id)}>
      <div className="meta">
        <b style={{ fontFamily: "var(--mono)", fontSize: 16, minWidth: 34 }}>{i.score}</b>
        <span className="mono muted">{i.id}</span>
        {showState ? <Badge>{i.state}</Badge> : null}
        {i.owner ? <Badge>{i.owner}</Badge> : null}
        <span className="spacer" />
        <Badge cls={riskCls(i.risk)}>{i.risk} risk</Badge>
      </div>
      <div className="t">{i.problem}</div>
      <div className="files">{i.symbols} symbol{i.symbols === 1 ? "" : "s"} · ~{i.tokens >= 1000 ? (i.tokens / 1000).toFixed(1) + "k" : i.tokens} tokens{i.suspect ? ` · ${i.suspect}` : ""}</div>
    </div>
  );
  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <h1 style={{ margin: 0 }}>SNIPE</h1>
        <span className="live"><span className="dot on" />scan · narrow · infer · patch · evaluate</span>
        <span className="spacer" />
        <button onClick={() => scan()} disabled={!!busy}><Icon id="scan" />Scan</button>
        <button onClick={() => scan("tests")} disabled={!!busy}><Icon id="play" />Run tests → issues</button>
        <button onClick={() => setReporting(true)}>Report failure…</button>
        <button onClick={() => setAdding(true)}><Icon id="plus" />Add</button>
        <button className="primary" onClick={next} disabled={!!busy}>Next snipe</button>
        <a href="/api/snipe.md" target="_blank" rel="noreferrer"><Badge>SNIPE.md</Badge></a>
      </div>
      <div className="row card" style={{ marginBottom: 12, gap: 18 }}>
        <span><span className="dot on" /> Graph</span>
        <span><span className={`dot ${w?.alive ? "on" : "off"}`} /> Watcher</span>
        <span><span className={`dot ${b.test_cmd ? "on" : ""}`} /> Tests {b.test_cmd ? <span className="muted mono small">{b.test_cmd}</span> : <span className="muted small">not configured</span>}</span>
        <span><span className={`dot ${connected ? "on" : "off"}`} /> MCP</span>
        <span className="spacer" />
        <span className="muted small">hot symbols {b.hot} · done {b.done_total} · deferred {b.deferred}</span>
      </div>
      <div className="grid g2">
        <Card title={<>READY TO SNIPE <span className="muted small">{b.ready.length}</span></>}>
          {b.ready.length ? b.ready.map((i) => <Row key={i.id} i={i} />) : <div className="empty">Nothing ready. Scan, run tests, or report a failure.</div>}
        </Card>
        <div className="grid" style={{ alignContent: "start" }}>
          <Card title={<>CURRENT <span className="muted small">{b.current.length}</span></>}>
            {b.current.length ? b.current.map((i) => (
              <div key={i.id} className="item" onClick={() => setOpenId(i.id)}>
                <div className="meta"><Badge cls="p3">{i.owner || "?"}</Badge><span className="mono muted">{i.id}</span><Badge>{i.state}</Badge><span className="spacer" />
                  <button className="small" onClick={(e) => { e.stopPropagation(); verify(i.id); }} disabled={!!busy}>Verify</button></div>
                <div className="t">{i.problem}</div>
              </div>
            )) : <div className="muted small">no agent is working on anything</div>}
          </Card>
          <Card title={<>INVESTIGATING <span className="muted small">{b.investigating.length}</span></>}>
            {b.investigating.length ? b.investigating.map((i) => <Row key={i.id} i={i} showState />) : <div className="muted small">nothing under investigation</div>}
          </Card>
          <Card title={<span className="sym" onClick={() => setShowOpps(!showOpps)}>{showOpps ? "▾" : "▸"} OPPORTUNITIES <span className="muted small">{b.opportunities_total} dormant</span></span>}>
            {showOpps ? b.opportunities.map((i) => <Row key={i.id} i={i} />) : <div className="muted small">refactors, cleanups and cycles stay here until you pick one</div>}
          </Card>
        </div>
      </div>
      {b.done.length ? <Card title={<>DONE <span className="muted small">last {b.done.length} of {b.done_total}</span></>} className="card">
        {b.done.map((i) => <div key={i.id} className="small"><span className="mono muted">{i.id}</span> {i.problem}</div>)}
      </Card> : null}
      <Card title="Log" className="card">{b.log.map((l, i) => <div key={i} className="ev"><span className="t">{l.t.slice(5)}</span><span>{l.msg}</span></div>)}</Card>
      <Modal open={openId !== null} onClose={() => setOpenId(null)}>{openId ? <ItemView id={openId} items={data.items} onDone={() => { setOpenId(null); reload(); }} verify={verify} /> : null}</Modal>
      <Modal open={reporting} onClose={() => setReporting(false)}><ReportForm onDone={(n) => { setReporting(false); toast(`${n} issue(s) created`, "ok"); reload(); }} /></Modal>
      <Modal open={adding} onClose={() => setAdding(false)}><AddForm onDone={() => { setAdding(false); reload(); }} /></Modal>
    </>
  );
}

function ItemView({ id, items, onDone, verify }: { id: string; items: SnipeItem[]; onDone: () => void; verify: (id: string, deep?: boolean) => void }) {
  const it = items.find((x) => x.id === id);
  const [level, setLevel] = useState(1);
  const { data: ctx } = useData(() => api<string>("/api/snipe/context", { id, level }), [id, level], ["snipe"]);
  const [note, setNote] = useState("");
  const [state, setState] = useState(it?.state || "FOUND");
  const [priority, setPriority] = useState(it?.priority || "INVESTIGATE");
  if (!it) return <div className="empty">gone</div>;
  return (
    <div className="form">
      <div className="row"><b style={{ fontFamily: "var(--mono)", fontSize: 20 }}>{it.score}</b><span className="mono muted">{it.id}</span><Badge>{it.state}</Badge><Badge cls="p3">{it.priority}</Badge><Badge cls={riskCls(it.risk)}>{it.risk} risk</Badge>{it.owner ? <Badge>{it.owner}</Badge> : null}<span className="spacer" /><span className="muted small">{it.source}</span></div>
      <h2 style={{ margin: "10px 0" }}>{it.problem}</h2>
      {it.evidence?.length ? <div className="small">{it.evidence.map((e, i) => <div key={i} className="muted">• {e}</div>)}</div> : null}
      {it.suspect ? <p className="small">suspect <SymLink name={it.suspect} path={it.suspect_path || undefined} /> {it.suspect_path ? <FileLink path={it.suspect_path} line={it.suspect_line || undefined} /> : null} · confidence {Math.round((it.confidence || 0) * 100)}%</p> : null}
      {it.candidates?.length ? <table><thead><tr><th>conf</th><th>candidate</th><th>evidence</th></tr></thead><tbody>{it.candidates.map((c, i) => <tr key={i}><td className="num">{c.confidence.toFixed(2)}</td><td><SymLink name={c.symbol} path={c.path} /><div className="muted small mono">{c.path}:{c.line}</div></td><td className="small">{c.evidence.map((e, j) => <div key={j}>+ {e}</div>)}{c.counter.map((e, j) => <div key={j} className="muted">− {e}</div>)}</td></tr>)}</tbody></table> : null}
      {it.verification?.length ? <p className="small">verification: {it.verification.map((t) => <span key={t}><FileLink path={t} /> </span>)}</p> : null}
      <div className="row" style={{ marginTop: 8 }}><label style={{ margin: 0 }}>Context level</label>{[0, 1, 2, 3, 4, 5, 6].map((l) => <button key={l} className={`small ${level === l ? "primary" : ""}`} onClick={() => setLevel(l)}>{l}</button>)}<span className="muted small">climb only as high as needed</span></div>
      <pre style={{ marginTop: 8, maxHeight: 360 }}>{ctx ?? "…"}</pre>
      <div className="row" style={{ marginTop: 12 }}>
        <select value={state} onChange={(e) => setState(e.target.value)}>{STATES.map((s) => <option key={s}>{s}</option>)}</select>
        <select value={priority} onChange={(e) => setPriority(e.target.value)}>{PRIORITIES.map((s) => <option key={s}>{s}</option>)}</select>
        <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="note" style={{ flex: 1 }} />
        <button onClick={async () => { await post("/api/snipe/update", { id: it.id, state, priority, note: note || null, agent: "dashboard" }); onDone(); }}>Save</button>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        <button onClick={async () => { await post("/api/snipe/claim", { id: it.id, agent: "dashboard" }); onDone(); }}>Claim</button>
        <button onClick={() => { verify(it.id); onDone(); }}>Verify</button>
        <button onClick={() => { verify(it.id, true); onDone(); }}>Verify (deep)</button>
        <button onClick={async () => { const reason = prompt("Why is this no longer a snipe?") || "aborted"; await post("/api/snipe/abort", { id: it.id, reason }); onDone(); }}>Abort snipe</button>
        <span className="spacer" /><button onClick={onDone}>Close</button>
      </div>
    </div>
  );
}

function ReportForm({ onDone }: { onDone: (n: number) => void }) {
  const [text, setText] = useState("");
  const [source, setSource] = useState("test");
  return (
    <div className="form">
      <h2>Report a failure</h2>
      <p className="muted small">Paste test, typecheck, lint or runtime output. Each failing test / distinct location becomes an issue and is narrowed to a suspect symbol.</p>
      <select value={source} onChange={(e) => setSource(e.target.value)}>{["test", "typecheck", "lint", "runtime"].map((s) => <option key={s}>{s}</option>)}</select>
      <textarea rows={12} value={text} onChange={(e) => setText(e.target.value)} style={{ fontFamily: "var(--mono)", marginTop: 8 }} />
      <div className="row" style={{ marginTop: 12 }}><span className="spacer" /><button className="primary" disabled={!text.trim()} onClick={async () => { const r = await post("/api/snipe/report", { text, source }); onDone(r.created.length); }}>Create issues</button></div>
    </div>
  );
}

function AddForm({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState({ problem: "", evidence: "", kind: "issue", suspect: "" });
  const set = (k: string, v: string) => setF({ ...f, [k]: v });
  return (
    <div className="form">
      <h2>New record</h2>
      <label>Problem</label><input value={f.problem} onChange={(e) => set("problem", e.target.value)} autoFocus />
      <label>Kind</label><select value={f.kind} onChange={(e) => set("kind", e.target.value)}><option value="issue">issue (defect: attacked automatically)</option><option value="opportunity">opportunity (dormant)</option></select>
      <label>Evidence (one per line: path:line, symbol names, messages)</label><textarea rows={4} value={f.evidence} onChange={(e) => set("evidence", e.target.value)} />
      <label>Suspect symbol (optional)</label><input value={f.suspect} onChange={(e) => set("suspect", e.target.value)} />
      <div className="row" style={{ marginTop: 12 }}><span className="spacer" /><button onClick={onDone}>Cancel</button>
        <button className="primary" disabled={!f.problem.trim()} onClick={async () => { await post("/api/snipe/add", { ...f, evidence: f.evidence.split("\n").map((s) => s.trim()).filter(Boolean), suspect: f.suspect || null }); onDone(); }}>Add</button></div>
    </div>
  );
}
