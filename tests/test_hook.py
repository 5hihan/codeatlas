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
    outside = tmp_path / "Reference" / "upstream"  # a reference checkout CodeAtlas does not index
    (outside / "Source").mkdir(parents=True)
    (outside / "Source" / "Engine.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
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
    # a text regex and a Glob run unchanged: the index has nothing better than ripgrep for them (a denial there
    # was measured as a pure retry tax)
    assert hook.decide(ev("Grep", indexed, pattern="TODO|FIXME"), P) is None
    assert hook.decide(ev("Glob", indexed, pattern="**/*.py"), P) is None


def test_not_indexed_or_off_passes(tmp_path: Path, indexed: Path, monkeypatch):
    plain = tmp_path / "elsewhere"
    plain.mkdir()
    assert hook.decide(ev("Grep", plain, pattern="x"), P) is None
    monkeypatch.setenv("CODEATLAS_GUARD", "off")
    assert hook.decide(ev("Grep", indexed, pattern="x"), P) is None
    monkeypatch.delenv("CODEATLAS_GUARD")
    assert hook.decide(ev("Grep", indexed, pattern="x"), P, mode="off") is None
    assert hook.decide(ev("NotebookEdit", indexed, notebook_path="a.ipynb"), P) is None


# ------------------------------------------------------------------ the two documented bypasses
def test_a_command_may_not_switch_the_guard_off(indexed: Path):
    """The model wrote `export CODEATLAS_GUARD=off; <work>` because an early deny message advertised the switch."""
    for cmd in ("export CODEATLAS_GUARD=off; python build.py",
                "CODEATLAS_GUARD=off rg -n foo src",
                'set CODEATLAS_GUARD=off',
                'os.environ["CODEATLAS_GUARD"] = "off"'):
        r = hook.decide(ev("Bash", indexed, command=cmd), P)
        assert r and "belongs to the user" in r, cmd
    # and no deny reason anywhere tells the model how to disable the guard
    reasons = [hook.decide(ev("Grep", indexed, pattern="x"), P),
               hook.decide(ev("Read", indexed, file_path=str(indexed / "src" / "app.py")), P),
               hook.decide(ev("Bash", indexed, command="rg -n foo src"), P),
               hook.decide(ev("Write", indexed, file_path=str(indexed / "src" / "app.py"), content=""), P)]
    assert all(r and "GUARD=off" not in r and "GUARD=search" not in r for r in reasons)


def test_every_statement_is_inspected_not_just_the_first(indexed: Path):
    """`export X=1; cd src; A=$(find . -name y); sed -n 1,50p $A | grep -v '^//'` hid the search in v0.1.1."""
    for cmd in ("export FOO=1; cd src && rg -n foo .",
                "cd src; ls; grep -rn foo .",
                'A=$(grep -rn foo src); echo $A',
                "python build.py\ngrep -rn foo src"):
        r = hook.decide(ev("Bash", indexed, command=cmd), P)
        assert r and f'{P}__references("foo")' in r, cmd
    # listing files and text regexes run unchanged wherever they sit in the command
    for cmd in ('A=$(find . -name "*.py"); echo $A', "ls -la && find . -name '*.py'", "cd src; grep -rn 'def .*(' ."):
        assert hook.decide(ev("Bash", indexed, command=cmd), P) is None, cmd


# ------------------------------------------------------------------ outside the indexed repository
def test_work_outside_the_repository_is_never_gated(tmp_path: Path, indexed: Path):
    """CodeAtlas indexes one repo; denying a search of a reference checkout would leave no legal tool."""
    ref = tmp_path / "Reference" / "upstream"
    assert hook.decide(ev("Bash", indexed, command=f"cd {ref.as_posix()} && grep -rn Engine Source"), P) is None
    assert hook.decide(ev("Bash", indexed, command=f'cd {ref.as_posix()}; A=$(find Source -name "*.cpp"); '
                                                   f"sed -n 1,40p $A"), P) is None
    assert hook.decide(ev("Bash", indexed, command=f"cat {(ref / 'Source' / 'Engine.cpp').as_posix()}"), P) is None
    assert hook.decide(ev("Grep", indexed, pattern="Engine", path=str(ref)), P) is None
    assert hook.decide(ev("Read", indexed, file_path=str(ref / "Source" / "Engine.cpp")), P) is None
    assert hook.decide(ev("Write", indexed, file_path=str(ref / "Source" / "new.cpp"), content="x"), P) is None
    # a `cd` into the reference tree does not excuse a search back inside the repo
    assert hook.decide(ev("Bash", indexed, command=f"cd {ref.as_posix()}; grep -rn foo {indexed.as_posix()}/src"), P)


@pytest.mark.parametrize("cmd,kind", [
    ("grep -rn foo src", "grep"),
    ("rg -n 'def x' --type py", "grep"),
    ("cd src && rg foo | head -5", "grep"),
    ("FOO=1 grep x y", "grep"),
    ("git grep -n foo", "grep"),
    ("find . -name '*.py'", "find"),
    ("export X=1; ls; rg foo", "grep"),
    ("A=$(find . -name x)", "find"),
    ("git log --oneline | grep fix", None),      # filter in a pipeline is fine
    ("pytest -q", None),
    ("ls -la src", None),
    ("cat src/app.py | grep x", None),
    ("", None),
])
def test_bash_search_detection(cmd, kind):
    assert hook.bash_search(cmd) == kind


def test_heredoc_bodies_are_not_parsed_as_shell(indexed: Path):
    """A script's own lines must not be read as statements, or `grep = 1` in Python would look like a search."""
    cmd = "python - <<'EOF'\ngrep = 1\nfind = 2\nprint(grep, find)\nEOF"
    assert hook.bash_search(cmd) is None
    assert hook.decide(ev("Bash", indexed, command=cmd), P) is None


def test_bash_grep_is_redirected_but_other_commands_pass(indexed: Path):
    r = hook.decide(ev("Bash", indexed, command="rg -n foo src"), P)
    assert r and f"{P}__references(\"foo\")" in r
    assert hook.decide(ev("Bash", indexed, command="grep -rn 'def .*' src"), P) is None, "a text regex runs"
    assert hook.decide(ev("Bash", indexed, command="find src -name '*.py'"), P) is None, "listing files runs"
    assert hook.decide(ev("Bash", indexed, command="pytest -q"), P) is None
    assert hook.decide(ev("Bash", indexed, command="git diff | grep '^+'"), P) is None
    assert hook.bash_grep_identifier("grep -rn -e handle src") == "handle"
    assert hook.bash_grep_identifier("rg --type py handle_request .") == "handle_request"
    assert hook.bash_grep_identifier("grep -r 'a.*b' .") is None


# ------------------------------------------------------------------ reading
def test_read_must_be_a_bounded_window(indexed: Path):
    src = str(indexed / "src" / "app.py")
    r = hook.decide(ev("Read", indexed, file_path=src), P)
    assert r and f'{P}__file_outline("src/app.py")' in r and "symbol_source" in r and "10 lines, 3 symbols" in r
    assert hook.decide(ev("Read", indexed, file_path=src, offset=10, limit=40), P) is None
    assert hook.decide(ev("Read", indexed, file_path=src, offset=1), P)          # no limit: unbounded
    assert hook.decide(ev("Read", indexed, file_path=src, limit=5000), P)        # limit beyond the cap
    # files the index does not know, non-source files and files outside the repo still pass
    assert hook.decide(ev("Read", indexed, file_path=str(indexed / "src" / "loose.py")), P) is None
    assert hook.decide(ev("Read", indexed, file_path=str(indexed / "README.md")), P) is None
    # `search` keeps only the search redirects; `strict` is the old name for the default
    assert hook.decide(ev("Read", indexed, file_path=src), P, mode="search") is None
    assert hook.decide(ev("Grep", indexed, pattern="x"), P, mode="search")
    assert hook.decide(ev("Read", indexed, file_path=src), P, mode="strict")


def test_bash_cat_of_indexed_file_is_redirected(indexed: Path):
    r = hook.decide(ev("Bash", indexed, command="cat src/app.py"), P)
    assert r and "cat" in r and f'{P}__file_outline("src/app.py")' in r
    assert hook.decide(ev("Bash", indexed / "src", command="cat -n app.py | head"), P)     # relative to cwd
    assert hook.decide(ev("Bash", indexed, command="cd src; cat app.py"), P)               # relative to a cd
    assert hook.decide(ev("Bash", indexed, command="cat README.md"), P) is None
    assert hook.decide(ev("Bash", indexed, command="cat src/loose.py"), P) is None
    assert hook.decide(ev("Bash", indexed, command="head -20 src/app.py"), P) is None
    assert hook.decide(ev("Bash", indexed, command="sed -n '3,4p' src/app.py"), P) is None
    assert hook.decide(ev("Bash", indexed, command="cat src/app.py"), P, mode="search") is None


# ------------------------------------------------------------------ writing
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


def test_shell_rewrites_of_an_indexed_file_are_redirected(indexed: Path):
    """sed -i, redirection, tee and inline scripts are Write by another name."""
    for cmd in ("sed -i 's/return 1/return 2/' src/app.py",
                "sed -i.bak -e 's/a/b/' src/app.py",
                "cd src && sed -i 's/a/b/' app.py",
                "echo 'x = 1' > src/app.py",
                "cat tpl.py >> src/app.py",
                "cat tpl.py | tee src/app.py",
                "python - <<'EOF'\nimport pathlib\npathlib.Path('src/app.py').write_text('x')\nEOF",
                "python -c \"open('src/app.py', 'w').write('x')\""):
        r = hook.decide(ev("Bash", indexed, command=cmd), P)
        assert r and "replace_symbol" in r, cmd
    # reading, unindexed targets, scratch files and /dev/null pass
    assert hook.decide(ev("Bash", indexed, command="sed -n '1,5p' src/app.py"), P) is None
    assert hook.decide(ev("Bash", indexed, command="sed -i 's/a/b/' src/loose.py"), P) is None
    assert hook.decide(ev("Bash", indexed, command="echo hi > out.txt"), P) is None
    assert hook.decide(ev("Bash", indexed, command="pytest -q > /dev/null 2>&1"), P) is None
    assert hook.decide(ev("Bash", indexed, command="sed -i 's/a/b/' src/app.py"), P, mode="search") is None


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
    entry, approve = hooks["hooks"]["PreToolUse"]
    assert all(t in entry["matcher"] for t in ("Grep", "Glob", "Bash", "Read", "Edit", "MultiEdit", "Write"))
    assert entry["hooks"][0]["command"] == "codeatlas"
    assert entry["hooks"][0]["args"] == ["hook", "pretool", "--prefix", P]
    assert approve["matcher"] == f"{P}__.*" and approve["hooks"][0]["args"][:2] == ["hook", "pretool"]
