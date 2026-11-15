"""MCP server exposing the code map, symbol-level edits and the SNIPE fix loop.

Run with ``codeatlas serve`` (stdio transport).  Starting the server also
starts the file watcher (index stays current) and the localhost dashboard.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Union

from . import __version__, changes, connect, context, edits, fmt, rank, patterns as pattern_tools, relationships as relation_tools
from .graph import Atlas
from .indexer import Index
from .snipe import Snipe, render_board, render_item, render_next, render_verify
from .watcher import Watcher

log = logging.getLogger("codeatlas")

INSTRUCTIONS = """CodeAtlas holds a tree-sitter map of this repository (symbols, references, call graph, imports, env keys, SQL
tables); use its tools instead of Grep, Glob, Read and Edit on indexed source files. Answers are compact text with
path:line anchors. Start a task with task_context("<what you are doing>", scope="dir/"): relevant symbols with
source, callers, tests and config, ranked by how they connect to what you already looked at (known=[...] names
it, known=[] starts fresh). Locate with find_symbols / file_outline, read with symbol / symbol_source, check
references / impact / triggers before changing anything, connections(target, known) for how code you know reaches
code you need, replace_symbol / insert_code to edit in place (parse-checked, re-indexed at once);
change_context(base_ref) reviews what you changed. Fix work runs the SNIPE loop: next_snipe -> claim_snipe ->
get_snipe_context -> patch the smallest surface -> verify_snipe; snipe_report feeds failures in; never widen a
snipe into a refactor."""


# Tools kept in Claude's context from the start (Claude Code defers the other MCP tool schemas behind a search):
# the ones an agent reaches for first, so the first useful call needs no search round-trip. Measured in real
# sessions: with none always-loaded the same questions took 6-7 calls instead of 2-4 (two ToolSearch round trips
# and a fallback to Grep/Read) and cost 50-100% more; `references` and `impact` are where "who calls / what breaks"
# questions go, so a search before them was the remaining waste. `symbol` and `file_outline` were used once each
# in twenty measured sessions, so their schemas (about 400 tokens on every request) are deferred.
ALWAYS_LOAD = frozenset(os.environ.get("CODEATLAS_ALWAYS_LOAD",
                                       "task_context,find_symbols,symbol_source,references,impact").split(","))


class App:
    """Holds the per-repo runtime: index, atlas, snipe, watcher and dashboard."""

    def __init__(self, repo: Path, watch: bool = True, dashboard: bool = True, port: Optional[int] = None,
                 profile: str = "all"):
        self.repo = Path(repo).resolve()
        self.profile = profile
        self.ix = Index(self.repo)
        self.atlas = Atlas(self.ix)
        self.snipe = Snipe(self.atlas)
        self.watcher: Optional[Watcher] = None
        self.dashboard = None
        self.working = connect.WorkingSet()   # what this agent looked at: default `known` anchors
        self.events: list[dict] = []
        self.started = time.time()
        self._lock = self.ix.lock
        from .dashboard import EventBus
        self.bus = EventBus()
        self.ix.on_update.append(self._record_update)
        self.snipe.on_save = lambda: self._push_event({"type": "snipe"})
        if watch:
            self.watcher = Watcher(self.ix, on_change=self._on_watch).start()
        if dashboard:
            from .dashboard import Dashboard
            try:
                self.dashboard = Dashboard(self, port=port).start()
            except Exception as e:  # pragma: no cover
                log.warning("dashboard failed to start: %s", e)

    # ------------------------------------------------------------------ events
    def _record_update(self, stats) -> None:
        if stats.parsed or stats.removed or stats.relationship_files:
            self._push_event({"type": "index", **stats.as_dict()})
            if not stats.parsed and not stats.removed:
                return
            try:
                r = self.snipe.scan_delta(stats.delta)  # SCAN: only the changed surface
                if r["created"] or r["closed"]:
                    self._push_event({"type": "snipe", "created": r["created"], "closed": r["closed"]})
            except Exception as e:  # pragma: no cover
                log.warning("snipe delta scan failed: %s", e)

    def _on_watch(self, info: dict) -> None:
        self._push_event({"type": "watch", **info})

    def _push_event(self, ev: dict) -> None:
        ev["t"] = time.time()
        if ev["type"] != "snipe":
            self.events.append(ev)
            del self.events[:-200]
        self.bus.publish(ev)

    def log_tool(self, name: str, args: dict, ms: float) -> None:
        self._push_event({"type": "tool", "tool": name, "args": {k: (str(v)[:80]) for k, v in args.items() if v is not None},
                          "ms": round(ms, 1)})

    # ------------------------------------------------------------------ helpers
    def fresh(self) -> None:
        self.atlas.ensure_fresh()

    def remember(self, path: Optional[str], line: Optional[int] = None) -> None:
        """Record a position the agent looked at (the enclosing symbol, else the file) as a known anchor."""
        if not path:
            return
        rel = self.ix.rel(path)
        if line is not None:
            row = self.ix.conn.execute(
                "SELECT s.qualified, s.start_line FROM symbols s JOIN files f ON f.id=s.file_id WHERE f.path=? AND "
                "s.start_line<=? AND s.end_line>=? ORDER BY (s.end_line-s.start_line) LIMIT 1", (rel, line, line)).fetchone()
            if row:
                self.working.note_symbol({"path": rel, "start_line": row[1], "qualified": row[0]})
                return
        self.working.note_file(rel)

    def known_anchors(self, known: Optional[list]) -> tuple[list[str], str]:
        """Explicit `known` wins; None falls back to the session's working set; [] means start fresh."""
        if known is None and len(self.working):
            return self.working.anchors(), "recent"
        return list(known or []), "given"

    def status(self) -> dict:
        c = self.ix.conn
        return {
            "version": __version__, "repo": str(self.repo), "files": self.ix.file_count(),
            "symbols": c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "refs": c.execute("SELECT COUNT(*) FROM refs").fetchone()[0],
            "last_update": self.ix.get_meta("last_update"),
            "relationships_updated": self.ix.get_meta("relationships_updated"),
            "watcher": self.watcher.status() if self.watcher else None,
            "dashboard": self.dashboard.url if self.dashboard else None,
            "uptime_s": round(time.time() - self.started),
            "snipe": {"ready": len(self.snipe.board()["ready"]), "current": len(self.snipe.board()["current"]),
                      "open": len(self.snipe.items(kind="issue", state="FOUND,TARGETED,FIXING,VERIFYING,BLOCKED"))},
        }

    def close(self) -> None:
        if self.watcher:
            self.watcher.stop()
        if self.dashboard:
            self.dashboard.stop()
        self.ix.close()


_app: Optional[App] = None


def get_app() -> App:
    assert _app is not None, "App not initialised"
    return _app


def build_server(app: App):
    global _app
    _app = app
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(name="codeatlas", instructions=INSTRUCTIONS, version=__version__)

    def guarded(fn):
        """Serialise DB access, keep the index fresh, and log the call for the dashboard."""
        import functools

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            t0 = time.time()
            with app._lock:
                app.fresh()
                try:
                    return fn(*a, **kw)
                except Exception as e:
                    # a bug in one tool must not strand the model with an opaque "Error executing tool": say what
                    # failed and what still works (measured: an opaque error sent it back to Grep and Read)
                    logging.getLogger(__name__).exception("tool %s failed", fn.__name__)
                    return (f"{fn.__name__} failed ({type(e).__name__}: {str(e)[:200]}). The index is still available: "
                            f"retry without optional arguments, or use find_symbols / symbol_source / references.")
                finally:
                    app.log_tool(fn.__name__, kw, (time.time() - t0) * 1000)
        return wrapper

    PROFILES = {"nav": {"nav"}, "edit": {"nav", "edit"}, "snipe": {"nav", "snipe"}, "all": {"nav", "edit", "snipe"}}
    enabled = PROFILES.get(app.profile, PROFILES["all"])
    from mcp.types import ToolAnnotations

    def tool(fn=None, *, group: str = "nav", read_only: Optional[bool] = None, always_load: bool = False):
        """Register a tool in a group; groups outside the active profile are not exposed.

        Navigation tools are read-only and idempotent (Claude Code runs them in parallel with other read-only
        calls and may retry them after a dropped connection); nothing here reaches outside the repository.
        Claude Code defers MCP tool schemas behind a tool search by default: ``always_load`` keeps the few tools an
        agent reaches for first (ALWAYS_LOAD) in context from the start, so no search round-trip precedes them.
        """
        def deco(f):
            if group in enabled:
                ro = (group == "nav") if read_only is None else read_only
                ann = ToolAnnotations(readOnlyHint=ro, idempotentHint=ro, destructiveHint=not ro, openWorldHint=False)
                meta = {"anthropic/alwaysLoad": True} if (always_load or f.__name__ in ALWAYS_LOAD) else None
                return mcp.tool(annotations=ann, meta=meta)(guarded(f))
            return f
        return deco(fn) if fn is not None else deco

    # ------------------------------------------------------------------ index / overview
    @tool
    def index_status() -> str:
        """Index health: file/symbol counts, watcher state, dashboard URL, last update time."""
        s = app.status()
        w = s["watcher"] or {}
        return (f"CodeAtlas {s['version']} on {s['repo']}\n{s['files']} files, {s['symbols']} symbols, {s['refs']} refs; "
                f"last update {fmt._age(float(s['last_update']) if s['last_update'] else None)} "
                f"(index version {s['last_update']}, tool profile {app.profile})\n"
                f"watcher: {w.get('backend')} alive={w.get('alive')} events={w.get('events')}\n"
                f"dashboard: {s['dashboard'] or 'off'}\nsnipe: {s['snipe']}")

    @tool(read_only=False)
    def index_repo(full: bool = False) -> str:
        """Force a re-scan of the repository. The index already auto-updates on file changes; use full=True after
        changing .gitignore or if results look stale."""
        st = app.ix.update(full=full)
        rank.compute_pagerank(app.ix)
        return f"indexed: {st.as_dict()}"

    @tool
    def repo_overview() -> str:
        """Start here: languages, top directories, hub files, entry points, key symbols, finding counts (~400 tokens)."""
        return fmt.overview(app.atlas.overview())

    @tool
    def repo_map(token_budget: Optional[int] = None, focus_paths: Optional[list[str]] = None,
                 focus_names: Optional[list[str]] = None, exported_only: bool = False) -> str:
        """Complete map of the repository: every file with its symbols (classes with their methods, functions with
        signatures), files ordered by PageRank. Nothing is cut unless token_budget is given, in which case only the
        top-ranked files/symbols that fit are shown. focus_paths/focus_names bias the ranking toward what you are
        working on. Use it instead of listing or reading files."""
        return rank.repo_map(app.ix, token_budget=token_budget, focus_paths=focus_paths, focus_names=focus_names,
                             exported_only=exported_only)

    @tool
    def task_context(task: str, scope: str = "", token_budget: int = 0, include_source: Optional[bool] = None,
                     known: Optional[list[str]] = None) -> str:
        """Start here for any task: the symbols relevant to the task text (search over names, signatures, docs and
        module names, boosted by PageRank and graph proximity) with source for the strongest, their callers when
        the task is a change, tests and config, in one budgeted answer with path:line anchors. scope = path
        prefix. known = symbols/files/path:line you already understand (default: what you looked at this session;
        [] = fresh): results are ranked by how they connect to it and the route to each is shown. Bodies are
        included for tasks and "how does it work" questions, signatures only for "where is / who calls"
        questions."""
        anchors, source = app.known_anchors(known)
        r = context.task_context(app.ix, app.atlas, task, scope or None, token_budget or None, include_source,
                                 known=anchors, known_source=source)
        for it in r["items"]:
            if it.get("source"):   # every body shown is known from here on: a later answer in the session does not repeat it
                app.working.note_symbol(it)
        return context.render(r)

    @tool
    def connections(target: Union[str, list[str]], known: Optional[list[str]] = None, max_hops: int = 6,
                    limit: int = 4) -> str:
        """How what you know reaches what you want: the shortest labelled routes from known symbols/files to each
        target through calls, callback registrations, class membership, file containment, imports and
        env/table/config relationships, every hop a path:line (A --calls@12--> B; A <--calls@12-- B means B calls
        A). known defaults to what you looked at this session. Use it for "how does A reach B" or to place a symbol
        you just found relative to code you understand. Lists the symbols on the way and says when no route exists
        within max_hops; limit = distinct routes per target."""
        anchors, source = app.known_anchors(known)
        return connect.render(connect.connections(app.ix, app.atlas, target, anchors, max_hops=max_hops, limit=limit,
                                                  known_source=source))

    @tool
    def change_context(base_ref: str = "HEAD", include_untracked: bool = True, depth: int = 3,
                       comparison: str = "working", merge_base: bool = False, token_budget: Optional[int] = None,
                       max_files: int = 200, max_symbols: int = 60) -> str:
        """Git diff blast radius: changed/removed symbols, transitive consumers, config/schema dependencies,
        and candidate tests with evidence. comparison: working (includes staged), staged, or branch (HEAD).
        merge_base=True compares a feature branch against its common ancestor with base_ref. Reports caps,
        uncertainty and index versions; automatic budgets adapt to change risk and fanout. Set token_budget to override. Does not run tests or modify Git."""
        return changes.render(changes.change_context(app.ix, app.atlas, base_ref, include_untracked, max_symbols,
            depth=depth, max_files=max_files, comparison=comparison, merge_base=merge_base, token_budget=token_budget))

    @tool
    def relationships(path: Optional[str] = None, kind: Optional[str] = None, direction: str = "both",
                      limit: int = 50, offset: int = 0, token_budget: int = 2000) -> str:
        """Evidence-backed code/config/schema dependencies. source consumes target; direction=in finds
        consumers of path, out finds dependencies. Includes unresolved local targets and pagination."""
        return relation_tools.render(relation_tools.relationships(app.ix, path, kind, direction, limit, offset), token_budget)

    @tool
    def programming_patterns(path: Optional[str] = None, pattern: Optional[str] = None, limit: int = 50,
                             offset: int = 0, token_budget: int = 2000, catalogue: bool = False) -> str:
        """Explain 12 programming patterns at actual source locations, with syntax evidence and confidence.
        Filter by file/path and pattern ID; catalogue=True lists the supported concepts without source scans."""
        if catalogue:
            return json.dumps(pattern_tools.catalogue(), ensure_ascii=False, separators=(",", ":"))
        return pattern_tools.render(pattern_tools.patterns(app.ix, path, pattern, limit, offset), token_budget)

    @tool
    def list_files(prefix: str = "", limit: int = 300) -> str:
        """List indexed source files under a path prefix with language, line and symbol counts."""
        rows = app.atlas.list_files(prefix, limit)
        return "\n".join(f"{r['path']}  {r['lang']} {r['lines']}L {r['symbols']}sym" for r in rows) or "(none)"

    # ------------------------------------------------------------------ files / symbols
    @tool
    def file_outline(path: str, include_private: bool = True, show_imports: bool = True, full: bool = False) -> str:
        """Tree of classes/functions/methods in a file with line ranges, signatures, reference counts, imports and
        importers. Read this instead of the file. Files with more than 60 symbols are summarised (the most
        referenced members of each class, the most referenced top-level symbols, with a count of what was folded
        and how to get it); full=True lists everything."""
        o = app.atlas.outline(path, include_private)
        if o is None:
            return f"file not indexed: {path} (unsupported language, ignored, or wrong path; try list_files)"
        app.working.note_file(o["path"])
        return fmt.outline(o, show_imports, compact=not full)

    @tool
    def find_symbols(query: str, kind: str = "", path: str = "", limit: int = 25) -> str:
        """Find definitions by name (exact, prefix, substring, fuzzy CamelCase). kind: function|method|class|
        interface|type|enum|struct|module; path: substring filter."""
        return fmt.symbol_list(app.atlas.find_symbols(query, kind or None, path or None, limit))

    @tool
    def symbol(name: str, path: Optional[str] = None, full: bool = False) -> str:
        """Everything about one symbol: location, signature, doc, complexity, members, callers, callees.
        name may be plain (foo), qualified (Class.foo) or path:line. Use path= to disambiguate. Hubs are folded
        (12 members, callers grouped by file, 20 callees, each fold naming the call that lists the rest); full=True
        lists everything."""
        d = app.atlas.symbol_detail(name, path)
        if d.get("found"):
            app.working.note_symbol(d)
        return fmt.symbol_detail(d, compact=not full)

    @tool
    def symbol_source(target: str, path: str = "", context: int = 0, max_lines: int = 400) -> str:
        """Source text of one symbol (or path:start-end range) with line numbers. Cheaper than reading the file."""
        r = app.atlas.source(target, path or None, context, max_lines)
        if r.get("found"):
            app.remember(r["path"], r["start"] + context)
        return fmt.source(r)

    @tool
    def references(name: str, path: str = "", kind: str = "", limit: int = 100, offset: int = 0) -> str:
        """Every use of a name, grouped by file with line and enclosing symbol; path (or a qualified name) pins
        one definition. Reports the true total and a next offset. kind: call | new | arg | ref | import."""
        r = app.atlas.references(name, kind or None, limit, False, path or None, offset)
        if r.get("resolved_to") and r["definitions"]:
            d = r["definitions"][0]
            app.working.note_symbol({"path": d["path"], "start_line": d["line"], "qualified": d["qualified"]})
        return fmt.references(r)

    @tool
    def impact(name: str, path: str = "", depth: int = 3, full: bool = False) -> str:
        """Blast radius: transitive callers up to depth hops, affected files and tests. Run before changing a
        signature or behaviour. Crowded levels are grouped by file; full=True lists every symbol."""
        d = app.atlas.impact(name, path or None, depth)
        if d.get("found"):
            app.working.note_symbol(d["root"])
        return fmt.impact(d, compact=not full)

    @tool
    def triggers(name: str, path: Optional[str] = None) -> str:
        """What causes a symbol to run: route/event decorators, callback registrations (addEventListener,
        app.get, subscribe...), direct callers, owning class. Answers 'what triggers what'."""
        d = app.atlas.triggers(name, path)
        if d.get("found"):
            app.working.note_symbol(d["symbol"])
        return fmt.triggers(d)

    @tool
    def dependencies(path: str) -> str:
        """File-level imports (resolved to repo files), external packages, importers and transitive dependents."""
        d = app.atlas.dependencies(path)
        return fmt.dependencies(d) if d else f"file not indexed: {path}"

    @tool
    def module_graph(depth: int = 2, min_edges: int = 2) -> str:
        """Directory-level import graph (which modules depend on which), aggregated at the given path depth."""
        return fmt.module_graph(app.atlas.module_graph(depth, min_edges))

    @tool
    def hotspots(limit: int = 12) -> str:
        """Most complex and longest functions, largest files, key symbols by PageRank, import cycles."""
        return fmt.hotspots(app.atlas.hotspots(limit))

    @tool
    def unreferenced(limit: int = 50, include_exported: bool = False) -> str:
        """Dead-code candidates: functions/classes with no references anywhere. Verify with triggers/grep before deleting."""
        return fmt.unreferenced(app.atlas.unreferenced(limit, include_exported))

    @tool
    def findings(code: Optional[str] = None, path: Optional[str] = None, limit: int = 100) -> str:
        """Static findings from the index: TAG_TODO/TAG_FIXME/TAG_BUG comment tags, EMPTY_CATCH, SWALLOWED_EXCEPTION,
        BARE_EXCEPT. Filter by code prefix or path."""
        return fmt.findings(app.atlas.findings(code, path, limit))

    @tool
    def grep(pattern: str, glob: Optional[str] = None, limit: int = 100) -> str:
        """Regex search over indexed source files (smart case). glob like '*.ts' or 'backend/*'. Use for strings,
        config keys and dynamic names the symbol index cannot see."""
        return fmt.grep(app.atlas.grep(pattern, glob, limit))

    # ------------------------------------------------------------------ edits
    @tool(group="edit")
    def replace_symbol(name: str, new_text: str, path: Optional[str] = None, dry_run: bool = False,
                       expected_hash: Optional[str] = None, force: bool = False) -> str:
        """Replace exactly one declaration (its syntax-tree span incl. decorators/export) with new_text, re-indented.
        Neighbours on the same line are untouched. The new file is parsed first and refused if it adds syntax
        errors (force=True overrides). Pass expected_hash (old_hash from a previous call) to guard against
        concurrent changes. Returns old text + hashes; re-indexes immediately. dry_run previews."""
        r = edits.replace_symbol(app.atlas, name, new_text, path, dry_run, expected_hash, force)
        if r.get("ok"):
            app.remember(r["path"], int(str(r.get("replaced_lines", "0")).split("-")[0] or 0) or None)
        return json.dumps(r, indent=1)

    @tool(group="edit")
    def insert_code(name: str, new_text: str, where: str = "after", path: Optional[str] = None,
                    dry_run: bool = False, force: bool = False) -> str:
        """Insert new_text on its own lines before or after an existing symbol (where='before'|'after'), matching
        its indentation. Parse-validated before writing."""
        r = edits.insert_relative(app.atlas, name, new_text, where, path, dry_run, force)
        if r.get("ok"):
            app.remember(r["path"], r.get("inserted_at_line"))
        return json.dumps(r, indent=1)

    @tool(group="edit")
    def rename_symbol(name: str, new_name: str, path: Optional[str] = None, dry_run: bool = True,
                      include_tests: bool = True) -> str:
        """Rename a symbol at its definition and every reference that resolves to it (identifier nodes only:
        strings, comments and same-named symbols elsewhere are untouched; ambiguous references are listed and
        skipped). Defaults to dry_run=True: review the plan, then call again with dry_run=False."""
        for s in app.atlas.resolve_symbol(name, path)[:1]:
            app.working.note_symbol(s)
        return json.dumps(edits.rename_symbol(app.atlas, name, new_name, path, dry_run, include_tests), indent=1)

    # ------------------------------------------------------------------ SNIPE loop
    @tool(group="snipe", read_only=True)
    def next_snipe(agent: Optional[str] = None) -> str:
        """The highest-value isolated issue to fix next (your own in-progress snipe first). Returns the problem,
        evidence, suspect symbol with confidence, related symbols, context cost, risk and the verification tests.
        Pass agent="<stable id>" so several agents never pick the same item."""
        return render_next(app.snipe.next(agent))

    @tool(group="snipe", read_only=False)
    def claim_snipe(id: str, agent: Optional[str] = None) -> str:
        """Take ownership of an issue (state FIXING) and snapshot its files so verify_snipe can measure the patch
        surface. Returns level-1 context (symbol names + ranked candidates)."""
        r = app.snipe.claim(id, agent)
        if r["ok"]:
            it = app.snipe.get(id) or {}
            app.remember(it.get("suspect_path"), it.get("suspect_line"))
        return app.snipe.context(id, 1) if r["ok"] else r["error"]

    @tool(group="snipe", read_only=True)
    def get_snipe_context(id: str, level: int = 1) -> str:
        """Lazy context for an issue. Climb only as high as needed: 0 summary, 1 symbol names + candidates with
        evidence/counter-evidence, 2 signatures, 3 implementation bodies of suspect + related, 4 callers and
        dependencies, 5 the full suspect file, 6 subsystem context (task_context)."""
        return app.snipe.context(id, int(level))

    @tool(group="snipe", read_only=False)
    def verify_snipe(id: str, deep: bool = False) -> str:
        """Evaluate outward from what you changed: level 1 parse check + dead-reference check + targeted tests;
        level 2 tests of callers (only if level 1 passed); level 3 full suite only when deep=True or risk is high.
        PASS -> DONE, FAIL -> back to FIXING with the failure as evidence, blast radius over the limit -> SNIPE
        ABORTED (promoted to INVESTIGATE)."""
        return render_verify(app.snipe.verify(id, deep))

    @tool(group="snipe", read_only=False)
    def snipe_report(text: str, source: str = "test") -> str:
        """Feed external evidence: paste failing test / typecheck / lint / runtime output. Each failing test or
        distinct location becomes an issue, narrowed to a suspect symbol. source: test | typecheck | lint | runtime."""
        created = app.snipe.report(text, source)
        return "\n".join(render_item(c, brief=True) for c in created) if created else "no failures recognised in that output"

    @tool(group="snipe", read_only=False)
    def snipe_scan(run: Optional[str] = None) -> str:
        """Scan for concrete problems. Without run: cheap static pass (unresolved relative imports become issues;
        complexity/cycles/swallowed errors become dormant opportunities). run='tests'|'typecheck'|'lint' also runs
        the configured command and turns failures into issues. Index deltas are scanned automatically on every save."""
        r = app.snipe.scan(run)
        L = [f"scan: {len(r['created'])} new, {r['issues_open']} issues open, {r['opportunities']} opportunities (dormant)"]
        if r.get("run"):
            rr = r["run"]
            L.append(f"{run}: " + (f"{'ok' if rr.get('ok') else 'FAILED'} ({rr.get('seconds')}s) -> issues {rr.get('issues')}" if rr.get("ran") else rr.get("error", "")))
        return "\n".join(L)

    @tool(group="snipe", read_only=True)
    def snipe_board() -> str:
        """Ready-to-snipe list (score, symbols, tokens, risk), investigating, current work per agent, counts."""
        return render_board(app.snipe.board(), watcher_alive=bool(app.watcher and app.watcher.status()["alive"]))

    @tool(group="snipe", read_only=True)
    def snipe_list(kind: str = "issue", state: Optional[str] = None, priority: Optional[str] = None, limit: int = 30) -> str:
        """List records. kind: issue | opportunity. state: comma list of FOUND,TARGETED,FIXING,VERIFYING,DONE,BLOCKED.
        priority: 'SNIPE NOW' | INVESTIGATE | DEFER | IGNORE."""
        rows = app.snipe.items(kind, state, priority)[:limit]
        return "\n".join(f"{i['score']:>3} {i['id']:<8} {i['state']:<10} {i['priority'] or '-':<11} {i['problem'][:80]}"
                          + (f"  -> {i['suspect']}" if i.get("suspect") else "") for i in rows) or "(none)"

    @tool(group="snipe", read_only=True)
    def snipe_item(id: str) -> str:
        """Full record of one issue/opportunity: evidence, candidates, factors, notes."""
        it = app.snipe.get(id)
        return render_item(it) if it else f"no item {id}"

    @tool(group="snipe", read_only=False)
    def snipe_add(problem: str, evidence: Optional[list[str]] = None, kind: str = "issue", suspect: Optional[str] = None,
                  priority: Optional[str] = None) -> str:
        """Record a concrete defect (kind=issue) or a dormant improvement (kind=opportunity). Give evidence as
        'path:line' or symbol names so NARROW can find the suspect. Do not fix drive-by findings; log them."""
        rec = app.snipe.add(problem, evidence, kind, suspect, priority=priority, source="manual")
        app.snipe.save()
        return render_item(rec, brief=True)

    @tool(group="snipe", read_only=False)
    def snipe_update(id: str, state: Optional[str] = None, note: Optional[str] = None, priority: Optional[str] = None,
                     suspect: Optional[str] = None, kind: Optional[str] = None, agent: Optional[str] = None) -> str:
        """Change state (FOUND|TARGETED|FIXING|VERIFYING|DONE|BLOCKED), priority, suspect or kind; add a note."""
        r = app.snipe.update(id, state, note, priority, suspect, kind, agent)
        return render_item(r["item"], brief=True) if r["ok"] else r["error"]

    @tool(group="snipe", read_only=False)
    def snipe_abort(id: str, reason: str) -> str:
        """Stop condition: the fix stopped being a snipe (schema migration, public API change, many modules).
        Marks it BLOCKED / INVESTIGATE with the reason so nobody wanders for 40 minutes."""
        r = app.snipe.abort(id, reason)
        return render_item(r["item"], brief=True) if r["ok"] else r["error"]

    @tool(group="snipe", read_only=False)
    def snipe_config(test_cmd: Optional[str] = None, full_test_cmd: Optional[str] = None, typecheck_cmd: Optional[str] = None,
                     lint_cmd: Optional[str] = None, max_patch_symbols: Optional[int] = None,
                     max_patch_files: Optional[int] = None, test_timeout: Optional[int] = None) -> str:
        """Configure verification. test_cmd is a template with {file} (e.g. 'npx vitest run {file}',
        'python -m pytest {file} -q'); auto-detected from package.json/pyproject when unset. Stop-condition limits too."""
        cfg = app.snipe.set_config(test_cmd=test_cmd, full_test_cmd=full_test_cmd, typecheck_cmd=typecheck_cmd, lint_cmd=lint_cmd,
                                   max_patch_symbols=max_patch_symbols, max_patch_files=max_patch_files, test_timeout=test_timeout)
        det = app.snipe.detect_test_cmd()
        return json.dumps({**cfg, "detected_test_cmd": det[0] if det else None}, indent=1)

    # ------------------------------------------------------------------ resources / prompts
    @mcp.resource("codeatlas://snipe")
    def snipe_md() -> str:
        """The SNIPE.md board."""
        return app.snipe.render_md()

    @mcp.resource("codeatlas://map")
    def map_resource() -> str:
        """Ranked repository map (2000 tokens)."""
        with app._lock:
            return rank.repo_map(app.ix, 2000)

    @mcp.prompt()
    def onboard() -> str:
        """How to work on this repo with CodeAtlas."""
        return ("Use CodeAtlas tools before reading files: task_context, file_outline, symbol, impact, triggers, "
                "references; connections(target, known) for how code you know reaches code you need. "
                "Fix work: next_snipe -> claim_snipe -> get_snipe_context -> patch -> verify_snipe -> next.")


    def _strip_titles(node) -> None:
        """Pydantic writes a `title` for every parameter ("Token Budget"); they say nothing the name does not and
        cost about 130 tokens on every request for the always-loaded schemas."""
        if isinstance(node, dict):
            node.pop("title", None)
            for v in node.values():
                _strip_titles(v)
        elif isinstance(node, list):
            for v in node:
                _strip_titles(v)
    try:
        for t in mcp._tool_manager.list_tools():
            _strip_titles(t.parameters)
    except Exception:  # an SDK without that attribute keeps its titles
        pass

    return mcp


def serve(repo: Path, watch: bool = True, dashboard: bool = True, port: Optional[int] = None,
          profile: str = "all") -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    app = App(repo, watch=watch, dashboard=dashboard, port=port, profile=profile)
    # initial index in the background so the MCP handshake is instant; tools wait on the lock
    def _initial():
        with app._lock:
            st = app.ix.update()
            rank.ensure_rank(app.ix)
        log.info("initial index: %s", st.as_dict())
    threading.Thread(target=_initial, daemon=True).start()
    mcp = build_server(app)
    log.info("codeatlas %s serving %s (dashboard: %s)", __version__, app.repo, app.dashboard.url if app.dashboard else "off")
    try:
        mcp.run(transport="stdio")
    finally:
        app.close()
