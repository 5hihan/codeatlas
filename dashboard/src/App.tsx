import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { age, fmtN, post } from "./api";
import { useLiveCtx } from "./live";
import { Icon, Icons } from "./ui";
import Overview from "./pages/Overview";
import RepoMap from "./pages/RepoMap";
import Explore from "./pages/Explore";
import Files from "./pages/Files";
import Hotspots from "./pages/Hotspots";
import Modules from "./pages/Modules";
import Findings from "./pages/Findings";
import Snipe from "./pages/Snipe";
import Activity from "./pages/Activity";
import { Changes, Patterns, Relationships } from "./pages/Intelligence";

const NAV = [
  { sep: "Code" },
  { to: "/overview", icon: "grid", label: "Overview" },
  { to: "/map", icon: "map", label: "Repo map" },
  { to: "/explore", icon: "search", label: "Explore symbols" },
  { to: "/files", icon: "file", label: "Files" },
  { to: "/hotspots", icon: "fire", label: "Hotspots" },
  { to: "/modules", icon: "graph", label: "Modules" },
  { to: "/relationships", icon: "graph", label: "Relationships" },
  { to: "/patterns", icon: "file", label: "Programming patterns" },
  { to: "/changes", icon: "pulse", label: "Git diff blast radius" },
  { to: "/findings", icon: "alert", label: "Findings" },
  { sep: "Work" },
  { to: "/snipe", icon: "board", label: "SNIPE" },
  { sep: "System" },
  { to: "/activity", icon: "pulse", label: "Activity" },
];

export default function App() {
  const { status, connected, toast, refreshStatus } = useLiveCtx();
  const w = status?.watcher;
  const reindex = async (full: boolean) => {
    toast(full ? "full re-index started…" : "re-scan started…");
    const r = await post("/api/reindex", { full });
    toast(`re-indexed: ${r.parsed} parsed, ${r.removed} removed in ${r.seconds}s`, "ok");
    refreshStatus();
  };
  return (
    <>
      <Icons />
      <header id="top">
        <h1 className="brand"><span className="logo"><i />CodeAtlas</span><small>code map, SNIPE loop and live index</small></h1>
        <div className="repo" title={status?.repo}>{status?.repo}</div>
        <div className="chips">
          <span className="chip" title={connected ? "live event stream connected" : "reconnecting…"}>
            <span className={`dot ${connected ? "on" : "off"}`} />{connected ? "live" : "offline"}
          </span>
          <span className="chip" title="file watcher"><span className={`dot ${w?.alive ? "on" : ""}`} />{w?.alive ? `watching (${w.backend})` : "no watcher"}</span>
          <span className="chip"><b>{fmtN(status?.files)}</b> files</span>
          <span className="chip"><b>{fmtN(status?.symbols)}</b> symbols</span>
          <span className="chip"><b>{fmtN(status?.refs)}</b> refs</span>
          <span className="chip">index {age(status?.last_update)}</span>
        </div>
        <button className="small" onClick={() => reindex(false)}><Icon id="refresh" />Re-scan</button>
        <button className="small" onClick={() => reindex(true)}>Full re-index</button>
      </header>
      <div id="layout">
        <nav id="nav">
          {NAV.map((n, i) => "sep" in n ? <div className="sep" key={i}>{n.sep}</div> :
            <NavLink key={n.to} to={n.to!} className={({ isActive }) => (isActive ? "active" : "")}><Icon id={n.icon!} />{n.label}</NavLink>)}
        </nav>
        <main>
          <Routes>
            <Route path="/" element={<Navigate to="/overview" replace />} />
            <Route path="/overview" element={<Overview />} />
            <Route path="/map" element={<RepoMap />} />
            <Route path="/explore" element={<Explore />} />
            <Route path="/files" element={<Files />} />
            <Route path="/hotspots" element={<Hotspots />} />
            <Route path="/modules" element={<Modules />} />
            <Route path="/relationships" element={<Relationships />} />
            <Route path="/patterns" element={<Patterns />} />
            <Route path="/changes" element={<Changes />} />
            <Route path="/findings" element={<Findings />} />
            <Route path="/snipe" element={<Snipe />} />
            <Route path="/scrum" element={<Navigate to="/snipe" replace />} />
            <Route path="/activity" element={<Activity />} />
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Routes>
        </main>
      </div>
    </>
  );
}
