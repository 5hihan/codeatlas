"""Claude Code ``PreToolUse`` guard: steer text search to CodeAtlas when the repository is indexed.

Prose in CLAUDE.md is advice; the model still reaches for the built-in ``Grep``/``Glob`` tools (and ``grep``/``rg``
in Bash) because their schemas are always loaded while MCP tools are deferred. This hook turns the advice into a
rule: when a ``.codeatlas/index.db`` exists for the working directory, a search made with the built-in tools is
denied and the reason names the CodeAtlas tool to call instead. Everything else passes through untouched.

Usage (hooks.json / settings.json)::

    codeatlas hook pretool [--prefix mcp__plugin_codeatlas_atlas] [--mode default|strict|off]

* ``default`` gates ``Grep``, ``Glob`` and search commands run through ``Bash``.
* ``strict`` additionally gates whole-file ``Read`` of indexed source files (no ``offset``/``limit``).
* ``off`` (or ``CODEATLAS_GUARD=off``) lets everything through.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Optional

DEFAULT_PREFIX = "mcp__plugin_codeatlas_atlas"
SEARCH_CMDS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "ug", "ugrep", "git-grep"}
LIST_CMDS = {"find", "fd", "fdfind"}
# languages the indexer parses; Read of anything else (docs, json, lock files) is never gated
SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts", ".swift",
              ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".rb", ".php", ".scala", ".lua", ".dart", ".m", ".mm"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def find_index(start: Path, stop: Optional[Path] = None) -> Optional[Path]:
    """Nearest ``.codeatlas/index.db`` at or above *start* (Claude's cwd may be a sub-directory of the repo).

    *stop* bounds the walk (tests); by default it climbs to the filesystem root.
    """
    p = start.resolve()
    for d in (p, *p.parents):
        db = d / ".codeatlas" / "index.db"
        if db.is_file():
            return d
        if stop is not None and d == stop:
            break
    return None


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


def bash_search(command: str) -> Optional[str]:
    """Return 'grep' or 'find' when the *primary* action of a Bash command is a text search / file listing.

    A search used as a filter later in a pipeline (``git log | grep x``) is left alone; only a segment that starts
    the command (after an optional ``cd``) counts, since that is the model using Bash as its search tool.
    """
    segs = _first_words(command)
    if segs and segs[0][0] == "cd" and len(segs) > 1:
        segs = segs[1:]
    if not segs:
        return None
    argv = segs[0]
    head = Path(argv[0]).name.lower()
    head = head[:-4] if head.endswith(".exe") else head
    if head in SEARCH_CMDS:
        return "grep"
    if head == "git" and len(argv) > 1 and argv[1] == "grep":
        return "grep"
    if head in LIST_CMDS:
        return "find"
    return None


def decide(event: dict, prefix: str = DEFAULT_PREFIX, mode: str = "default") -> Optional[str]:
    """Return a deny reason for this PreToolUse event, or None to let the call through."""
    mode = (os.environ.get("CODEATLAS_GUARD") or mode or "default").lower()
    if mode == "off":
        return None
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    if tool not in ("Grep", "Glob", "Bash", "Read"):
        return None
    root = find_index(Path(event.get("cwd") or os.getcwd()))
    if root is None:
        return None
    tail = (f" (If the tool schema is not loaded yet, call ToolSearch with \"select:{prefix}__grep\" first. "
            f"This guard is CodeAtlas; CODEATLAS_GUARD=off disables it.)")
    if tool == "Grep":
        pattern = str(inp.get("pattern") or "")
        hint = (f"For an identifier like '{pattern}' prefer `{prefix}__find_symbols(query=\"{pattern}\")` and "
                f"`{prefix}__references(\"{pattern}\")`, which return definitions, callers and path:line anchors. "
                if _IDENT.match(pattern) else "")
        return (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__grep(pattern=..., glob=..., limit=...)` "
                f"instead of the built-in Grep: it searches the same files and returns path:line hits. {hint}"
                f"Use `{prefix}__task_context(task, scope)` when you are starting a task rather than looking for one string." + tail)
    if tool == "Glob":
        return (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__list_files(prefix=\"dir/\")` to list "
                f"source files (with language, line and symbol counts) or `{prefix}__find_symbols(query)` to locate a "
                f"symbol, instead of Glob." + tail)
    if tool == "Bash":
        kind = bash_search(str(inp.get("command") or ""))
        if kind == "grep":
            return (f"This repository is indexed by CodeAtlas ({root}). Do not shell out to grep/rg: use "
                    f"`{prefix}__grep(pattern=..., glob=...)` for text, `{prefix}__find_symbols` / `{prefix}__references` "
                    f"for identifiers." + tail)
        if kind == "find":
            return (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__list_files(prefix=\"dir/\")` "
                    f"instead of find." + tail)
        return None
    if tool == "Read" and mode == "strict":
        fp = str(inp.get("file_path") or inp.get("path") or "")
        if inp.get("offset") is None and inp.get("limit") is None and Path(fp).suffix.lower() in SOURCE_EXT \
                and _under(root, fp):
            rel = _rel(root, fp)
            return (f"This file is indexed by CodeAtlas. Get its structure with `{prefix}__file_outline(\"{rel}\")` and "
                    f"read only the symbols you need with `{prefix}__symbol_source(name, path=\"{rel}\")`. "
                    f"Read with an explicit offset/limit is still allowed when you need raw lines." + tail)
    return None


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
    ap.add_argument("--mode", default=None, choices=["default", "strict", "off"])
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
