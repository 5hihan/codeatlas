"""Query layer over the index: everything the MCP tools and dashboard need.

All methods return plain dicts/lists (JSON-able).  Formatting for LLM
consumption happens in ``fmt.py``.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from .indexer import Index
from .resolve import get_resolver

TEST_HINT = re.compile(r"(^|/)(tests?|__tests__|spec|specs|e2e|testing)(/|$)|(\.|_)(test|spec)s?\.[a-z]+$|^test_", re.I)
ENTRY_NAMES = {"main", "Main", "run", "start", "serve", "handler", "lambda_handler", "App", "app", "index", "cli"}
TRIGGER_DECORATOR_RE = re.compile(
    r"(route|get|post|put|patch|delete|mapping|listener|receiver|subscribe|on_|event|task|cron|schedule|command|"
    r"fixture|hook|middleware|handler|controller|endpoint|websocket|consumer|job|signal|composable|preview|"
    r"test|bean|component|service|provider|injectable|observer|action|reducer|effect|query|mutation|resolver)",
    re.I)
TRIGGER_CALL_RE = re.compile(
    r"^(on|once|addEventListener|addListener|subscribe|listen|register|route|get|post|put|patch|delete|use|"
    r"schedule|setTimeout|setInterval|then|catch|finally|forEach|map|filter|connect|bind|emit|dispatch|"
    r"add_url_rule|add_route|Route|Handle|HandleFunc|watch|effect|useEffect|useCallback|useMemo|addAction|"
    r"addFilter|signal|receiver|task|command|Thread|spawn|go|run_in_executor|create_task|ensure_future|"
    r"setOnClickListener|LaunchedEffect|DisposableEffect|onClick|onChange|onSubmit|Button|IconButton|"
    r"rememberCoroutineScope|launch|async|collect|onEach|observe|addObserver|NotificationCenter|Task|"
    r"DispatchQueue|sink|assign|onReceive|onAppear|onTapGesture|handle|handler|callback|cb)$", re.I)


EXAMPLE_HINT = re.compile(r"(^|/)(docs?|docs_src|documentation|examples?|samples?|demos?|benchmarks?|bench|perf|"
                          r"perf-measures|fixtures?|playground|tutorials?|snippets?|sandbox)(/|$)", re.I)


def is_test_path(path: str) -> bool:
    return bool(TEST_HINT.search(path))


def is_example_path(path: str) -> bool:
    """Documentation samples, examples, benchmarks, fixtures: real code, but rarely what a task is about."""
    return bool(EXAMPLE_HINT.search(path))


class Atlas:
    """High level read API over an :class:`Index`."""

    def __init__(self, index: Index, freshness_seconds: float = 20.0):
        self.ix = index
        self.freshness = freshness_seconds

    # ------------------------------------------------------------------ freshness
    def ensure_fresh(self, force: bool = False) -> Optional[dict]:
        """Stat-scan the repo if the last scan is older than ``freshness`` seconds."""
        if force or time.time() - self.ix.last_scan > self.freshness:
            return self.ix.update().as_dict()
        return None

    @property
    def conn(self):
        return self.ix.conn

    # ------------------------------------------------------------------ files
    def file_id(self, path: str) -> Optional[int]:
        rel = self.ix.rel(path)
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()
        if row:
            return row[0]
        # fuzzy: unique suffix match
        rows = self.conn.execute("SELECT id, path FROM files WHERE path LIKE ?", ("%" + rel.lstrip("/"),)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        if rows:
            rows.sort(key=lambda r: len(r[1]))
            return rows[0][0]
        return None

    def file_path(self, fid: int) -> str:
        row = self.conn.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
        return row[0] if row else "?"

    def list_files(self, prefix: str = "", limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT f.path, f.lang, f.lines, (SELECT COUNT(*) FROM symbols s WHERE s.file_id=f.id) AS symbols "
            "FROM files f WHERE f.path LIKE ? ORDER BY f.path LIMIT ?", (prefix + "%", limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ overview
    def overview(self) -> dict:
        c = self.conn
        langs = [tuple(r) for r in c.execute("SELECT lang, COUNT(*), SUM(lines) FROM files GROUP BY lang ORDER BY 2 DESC")]
        total_files = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        total_lines = c.execute("SELECT COALESCE(SUM(lines),0) FROM files").fetchone()[0]
        total_symbols = c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        kinds = [tuple(r) for r in c.execute("SELECT kind, COUNT(*) FROM symbols GROUP BY kind ORDER BY 2 DESC")]
        # top-level directories
        dirs: Counter = Counter()
        dir_lines: Counter = Counter()
        for path, lines in c.execute("SELECT path, lines FROM files"):
            parts = path.split("/")
            key = parts[0] if len(parts) > 1 else "(root)"
            if len(parts) > 2 and parts[0] in ("src", "lib", "app", "packages", "apps", "services", "modules"):
                key = f"{parts[0]}/{parts[1]}"
            dirs[key] += 1
            dir_lines[key] += lines or 0
        top_dirs = [{"dir": d, "files": n, "lines": dir_lines[d]} for d, n in dirs.most_common(25)]
        # key symbols (PageRank, excluding tests)
        from .rank import top_symbols
        most_ref = [(r["qualified"], r["in_deg"], r["path"]) for r in top_symbols(self.ix, limit=40)
                    if not is_test_path(r["path"])][:15]
        # entry points
        entries = c.execute(
            "SELECT s.qualified, s.kind, f.path, s.start_line FROM symbols s JOIN files f ON f.id=s.file_id "
            "WHERE s.name IN (%s) AND s.parent_id IS NULL ORDER BY length(f.path) LIMIT 15"
            % ",".join("?" * len(ENTRY_NAMES)), tuple(ENTRY_NAMES)).fetchall()
        # most imported files (hubs)
        hubs = c.execute(
            "SELECT f.path, COUNT(DISTINCT i.file_id) AS n FROM imports i JOIN files f ON f.id=i.resolved_file_id "
            "GROUP BY f.path ORDER BY n DESC LIMIT 12").fetchall()
        findings = [tuple(r) for r in c.execute("SELECT code, COUNT(*) FROM findings GROUP BY code ORDER BY 2 DESC")]
        packages = [{"project": r[0] or "(root)", "files": r[1], "lines": r[2], "langs": r[3]} for r in c.execute(
            "SELECT project, COUNT(*), SUM(lines), GROUP_CONCAT(DISTINCT lang) FROM files GROUP BY project ORDER BY 2 DESC")]
        last = self.ix.get_meta("last_update")
        return {
            "repo": str(self.ix.repo), "files": total_files, "lines": total_lines, "symbols": total_symbols,
            "languages": [{"lang": l, "files": n, "lines": ln} for l, n, ln in langs],
            "kinds": [{"kind": k, "count": n} for k, n in kinds],
            "top_dirs": top_dirs,
            "key_symbols": [{"name": r[0], "in_deg": r[1], "path": r[2]} for r in most_ref],
            "entry_points": [{"symbol": r[0], "kind": r[1], "path": r[2], "line": r[3]} for r in entries],
            "hubs": [{"path": r[0], "imported_by": r[1]} for r in hubs],
            "findings": [{"code": k, "count": n} for k, n in findings],
            "packages": packages,
            "last_update": float(last) if last else None,
        }

    # ------------------------------------------------------------------ outline
    def outline(self, path: str, include_private: bool = True) -> Optional[dict]:
        fid = self.file_id(path)
        if fid is None:
            return None
        c = self.conn
        frow = c.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        syms = [dict(r) for r in c.execute(
            "SELECT id, name, kind, qualified, parent_id, start_line, end_line, signature, doc, complexity, decorators, "
            "exported FROM symbols WHERE file_id=? ORDER BY start_line", (fid,))]
        if not include_private:
            syms = [s for s in syms if s["exported"]]
        counts = self._ref_counts([s["id"] for s in syms])
        for s in syms:
            s["ref_files"] = counts.get(s["id"], 0)
            s["decorators"] = json.loads(s["decorators"]) if s["decorators"] else []
        imports = [dict(r) for r in c.execute(
            "SELECT i.module, i.raw, i.line, i.names, f.path AS resolved FROM imports i LEFT JOIN files f "
            "ON f.id=i.resolved_file_id WHERE i.file_id=? ORDER BY i.line", (fid,))]
        for im in imports:
            im["names"] = json.loads(im["names"]) if im["names"] else []
        importers = [r[0] for r in c.execute(
            "SELECT DISTINCT f.path FROM imports i JOIN files f ON f.id=i.file_id WHERE i.resolved_file_id=? "
            "ORDER BY f.path LIMIT 200", (fid,))]
        findings = [dict(r) for r in c.execute(
            "SELECT line, code, message FROM findings WHERE file_id=? ORDER BY line", (fid,))]
        self._attach_pattern_tags(fid, syms)
        return {"path": frow["path"], "lang": frow["lang"], "lines": frow["lines"], "symbols": syms,
                "imports": imports, "imported_by": importers, "findings": findings,
                "patterns": self.programming_patterns(frow["path"], limit=12),
                "relationships": self.relationships(frow["path"], limit=8)}

    def _attach_pattern_tags(self, fid: int, syms: list[dict]) -> None:
        """Compact per-symbol pattern tags (one query per file; innermost symbol wins)."""
        for s in syms:
            s["pattern_tags"] = []
        if not syms:
            return
        try:
            rows = self.conn.execute("SELECT pattern, line FROM code_patterns WHERE file_id=? ORDER BY line", (fid,)).fetchall()
        except Exception:
            return
        spans = sorted(syms, key=lambda s: (s["end_line"] - s["start_line"]))
        tags: dict[int, set[str]] = {}
        for pat, line in rows:
            for s in spans:  # smallest enclosing span first
                if s["start_line"] <= line <= s["end_line"]:
                    tags.setdefault(s["id"], set()).add(pat)
                    break
        for s in syms:
            s["pattern_tags"] = sorted(tags.get(s["id"], ()))

    def programming_patterns(self, path=None, pattern=None, limit=50, offset=0, **ranges) -> dict:
        from .patterns import patterns
        return patterns(self.ix, path, pattern, limit, offset, **ranges)

    def relationships(self, path=None, kind=None, direction="both", limit=50, offset=0) -> dict:
        from .relationships import relationships
        return relationships(self.ix, path, kind, direction, limit, offset)

    # ------------------------------------------------------------------ symbols
    def find_symbols(self, query: str, kind: Optional[str] = None, path: Optional[str] = None,
                     limit: int = 25) -> list[dict]:
        c = self.conn
        q = query.strip()
        where = []
        params: list[Any] = []
        if kind:
            where.append("s.kind=?")
            params.append(kind)
        if path:
            where.append("f.path LIKE ?")
            params.append("%" + self.ix.rel(path) + "%")
        wsql = (" AND " + " AND ".join(where)) if where else ""
        rows = []
        # exact -> qualified exact -> prefix -> substring (case-insensitive)
        for cond, val in (("s.name = ?", q), ("s.qualified = ?", q), ("s.name LIKE ?", q + "%"),
                          ("s.qualified LIKE ?", "%" + q + "%"), ("s.name LIKE ?", "%" + q + "%")):
            rows += c.execute(
                "SELECT s.id, s.name, s.kind, s.qualified, s.start_line, s.end_line, s.signature, s.doc, s.complexity, "
                "s.exported, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE " + cond + wsql +
                " ORDER BY length(f.path), s.start_line LIMIT ?", (val, *params, limit * 2)).fetchall()
            if len(rows) >= limit:
                break
        seen: set[int] = set()
        out = []
        for r in rows:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(dict(r))
            if len(out) >= limit:
                break
        # fuzzy fallback: split camel/snake words
        if not out and len(q) >= 3:
            words = [w for w in re.split(r"[_\W]+|(?<=[a-z])(?=[A-Z])", q) if w]
            if words:
                cond = " AND ".join("s.name LIKE ?" for _ in words)
                rows = c.execute(
                    "SELECT s.id, s.name, s.kind, s.qualified, s.start_line, s.end_line, s.signature, s.doc, "
                    "s.complexity, s.exported, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE " + cond + wsql +
                    " LIMIT ?", (*["%" + w + "%" for w in words], *params, limit)).fetchall()
                out = [dict(r) for r in rows]
        counts = self._ref_counts([o["id"] for o in out])
        for o in out:
            o["ref_files"] = counts.get(o["id"], 0)
        return out

    def _ref_counts(self, sids: list[int]) -> dict[int, int]:
        """Resolved reference file counts (computed with PageRank); {} when the rank table is stale/missing."""
        if not sids:
            return {}
        from .rank import ensure_rank
        ensure_rank(self.ix)
        out: dict[int, int] = {}
        for i in range(0, len(sids), 500):
            chunk = sids[i:i + 500]
            q = "SELECT symbol_id, ref_files FROM symbol_refs WHERE symbol_id IN (%s)" % ",".join("?" * len(chunk))
            out.update({r[0]: r[1] for r in self.conn.execute(q, chunk)})
        return out

    def _ref_file_count(self, sid: int) -> int:
        return self._ref_counts([sid]).get(sid, 0)

    def resolve_symbol(self, name: str, path: Optional[str] = None) -> list[dict]:
        """Resolve a name / qualified name / 'path:line' to candidate symbols."""
        c = self.conn
        name = name.strip()
        m = re.match(r"^(.+?):(\d+)$", name)
        if m and "/" in m.group(1) or (m and m.group(1).endswith(tuple(f".{e}" for e in ("py", "ts", "js", "go", "rs", "kt", "swift", "java", "c", "cpp", "cs", "rb", "php")))):
            fid = self.file_id(m.group(1))
            line = int(m.group(2))
            if fid is not None:
                rows = c.execute(
                    "SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.file_id=? AND "
                    "s.start_line<=? AND s.end_line>=? ORDER BY (s.end_line-s.start_line) LIMIT 1",
                    (fid, line, line)).fetchall()
                return [dict(r) for r in rows]
            return []
        params: list[Any] = []
        psql = ""
        if path:
            psql = " AND f.path LIKE ?"
            params.append("%" + self.ix.rel(path) + "%")
        bare = name.rsplit(".", 1)[-1]
        rows = c.execute(
            "SELECT s.*, f.path, f.lang FROM symbols s JOIN files f ON f.id=s.file_id "
            "WHERE (s.qualified = ? OR s.name = ? OR s.qualified LIKE ?)" + psql +
            " ORDER BY (s.qualified = ?) DESC, (s.name = ?) DESC, s.exported DESC, "
            "(f.path LIKE '%test%') ASC, length(f.path) LIMIT 30",
            (name, bare, "%." + name, *params, name, bare)).fetchall()
        return [dict(r) for r in rows]

    def symbol_detail(self, name: str, path: Optional[str] = None) -> dict:
        cands = self.resolve_symbol(name, path)
        if not cands:
            similar = self.find_symbols(name, limit=8)
            return {"found": False, "similar": similar}
        s = cands[0]
        d = self._symbol_dict(s)
        d["ambiguous"] = [{"qualified": x["qualified"], "path": x["path"], "line": x["start_line"]}
                          for x in cands[1:6]]
        d["callers"] = self.callers(s["id"])[:30]
        d["callees"] = self.callees(s["id"])[:30]
        d["children"] = [dict(r) for r in self.conn.execute(
            "SELECT id, name, kind, start_line, end_line, signature FROM symbols WHERE parent_id=? ORDER BY start_line",
            (s["id"],))]
        d["ref_files"] = self._ref_file_count(s["id"])
        d["patterns"] = self.programming_patterns(s["path"], limit=12,
                                                  start_line=s["start_line"], end_line=s["end_line"])
        d["pattern_tags"] = sorted({it["pattern"] for it in d["patterns"].get("items", [])})
        d["found"] = True
        return d

    def _symbol_dict(self, s: dict) -> dict:
        return {
            "id": s["id"], "name": s["name"], "kind": s["kind"], "qualified": s["qualified"], "path": s["path"],
            "start_line": s["start_line"], "end_line": s["end_line"], "signature": s["signature"], "doc": s["doc"],
            "complexity": s["complexity"], "exported": bool(s["exported"]),
            "decorators": json.loads(s["decorators"]) if s.get("decorators") else [],
            "lines": s["end_line"] - s["start_line"] + 1,
        }

    def symbol_by_id(self, sid: int) -> Optional[dict]:
        r = self.conn.execute("SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.id=?",
                              (sid,)).fetchone()
        return self._symbol_dict(dict(r)) if r else None

    # ------------------------------------------------------------------ source
    def source(self, target: str, path: Optional[str] = None, context: int = 0, max_lines: int = 400) -> dict:
        """Return the source text of a symbol (or ``path:start-end``)."""
        m = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", target)
        if m and (path is None) and ("/" in m.group(1) or "." in m.group(1)) and self.file_id(m.group(1)) is not None:
            rel = self.file_path(self.file_id(m.group(1)))
            start = int(m.group(2))
            end = int(m.group(3) or start)
            label = f"{rel}:{start}-{end}"
        else:
            cands = self.resolve_symbol(target, path)
            if not cands:
                return {"found": False, "similar": self.find_symbols(target, limit=8)}
            s = cands[0]
            rel, start, end = s["path"], s["start_line"], s["end_line"]
            label = f"{s['qualified']} ({rel}:{start}-{end})"
        src = self.ix.read_source(rel)
        if src is None:
            return {"found": False, "error": f"cannot read {rel}"}
        lines = src.decode("utf-8", "replace").splitlines()
        a = max(1, start - context)
        b = min(len(lines), end + context)
        truncated = False
        if b - a + 1 > max_lines:
            b = a + max_lines - 1
            truncated = True
        body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(a, b + 1))
        return {"found": True, "label": label, "path": rel, "start": a, "end": b, "text": body, "truncated": truncated,
                "total_lines": len(lines)}

    # ------------------------------------------------------------------ references
    def references(self, name: str, kind: Optional[str] = None, limit: int = 200, exclude_tests: bool = False,
                   path: Optional[str] = None, offset: int = 0) -> dict:
        """Uses of a name. When the name (or path) identifies one definition, only references that resolve
        to it are listed; references to same-named symbols elsewhere are counted separately."""
        c = self.conn
        bare = name.rsplit(".", 1)[-1]
        params: list[Any] = [bare]
        ksql = ""
        if kind:
            ksql = " AND r.kind=?"
            params.append(kind)
        rows = c.execute(
            "SELECT r.line, r.col, r.kind, r.qualifier, r.enclosing_id, f.path, f.id AS fid, s.qualified AS enclosing, "
            "r.name AS rname FROM refs r JOIN files f ON f.id=r.file_id LEFT JOIN symbols s ON s.id=r.enclosing_id "
            "WHERE r.name=?" + ksql + " ORDER BY f.path, r.line", tuple(params)).fetchall()
        defs = self.resolve_symbol(name, path)
        target_id = defs[0]["id"] if defs and (path or "." in name or len(defs) == 1) else None
        res = self.resolver() if target_id is not None else None
        if res is not None:
            # uses under an import alias (`import { foo as bar }` ... `bar()`)
            for afid, local in res.alias_rev.get((bare, defs[0]["file_id"]), []):
                rows += c.execute(
                    "SELECT r.line, r.col, r.kind, r.qualifier, r.enclosing_id, f.path, f.id AS fid, s.qualified AS enclosing, "
                    "r.name AS rname FROM refs r JOIN files f ON f.id=r.file_id LEFT JOIN symbols s ON s.id=r.enclosing_id "
                    "WHERE r.name=? AND f.id=?" + ksql + " ORDER BY r.line", (local, afid, *params[1:])).fetchall()
            rows.sort(key=lambda r: (r["path"], r["line"]))
        def_locs = {(d["path"], d["start_line"]) for d in defs}
        by_file: dict[str, list[dict]] = defaultdict(list)
        matched = 0   # true number of matching references
        shown = 0
        elsewhere = 0
        for r in rows:
            if exclude_tests and is_test_path(r["path"]):
                continue
            if (r["path"], r["line"]) in def_locs and r["kind"] == "ref":
                continue
            if res is not None:
                rs = res.resolve(r["rname"], r["fid"], r["qualifier"], r["enclosing_id"], r["kind"])
                if target_id not in rs.targets:
                    elsewhere += 1
                    continue
            matched += 1
            if matched <= offset or shown >= limit:
                continue
            by_file[r["path"]].append({"line": r["line"], "kind": r["kind"], "qualifier": r["qualifier"],
                                       "enclosing": r["enclosing"]})
            shown += 1
        kinds = Counter(x["kind"] for v in by_file.values() for x in v)
        return {"name": bare, "definitions": [{"qualified": d["qualified"], "path": d["path"], "line": d["start_line"],
                                               "kind": d["kind"]} for d in defs[:5]],
                "resolved_to": defs[0]["qualified"] + " " + defs[0]["path"] if target_id is not None else None,
                "elsewhere": elsewhere,
                "total": matched, "shown": shown, "offset": offset, "truncated": matched > offset + shown,
                "next_offset": offset + shown if matched > offset + shown else None,
                "files": len(by_file), "kinds": dict(kinds),
                "by_file": [{"path": p, "refs": v} for p, v in sorted(by_file.items())],
                "index_version": self.ix.get_meta("last_update")}

    def resolver(self):
        return get_resolver(self.ix)

    def callers(self, sid: int, limit: int = 100, min_confidence: str = "medium") -> list[dict]:
        """Symbols that call/construct/pass this symbol, keeping only references that resolve to it."""
        s = self.conn.execute("SELECT name, file_id FROM symbols WHERE id=?", (sid,)).fetchone()
        if not s:
            return []
        res = self.resolver()
        allowed = {"high": ("high",), "medium": ("high", "medium"), "low": ("high", "medium", "low")}[min_confidence]
        rows = self.conn.execute(
            "SELECT e.id AS sid, e.qualified, e.kind, f.path, f.id AS fid, r.line, r.kind AS rkind, r.qualifier, r.name AS rname "
            "FROM refs r LEFT JOIN symbols e ON e.id=r.enclosing_id JOIN files f ON f.id=r.file_id "
            "WHERE r.name=? AND r.kind IN ('call','new','arg') AND (r.enclosing_id IS NULL OR r.enclosing_id != ?) "
            "ORDER BY (f.id=?) DESC, f.path, r.line", (s["name"], sid, s["file_id"])).fetchall()
        # files that import this symbol under another name (`import { foo as bar }`) call it as `bar`
        for afid, local in res.alias_rev.get((s["name"], s["file_id"]), []):
            rows += self.conn.execute(
                "SELECT e.id AS sid, e.qualified, e.kind, f.path, f.id AS fid, r.line, r.kind AS rkind, r.qualifier, r.name AS rname "
                "FROM refs r LEFT JOIN symbols e ON e.id=r.enclosing_id JOIN files f ON f.id=r.file_id "
                "WHERE r.name=? AND f.id=? AND r.kind IN ('call','new','arg') ORDER BY r.line", (local, afid)).fetchall()
        out = []
        seen: set[tuple] = set()
        for r in rows:
            ref_name = r["rname"]
            # module-level code and anonymous callbacks (tests, event handlers) have no enclosing symbol:
            # report them as a pseudo-caller per file+line so they are not silently dropped
            key = (r["sid"] or f"top:{r['path']}", r["line"])
            if key in seen:
                continue
            rs = res.resolve(ref_name, r["fid"], r["qualifier"], r["sid"], r["rkind"])
            if sid not in rs.targets or rs.confidence not in allowed:
                continue
            seen.add(key)
            out.append({"id": r["sid"], "qualified": r["qualified"] or "<top-level>", "kind": r["kind"] or "module",
                        "path": r["path"], "line": r["line"], "via": r["rkind"], "qualifier": r["qualifier"],
                        "confidence": rs.confidence, "ambiguous": len(rs.targets) > 1})
            if len(out) >= limit:
                break
        return out

    def callees(self, sid: int, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT r.name, r.kind, r.qualifier, MIN(r.line) AS line, COUNT(*) AS n FROM refs r WHERE r.enclosing_id=? "
            "AND r.kind IN ('call','new') GROUP BY r.name, r.kind, r.qualifier ORDER BY line LIMIT ?", (sid, limit)).fetchall()
        src = self.conn.execute("SELECT file_id FROM symbols WHERE id=?", (sid,)).fetchone()
        fid = src["file_id"] if src else None
        res = self.resolver()
        out = []
        for r in rows:
            target = None
            if fid is not None:
                rs = res.resolve(r["name"], fid, r["qualifier"], sid, r["kind"])
                if rs.targets and rs.confidence in ("high", "medium") and len(rs.targets) == 1:
                    target = self._target_info(rs.targets[0], rs.how)
                elif rs.targets and rs.confidence in ("high", "medium"):
                    target = self._target_info(rs.targets[0], rs.how)
                    target["resolution"] = f"{rs.how}({len(rs.targets)} candidates)"
                elif rs.targets:
                    target = {"id": None, "qualified": None, "path": None, "line": None,
                              "resolution": f"ambiguous({len(rs.targets)})"}
            out.append({"name": r["name"], "kind": r["kind"], "qualifier": r["qualifier"], "line": r["line"],
                        "count": r["n"], "target": target})
        return out

    def _target_info(self, tid: int, how: str) -> dict:
        r = self.conn.execute("SELECT s.id, s.qualified, f.path, s.start_line FROM symbols s JOIN files f ON f.id=s.file_id "
                              "WHERE s.id=?", (tid,)).fetchone()
        return {"id": r[0], "qualified": r[1], "path": r[2], "line": r[3], "resolution": how}

    def impact(self, name: str, path: Optional[str] = None, depth: int = 3, limit: int = 60) -> dict:
        """Transitive callers: what could break if this symbol changes."""
        cands = self.resolve_symbol(name, path)
        if not cands:
            return {"found": False, "similar": self.find_symbols(name, limit=8)}
        root = cands[0]
        seen: dict[int, int] = {root["id"]: 0}
        frontier = [root["id"]]
        edges: list[dict] = []
        truncated = False
        top_level: list[dict] = []
        for d in range(1, depth + 1):
            nxt = []
            for sid in frontier:
                cls = self.callers(sid, limit=61)
                if len(cls) > 60:
                    truncated = True
                for cl in cls[:60]:
                    edges.append({"from": cl["id"], "to": sid, "depth": d, "path": cl["path"], "line": cl["line"],
                                  "qualified": cl["qualified"], "via": cl["via"]})
                    if cl["id"] is None:
                        top_level.append({"path": cl["path"], "line": cl["line"], "depth": d})
                        continue
                    if cl["id"] not in seen:
                        seen[cl["id"]] = d
                        nxt.append(cl["id"])
                if len(seen) > limit * 3:
                    truncated = True
                    break
            frontier = nxt
            if not frontier:
                break
        deeper = bool(frontier)  # unexplored callers beyond `depth` hops (not a cap)
        files: Counter = Counter(e["path"] for e in edges)
        tests = [p for p in files if is_test_path(p)]
        levels: dict[int, list[dict]] = defaultdict(list)
        for sid, d in seen.items():
            if sid == root["id"]:
                continue
            s = self.symbol_by_id(sid)
            if s:
                levels[d].append({"qualified": s["qualified"], "path": s["path"], "line": s["start_line"]})
        return {"found": True, "root": self._symbol_dict(root), "depth": depth,
                "affected_symbols": len(seen) - 1, "affected_files": len(files),
                "levels": {str(k): v[:limit] for k, v in sorted(levels.items())},
                "top_level_sites": top_level[:40],
                "files": [{"path": p, "edges": n} for p, n in files.most_common(40)],
                "tests": tests[:20], "truncated": truncated, "deeper": deeper,
                "index_version": self.ix.get_meta("last_update")}

    def triggers(self, name: str, path: Optional[str] = None) -> dict:
        """What causes this symbol to run: decorators/annotations, registration calls, callers, entry-ness."""
        cands = self.resolve_symbol(name, path)
        if not cands:
            return {"found": False, "similar": self.find_symbols(name, limit=8)}
        s = cands[0]
        d = self._symbol_dict(s)
        c = self.conn
        res = self.resolver()
        registrations = []
        for r in c.execute(
                "SELECT r.qualifier AS via, f.path, f.id AS fid, r.line, r.enclosing_id, e.qualified AS enclosing "
                "FROM refs r JOIN files f ON f.id=r.file_id LEFT JOIN symbols e ON e.id=r.enclosing_id "
                "WHERE r.name=? AND r.kind='arg' ORDER BY f.path, r.line LIMIT 200", (s["name"],)):
            rs = res.resolve(s["name"], r["fid"], None, r["enclosing_id"], "arg")
            if s["id"] not in rs.targets or rs.confidence not in ("high", "medium"):
                continue
            registrations.append({"via": r["via"], "path": r["path"], "line": r["line"], "enclosing": r["enclosing"],
                                  "event_like": bool(TRIGGER_CALL_RE.search(r["via"] or "")), "confidence": rs.confidence})
            if len(registrations) >= 40:
                break
        decorators = d["decorators"]
        dec_hits = [x for x in decorators if TRIGGER_DECORATOR_RE.search(x)]
        callers = self.callers(s["id"], limit=40)
        # parent class + its triggers (e.g. a method on a controller)
        parent = None
        if s.get("parent_id"):
            p = self.symbol_by_id(s["parent_id"])
            if p:
                parent = {"qualified": p["qualified"], "kind": p["kind"], "decorators": p["decorators"],
                          "callers": len(self.callers(p["id"], limit=50))}
        entry = s["name"] in ENTRY_NAMES or s["name"].startswith("test")
        exported_unused = d["exported"] and not callers and not registrations
        return {"found": True, "symbol": d, "decorators": decorators, "trigger_decorators": dec_hits,
                "registrations": registrations, "callers": callers, "parent": parent,
                "entry_point": entry, "exported_no_internal_callers": exported_unused}

    # ------------------------------------------------------------------ dependencies
    def dependencies(self, path: str) -> Optional[dict]:
        fid = self.file_id(path)
        if fid is None:
            return None
        c = self.conn
        rel = self.file_path(fid)
        internal = [dict(r) for r in c.execute(
            "SELECT DISTINCT f.path, i.names FROM imports i JOIN files f ON f.id=i.resolved_file_id WHERE i.file_id=? "
            "ORDER BY f.path", (fid,))]
        for x in internal:
            x["names"] = json.loads(x["names"]) if x["names"] else []
        external = sorted({r[0] for r in c.execute(
            "SELECT module FROM imports WHERE file_id=? AND resolved_file_id IS NULL", (fid,))})
        dependents = [r[0] for r in c.execute(
            "SELECT DISTINCT f.path FROM imports i JOIN files f ON f.id=i.file_id WHERE i.resolved_file_id=? ORDER BY f.path",
            (fid,))]
        # transitive dependents (2 levels)
        lvl2: set[str] = set()
        for dp in dependents[:50]:
            did = self.file_id(dp)
            for r in c.execute("SELECT f.path FROM imports i JOIN files f ON f.id=i.file_id WHERE i.resolved_file_id=?",
                               (did,)):
                if r[0] != rel and r[0] not in dependents:
                    lvl2.add(r[0])
        return {"path": rel, "imports_internal": internal, "imports_external": external,
                "imported_by": dependents, "imported_by_transitive": sorted(lvl2)[:80]}

    def module_graph(self, depth: int = 2, min_edges: int = 1) -> dict:
        """Aggregate the file import graph to directory modules."""
        c = self.conn
        def key(p: str) -> str:
            parts = p.split("/")[:-1]
            return "/".join(parts[:depth]) if parts else "(root)"
        edges: Counter = Counter()
        nodes: Counter = Counter()
        for path, in c.execute("SELECT path FROM files"):
            nodes[key(path)] += 1
        for a, b in c.execute(
                "SELECT fa.path, fb.path FROM imports i JOIN files fa ON fa.id=i.file_id JOIN files fb ON fb.id=i.resolved_file_id"):
            ka, kb = key(a), key(b)
            if ka != kb:
                edges[(ka, kb)] += 1
        return {"nodes": [{"id": k, "files": n} for k, n in nodes.most_common()],
                "edges": [{"from": a, "to": b, "count": n} for (a, b), n in edges.most_common() if n >= min_edges]}

    def cycles(self, limit: int = 20) -> list[list[str]]:
        """Import cycles between files (Tarjan SCC, sizes >= 2)."""
        c = self.conn
        graph: dict[int, set[int]] = defaultdict(set)
        for a, b in c.execute("SELECT file_id, resolved_file_id FROM imports WHERE resolved_file_id IS NOT NULL"):
            if a != b:
                graph[a].add(b)
        index = 0
        stack: list[int] = []
        on_stack: set[int] = set()
        idx: dict[int, int] = {}
        low: dict[int, int] = {}
        sccs: list[list[int]] = []

        def strong(v: int) -> None:
            nonlocal index
            work = [(v, iter(graph.get(v, ())))]
            idx[v] = low[v] = index
            index += 1
            stack.append(v)
            on_stack.add(v)
            while work:
                node, it = work[-1]
                advanced = False
                for w in it:
                    if w not in idx:
                        idx[w] = low[w] = index
                        index += 1
                        stack.append(w)
                        on_stack.add(w)
                        work.append((w, iter(graph.get(w, ()))))
                        advanced = True
                        break
                    elif w in on_stack:
                        low[node] = min(low[node], idx[w])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == idx[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1:
                        sccs.append(comp)

        for v in list(graph):
            if v not in idx:
                strong(v)
        sccs.sort(key=len)
        return [[self.file_path(f) for f in comp][:15] for comp in sccs[:limit]]

    # ------------------------------------------------------------------ hotspots
    def hotspots(self, limit: int = 15) -> dict:
        c = self.conn
        complex_ = [dict(r) for r in c.execute(
            "SELECT s.qualified, f.path, s.start_line, s.complexity, (s.end_line-s.start_line+1) AS lines FROM symbols s "
            "JOIN files f ON f.id=s.file_id WHERE s.kind IN ('function','method') ORDER BY s.complexity DESC LIMIT ?",
            (limit,))]
        long_ = [dict(r) for r in c.execute(
            "SELECT s.qualified, f.path, s.start_line, s.complexity, (s.end_line-s.start_line+1) AS lines FROM symbols s "
            "JOIN files f ON f.id=s.file_id WHERE s.kind IN ('function','method') ORDER BY lines DESC LIMIT ?", (limit,))]
        big_files = [dict(r) for r in c.execute(
            "SELECT f.path, f.lines, COUNT(s.id) AS symbols FROM files f LEFT JOIN symbols s ON s.file_id=f.id "
            "GROUP BY f.id ORDER BY f.lines DESC LIMIT ?", (limit,))]
        from .rank import top_symbols
        key = [{"qualified": r["qualified"], "path": r["path"], "start_line": r["start_line"], "in_deg": r["in_deg"]}
               for r in top_symbols(self.ix, limit=limit * 3) if not is_test_path(r["path"])][:limit]
        return {"most_complex": complex_, "longest": long_, "largest_files": big_files, "key_symbols": key,
                "cycles": self.cycles(limit=10)}

    def unreferenced(self, limit: int = 100, include_exported: bool = False) -> list[dict]:
        """Symbols no resolved reference points at: dead-code candidates (verify before deleting)."""
        c = self.conn
        from .rank import ensure_rank
        ensure_rank(self.ix)
        rows = c.execute(
            "SELECT s.id, s.name, s.kind, s.qualified, s.exported, f.path, s.start_line, (s.end_line-s.start_line+1) AS lines "
            "FROM symbols s JOIN files f ON f.id=s.file_id LEFT JOIN symbol_refs sr ON sr.symbol_id=s.id "
            "WHERE s.kind IN ('function','method','class') AND COALESCE(sr.ref_files, 0) = 0 "
            "AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.name=s.name AND r.file_id=s.file_id AND r.line != s.start_line) "
            "AND s.name NOT IN ('main','__init__','constructor','init','setUp','tearDown','render','toString','equals','hashCode','__str__','__repr__','__eq__','__hash__','__enter__','__exit__','__call__','__len__','__iter__','__next__','__getitem__','__setitem__','__contains__','__aenter__','__aexit__','onCreate','onDestroy','onResume','onPause','onStart','onStop','viewDidLoad','body','Preview') "
            "AND s.name NOT LIKE 'test%' AND s.name NOT LIKE 'Test%' AND s.name NOT LIKE '%Preview' "
            "AND s.name NOT LIKE 'use%' AND s.name NOT LIKE 'on%' "
            "AND substr(s.name,1,1) != '_' " + ("" if include_exported else "AND s.exported=0 ") +
            "ORDER BY lines DESC LIMIT ?", (limit * 2,)).fetchall()
        out = []
        for r in rows:
            if is_test_path(r["path"]):
                continue
            d = dict(r)
            d["config_refs"] = self._config_mentions(r["name"])
            if d["config_refs"]:
                continue  # wired up from a manifest / plist / xml / config: framework entry point
            out.append(d)
            if len(out) >= limit:
                break
        return out

    _CONFIG_EXT = (".plist", ".xml", ".json", ".yaml", ".yml", ".toml", ".gradle", ".kts", ".properties", ".cfg",
                   ".ini", ".env", ".html", ".storyboard", ".xib", ".entitlements", ".txt", ".md")

    def _config_files(self) -> list[str]:
        """Small non-source config files (not in the index) that may name entry points."""
        ver = self.ix.get_meta("last_update") or ""
        cache = getattr(self, "_cfg_cache", None)
        if cache and cache[0] == ver:
            return cache[1]
        from .indexer import DEFAULT_IGNORE_DIRS
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.ix.repo):
            dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS and not d.startswith(".")
                           and not d.endswith(".xcodeproj")]
            for fn in filenames:
                if fn.lower().endswith(self._CONFIG_EXT):
                    p = os.path.join(dirpath, fn)
                    try:
                        if os.path.getsize(p) <= 300_000:
                            found.append(p)
                    except OSError:
                        pass
            if len(found) > 3000:
                break
        self._cfg_cache = (ver, found)
        return found

    def _config_mentions(self, name: str) -> list[str]:
        if len(name) < 4:
            return []
        rx = re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(name.encode()) + rb"(?![A-Za-z0-9_])")
        hits = []
        for p in self._config_files():
            try:
                with open(p, "rb") as fh:
                    if rx.search(fh.read()):
                        hits.append(os.path.relpath(p, self.ix.repo).replace("\\", "/"))
                        if len(hits) >= 3:
                            break
            except OSError:
                continue
        return hits

    # ------------------------------------------------------------------ text search
    def grep(self, pattern: str, glob: Optional[str] = None, limit: int = 100,
             ignore_case: Optional[bool] = None) -> dict:
        if ignore_case is None:  # smart case
            ignore_case = pattern == pattern.lower()
        try:
            rx = re.compile(pattern.encode(), re.I if ignore_case else 0)
        except re.error as e:
            return {"error": f"bad regex: {e}"}
        import fnmatch
        hits: list[dict] = []
        files_hit = 0
        for path, in self.conn.execute("SELECT path FROM files ORDER BY path"):
            if glob and not fnmatch.fnmatch(path, glob) and not fnmatch.fnmatch(path, "*/" + glob):
                continue
            src = self.ix.read_source(path)
            if not src or not rx.search(src):
                continue
            files_hit += 1
            for i, line in enumerate(src.splitlines(), 1):
                if rx.search(line):
                    hits.append({"path": path, "line": i, "text": line.decode("utf-8", "replace").strip()[:200]})
                    if len(hits) >= limit:
                        return {"pattern": pattern, "hits": hits, "files": files_hit, "truncated": True}
        return {"pattern": pattern, "hits": hits, "files": files_hit, "truncated": False}

    # ------------------------------------------------------------------ findings
    def findings(self, code: Optional[str] = None, path: Optional[str] = None, limit: int = 200) -> list[dict]:
        where = []
        params: list[Any] = []
        if code:
            where.append("fd.code LIKE ?")
            params.append(code.upper() + "%")
        if path:
            where.append("f.path LIKE ?")
            params.append("%" + self.ix.rel(path) + "%")
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.conn.execute(
            "SELECT fd.code, fd.message, fd.line, f.path FROM findings fd JOIN files f ON f.id=fd.file_id " + wsql +
            " ORDER BY fd.code, f.path, fd.line LIMIT ?", (*params, limit)).fetchall()
        return [dict(r) for r in rows]

    def file_tree(self, max_depth: int = 3) -> dict:
        """Nested directory tree with file/line counts (for the dashboard)."""
        root: dict = {"name": "", "dirs": {}, "files": 0, "lines": 0}
        for path, lines in self.conn.execute("SELECT path, lines FROM files"):
            parts = path.split("/")
            node = root
            node["files"] += 1
            node["lines"] += lines or 0
            for p in parts[:-1][:max_depth]:
                node = node["dirs"].setdefault(p, {"name": p, "dirs": {}, "files": 0, "lines": 0})
                node["files"] += 1
                node["lines"] += lines or 0
        def conv(n: dict) -> dict:
            return {"name": n["name"], "files": n["files"], "lines": n["lines"],
                    "dirs": sorted((conv(d) for d in n["dirs"].values()), key=lambda x: -x["files"])}
        return conv(root)
