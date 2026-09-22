"""The Claude Code PreToolUse guard (`codeatlas hook pretool`)."""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from codeatlas import hook

P = "mcp__plugin_codeatlas_atlas"

APP = """import os

def f():
    return 1

class C:
    def m(self):
        return 2

X = 3
"""
# (name, kind, qualified, start, end) as the indexer would store them for APP
SYMBOLS = [("f", "function", "f", 3, 4), ("C", "class", "C", 6, 8), ("m", "method", "C.m", 7, 8)]


@pytest.fixture
def indexed(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    (repo / ".codeatlas").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(APP, encoding="utf-8")
    (repo / "src" / "loose.py").write_text("y = 2\n", encoding="utf-8")  # on disk but not in the index
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")
    con = sqlite3.connect(repo / ".codeatlas" / "index.db")
    con.executescript("""
        CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, lang TEXT NOT NULL, lines INTEGER);
        CREATE TABLE symbols(id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, name TEXT, kind TEXT, qualified TEXT,
                             start_line INTEGER, end_line INTEGER);
        INSERT INTO files(id, path, lang, lines) VALUES (1, 'src/app.py', 'python', 10);
    """)
    con.executemany("INSERT INTO symbols(file_id,name,kind,qualified,start_line,end_line) VALUES (1,?,?,?,?,?)", SYMBOLS)
    con.commit()
    con.close()
    monkeypatch.delenv("CODEATLAS_GUARD", raising=False)
    # the developer's machine may have an index above tmp_path (e.g. $HOME): never climb past the sandbox
    real = hook.find_index
    monkeypatch.setattr(hook, "find_index", lambda start, stop=None: real(start, stop=tmp_path.resolve()))
    return repo


def ev(tool: str, cwd: Path, **inp) -> dict:
    return {"tool_name": tool, "tool_input": inp, "cwd": str(cwd)}


def test_grep_and_glob_are_redirected_when_indexed(indexed: Path):
    r = hook.decide(ev("Grep", indexed / "src", pattern="find_symbols"), P)
    assert r and f"{P}__grep(" in r and f"{P}__find_symbols" in r and "ToolSearch" in r
    r = hook.decide(ev("Grep", indexed, pattern="TODO|FIXME"), P)
    assert r and f"{P}__grep(" in r and "find_symbols(query" not in r  # not an identifier: no symbol hint
    r = hook.decide(ev("Glob", indexed, pattern="**/*.py"), P)
    assert r and f"{P}__list_files" in r


def test_not_indexed_or_off_passes(tmp_path: Path, indexed: Path, monkeypatch):
    plain = tmp_path / "elsewhere"
    plain.mkdir()
    assert hook.decide(ev("Grep", plain, pattern="x"), P) is None
    monkeypatch.setenv("CODEATLAS_GUARD", "off")
    assert hook.decide(ev("Grep", indexed, pattern="x"), P) is None
    monkeypatch.delenv("CODEATLAS_GUARD")
    assert hook.decide(ev("Grep", indexed, pattern="x"), P, mode="off") is None
    assert hook.decide(ev("NotebookEdit", indexed, notebook_path="a.ipynb"), P) is None


@pytest.mark.parametrize("cmd,kind", [
    ("grep -rn foo src", "grep"),
    ("rg -n 'def x' --type py", "grep"),
    ("cd src && rg foo | head -5", "grep"),
    ("FOO=1 grep x y", "grep"),
    ("git grep -n foo", "grep"),
    ("find . -name '*.py'", "find"),
    ("git log --oneline | grep fix", None),      # filter in a pipeline is fine
    ("pytest -q", None),
    ("ls -la src", None),
    ("cat src/app.py | grep x", None),
    ("", None),
])
def test_bash_search_detection(cmd, kind):
    assert hook.bash_search(cmd) == kind


def test_bash_grep_is_redirected_but_other_commands_pass(indexed: Path):
    r = hook.decide(ev("Bash", indexed, command="rg -n foo src"), P)
    assert r and f"{P}__grep(" in r
    assert hook.decide(ev("Bash", indexed, command="pytest -q"), P) is None
    assert hook.decide(ev("Bash", indexed, command="git diff | grep '^+'"), P) is None


def test_whole_file_read_is_redirected_by_default(indexed: Path):
    src = str(indexed / "src" / "app.py")
    r = hook.decide(ev("Read", indexed, file_path=src), P)
    assert r and f'{P}__file_outline("src/app.py")' in r and "symbol_source" in r and "10 lines, 3 symbols" in r
    # partial reads, files the index does not know, non-source files and files outside the repo still pass
    assert hook.decide(ev("Read", indexed, file_path=src, offset=10, limit=40), P) is None
    assert hook.decide(ev("Read", indexed, file_path=str(indexed / "src" / "loose.py")), P) is None
    assert hook.decide(ev("Read", indexed, file_path=str(indexed / "README.md")), P) is None
    assert hook.decide(ev("Read", indexed, file_path=str(indexed.parent / "other.py")), P) is None
    # `search` keeps only the search redirects; `strict` is the old name for the default
    assert hook.decide(ev("Read", indexed, file_path=src), P, mode="search") is None
    assert hook.decide(ev("Grep", indexed, pattern="x"), P, mode="search")
    assert hook.decide(ev("Read", indexed, file_path=src), P, mode="strict")


def test_bash_cat_of_indexed_file_is_redirected(indexed: Path):
    r = hook.decide(ev("Bash", indexed, command="cat src/app.py"), P)
    assert r and "cat" in r and f'{P}__file_outline("src/app.py")' in r
    assert hook.decide(ev("Bash", indexed / "src", command="cat -n app.py | head"), P)  # relative to cwd
    assert hook.decide(ev("Bash", indexed, command="cat README.md"), P) is None
    assert hook.decide(ev("Bash", indexed, command="cat src/loose.py"), P) is None
    assert hook.decide(ev("Bash", indexed, command="head -20 src/app.py"), P) is None
    assert hook.decide(ev("Bash", indexed, command="sed -n '3,4p' src/app.py"), P) is None
    assert hook.decide(ev("Bash", indexed, command="cat src/app.py"), P, mode="search") is None


def test_edit_inside_a_declaration_is_redirected_to_replace_symbol(indexed: Path, monkeypatch):
    src = str(indexed / "src" / "app.py")
    r = hook.decide(ev("Edit", indexed, file_path=src, old_string="    return 1", new_string="    return 10"), P)
    assert r and f'{P}__replace_symbol(name="f", path="src/app.py"' in r and "lines 4-4" in r and "insert_code" in r
    # innermost declaration wins
    r = hook.decide(ev("Edit", indexed, file_path=src, old_string="return 2", new_string="return 3"), P)
    assert r and 'replace_symbol(name="C.m"' in r
    # MultiEdit is inspected edit by edit
    r = hook.decide(ev("MultiEdit", indexed, file_path=src,
                       edits=[{"old_string": "import os", "new_string": "import os, sys"},
                              {"old_string": "def f():", "new_string": "def g():"}]), P)
    assert r and 'replace_symbol(name="f"' in r
    # outside any declaration, unknown text, unindexed or non-source files pass
    assert hook.decide(ev("Edit", indexed, file_path=src, old_string="import os", new_string="import sys"), P) is None
    assert hook.decide(ev("Edit", indexed, file_path=src, old_string="X = 3", new_string="X = 4"), P) is None
    assert hook.decide(ev("Edit", indexed, file_path=src, old_string="nope", new_string="x"), P) is None
    assert hook.decide(ev("Edit", indexed, file_path=str(indexed / "src" / "loose.py"), old_string="y = 2",
                          new_string="y = 3"), P) is None
    assert hook.decide(ev("Edit", indexed, file_path=str(indexed / "README.md"), old_string="hi", new_string="yo"), P) is None
    # long declarations may still be edited in place
    monkeypatch.setattr(hook, "EDIT_SYMBOL_MAX_LINES", 1)
    assert hook.decide(ev("Edit", indexed, file_path=src, old_string="    return 1", new_string="    return 10"), P) is None
    monkeypatch.setattr(hook, "EDIT_SYMBOL_MAX_LINES", 150)
    assert hook.decide(ev("Edit", indexed, file_path=src, old_string="    return 1", new_string="x"), P, mode="search") is None


def test_write_over_indexed_file_is_redirected_but_new_files_pass(indexed: Path):
    r = hook.decide(ev("Write", indexed, file_path=str(indexed / "src" / "app.py"), content="x = 1\n"), P)
    assert r and "replace_symbol" in r and "Write is for new files" in r
    assert hook.decide(ev("Write", indexed, file_path=str(indexed / "src" / "new.py"), content="x = 1\n"), P) is None
    assert hook.decide(ev("Write", indexed, file_path=str(indexed / "src" / "loose.py"), content="y = 3\n"), P) is None
    assert hook.decide(ev("Write", indexed, file_path=str(indexed / "README.md"), content="# yo\n"), P) is None
    assert hook.decide(ev("Write", indexed, file_path=str(indexed / "src" / "app.py"), content=""), P, mode="search") is None


def test_broken_index_never_blocks(indexed: Path):
    (indexed / ".codeatlas" / "index.db").write_bytes(b"not a database")
    src = str(indexed / "src" / "app.py")
    assert hook.decide(ev("Read", indexed, file_path=src), P) is None
    assert hook.decide(ev("Edit", indexed, file_path=src, old_string="return 1", new_string="x"), P) is None
    assert hook.decide(ev("Grep", indexed, pattern="x"), P)  # search redirects do not need the index contents


def test_run_prints_deny_json_and_never_fails(indexed: Path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev("Grep", indexed, pattern="x"))))
    assert hook.run(["pretool", "--prefix", P]) == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert f"{P}__grep(" in out["permissionDecisionReason"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ev("Bash", indexed, command="pytest"))))
    assert hook.run(["pretool"]) == 0 and capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert hook.run([]) == 0 and capsys.readouterr().out == ""


def test_plugin_ships_the_hook(tmp_path: Path):
    from codeatlas import agents
    agents.write_plugin(tmp_path)
    hooks = json.loads((tmp_path / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = hooks["hooks"]["PreToolUse"]
    assert all(t in entry["matcher"] for t in ("Grep", "Glob", "Bash", "Read", "Edit", "MultiEdit", "Write"))
    assert entry["hooks"][0]["command"] == f"codeatlas hook pretool --prefix {P}"
