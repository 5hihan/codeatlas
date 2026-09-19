import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useData } from "../live";

export default function RepoMap() {
  const [sp, setSp] = useSearchParams();
  const budget = sp.get("budget") || "";
  const focus = sp.get("focus") || "";
  const [b, setB] = useState(Number(budget || 3000));
  const [automatic, setAutomatic] = useState(!budget);
  const apply = () => setSp({ budget: automatic ? "" : String(b), focus: f });
  const [f, setF] = useState(focus);
  const { data, loading } = useData(() => api<string>("/api/map", { budget, focus }), [budget, focus]);
  return (
    <>
      <h1>Repo map</h1>
      <div className="row card" style={{ marginBottom: 12 }}>
        <label style={{ margin: 0 }}><input type="checkbox" checked={automatic} onChange={(e) => setAutomatic(e.target.checked)} /> Adaptive budget</label>
        <label htmlFor="map-budget" style={{ margin: 0 }}>Token budget</label>
        <input id="map-budget" disabled={automatic} type="range" min={500} max={12000} step={250} value={b} onChange={(e) => setB(Number(e.target.value))} />
        <span className="mono">{automatic ? "Auto" : b}</span>
        <label htmlFor="map-focus" style={{ margin: "0 0 0 12px" }}>Focus path</label>
        <input id="map-focus" value={f} onChange={(e) => setF(e.target.value)} placeholder="backend/src" style={{ width: 240 }} onKeyDown={(e) => e.key === "Enter" && apply()} />
        <button className="primary" onClick={apply}>Render</button>
        <span className="muted small">Exactly what an agent gets from <code>repo_map</code>. Updates live as the index changes.</span>
      </div>
      <pre style={{ opacity: loading ? 0.6 : 1 }}>{data ?? "…"}</pre>
    </>
  );
}
