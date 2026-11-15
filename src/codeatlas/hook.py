"""Claude Code ``PreToolUse`` guard: steer reading, searching and editing to CodeAtlas when the repository is indexed.

Prose in CLAUDE.md is advice; the model still reaches for the built-in ``Grep``/``Glob``/``Read``/``Edit`` tools
because their schemas are always loaded while MCP tools are deferred. This hook turns the advice into a rule: when a
``.codeatlas/index.db`` exists for the working directory, a built-in call that CodeAtlas does better is denied and
the reason names the CodeAtlas tool (and arguments) to call instead, so the model recovers in the same turn.

Two things keep the rule honest, learned from a model that worked around the first version:

* **Every statement counts.** ``export X=1; cd src; A=$(find . -name x); sed -n 1,50p $A | grep -v '^//'`` is split
  on ``;``, ``&&``, ``||``, newlines, pipes and ``$(...)``, so chaining does not hide a search or a rewrite.
* **The switch belongs to the user.** A command that assigns ``CODEATLAS_GUARD`` is denied, and no deny reason
  advertises the variable: the model cannot talk itself out of the guard, only the user can turn it off.

What the index does not cover is never gated: unindexed files, docs and config, bounded partial reads, and work in
directories outside the indexed repository (a reference checkout, ``/tmp``, another project) where CodeAtlas has
nothing to offer and the shell is the only tool available.

Usage (hooks.json / settings.json)::

    codeatlas hook pretool [--prefix mcp__plugin_codeatlas_atlas] [--mode default|search|off]
    codeatlas hook session-start [--prefix ...]   SessionStart: a few lines of orientation (index size, which
                                                  tool to reach for) injected at startup, resume and after compaction
    codeatlas hook answer [--prefix ...]          for the plugin's mod (hooks/register.js): given a Read or Grep call
                                                  on stdin, print {"result": <text>} with the index's answer (the
                                                  file's outline, the identifier's definitions and uses), or {} to
                                                  let the call go ahead

The same PreToolUse handler also auto-approves CodeAtlas's own read-only tools (READ_ONLY_TOOLS): a query of the
index never needs a permission prompt, while edits, SNIPE state changes and test runs keep asking.
CODEATLAS_AUTO_APPROVE=off turns that off.

* ``default`` gates, for the indexed repository only:
  - ``Grep``/``Glob`` and ``grep``/``rg``/``find`` in any Bash statement -> grep / find_symbols / references / list_files
  - whole-file ``Read`` (no limit, or over READ_MAX_LINES) and ``cat`` in Bash -> file_outline / symbol_source
  - ``Edit``/``MultiEdit`` whose old_string lies inside a declaration -> replace_symbol / insert_code
  - ``Write`` over an existing source file, and shell rewrites (``sed -i``, ``>``, ``tee``, inline scripts that
    write) of an indexed source file -> replace_symbol / insert_code
  - any command that sets ``CODEATLAS_GUARD``
* ``search`` gates only the search tools (the pre-0.1.1 behaviour). ``strict`` is an alias of ``default``.
* ``off``, or ``CODEATLAS_GUARD=off`` in the user's own environment, lets everything through.
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
INPLACE_CMDS = {"sed", "gsed", "perl", "awk"}      # with -i: in-place rewrite
# languages the indexer parses; anything else (docs, json, lock files) is never gated. A static copy of
# languages.EXT_TO_LANG (a test keeps them equal): this module runs on every tool call and must import nothing it
# does not need.
SOURCE_EXT = frozenset("""
.bash .c .cc .cjs .cpp .cs .cts .cu .cxx .gemspec .go .h .hh .hpp .hxx .ipp .java .js .jsx .kt .kts .lua .mjs .mts
.php .py .pyi .rake .rb .rs .sc .scala .sh .swift .ts .tsx .zsh
""".split())
# an Edit inside a declaration up to this long is sent through replace_symbol; longer ones may use Edit
EDIT_SYMBOL_MAX_LINES = 150
# a Read of an indexed file must be a bounded window: an explicit limit of at most this many lines
READ_MAX_LINES = 250
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GUARD_SET = re.compile(r"CODEATLAS_GUARD\s*=|CODEATLAS_GUARD['\"]?\s*[,:]|setx?\s+CODEATLAS_GUARD|"
                        r"environ\[['\"]CODEATLAS_GUARD", re.I)
_STMT_SPLIT = re.compile(r"\|\||&&|;|\r?\n")
_SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\2$", re.S | re.M)
_REDIRECT = re.compile(r"(?<![<>&\d])\d?>{1,2}\s*([^\s|;&<>]+)")
_SCRIPT_WRITE = re.compile(r"write_text\(|write_bytes\(|open\([^)]*['\"][wa]b?\+?['\"]|writeFileSync\(|writeFile\(|"
                           r"fs\.write|Set-Content|Out-File|File\.write|IO\.write|\.replace_file|shutil\.(copy|move)")
_PATH_TOKEN = re.compile(r"[\w./\\~:-]+\.(?:" + "|".join(sorted(e[1:] for e in SOURCE_EXT)) + r")\b")


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
def _argv(segment: str) -> list[str]:
    """argv of one simple command, without leading env assignments, ``command``, ``sudo``, ``time``, ``nice``."""
    segment = segment.strip()
    if not segment:
        return []
    try:
        argv = shlex.split(segment, posix=True)
    except ValueError:
        argv = segment.split()
    while argv and ("=" in argv[0] and not argv[0].startswith("=") or argv[0] in ("command", "sudo", "time", "nice",
                                                                                  "export", "env", "set")):
        argv = argv[1:]
    return argv


def _head(argv: list[str]) -> str:
    head = Path(argv[0]).name.lower()
    return head[:-4] if head.endswith(".exe") else head


def _operands(argv: list[str]) -> list[str]:
    """Positional arguments, skipping flags and the value of a flag that takes one."""
    out, skip = [], False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            skip = a in ("-e", "-f", "--expression", "--file", "--include", "--exclude", "-o", "--output")
            continue
        out.append(a)
    return out


def _inplace(argv: list[str]) -> bool:
    return any(a == "--in-place" or a.startswith("--in-place=") or
               (a.startswith("-") and not a.startswith("--") and "i" in a[1:]) for a in argv[1:])


def _strip_heredocs(command: str) -> str:
    """Remove heredoc bodies: their lines are script source, not shell statements, and must not be parsed as such."""
    return _HEREDOC.sub("<<HEREDOC", command)


def bash_actions(command: str) -> list[tuple[str, Optional[str]]]:
    """Classify everything a Bash command does, as ``(kind, operand)`` pairs.

    Every statement is inspected (``;``, ``&&``, ``||``, newlines and the inside of ``$(...)``/backticks), so
    chaining does not hide a search. ``cd`` is tracked so operands resolve against the directory in force at that
    point, and each returned path operand is absolute-ish text the caller resolves. Within a pipeline only the first
    command counts as a search or a dump (``git log | grep x`` is a filter and passes), while in-place rewrites and
    ``>`` redirections count anywhere.

    kinds: ``grep``/``find`` (operand = the directory they run in), ``cat``/``write`` (operand = the file),
    ``guard`` (the command sets CODEATLAS_GUARD itself).
    """
    acts: list[tuple[str, Optional[str]]] = []
    if _GUARD_SET.search(command):
        acts.append(("guard", None))
    here = ""  # directory in force, relative to the tool's cwd unless a `cd` made it absolute
    for stmt in _STMT_SPLIT.split(_strip_heredocs(command)):
        # a command substitution runs where the enclosing statement runs, and its `cd` does not outlive it
        for inner in (m.group(1) or m.group(2) or "" for m in _SUBST.finditer(stmt)):
            sub_here = here
            for s in _STMT_SPLIT.split(inner):
                sub_here = _statement(s, sub_here, acts)
        here = _statement(_SUBST.sub("SUBST", stmt), here, acts)
    if _SCRIPT_WRITE.search(command):
        acts.extend(("write", t) for t in _PATH_TOKEN.findall(command))
    return acts


def _statement(stmt: str, here: str, acts: list[tuple[str, Optional[str]]]) -> str:
    """Record what one shell statement does and return the directory in force after it (``cd`` updates it)."""
    stmt = stmt.strip()
    if not stmt:
        return here
    for m in _REDIRECT.finditer(stmt):
        if m.group(1) != "/dev/null":
            acts.append(("write", _join(here, m.group(1))))
    cmds = [a for a in (_argv(seg) for seg in stmt.split("|")) if a]
    if not cmds:
        return here
    first = cmds[0]
    head = _head(first)
    if head == "cd":
        ops = _operands(first)
        return _join(here, ops[0]) if ops else ""
    if head in SEARCH_CMDS or (head == "git" and len(first) > 1 and first[1] == "grep"):
        acts.append(("grep", _search_dir(here, first)))
    elif head in LIST_CMDS:
        acts.append(("find", _search_dir(here, first)))
    elif head in CAT_CMDS:
        acts.extend(("cat", _join(here, a)) for a in _operands(first))
    for argv in cmds:
        h = _head(argv)
        if (h in INPLACE_CMDS and _inplace(argv)) or h == "tee":
            acts.extend(("write", _join(here, a)) for a in _operands(argv))
    return here


def _join(here: str, operand: str) -> str:
    """Resolve *operand* against the ``cd`` in force, keeping it relative to the tool's cwd when both are."""
    operand = os.path.expanduser(operand.strip("'\""))
    if not here or os.path.isabs(operand):
        return operand
    return os.path.join(os.path.expanduser(here), operand)


def _search_dir(here: str, argv: list[str]) -> str:
    """Where a search runs: its last path-looking operand, else the directory in force.

    Only the shape of the operand is used (a separator, ``.``/``..`` or ``~``), never a filesystem probe: the hook's
    own working directory is not the tool's, so ``Path(o).is_dir()`` would answer about the wrong tree.
    """
    ops = [o for o in _operands(argv) if "/" in o or "\\" in o or o in (".", "..") or o.startswith("~")]
    return _join(here, ops[-1]) if ops else (here or ".")


def bash_search(command: str) -> Optional[str]:
    """'grep' or 'find' when any statement of the command starts a text search / file listing, else None."""
    for kind, _ in bash_actions(command):
        if kind in ("grep", "find"):
            return kind
    return None


# ------------------------------------------------------------------ decisions
# CodeAtlas tools that only read the index: approved without a prompt (kept in sync with server.py by a test)
READ_ONLY_TOOLS = frozenset("""
change_context connections dependencies file_outline find_symbols findings grep hotspots impact index_status
list_files module_graph programming_patterns references relationships repo_map repo_overview symbol symbol_source
task_context triggers unreferenced next_snipe get_snipe_context snipe_board snipe_list snipe_item
""".split())


def _option(env_name: str, option: str, default: str = "") -> str:
    """A setting: the shell environment wins, then the plugin's userConfig (CLAUDE_PLUGIN_OPTION_<KEY>), then default."""
    v = os.environ.get(env_name)
    if v is None or v == "":
        v = os.environ.get(f"CLAUDE_PLUGIN_OPTION_{option.upper()}")
    if v is None or v == "" or v.startswith("${"):   # unresolved placeholder: behave as if unset
        return default
    return v


def approve(event: dict, prefix: str = DEFAULT_PREFIX) -> Optional[str]:
    """The reason to auto-approve a CodeAtlas read-only tool call, or None.

    Claude Code asks before the first use of every MCP tool unless a permission rule allows it; a hook that
    returns ``permissionDecision: "allow"`` skips that prompt. Only tools that read the index qualify: edits, SNIPE
    state changes and anything that runs tests keep the user's prompt.
    """
    if _option("CODEATLAS_AUTO_APPROVE", "auto_approve", "on").lower() in ("off", "0", "false", "no"):
        return None
    tool = event.get("tool_name") or ""
    if not tool.startswith(prefix + "__"):
        return None
    name = tool[len(prefix) + 2:]
    if name in READ_ONLY_TOOLS:
        return f"CodeAtlas {name} only reads the index"
    return None


def decide(event: dict, prefix: str = DEFAULT_PREFIX, mode: str = "default") -> Optional[str]:
    """Return a deny reason for this PreToolUse event, or None to let the call through."""
    mode = _option("CODEATLAS_GUARD", "guard_mode", mode or "default").lower()
    if mode == "off":
        return None
    full = mode != "search"
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    if tool not in GATED_TOOLS:
        return None
    cwd = Path(event.get("cwd") or os.getcwd())
    root = find_index(cwd)
    if root is None:
        return None
    if tool == "Grep":
        if not _inside(root, cwd, str(inp.get("path") or "")):
            return None  # searching another tree: CodeAtlas does not index it
        return _deny_grep(root, prefix, str(inp.get("pattern") or ""))
    if tool == "Glob":
        if not _inside(root, cwd, str(inp.get("path") or "")):
            return None
        return (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__list_files(prefix=\"dir/\")` to list "
                f"source files (with language, line and symbol counts) or `{prefix}__find_symbols(query)` to locate a "
                f"symbol, instead of Glob." + _tail(prefix, "grep"))
    if tool == "Bash":
        return _deny_bash(root, cwd, prefix, str(inp.get("command") or ""), full)
    if not full:
        return None
    fp = str(inp.get("file_path") or inp.get("path") or "")
    if tool == "Read":
        limit = inp.get("limit")
        if not (isinstance(limit, (int, float)) and not isinstance(limit, bool) and 0 < limit <= READ_MAX_LINES):
            return _deny_read(root, prefix, fp)
        return None
    if tool in ("Edit", "MultiEdit"):
        return _deny_edit(root, prefix, fp, inp.get("edits") or [inp])
    if tool == "Write":
        return _deny_write(root, prefix, fp)
    return None


def _tail(prefix: str, *tools: str) -> str:
    sel = ",".join(f"{prefix}__{t}" for t in tools)
    return (f" (If the tool schema is not loaded yet, call ToolSearch with \"select:{sel}\" first. This guard is "
            f"CodeAtlas, configured by the user in their own settings: a command cannot change it, and directories "
            f"outside this repository are not affected.)")


REASON_MAX_CHARS = 3600   # a deny reason that carries the answer stays well under the hook output cap


def identifier_files(root: Path, name: str, limit: int = 40) -> list[str]:
    """Files that define or name an identifier (for a Grep answered from the index: its `filenames`)."""
    try:
        con = _connect(root)
    except Exception:
        return []
    try:
        rows = con.execute("SELECT DISTINCT f.path FROM refs r JOIN files f ON f.id=r.file_id WHERE r.name=? "
                           "UNION SELECT DISTINCT f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=? "
                           "ORDER BY 1 LIMIT ?", (name, name, limit)).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def identifier_summary(root: Path, name: str) -> str:
    """What the index already knows about an identifier: definitions, then name matches grouped by file.

    A denied Grep for an identifier usually asked "who uses this": answering inside the deny reason saves the
    retry call. Name matches are unresolved, like grep's own hits; ``references`` resolves them.
    """
    try:
        con = _connect(root)
    except Exception:
        return ""
    try:
        defs = con.execute("SELECT s.kind, s.qualified, f.path, s.start_line, s.end_line FROM symbols s "
                           "JOIN files f ON f.id=s.file_id WHERE s.name=? ORDER BY f.path, s.start_line LIMIT 6", (name,)).fetchall()
        def_lines = {(d["path"], d["start_line"]) for d in defs}
        rows = con.execute("SELECT f.path, r.line, e.qualified AS enc FROM refs r JOIN files f ON f.id=r.file_id "
                           "LEFT JOIN symbols e ON e.id=r.enclosing_id WHERE r.name=? ORDER BY f.path, r.line LIMIT 600",
                           (name,)).fetchall()
    except Exception:
        return ""
    finally:
        con.close()
    by_file: dict[str, list[str]] = {}
    total = 0
    for r in rows:
        if (r["path"], r["line"]) in def_lines:
            continue
        total += 1
        by_file.setdefault(r["path"], []).append(f"{r['line']}" + (f" (in {r['enc'].rsplit('.', 1)[-1]})" if r["enc"] else ""))
    if not defs and not by_file:
        return ""
    parts = []
    if defs:
        parts.append("defined as " + "; ".join(f"{d['kind']} {d['qualified']} at {d['path']}:{d['start_line']}-{d['end_line']}" for d in defs))
    if by_file:
        files = sorted(by_file.items(), key=lambda kv: -len(kv[1]))
        shown = "; ".join(f"{p}: {', '.join(v[:6])}{f' +{len(v) - 6}' if len(v) > 6 else ''}" for p, v in files[:8])
        more = f"; +{len(files) - 8} more files" if len(files) > 8 else ""
        parts.append(f"named in {total} place(s) across {len(by_file)} file(s): {shown}{more}")
    return f" CodeAtlas already knows '{name}': " + ". ".join(parts) + "."


def _deny_grep(root: Path, prefix: str, pattern: str) -> str:
    hint = (f"For an identifier like '{pattern}' prefer `{prefix}__find_symbols(query=\"{pattern}\")` and "
            f"`{prefix}__references(\"{pattern}\")`, which return definitions, callers and path:line anchors. "
            if _IDENT.match(pattern) else "")
    known = identifier_summary(root, pattern) if _IDENT.match(pattern) else ""
    text = (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__grep(pattern=..., glob=..., limit=...)` "
            f"instead of the built-in Grep: it searches the same files and returns path:line hits. {hint}"
            f"Use `{prefix}__task_context(task, scope)` when you are starting a task rather than looking for one string."
            + known + _tail(prefix, "grep"))
    return text[:REASON_MAX_CHARS]


def _deny_bash(root: Path, cwd: Path, prefix: str, command: str, full: bool) -> Optional[str]:
    acts = bash_actions(command)
    if full and any(k == "guard" for k, _ in acts):
        return (f"This command sets CODEATLAS_GUARD. That setting belongs to the user and cannot be changed from "
                f"inside a command, so remove the assignment and run the rest. If you need to search or read this "
                f"repository, use `{prefix}__grep` / `{prefix}__find_symbols`, `{prefix}__file_outline` + "
                f"`{prefix}__symbol_source`, and `{prefix}__replace_symbol` / `{prefix}__insert_code` to change code. "
                f"If the command works on a directory outside this repository, it is not gated at all: run it "
                f"unchanged." + _tail(prefix, "grep", "symbol_source", "replace_symbol"))
    for kind, operand in acts:
        if kind in ("grep", "find") and _inside(root, cwd, operand or "."):
            if kind == "grep":
                return (f"This repository is indexed by CodeAtlas ({root}). Do not shell out to grep/rg in any "
                        f"statement of a command: use `{prefix}__grep(pattern=..., glob=...)` for text, "
                        f"`{prefix}__find_symbols` / `{prefix}__references` for identifiers. A search of a directory "
                        f"outside this repository is allowed." + _tail(prefix, "grep"))
            return (f"This repository is indexed by CodeAtlas ({root}). Use `{prefix}__list_files(prefix=\"dir/\")` "
                    f"instead of find. Listing a directory outside this repository is allowed." + _tail(prefix, "grep"))
        if not full:
            continue
        if kind == "cat":
            r = _deny_read(root, prefix, str(_abs(cwd, operand)), via="cat")
        elif kind == "write":
            r = _deny_bash_write(root, prefix, str(_abs(cwd, operand)))
        else:
            continue
        if r:
            return r
    return None


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
    text = (f"{rel} is indexed by CodeAtlas ({info['lines']} lines, {info['symbols']} symbols). {how}: get its "
            f"structure with `{prefix}__file_outline(\"{rel}\")`, then read only the declarations you need with "
            f"`{prefix}__symbol_source(\"<Name or Class.method>\", path=\"{rel}\")` (or "
            f"`{prefix}__symbol_source(\"{rel}:START-END\")` for a line range). `{prefix}__task_context(task, scope)` "
            f"gathers the relevant symbols across files in one call. Read is allowed as a bounded window: an explicit "
            f"offset with a limit of at most {READ_MAX_LINES} lines." + outline_summary(root, rel, prefix)
            + _tail(prefix, "file_outline", "symbol_source"))
    return text[:REASON_MAX_CHARS]


def outline_summary(root: Path, rel: str, prefix: str, limit: int = 40) -> str:
    """Top-level declarations of a file, one compact paragraph, so a denied Read already answers "what is in it"."""
    try:
        con = _connect(root)
    except Exception:
        return ""
    try:
        rows = con.execute(
            "SELECT s.name, s.kind, s.start_line, s.end_line, (SELECT COUNT(*) FROM symbols c WHERE c.parent_id=s.id) AS members "
            "FROM symbols s JOIN files f ON f.id=s.file_id WHERE f.path=? AND s.parent_id IS NULL ORDER BY s.start_line LIMIT ?",
            (rel, limit + 1)).fetchall()
    except Exception:
        return ""
    finally:
        con.close()
    if not rows:
        return ""
    icon = {"class": "C", "struct": "S", "interface": "I", "trait": "T", "enum": "E", "function": "f", "method": "m",
            "type": "t", "module": "M", "constant": "k", "variable": "v"}
    items = [f"{r['name']} {icon.get(r['kind'], r['kind'][:1])}:{r['start_line']}-{r['end_line']}"
             + (f" ({r['members']} members)" if r["members"] else "") for r in rows[:limit]]
    more = f"; +more: {prefix}__file_outline(\"{rel}\")" if len(rows) > limit else ""
    return f" Top-level declarations of {rel}: " + "; ".join(items) + more + "."


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


def _deny_bash_write(root: Path, prefix: str, fp: str) -> Optional[str]:
    hit = _indexed_source(root, fp)
    if not hit or not Path(fp).is_file():
        return None
    rel, info = hit
    return (f"This command rewrites {rel} from the shell (sed -i, redirection, tee or an inline script); the file is "
            f"indexed by CodeAtlas ({info['lines']} lines, {info['symbols']} symbols). Change it with "
            f"`{prefix}__replace_symbol(name, path=\"{rel}\", new_text=...)` or `{prefix}__insert_code(...)`, which "
            f"are parse-checked and re-indexed at once, or with Edit for lines outside declarations. If the command "
            f"only reads the file, use `{prefix}__symbol_source` instead." + _tail(prefix, "replace_symbol", "insert_code"))


def _abs(cwd: Path, operand: Optional[str]) -> Path:
    p = Path(os.path.expanduser(operand or ""))
    return p if p.is_absolute() else cwd / p


def _inside(root: Path, cwd: Path, operand: str) -> bool:
    """True when *operand* (a path, possibly relative to *cwd*) lies inside the indexed repository."""
    if not operand:
        return True
    return _under(root, str(_abs(cwd, operand)))


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


def session_context(event: dict, prefix: str = DEFAULT_PREFIX) -> Optional[str]:
    """Orientation for a SessionStart hook: what is indexed and which tool to reach for first.

    Silent outside projects and when CODEATLAS_GUARD=off. Claude Code defers MCP tool schemas behind a search and
    forgets how to work after compaction, so the same few lines return on ``startup``, ``resume``, ``clear`` and
    ``compact`` (shorter after compaction). Kept to a few hundred tokens: this is read on every session.
    """
    if _option("CODEATLAS_GUARD", "guard_mode", "default").lower() == "off":
        return None
    cwd = Path(event.get("cwd") or os.getcwd())
    from .roots import protected_reason
    if protected_reason(cwd):
        return None
    source = event.get("source") or "startup"
    root = find_index(cwd)
    stats = ""
    if root is not None:
        try:
            with _connect(root) as c:
                files = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                symbols = c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
                langs = [r[0] for r in c.execute("SELECT lang FROM files GROUP BY lang ORDER BY COUNT(*) DESC LIMIT 4")]
            stats = f" ({files} files, {symbols} symbols; {', '.join(langs)})"
            if root != cwd:
                stats += f" rooted at {root}"
        except Exception:
            stats = ""
    head = f"CodeAtlas has indexed this repository{stats}." if root is not None else (
        "CodeAtlas indexes this project when its MCP server starts (a few seconds for large repositories).")
    if source == "compact":
        return (f"{head} Keep using its tools instead of reading files: task_context(task, scope) to (re)start a task, "
                f"file_outline / symbol_source instead of Read, find_symbols / references instead of Grep, "
                f"connections(target, known) for how code you know reaches code you need. The server still remembers "
                f"what you looked at before compaction. Tools: {prefix}__*.")
    return "\n".join([
        head + f" Use its MCP tools ({prefix}__*) instead of reading files or grepping; every answer carries path:line anchors.",
        "- a task or question in prose: task_context(\"<what you are doing>\", scope=\"dir/\") returns the relevant symbols "
        "with source, callers, tests and config in one call; it remembers what you look at and shows how new results connect to it",
        "- a named symbol: symbol (signature, callers, callees); who calls it / what breaks: references, impact (never Grep "
        "for an identifier); what triggers it: triggers; how A reaches B: connections(target, known)",
        "- locate: find_symbols, file_outline (summarised for big files; never a whole-file Read); read: symbol_source for just "
        "the lines you need",
        "- edit: replace_symbol / insert_code (parse-checked, re-indexed at once) instead of rewriting files",
        "A hook denies built-in Grep/Glob, whole-file Read of indexed files and in-declaration Edit/Write, naming the CodeAtlas "
        "call to make instead; unindexed files, docs and config are never gated.",
    ])


def answer(event: dict, prefix: str = DEFAULT_PREFIX) -> Optional[dict]:
    """The index's answer to a built-in Read or Grep that the guard would deny: ``{"result": text}`` (plus
    ``"files"`` for a Grep) with what the model should get instead of the tool's own output; None when the call
    should simply go ahead.

    Used by the plugin's mod (``hooks/register.js``), which answers the call in place: no denial, no retry. A whole-file
    Read of an indexed source file becomes that file's top-level declarations with line ranges; a Grep for an
    identifier becomes its definitions and its name matches grouped by file. Anything else (bounded reads, regex
    searches, files the index does not know) returns None.
    """
    if _option("CODEATLAS_GUARD", "guard_mode", "default").lower() == "off":
        return None
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or os.getcwd())
    root = find_index(cwd)
    if root is None:
        return None
    if tool == "Read":
        fp = inp.get("file_path") or inp.get("path") or ""
        if inp.get("limit") and int(inp.get("limit") or 0) <= READ_MAX_LINES:
            return None   # a bounded window is allowed; the tool should run
        hit = _indexed_source(root, fp)
        if not hit:
            return None
        rel, info = hit
        outline = outline_summary(root, rel, prefix).strip()
        if not outline:
            return None
        return {"result": (
            f"{rel}: {info['lines']} lines, {info['symbols']} symbols; the index answered this Read with the file's "
            f"outline. {outline} Read one declaration with `{prefix}__symbol_source(\"<Name>\", path=\"{rel}\")`, "
            f"or a line range with `{prefix}__symbol_source(\"{rel}:START-END\")`; `{prefix}__file_outline(\"{rel}\")` "
            f"adds reference counts, imports and importers.")}
    if tool == "Grep":
        pattern = inp.get("pattern") or ""
        where = inp.get("path") or str(cwd)
        if not _IDENT.match(pattern) or not _inside(root, cwd, where):
            return None
        known = identifier_summary(root, pattern).strip()
        if not known:
            return None
        return {"result": (f"{known} (name matches from the index, grouped by file; `{prefix}__references(\"{pattern}\")` "
                           f"resolves them to one definition, `{prefix}__grep(pattern=...)` searches text)."),
                "files": identifier_files(root, pattern)}
    return None


def run(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``codeatlas hook pretool`` / ``session-start`` / ``answer``: read the event on stdin, print the
    hook output, always exit 0 (a failing hook must never block the agent)."""
    import argparse
    ap = argparse.ArgumentParser(prog="codeatlas hook")
    ap.add_argument("event", nargs="?", default="pretool", choices=["pretool", "session-start", "answer"])
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="MCP tool prefix to recommend")
    ap.add_argument("--mode", default=None, choices=["default", "search", "strict", "off"])
    ns = ap.parse_args(argv)
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0  # malformed or empty stdin: never block the tool
    if ns.event == "session-start":
        try:
            text = session_context(event, ns.prefix)
        except Exception:
            return 0
        if text:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                "additionalContext": text}}))
        return 0
    if ns.event == "answer":
        try:
            out = answer(event, ns.prefix)
        except Exception:
            out = None
        sys.stdout.write(json.dumps(out or {}))
        return 0
    try:
        ok = approve(event, ns.prefix)
        reason = None if ok else decide(event, ns.prefix, ns.mode or "default")
    except Exception:
        return 0
    if ok:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow", "permissionDecisionReason": ok}}))
    elif reason:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
    return 0
