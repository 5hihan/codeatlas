import { age } from "../api";
import { useLiveCtx } from "../live";
import { Card, Loading } from "../ui";

export default function Activity() {
  const { status, events, connected } = useLiveCtx();
  if (!status) return <Loading />;
  const w = status.watcher;
  return (
    <>
      <h1>Activity</h1>
      <div className="grid g2">
        <Card title="Watcher">
          <table><tbody>
            <tr><td>event stream</td><td>{connected ? <><span className="dot on" /> connected</> : <><span className="dot off" /> reconnecting</>}</td></tr>
            <tr><td>backend</td><td className="mono">{w?.backend}</td></tr>
            <tr><td>alive</td><td>{w?.alive ? <><span className="dot on" /> yes</> : "no"}</td></tr>
            <tr><td>events seen</td><td className="mono">{w?.events}</td></tr>
            <tr><td>last file event</td><td>{age(w?.last_event)}</td></tr>
            <tr><td>safety rescan</td><td>every {w?.rescan_every}s</td></tr>
            <tr><td>uptime</td><td>{status.uptime_s}s</td></tr>
          </tbody></table>
          <p className="muted small">Every file save is re-indexed within about a second. Every MCP tool call also verifies freshness, so agents never read stale data.</p>
        </Card>
        <Card title="Recent events">
          {events.length ? events.map((e, i) => (
            <div key={i} className="ev"><span className="t">{new Date(e.t * 1000).toLocaleTimeString()}</span>
              <span>{e.type === "tool" ? <><span className="tag">{e.tool}</span> {JSON.stringify(e.args)} <span className="muted">{e.ms}ms</span></> :
                e.type === "watch" || e.type === "index" ? <><span className="tag">{e.source || e.type}</span> parsed {e.parsed}, removed {e.removed}, refs {e.ref_files} in {e.seconds}s</> :
                  JSON.stringify(e)}</span></div>
          )) : <div className="muted">nothing yet</div>}
        </Card>
      </div>
    </>
  );
}
