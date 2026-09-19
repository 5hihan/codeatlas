"""The Claude Code PreToolUse guard (`codeatlas hook pretool`)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from codeatlas import hook

P = "mcp__plugin_codeatlas_atlas"


@pytest.fixture
def indexed(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    (repo / ".codeatlas").mkdir(parents=True)
    (repo / ".codeatlas" / "index.db").write_bytes(b"")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
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
    assert hook.decide(ev("Edit", indexed, file_path="a.py"), P) is None


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


def test_read_gated_only_in_strict_mode(indexed: Path):
    src = str(indexed / "src" / "app.py")
    assert hook.decide(ev("Read", indexed, file_path=src), P) is None
    r = hook.decide(ev("Read", indexed, file_path=src), P, mode="strict")
    assert r and 'file_outline("src/app.py")' in r and "symbol_source" in r
    # partial reads, non-source files and files outside the repo still pass
    assert hook.decide(ev("Read", indexed, file_path=src, offset=10, limit=40), P, mode="strict") is None
    assert hook.decide(ev("Read", indexed, file_path=str(indexed / "README.md")), P, mode="strict") is None
    assert hook.decide(ev("Read", indexed, file_path=str(indexed.parent / "other.py")), P, mode="strict") is None


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
    assert all(t in entry["matcher"] for t in ("Grep", "Glob", "Bash"))
    assert entry["hooks"][0]["command"] == f"codeatlas hook pretool --prefix {P}"
