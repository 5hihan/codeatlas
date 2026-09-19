import React from "react";
import { Link } from "react-router-dom";

const KIND_LETTER: Record<string, string> = {
  function: "f", method: "m", class: "C", struct: "S", interface: "I", enum: "E", type: "t", module: "M", object: "O",
  trait: "T", record: "R", constant: "k", macro: "#", property: "p", impl: "impl", variable: "v",
};

export const Kind = ({ k }: { k: string }) => <span className={`kind ${k}`}>{KIND_LETTER[k] || k[0]}</span>;

export const SymLink = ({ name, path, children }: { name: string; path?: string; children?: React.ReactNode }) => (
  <Link className="sym" to={`/explore?name=${encodeURIComponent(name)}&path=${encodeURIComponent(path || "")}`}>{children ?? name}</Link>
);

export const FileLink = ({ path, line }: { path: string; line?: number }) => (
  <Link className="mono" to={`/files?path=${encodeURIComponent(path)}`}>{path}{line ? <span className="muted">:{line}</span> : null}</Link>
);

export const Card = ({ title, children, className = "", right }: { title?: React.ReactNode; children: React.ReactNode; className?: string; right?: React.ReactNode }) => (
  <section className={`card ${className}`}>
    {title ? <div className="row" style={{ marginBottom: 8 }}><h2>{title}</h2><span className="spacer" />{right}</div> : null}
    {children}
  </section>
);

export const Badge = ({ children, cls = "" }: { children: React.ReactNode; cls?: string }) => <span className={`badge ${cls}`}>{children}</span>;

export const Empty = ({ children }: { children: React.ReactNode }) => <div className="empty">{children}</div>;

export const Loading = () => <div className="empty">Loading…</div>;

export function Modal({ open, onClose, children }: { open: boolean; onClose: () => void; children: React.ReactNode }) {
  if (!open) return null;
  return (
    <div className="modal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="box">{children}</div>
    </div>
  );
}

export const Icon = ({ id }: { id: string }) => <svg className="ico"><use href={`#i-${id}`} /></svg>;

export const Icons = () => (
  <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true">
    <defs>
      <symbol id="i-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></symbol>
      <symbol id="i-map" viewBox="0 0 24 24"><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2z" /><path d="M9 4v14M15 6v14" /></symbol>
      <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></symbol>
      <symbol id="i-file" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /></symbol>
      <symbol id="i-fire" viewBox="0 0 24 24"><path d="M12 22c4 0 7-3 7-7 0-3-2-5-3-7-1 3-2 4-4 4 0-3-1-6-3-8-1 4-4 6-4 11 0 4 3 7 7 7z" /></symbol>
      <symbol id="i-graph" viewBox="0 0 24 24"><circle cx="5" cy="6" r="2.5" /><circle cx="19" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M7 7.5 10.5 16M17 7.5 13.5 16M7.5 6h9" /></symbol>
      <symbol id="i-alert" viewBox="0 0 24 24"><path d="M12 3 2 20h20L12 3z" /><path d="M12 10v4M12 17h.01" /></symbol>
      <symbol id="i-board" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16M15 4v16" /></symbol>
      <symbol id="i-pulse" viewBox="0 0 24 24"><path d="M3 12h4l3-7 4 14 3-7h4" /></symbol>
      <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20 11A8 8 0 0 0 6.3 6.3L4 8.5M4 13a8 8 0 0 0 13.7 4.7l2.3-2.2" /><path d="M4 4v4.5h4.5M20 20v-4.5h-4.5" /></symbol>
      <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></symbol>
      <symbol id="i-play" viewBox="0 0 24 24"><path d="M6 4v16l14-8z" /></symbol>
      <symbol id="i-scan" viewBox="0 0 24 24"><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3" /><path d="M4 12h16" /></symbol>
    </defs>
  </svg>
);
