"""codeatlas command line.

  codeatlas serve [--repo PATH]         MCP server (stdio) + watcher + dashboard   <- what agents run
  codeatlas init [--global]             set up this repo (auto-detects Claude Code / Codex / Cursor); --global once per machine
  codeatlas index [--full]              build/refresh the index once
  codeatlas map [--budget N]            print the ranked repo map
  codeatlas outline PATH | symbol NAME | refs NAME | impact NAME | triggers NAME
  codeatlas snipe [board|scan|next|md|report FILE]   the SNIPE loop from the shell
  codeatlas dashboard [--open]          run only the dashboard
  codeatlas doctor                      check installation
  codeatlas hook pretool                Claude Code PreToolUse guard: Grep/Read/Edit/Write -> CodeAtlas tools (used by the plugin)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import webbrowser
from pathlib import Path
from typing import Optional

from . import __version__


def _repo(args) -> Path:
    p = Path(getattr(args, "repo", None) or os.environ.get("CODEATLAS_REPO") or os.getcwd()).resolve()
    if not p.is_dir():
        sys.exit(f"repo path does not exist: {p}")
    if not getattr(args, "global_", False):  # `init --global` may run from anywhere; it never touches an index
        from .roots import protected_reason
        reason = protected_reason(p)
        if reason:
            sys.exit(f"codeatlas: refusing to index: {reason}.\nCodeAtlas indexes project folders only: "
                     f"cd into a repository (or pass --repo <project>) and run it there.\n"
                     f"Set CODEATLAS_ALLOW_ANY_ROOT=1 to override deliberately.")
    return p


def _atlas(repo: Path):
    from .graph import Atlas
    from .indexer import Index
    ix = Index(repo)
    return ix, Atlas(ix)


# ------------------------------------------------------------------ commands
def cmd_serve(args) -> None:
    from .server import serve
    serve(_repo(args), watch=not args.no_watch, dashboard=not args.no_dashboard, port=args.port,
          profile=args.profile or os.environ.get("CODEATLAS_PROFILE", "all"))


def cmd_context(args) -> None:
    from . import context
    ix, atlas = _atlas(_repo(args))
    atlas.ensure_fresh()
    print(context.render(context.task_context(ix, atlas, args.task, args.scope, args.budget)))


def cmd_changes(args) -> None:
    from . import changes
    ix, atlas = _atlas(_repo(args))
    try:
        result = changes.change_context(ix, atlas, args.base, not args.no_untracked, args.max_symbols,
            comparison=args.comparison, merge_base=args.merge_base, depth=args.depth,
            max_files=args.max_files, token_budget=args.budget)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else changes.render(result))
    finally:
        ix.close()


def cmd_relationships(args) -> None:
    from . import relationships
    ix, atlas = _atlas(_repo(args))
    try:
        atlas.ensure_fresh()
        result = relationships.relationships(ix, args.path, args.kind, args.direction, args.limit, args.offset)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else relationships.render(result, args.budget))
    finally:
        ix.close()


def cmd_patterns(args) -> None:
    from . import patterns
    if args.catalogue:
        print(json.dumps(patterns.catalogue(), ensure_ascii=False, indent=2))
        return
    ix, atlas = _atlas(_repo(args))
    try:
        atlas.ensure_fresh()
        result = patterns.patterns(ix, args.path, args.pattern, args.limit, args.offset)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else patterns.render(result, args.budget))
    finally:
        ix.close()


def cmd_index(args) -> None:
    from . import rank
    ix, _ = _atlas(_repo(args))
    st = ix.update(full=args.full, progress=lambda m: print(m, file=sys.stderr, end="\r"))
    r = rank.compute_pagerank(ix)
    print(json.dumps({"index": st.as_dict(), "rank": r}, indent=1))


def cmd_map(args) -> None:
    from . import rank
    ix, atlas = _atlas(_repo(args))
    atlas.ensure_fresh()
    print(rank.repo_map(ix, args.budget, focus_paths=args.focus or None))


def cmd_query(args) -> None:
    from . import fmt
    ix, atlas = _atlas(_repo(args))
    atlas.ensure_fresh()
    c = args.cmd
    if c == "outline":
        o = atlas.outline(args.target)
        print(fmt.outline(o) if o else "not indexed")
    elif c == "symbol":
        print(fmt.symbol_detail(atlas.symbol_detail(args.target)))
    elif c == "refs":
        print(fmt.references(atlas.references(args.target)))
    elif c == "impact":
        print(fmt.impact(atlas.impact(args.target)))
    elif c == "triggers":
        print(fmt.triggers(atlas.triggers(args.target)))
    elif c == "deps":
        d = atlas.dependencies(args.target)
        print(fmt.dependencies(d) if d else "not indexed")
    elif c == "find":
        print(fmt.symbol_list(atlas.find_symbols(args.target)))
    elif c == "overview":
        print(fmt.overview(atlas.overview()))
    elif c == "hotspots":
        print(fmt.hotspots(atlas.hotspots()))
    elif c == "source":
        print(fmt.source(atlas.source(args.target)))


def cmd_snipe(args) -> None:
    from .snipe import Snipe, render_board, render_item, render_next
    ix, atlas = _atlas(_repo(args))
    atlas.ensure_fresh()
    sn = Snipe(atlas)
    a = args.action
    if a == "scan":
        r = sn.scan(args.run)
        print(f"{len(r['created'])} new, {r['issues_open']} issues open, {r['opportunities']} opportunities -> {sn.md_path}")
        if r.get("run"):
            print(r["run"])
    elif a == "next":
        print(render_next(sn.next(args.agent)))
    elif a == "md":
        print(sn.render_md())
    elif a == "report":
        text = Path(args.file).read_text(encoding="utf-8", errors="replace") if args.file else sys.stdin.read()
        for c in sn.report(text, args.source or "test"):
            print(render_item(c, brief=True))
    elif a == "context":
        print(sn.context(args.file or "", int(args.level or 1)))
    else:
        print(render_board(sn.board()))


def cmd_dashboard(args) -> None:
    import logging
    import time
    from .server import App
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    app = App(_repo(args), watch=True, dashboard=True, port=args.port)
    with app._lock:
        app.ix.update()
    url = app.dashboard.url if app.dashboard else None
    print(f"dashboard: {url}")
    if args.open and url:
        webbrowser.open(url)
    if app.dashboard and app.dashboard.reused:
        print("(already running in another process; nothing to do)")
        return
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        app.close()


def cmd_open(args) -> None:
    repo = _repo(args)
    info = repo / ".codeatlas" / "dashboard.json"
    if info.exists():
        url = json.loads(info.read_text()).get("url")
        print(url)
        webbrowser.open(url)
    else:
        print("no running dashboard for this repo; start `codeatlas serve` or `codeatlas dashboard --open`")


# ------------------------------------------------------------------ install
GUIDE_BLOCK = """
## CodeAtlas (code map + SNIPE MCP)
This repo is indexed by the `codeatlas` MCP server. Before reading files, use its tools:
start with `task_context("<what you are doing>", scope="dir/")`, then `file_outline`, `find_symbols`, `symbol`,
`symbol_source`, `references`, `impact`, `triggers`, `dependencies`; `change_context` before finishing.
Read symbols, not whole files. Edit precisely with `replace_symbol` / `insert_code` / `rename_symbol`
(syntax-checked); never rewrite a whole source file.
Fix work follows SNIPE (Scan, Narrow, Infer, Patch, Evaluate): `next_snipe(agent="<your id>")` ->
`claim_snipe(id)` -> `get_snipe_context(id, level)` (climb levels only as needed) -> smallest patch ->
`verify_snipe(id)` -> next. Paste failures into `snipe_report`; log drive-by findings with `snipe_add`; never
widen a snipe into a refactor (opportunities stay dormant). `SNIPE.md` mirrors the board. The index updates
itself on every file change. Dashboard: see `index_status`.
"""


def _server_command(repo: Path) -> tuple[str, list[str]]:
    exe = shutil.which("codeatlas")
    if exe:
        return exe, ["serve", "--repo", str(repo)]
    # fall back to running the module with this interpreter
    return sys.executable, ["-m", "codeatlas", "serve", "--repo", str(repo)]


def install_claude(repo: Path, scope: str = "project") -> str:
    cmd, args = _server_command(repo)
    if scope == "project":
        path = repo / ".mcp.json"
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data.setdefault("mcpServers", {})["codeatlas"] = {"command": cmd, "args": args}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return f"Claude Code: wrote {path} (project scope; approve the server when Claude asks)"
    path = Path.home() / ".claude.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("mcpServers", {})["codeatlas"] = {"command": cmd, "args": args}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f"Claude Code: wrote user-scope server into {path}"


def install_codex(repo: Path) -> str:
    cmd, args = _server_command(repo)
    path = Path.home() / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    name = "codeatlas" if not _toml_has_server(text, "codeatlas") or _toml_same_repo(text, repo) else f"codeatlas_{repo.name.lower()}"
    block = (f'\n[mcp_servers.{name}]\ncommand = {json.dumps(cmd)}\nargs = {json.dumps(args)}\n'
             f'startup_timeout_sec = 60\n')
    if _toml_has_server(text, name):
        text = _toml_replace_server(text, name, block)
    else:
        text = text.rstrip("\n") + "\n" + block
    path.write_text(text, encoding="utf-8")
    return f"Codex: registered mcp_servers.{name} in {path}"


def install_cursor(repo: Path) -> str:
    cmd, args = _server_command(repo)
    path = repo / ".cursor" / "mcp.json"
    path.parent.mkdir(exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("mcpServers", {})["codeatlas"] = {"command": cmd, "args": args}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f"Cursor: wrote {path}"


def _toml_has_server(text: str, name: str) -> bool:
    return f"[mcp_servers.{name}]" in text


def _toml_same_repo(text: str, repo: Path) -> bool:
    i = text.find("[mcp_servers.codeatlas]")
    if i < 0:
        return True
    seg = text[i:].split("\n[", 1)[0]
    return str(repo).replace("\\", "\\\\") in seg or str(repo) in seg or "--repo" not in seg


def _toml_replace_server(text: str, name: str, block: str) -> str:
    i = text.find(f"[mcp_servers.{name}]")
    j = text.find("\n[", i + 1)
    j = len(text) if j < 0 else j
    return text[:i].rstrip("\n") + "\n" + block.lstrip("\n") + text[j:].lstrip("\n") if j < len(text) else text[:i].rstrip("\n") + "\n" + block.lstrip("\n")


def write_guides(repo: Path) -> list[str]:
    out = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        p = repo / name
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        if "CodeAtlas (code map +" in text:
            continue
        p.write_text((text.rstrip("\n") + "\n" if text else f"# {repo.name}\n") + GUIDE_BLOCK, encoding="utf-8")
        out.append(str(p))
    return out


def cmd_plugin(args) -> None:
    from .agents import write_plugin
    target = Path(args.dir).resolve()
    files = write_plugin(target)
    print(f"plugin written to {target} ({len(files)} files)")
    print(f"  try it:     claude --plugin-dir \"{target}\"")
    print(f"  validate:   claude plugin validate \"{target}\"")
    print("  then use /codeatlas:find, /codeatlas:snipe, /codeatlas:review-changes or the codeatlas-* agents")


# ------------------------------------------------------------------ init (project) / init --global (machine)
def detect_agents() -> dict[str, bool]:
    home = Path.home()
    return {"claude": bool(shutil.which("claude")) or (home / ".claude").exists(),
            "codex": bool(shutil.which("codex")) or (home / ".codex").exists(),
            "cursor": (home / ".cursor").exists()}


def setup_global(msgs: list[str]) -> None:
    """One-time machine setup: the Claude Code plugin (agents + MCP server) or a user-scope server, Codex global."""
    home = Path.home()
    exe = shutil.which("codeatlas") or sys.executable
    args = ["serve"] if exe.endswith(("codeatlas", "codeatlas.exe", "codeatlas.EXE")) else ["-m", "codeatlas", "serve"]
    if shutil.which("claude"):
        from .agents import write_plugin
        market = home / ".codeatlas" / "marketplace"
        (market / ".claude-plugin").mkdir(parents=True, exist_ok=True)
        write_plugin(market / "codeatlas-plugin")
        (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
            "name": "codeatlas", "owner": {"name": "CodeAtlas"},
            "plugins": [{"name": "codeatlas", "source": "./codeatlas-plugin", "version": __version__,
                         "description": "CodeAtlas agents, commands and MCP server"}]}, indent=2), encoding="utf-8")
        import subprocess
        def run(*cmd):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
        ok, out = run("claude", "plugin", "marketplace", "add", str(market))
        if not ok and "already" not in " ".join(out).lower():
            run("claude", "plugin", "marketplace", "update", "codeatlas")
        ok, out = run("claude", "plugin", "install", "codeatlas@codeatlas", "--scope", "user", "-y")
        if ok:
            msgs.append("Claude Code: plugin 'codeatlas' installed at user scope (agents codeatlas-navigator/sniper/reviewer, "
                        "/codeatlas:find|snipe|review-changes, MCP server) -> works in every project")
        else:
            msgs.append("Claude Code: plugin install failed (" + " ".join(out)[:120] + "); falling back to a user-scope MCP server")
            _user_scope_claude(exe, args, msgs)
    elif (home / ".claude").exists():
        _user_scope_claude(exe, args, msgs)
    if shutil.which("codex") or (home / ".codex").exists():
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        block = f'\n[mcp_servers.codeatlas]\ncommand = {json.dumps(exe)}\nargs = {json.dumps(args)}\nstartup_timeout_sec = 60\n'
        text = _toml_replace_server(text, "codeatlas", block) if _toml_has_server(text, "codeatlas") else text.rstrip("\n") + "\n" + block
        path.write_text(text, encoding="utf-8")
        msgs.append(f"Codex: global mcp_servers.codeatlas in {path} (indexes whatever project Codex runs in)")
        msgs.append(install_codex_skill())


def install_codex_skill() -> str:
    """Codex discovers skills in ~/.agents/skills/<name>/SKILL.md and invokes them as `$codeatlas`."""
    from .agents import SKILL
    d = Path.home() / ".agents" / "skills" / "codeatlas"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return f"Codex: skill written to {d} (type `$codeatlas` in Codex; Codex uses `$`, not `/`)"


def _user_scope_claude(exe: str, args: list[str], msgs: list[str]) -> None:
    home = Path.home()
    path = home / ".claude.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("mcpServers", {})["codeatlas"] = {"command": exe, "args": args}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    from .agents import agent_files, PROJECT_PREFIX
    d = home / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in agent_files(PROJECT_PREFIX).items():
        (d / f"{name}.md").write_text(text, encoding="utf-8")
    msgs.append(f"Claude Code: user-scope MCP server + agents in {d} (every project)")


def cmd_init(args) -> None:
    """Set up CodeAtlas for the current repository (or the machine with --global). Safe to re-run."""
    repo = _repo(args)
    msgs: list[str] = []
    detected = detect_agents()
    if args.agent and args.agent != "auto":
        targets = ["claude", "codex", "cursor"] if args.agent == "all" else [args.agent]
    else:
        targets = [k for k, v in detected.items() if v] or ["claude"]
    if getattr(args, "global_", False):
        setup_global(msgs)
    else:
        for t in targets:
            if t == "claude":
                msgs.append(install_claude(repo, args.scope))
                if not args.no_agents:
                    from .agents import write_project_agents
                    write_project_agents(repo)
                    msgs.append(f"Claude Code: agents codeatlas-navigator/sniper/reviewer in {repo / '.claude' / 'agents'}")
            elif t == "codex":
                msgs.append(install_codex(repo))
            elif t == "cursor":
                msgs.append(install_cursor(repo))
    # init never indexes: the index is built by the agent's server (`codeatlas serve`) when a session starts in
    # a project folder, and kept live by the watcher. `--global` may run from anywhere and touches no project.
    if not getattr(args, "global_", False):
        if not args.no_guides:
            for p in write_guides(repo):
                msgs.append(f"wrote agent guidance into {p}")
        gi = repo / ".gitignore"
        marker = ".codeatlas/index.db"
        if not gi.exists() or marker not in gi.read_text(encoding="utf-8", errors="replace"):
            with gi.open("a", encoding="utf-8") as fh:
                fh.write("\n# CodeAtlas index (SNIPE.md is meant to be committed)\n.codeatlas/index.db*\n.codeatlas/dashboard.json\n")
            msgs.append("added .codeatlas/index.db to .gitignore")
        msgs.append("the index builds itself when your agent starts here (or run `codeatlas index` now)")
    print("\n".join(msgs))
    agents_line = ", ".join(t for t in targets) if not getattr(args, "global_", False) else "all detected agents"
    print(f"\nCodeAtlas is ready for {agents_line}. Open your agent in a project folder: the server indexes that folder, "
          "then the watcher and dashboard keep it live."
          + ("" if getattr(args, "global_", False) or not detected.get("claude") else
             "\nTip: `codeatlas init --global` once makes every project work without this step (Claude Code plugin + Codex)."))


def cmd_hook(args) -> None:
    from .hook import run
    sys.exit(run(args.hook_args))


def cmd_doctor(args) -> None:
    ok = True
    print(f"codeatlas {__version__} / python {sys.version.split()[0]}")
    try:
        import tree_sitter, tree_sitter_language_pack  # noqa
        from .languages import supported_languages, get_parser
        for lang in supported_languages():
            get_parser(lang)
        print(f"tree-sitter OK ({len(supported_languages())} languages)")
    except Exception as e:
        ok = False
        print(f"tree-sitter FAILED: {e}")
    try:
        from mcp.server.mcpserver import MCPServer  # noqa
        print("mcp SDK OK")
    except Exception as e:
        ok = False
        print(f"mcp SDK FAILED: {e}")
    try:
        import watchdog  # noqa
        print("watchdog OK")
    except Exception:
        print("watchdog missing (polling fallback will be used)")
    exe = shutil.which("codeatlas")
    print(f"codeatlas on PATH: {exe or 'NO (use `python -m codeatlas` or add the scripts dir to PATH)'}")
    print("all good" if ok else "problems found")


# ------------------------------------------------------------------ parser
def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="codeatlas", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    def add(name, fn, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("--repo", help="repository root (default: cwd or $CODEATLAS_REPO)")
        p.set_defaults(fn=fn)
        return p

    p = add("serve", cmd_serve, help="run the MCP server (stdio) with watcher and dashboard")
    p.add_argument("--no-watch", action="store_true")
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--port", type=int)
    p.add_argument("--profile", choices=["nav", "edit", "snipe", "all"], help="which tool groups to expose")
    p = add("context", cmd_context, help="task_context from the command line")
    p.add_argument("task")
    p.add_argument("--scope")
    p.add_argument("--budget", type=int, default=None, help="token budget (default: adaptive)")
    p = add("changes", cmd_changes, help="change_context vs a git ref")
    p.add_argument("--base", default="HEAD")
    p.add_argument("--comparison", choices=["working", "staged", "branch"], default="working")
    p.add_argument("--merge-base", action="store_true", help="compare with common ancestor of base and HEAD")
    p.add_argument("--no-untracked", action="store_true")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--max-files", type=int, default=200)
    p.add_argument("--max-symbols", type=int, default=60)
    p.add_argument("--budget", type=int, default=None, help="token budget (default: adaptive)")
    p.add_argument("--json", action="store_true")
    for name, handler in (("relationships", cmd_relationships), ("patterns", cmd_patterns)):
        p = add(name, handler, help="code/config/schema dependencies" if name == "relationships" else "programming patterns in context")
        p.add_argument("path", nargs="?")
        p.add_argument("--limit", type=int, default=50)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--budget", type=int, default=2000)
        p.add_argument("--json", action="store_true")
        if name == "relationships":
            p.add_argument("--kind")
            p.add_argument("--direction", choices=["both", "in", "out"], default="both")
        else:
            p.add_argument("--pattern")
            p.add_argument("--catalogue", action="store_true")
    p = add("index", cmd_index, help="build or refresh the index")
    p.add_argument("--full", action="store_true")
    p = add("map", cmd_map, help="print the ranked repo map")
    p.add_argument("--budget", type=int, default=None, help="token budget (default: adaptive)")
    p.add_argument("--focus", action="append")
    for name in ("outline", "symbol", "refs", "impact", "triggers", "deps", "find", "source"):
        p = add(name, cmd_query, help=f"{name} query")
        p.add_argument("target")
    for name in ("overview", "hotspots"):
        add(name, cmd_query, help=f"{name} query")
    p = add("snipe", cmd_snipe, help="SNIPE loop: board | scan [--run tests] | next | md | report FILE | context ID")
    p.add_argument("action", nargs="?", default="board", choices=["board", "scan", "next", "md", "report", "context"])
    p.add_argument("file", nargs="?", help="report: output file (default stdin); context: issue id")
    p.add_argument("--run", choices=["tests", "typecheck", "lint"])
    p.add_argument("--source", choices=["test", "typecheck", "lint", "runtime"])
    p.add_argument("--agent")
    p.add_argument("--level", type=int, default=1)
    p = add("dashboard", cmd_dashboard, help="run only the dashboard")
    p.add_argument("--port", type=int)
    p.add_argument("--open", action="store_true")
    add("open", cmd_open, help="open the running dashboard in a browser")
    for name in ("init", "install"):
        p = add(name, cmd_init, help="set up CodeAtlas here: detect Claude Code / Codex / Cursor, register the server, "
                                      "write agents + guidance; the index builds when the agent starts (idempotent)")
        p.add_argument("agent", nargs="?", choices=["auto", "claude", "codex", "cursor", "all"], default="auto",
                       help="which agent(s) to register (default: auto-detect installed ones)")
        p.add_argument("--global", dest="global_", action="store_true",
                       help="one-time machine setup: Claude Code plugin (agents + server) and Codex global server")
        p.add_argument("--scope", choices=["project", "user"], default="project", help="Claude Code config scope")
        p.add_argument("--no-guides", action="store_true", help="do not touch CLAUDE.md / AGENTS.md")
        p.add_argument("--no-agents", action="store_true", help="do not write .claude/agents/codeatlas-*.md")
    p = add("plugin", cmd_plugin, help="export the Claude Code plugin (agents, skill, commands, MCP server)")
    p.add_argument("action", nargs="?", default="export", choices=["export"])
    p.add_argument("dir", nargs="?", default="codeatlas-plugin", help="target directory (default ./codeatlas-plugin)")
    add("doctor", cmd_doctor, help="check the installation")
    p = sub.add_parser("hook", help="Claude Code hook runner (PreToolUse guard that redirects Grep/Glob/Read/Edit/Write "
                                    "of indexed files to CodeAtlas tools)")
    p.add_argument("hook_args", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_hook)

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return
    args.fn(args)


if __name__ == "__main__":
    main()
