import { useEffect, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useData } from "../live";
import { Card, Loading } from "../ui";

interface GNode { id: string; files: number; x: number; y: number; vx: number; vy: number; fixed?: boolean }

export default function Modules() {
  const [sp, setSp] = useSearchParams();
  const depth = Number(sp.get("depth") || 2);
  const { data: g } = useData(() => api("/api/modules", { depth, min: 1 }), [depth]);
  const ref = useRef<SVGSVGElement>(null);
  const nav = useNavigate();

  useEffect(() => {
    const svg = ref.current;
    if (!svg || !g) return;
    svg.innerHTML = "";
    const W = svg.clientWidth || 1000, H = 640;
    const nodes: GNode[] = g.nodes.slice(0, 80).map((n: any, i: number) => ({ ...n, x: W / 2 + Math.cos(i) * 220 + Math.random() * 40, y: H / 2 + Math.sin(i) * 200 + Math.random() * 40, vx: 0, vy: 0 }));
    const byId: Record<string, GNode> = {};
    nodes.forEach((n) => (byId[n.id] = n));
    const edges = g.edges.filter((e: any) => byId[e.from] && byId[e.to]);
    const NS = "http://www.w3.org/2000/svg";
    svg.insertAdjacentHTML("afterbegin", '<defs><marker id="arrow" viewBox="0 0 10 10" refX="18" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0L10,5L0,10z" fill="#3a4664"/></marker></defs>');
    const L = edges.map((e: any) => { const l = document.createElementNS(NS, "line"); l.setAttribute("stroke", "#2e3850"); l.setAttribute("stroke-width", String(Math.min(6, 1 + Math.log2(e.count)))); l.setAttribute("marker-end", "url(#arrow)"); svg.appendChild(l); return l; });
    let dragging: GNode | null = null;
    const G = nodes.map((n) => {
      const gEl = document.createElementNS(NS, "g");
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("r", String(6 + Math.min(22, Math.sqrt(n.files) * 2))); c.setAttribute("fill", "#161c29"); c.setAttribute("stroke", "#7aa2ff"); c.setAttribute("stroke-width", "1.5");
      const t = document.createElementNS(NS, "text"); t.textContent = n.id; t.setAttribute("dy", "4"); t.setAttribute("text-anchor", "middle");
      gEl.appendChild(c); gEl.appendChild(t); (gEl as any).style.cursor = "grab"; svg.appendChild(gEl);
      gEl.addEventListener("mousedown", () => { dragging = n; n.fixed = true; });
      gEl.addEventListener("dblclick", () => nav(`/files?path=${encodeURIComponent(n.id + "/")}`));
      return gEl;
    });
    const onMove = (e: MouseEvent) => { if (dragging) { const r = svg.getBoundingClientRect(); dragging.x = e.clientX - r.left; dragging.y = e.clientY - r.top; } };
    const onUp = () => { dragging = null; };
    svg.addEventListener("mousemove", onMove); window.addEventListener("mouseup", onUp);
    let it = 0, raf = 0;
    const step = () => {
      it++;
      for (const a of nodes) for (const b of nodes) { if (a === b) continue; const dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01, f = 2200 / d2; a.vx += dx * f; a.vy += dy * f; }
      for (const e of edges) { const a = byId[e.from], b = byId[e.to]; const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) + 0.01, f = (d - 140) * 0.004 * Math.min(3, Math.log2(1 + e.count)); a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f; }
      for (const n of nodes) { n.vx += (W / 2 - n.x) * 0.002; n.vy += (H / 2 - n.y) * 0.002; if (!n.fixed) { n.x += n.vx * 0.35; n.y += n.vy * 0.35; } n.vx *= 0.6; n.vy *= 0.6; n.x = Math.max(30, Math.min(W - 30, n.x)); n.y = Math.max(20, Math.min(H - 20, n.y)); }
      nodes.forEach((n, i) => G[i].setAttribute("transform", `translate(${n.x},${n.y})`));
      edges.forEach((e: any, i: number) => { const a = byId[e.from], b = byId[e.to]; L[i].setAttribute("x1", String(a.x)); L[i].setAttribute("y1", String(a.y)); L[i].setAttribute("x2", String(b.x)); L[i].setAttribute("y2", String(b.y)); });
      if (it < 400 || dragging) raf = requestAnimationFrame(step); else raf = requestAnimationFrame(() => { it = 399; step(); });
    };
    step();
    return () => { cancelAnimationFrame(raf); svg.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
  }, [g, nav]);

  if (!g) return <Loading />;
  return (
    <>
      <h1>Module graph</h1>
      <div className="row card" style={{ marginBottom: 12 }}>
        <label style={{ margin: 0 }}>Depth</label>
        <select value={depth} onChange={(e) => setSp({ depth: e.target.value })}>{[1, 2, 3, 4].map((d) => <option key={d} value={d}>{d}</option>)}</select>
        <span className="muted small">Directory-level import edges. Node size = files, edge width = number of imports. Drag nodes; double-click opens the directory.</span>
      </div>
      <Card className="card" ><svg ref={ref} width="100%" height="640" /></Card>
    </>
  );
}
