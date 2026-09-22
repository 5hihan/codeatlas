"""Claude Code ``PreToolUse`` guard: steer reading, searching and editing to CodeAtlas when the repository is indexed.

Prose in CLAUDE.md is advice; the model still reaches for the built-in ``Grep``/``Glob``/``Read``/``Edit`` tools
because their schemas are always loaded while MCP tools are deferred. This hook turns the advice into a rule: when a
``.codeatlas/index.db`` exists for the working directory, a built-in call that CodeAtlas does better is denied and
the reason names the CodeAtlas tool (and arguments) to call instead, so the model recovers in the same turn.
Everything the index does not know about (unindexed files, docs, config, partial reads) passes through untouched.

Usage (hooks.json / settings.json)::

    codeatlas hook pretool [--prefix mcp__plugin_codeatlas_atlas] [--mode default|search|off]

* ``default`` gates, for files the index knows:
  - ``Grep``/``Glob`` and ``grep``/``rg``/``find`` run through ``Bash``      -> grep / find_symbols / references / list_files
  - whole-file ``Read`` (no offset/limit) and ``cat`` through ``Bash``       -> file_outline / symbol_source
  - ``Edit``/``MultiEdit`` whose old_string lies inside a declaration       -> replace_symbol / insert_code
  - ``Write`` over an existing source file                                  -> replace_symbol / insert_code
* ``search`` gates only the search tools (the pre-0.1.1 behaviour). ``strict`` is an alias of ``default``.
* ``off`` (or ``CODEATLAS_GUARD=off``) lets everything through.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_PREFIX = "mcp__plugin_codeatlas_atlas"
GATED_TOOLS = ("Grep", "Glob", "Bash", "Read", "Edit", "MultiEdit", "Write")
SEARCH_CMDS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "ug", "ugrep", "git-grep"}
LIST_CMDS = {"find", "fd", "fdfind"}
CAT_CMDS = {"cat", "bat", "type", "less", "more"}  # whole-file dumps; head/tail/sed -n are partial and pass
# languages the indexer parses; anything else (docs, json, lock files) is never gated
SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts", ".swift",
              ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".rb", ".php", ".scala", ".lua", ".dart", ".m", ".mm"}
# an Edit inside a declaration up to this long is sent through replace_symbol; longer ones may use Edit
EDIT_SYMBOL_MAX_LINES = 150
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def find_index(start: Path, stop: Optional[Path] = None) -> Optional[Path]:
    """Nearest ``.codeatlas/index.db`` at or above *start* (Claude's cwd may be a sub-directory of the repo).

    *stop* bounds the walk (tests); by default it climbs to the filesystem root.
    """
    from .roots import protected_reason
    p = start.resolve()
    for d in (p, *p.parents):
        db = d / ".codeatlas" / "index.db"
        if db.is_file() and not protected_reason(d):  # a stray index in $HOME or a user folder does not count
            return d
        if stop is not None and d == stop:
            break
    return None


# ------------------------------------------------------------------ index lookups (read-only, never raise)
def _connect(root: Path):
    import sqlite3
    db = (root / ".codeatlas" / "index.db").resolve()
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    con.row_factory = sqlite3.Row
    return con


def indexed_file(root: Path, rel: str) -> Optional[dict]:
    """``{"lines", "symbols"}`` when *rel* is in the index, else None (unknown files are never gated)."""
    try:
        con = _connect(root)
        try:
            f = con.execute("SELECT id, lines FROM files WHERE path=?", (rel,)).fetchone()
            if f is None:
                return None
            n = con.execute("SELECT COUNT(*) FROM symbols WHERE file_id=?", (f["id"],)).fetchone()[0]
            return {"lines": f["lines"] or 0, "symbols": int(n)}
        finally:
            con.close()
    except Exception:
        return None


def enclosing_symbol(root: Path, rel: str, start: int, end: int) -> Optional[dict]:
    """Innermost indexed declaration whose line span covers lines *start*..*end* of *rel*."""
    try:
        con = _connect(root)
        try:
            r = con.execute(
                "SELECT s.qualified, s.kind, s.start_line, s.end_line FROM symbols s JOIN files f ON s.file_id=f.id "
                "WHERE f.path=? AND s.start_line<=? AND s.end_line>=? ORDER BY (s.end_line - s.start_line) LIMIT 1",
                (rel, start, end)).fetchone()
            return dict(r) if r is not None else None
        finally:
            con.close()
    except Exception:
        return None


# ------------------------------------------------------------------ Bash command inspection
def _first_words(command: str) -> list[list[str]]:
    """Split a shell command line into segments (``|``, ``&&``, ``||``, ``;``) and return each segment's argv."""
    out: list[list[str]] = []
    for seg in re.split(r"\|\||&&|;|\||\n", command):
        seg = seg.strip()
        if not seg:
            continue
        try:
            argv = shlex.split(seg, posix=True)
        except ValueError:
            argv = seg.split()
        # drop leading env assignments, `cd X`, `command`, `sudo`, `time`
        while argv and ("=" in argv[0] and not argv[0].startswith("=") or argv[0] in ("command", "sudo", "time", "nice")):
            argv = argv[1:]
        if argv:
            out.append(argv)
    return out


def _primary(command: str) -> Optional[list[str]]:
    """argv of the segment that starts the command (after an optional leading ``cd``)."""
    segs = _first_words(command)
    if segs and segs[0][0] == "cd" and len(segs) > 1:
        segs = segs[1:]
    return segs[0] if segs else None


def _head(argv: list[str]) -> str:
    head = Path(argv[0]).name.lower()
    return head[:-4] if head.endswith(".exe") else head


def bash_search(command: str) -> Optional[str]:
    """Return 'grep' or 'find' when the *primary* action of a Bash command is a text search / file listing.

    A search used as a filter later in a pipeline (``git log | grep x``) is left alone; only a segment that starts
    the command (after an optional ``cd``) counts, since that is the model using Bash as its search tool.
    """
    argv = _primary(command)
    if not argv:
        return None
    head = _head(argv)
    if head in SEARCH_CMDS:
        return "grep"
    if head == "git" and len(argv) > 1 and argv[1] == "grep":
        return "grep"
    if head in LIST_CMDS:
        return "find"
    return None


def bash_cat_files(command: str) -> list[str]:
    """File operands when the primary action of a Bash command is a whole-file dump (``cat``, ``bat``, ``type``)."""
    argv = _primary(command)
    if not argv or _head(argv) not in CAT_CMDS:
        return []
    return [a for a in argv[1:] if not a.startswith("-")]


# ------------------------------------------------------------------ decisions
def decide(event: dict, prefix: str = DEFAULT_PREFIX, mode: str = "default") -> Optional[str]:
    """Return a deny reason for this PreToolUse event, or None to let the call through."""
    mode = (os.environ.get("CODEATLAS_GUARD") or mode or "default").lower()
    if mode == "off":
        return None
    full = mode != "search"
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    if tool not in GATED_TOOLS:
        return None
    root = find_index(Path(event.get("cwd") or os.getcwd()))
    if root is None:
        return None
    if tool == "Grep":
        return _deny_grep(root, prefix, str(inp.get("pattern") or ""))
    if tool == "Glob":
        return (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__list_files(prefix=\"dir/\")` to list "
                f"source files (with language, line and symbol counts) or `{prefix}__find_symbols(query)` to locate a "
                f"symbol, instead of Glob." + _tail(prefix, "grep"))
    if tool == "Bash":
        command = str(inp.get("command") or "")
        kind = bash_search(command)
        if kind == "grep":
            return (f"This repository is indexed by CodeAtlas ({root}). Do not shell out to grep/rg: use "
                    f"`{prefix}__grep(pattern=..., glob=...)` for text, `{prefix}__find_symbols` / `{prefix}__references` "
                    f"for identifiers." + _tail(prefix, "grep"))
        if kind == "find":
            return (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__list_files(prefix=\"dir/\")` "
                    f"instead of find." + _tail(prefix, "grep"))
        if full:
            for fp in bash_cat_files(command):
                r = _deny_read(root, prefix, str((Path(event.get("cwd") or root) / fp)), via="cat")
                if r:
                    return r
        return None
    if not full:
        return None
    fp = str(inp.get("file_path") or inp.get("path") or "")
    if tool == "Read":
        if inp.get("offset") is None and inp.get("limit") is None:
            return _deny_read(root, prefix, fp)
        return None
    if tool in ("Edit", "MultiEdit"):
        return _deny_edit(root, prefix, fp, inp.get("edits") or [inp])
    if tool == "Write":
        return _deny_write(root, prefix, fp)
    return None


def _tail(prefix: str, *tools: str) -> str:
    sel = ",".join(f"{prefix}__{t}" for t in tools)
    return (f" (If the tool schema is not loaded yet, call ToolSearch with \"select:{sel}\" first. "
            f"This guard is CodeAtlas; CODEATLAS_GUARD=search keeps only the search redirects, CODEATLAS_GUARD=off "
            f"disables it.)")


def _deny_grep(root: Path, prefix: str, pattern: str) -> str:
    hint = (f"For an identifier like '{pattern}' prefer `{prefix}__find_symbols(query=\"{pattern}\")` and "
            f"`{prefix}__references(\"{pattern}\")`, which return definitions, callers and path:line anchors. "
            if _IDENT.match(pattern) else "")
    return (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__grep(pattern=..., glob=..., limit=...)` "
            f"instead of the built-in Grep: it searches the same files and returns path:line hits. {hint}"
            f"Use `{prefix}__task_context(task, scope)` when you are starting a task rather than looking for one string."
            + _tail(prefix, "grep"))


def _indexed_source(root: Path, fp: str) -> Optional[tuple[str, dict]]:
    """(rel, index row) when *fp* is a source file inside *root* that the index knows; else None."""
    if not fp or Path(fp).suffix.lower() not in SOURCE_EXT or not _under(root, fp):
        return None
    rel = _rel(root, fp)
    info = indexed_file(root, rel)
    return (rel, info) if info else None


def _deny_read(root: Path, prefix: str, fp: str, via: str = "Read") -> Optional[str]:
    hit = _indexed_source(root, fp)
    if not hit:
        return None
    rel, info = hit
    how = "Do not dump it with cat" if via == "cat" else "Do not Read the whole file"
    return (f"{rel} is indexed by CodeAtlas ({info['lines']} lines, {info['symbols']} symbols). {how}: get its "
            f"structure with `{prefix}__file_outline(\"{rel}\")`, then read only the declarations you need with "
            f"`{prefix}__symbol_source(\"<Name or Class.method>\", path=\"{rel}\")` (or "
            f"`{prefix}__symbol_source(\"{rel}:START-END\")` for a line range). `{prefix}__task_context(task, scope)` "
            f"gathers the relevant symbols across files in one call. Read with an explicit offset/limit is still "
            f"allowed when you need raw lines." + _tail(prefix, "file_outline", "symbol_source"))


def _edit_ranges(root: Path, rel: str, edits: Iterable[dict]) -> list[tuple[int, int]]:
    """Line ranges (1-based, inclusive) that each edit's old_string occupies in the file; misses are skipped."""
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for e in edits:
        old = str((e or {}).get("old_string") or "")
        i = text.find(old) if old else -1
        if i < 0:
            continue  # the Edit tool will report the mismatch itself
        start = text.count("\n", 0, i) + 1
        out.append((start, start + old.count("\n")))
    return out


def _deny_edit(root: Path, prefix: str, fp: str, edits: Iterable[dict]) -> Optional[str]:
    hit = _indexed_source(root, fp)
    if not hit:
        return None
    rel, _ = hit
    for start, end in _edit_ranges(root, rel, edits):
        sym = enclosing_symbol(root, rel, start, end)
        if sym is None or sym["end_line"] - sym["start_line"] + 1 > EDIT_SYMBOL_MAX_LINES:
            continue
        q = sym["qualified"]
        return (f"This edit (lines {start}-{end}) is inside {sym['kind']} `{q}` ({rel}:{sym['start_line']}-"
                f"{sym['end_line']}), which CodeAtlas indexes. Replace the declaration with "
                f"`{prefix}__replace_symbol(name=\"{q}\", path=\"{rel}\", new_text=<the whole updated declaration>)`: "
                f"it is parse-checked and re-indexed at once. Read it first with "
                f"`{prefix}__symbol_source(\"{q}\", path=\"{rel}\")` if you have not. To add code next to it use "
                f"`{prefix}__insert_code(name=\"{q}\", new_text=..., where=\"before\"|\"after\", path=\"{rel}\")`; to rename "
                f"it everywhere use `{prefix}__rename_symbol`. Edit remains allowed outside declarations (imports, "
                f"module-level statements) and inside declarations longer than {EDIT_SYMBOL_MAX_LINES} lines."
                + _tail(prefix, "replace_symbol", "insert_code"))
    return None


def _deny_write(root: Path, prefix: str, fp: str) -> Optional[str]:
    hit = _indexed_source(root, fp)
    if not hit or not Path(fp).is_file():
        return None  # new files are fine
    rel, info = hit
    if info["symbols"] == 0:
        return None
    return (f"{rel} already exists and is indexed by CodeAtlas ({info['lines']} lines, {info['symbols']} symbols). "
            f"Do not rewrite the whole file with Write: change one declaration at a time with "
            f"`{prefix}__replace_symbol(name, path=\"{rel}\", new_text=...)`, add code with "
            f"`{prefix}__insert_code(name, new_text, where, path=\"{rel}\")`, and use Edit only for lines outside "
            f"declarations. Write is for new files." + _tail(prefix, "replace_symbol", "insert_code"))


def _under(root: Path, fp: str) -> bool:
    try:
        Path(fp).resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _rel(root: Path, fp: str) -> str:
    try:
        return Path(fp).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return fp


def run(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``codeatlas hook pretool``: read the event on stdin, print a decision, always exit 0."""
    import argparse
    ap = argparse.ArgumentParser(prog="codeatlas hook")
    ap.add_argument("event", nargs="?", default="pretool", choices=["pretool"])
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="MCP tool prefix to recommend")
    ap.add_argument("--mode", default=None, choices=["default", "search", "strict", "off"])
    ns = ap.parse_args(argv)
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0  # malformed or empty stdin: never block the tool
    try:
        reason = decide(event, ns.prefix, ns.mode or "default")
    except Exception:
        return 0
    if reason:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    return 0
