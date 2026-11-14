"""Plugin-platform features: SessionStart orientation, compact outlines, tool annotations, manifest and evals."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from codeatlas import __version__, fmt, hook
from codeatlas.agents import plugin_hooks, write_plugin
from codeatlas.graph import Atlas
from codeatlas.indexer import Index


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "svc.py").write_text("def handle(x):\n    return x\n\nclass Store:\n    def save(self, x):\n        return x\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    ix.close()
    return tmp_path


def _run_hook(argv: list[str], event: dict, capsys, monkeypatch) -> dict | None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert hook.run(argv) == 0
    out = capsys.readouterr().out
    return json.loads(out) if out else None


def test_session_start_orients_when_indexed(repo: Path, capsys, monkeypatch):
    monkeypatch.delenv("CODEATLAS_GUARD", raising=False)
    out = _run_hook(["session-start", "--prefix", "mcp__plugin_codeatlas_atlas"],
                    {"cwd": str(repo), "source": "startup", "hook_event_name": "SessionStart"}, capsys, monkeypatch)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "1 files, 3 symbols" in ctx and "task_context" in ctx and "mcp__plugin_codeatlas_atlas__*" in ctx
    assert "connections(target, known)" in ctx and "replace_symbol" in ctx
    assert len(ctx) < 1800, "orientation must stay a few hundred tokens: it is read on every session"
    # a sub-directory of the repo finds the same index
    sub = _run_hook(["session-start"], {"cwd": str(repo / "app"), "source": "resume"}, capsys, monkeypatch)
    assert "rooted at" in sub["hookSpecificOutput"]["additionalContext"]


def test_session_start_after_compaction_is_a_short_reminder(repo: Path, capsys, monkeypatch):
    monkeypatch.delenv("CODEATLAS_GUARD", raising=False)
    full = _run_hook(["session-start"], {"cwd": str(repo), "source": "startup"}, capsys, monkeypatch)
    compact = _run_hook(["session-start"], {"cwd": str(repo), "source": "compact"}, capsys, monkeypatch)
    f, c = full["hookSpecificOutput"]["additionalContext"], compact["hookSpecificOutput"]["additionalContext"]
    assert len(c) < len(f) and "remembers what you looked at before compaction" in c and "task_context" in c


def test_session_start_is_silent_outside_projects_and_when_off(tmp_path_factory, repo: Path, capsys, monkeypatch):
    monkeypatch.setenv("CODEATLAS_GUARD", "off")
    assert _run_hook(["session-start"], {"cwd": str(repo), "source": "startup"}, capsys, monkeypatch) is None
    monkeypatch.delenv("CODEATLAS_GUARD", raising=False)
    assert _run_hook(["session-start"], {"cwd": str(Path.home()), "source": "startup"}, capsys, monkeypatch) is None
    # an unindexed project (not inside the indexed one) still gets a one-line orientation: the server indexes on start
    other = tmp_path_factory.mktemp("fresh")
    (other / "x.py").write_text("x = 1\n", encoding="utf-8")
    out = _run_hook(["session-start"], {"cwd": str(other), "source": "startup"}, capsys, monkeypatch)
    assert "indexes this project when its MCP server starts" in out["hookSpecificOutput"]["additionalContext"]
    # malformed stdin never fails the hook
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert hook.run(["session-start"]) == 0 and capsys.readouterr().out == ""


def test_plugin_hooks_register_session_start_and_guard():
    h = plugin_hooks("mcp__plugin_codeatlas_atlas")["hooks"]
    ss = h["SessionStart"][0]
    assert ss["matcher"] == "startup|resume|clear|compact"
    assert ss["hooks"][0]["command"] == "codeatlas"
    assert ss["hooks"][0]["args"] == ["hook", "session-start", "--prefix", "mcp__plugin_codeatlas_atlas"]
    assert ss["hooks"][0]["timeout"] <= 10
    guard, approve_ = h["PreToolUse"]
    assert guard["matcher"] == "Grep|Glob|Bash|Read|Edit|MultiEdit|Write"
    assert guard["hooks"][0]["args"][:2] == ["hook", "pretool"] and "continueOnBlock" not in guard["hooks"][0]
    assert approve_["matcher"] == "mcp__plugin_codeatlas_atlas__.*"
    for entry in h["PreToolUse"] + h["SessionStart"]:
        for hk in entry["hooks"]:
            assert hk["command"] == "codeatlas" and isinstance(hk["args"], list), "exec form: no shell quoting"


def test_auto_approve_read_only_tools_only(capsys, monkeypatch):
    monkeypatch.delenv("CODEATLAS_AUTO_APPROVE", raising=False)
    prefix = "mcp__plugin_codeatlas_atlas"
    out = _run_hook(["pretool", "--prefix", prefix], {"tool_name": f"{prefix}__task_context", "tool_input": {"task": "x"},
                                                      "hook_event_name": "PreToolUse", "cwd": "/"}, capsys, monkeypatch)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    for name in ("replace_symbol", "insert_code", "rename_symbol", "claim_snipe", "verify_snipe", "snipe_add", "index_repo"):
        assert _run_hook(["pretool", "--prefix", prefix], {"tool_name": f"{prefix}__{name}", "tool_input": {},
                                                           "hook_event_name": "PreToolUse", "cwd": "/"}, capsys, monkeypatch) is None, name
    # another server's tool, or the project-layout prefix, is not ours to approve
    assert _run_hook(["pretool", "--prefix", prefix], {"tool_name": "mcp__other__task_context", "tool_input": {},
                                                       "hook_event_name": "PreToolUse", "cwd": "/"}, capsys, monkeypatch) is None
    monkeypatch.setenv("CODEATLAS_AUTO_APPROVE", "off")
    assert _run_hook(["pretool", "--prefix", prefix], {"tool_name": f"{prefix}__task_context", "tool_input": {},
                                                       "hook_event_name": "PreToolUse", "cwd": "/"}, capsys, monkeypatch) is None


def test_read_only_tool_list_matches_server_annotations(repo: Path):
    from codeatlas.server import App, build_server
    app = App(repo, watch=False, dashboard=False)
    with app._lock:
        app.ix.update()
    mcp = build_server(app)

    async def go():
        tools = await mcp.list_tools()
        return {t.name for t in tools if t.annotations and t.annotations.read_only_hint}
    assert asyncio.run(go()) == set(hook.READ_ONLY_TOOLS)
    app.close()


def test_hook_gates_exactly_the_indexed_languages():
    from codeatlas.languages import EXT_TO_LANG
    assert set(hook.SOURCE_EXT) == set(EXT_TO_LANG)


def test_compact_outline_folds_big_files_only(tmp_path: Path):
    lines = ["class Big:"] + [f"    def m{i}(self):\n        return {i}" for i in range(30)]
    lines += [f"def f{i}():\n    return {i}" for i in range(50)]
    lines += ["def caller():\n    return " + " + ".join(f"f{i}()" for i in range(5)) + " + Big().m1() + Big().m2()"]
    (tmp_path / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "small.py").write_text("def a():\n    return 1\n\ndef b():\n    return a()\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    atlas = Atlas(ix)
    big = atlas.outline("big.py")
    full, compact = fmt.outline(big), fmt.outline(big, compact=True)
    assert len(compact) < len(full) * 0.6
    assert "summarised" in compact.splitlines()[0] and "summarised" not in full
    assert "more (less referenced)" in compact and 'file_outline("big.py", full=True)' in compact
    assert "f0" in compact and "f1" in compact, "referenced symbols survive the fold"
    assert "m1" in compact and "m2" in compact and 'symbol("Big")' in compact
    assert "m29" not in compact, "unreferenced members are folded"
    assert "f49" in full and "more (less referenced)" not in full
    small = atlas.outline("small.py")
    assert fmt.outline(small, compact=True) == fmt.outline(small), "small files are never folded"
    ix.close()


def test_tool_annotations_and_always_load(repo: Path):
    from codeatlas.server import App, build_server, ALWAYS_LOAD
    app = App(repo, watch=False, dashboard=False)
    with app._lock:
        app.ix.update()
    mcp = build_server(app)

    async def go():
        tools = {t.name: t for t in await mcp.list_tools()}
        always = {n for n, t in tools.items() if (t.meta or {}).get("anthropic/alwaysLoad")}
        assert always == set(ALWAYS_LOAD) and {"task_context", "file_outline", "symbol_source", "find_symbols"} <= always
        assert 3 <= len(always) <= 5, "the documented 3-5 upfront tools: the rest stay deferred"
        upfront = sum(len(json.dumps({"n": t.name, "d": t.description, "s": t.input_schema})) for n, t in tools.items() if n in always)
        assert upfront / 3.6 < 1200, f"always-on schema cost must stay about a thousand tokens, got ~{upfront / 3.6:.0f}"
        for name in ("task_context", "connections", "symbol", "references", "impact", "grep", "next_snipe", "snipe_board"):
            a = tools[name].annotations
            assert a.read_only_hint and a.idempotent_hint and not a.destructive_hint and a.open_world_hint is False, name
        for name in ("replace_symbol", "insert_code", "rename_symbol", "index_repo", "claim_snipe", "verify_snipe", "snipe_add"):
            assert not tools[name].annotations.read_only_hint, name
        r = await mcp.call_tool("file_outline", {"path": "app/svc.py", "full": True})
        text = r[0].text if isinstance(r, (list, tuple)) else r.content[0].text
        assert "Store" in text
    asyncio.run(go())
    app.close()


def test_plugin_manifest_marketplace_and_evals(tmp_path: Path):
    files = write_plugin(tmp_path / "plugin")
    manifest = json.loads((tmp_path / "plugin" / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["version"] == __version__ and manifest["repository"].startswith("https://") and manifest["keywords"]
    root_market = json.loads((Path(__file__).resolve().parents[1] / ".claude-plugin" / "marketplace.json").read_text())
    assert root_market["plugins"][0]["version"] == __version__, "marketplace.json must follow the package version"
    hooks = json.loads((tmp_path / "plugin" / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "PreToolUse"}
    cases = {p.parent.parent.name if p.parent.name == "graders" else p.parent.name
             for p in (tmp_path / "plugin" / "evals").rglob("*.md")}
    assert {"where-is-it", "no-code-question"} <= cases
    for case in ("where-is-it", "no-code-question"):
        d = tmp_path / "plugin" / "evals" / case
        assert (d / "prompt.md").exists() and list((d / "graders").glob("*.md")), "a case needs a prompt and a grader"
        for g in (d / "graders").glob("*.md"):
            fm = g.read_text(encoding="utf-8").split("---")[1]
            assert "type: regex" in fm and "pattern:" in fm and "target:" in fm
    scaffold = (tmp_path / "plugin" / "evals" / "where-is-it" / "case.yaml").read_text(encoding="utf-8")
    assert "scaffold_script" in scaffold and "apply_discount" in scaffold
    assert (tmp_path / "plugin" / "evals" / ".gitignore").read_text() == "results/\n"
    # the committed plugin directory is exactly what the generator produces
    repo_plugin = Path(__file__).resolve().parents[1] / "codeatlas-plugin"
    for f in files:
        rel = Path(f).relative_to(tmp_path / "plugin")
        assert (repo_plugin / rel).read_text(encoding="utf-8") == Path(f).read_text(encoding="utf-8"), rel


def test_compact_symbol_and_impact_fold_hubs_only(tmp_path: Path):
    lines = ["class Hub:"] + [f"    def m{i}(self):\n        return {i}" for i in range(20)]
    lines += [f"def caller{i}():\n    return Hub().m0()" for i in range(30)]
    lines += ["def leaf():\n    return 1", "def one_caller():\n    return leaf()"]
    (tmp_path / "pkg.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    atlas = Atlas(ix)
    hub = atlas.symbol_detail("Hub")
    full, compact = fmt.symbol_detail(hub), fmt.symbol_detail(hub, compact=True)
    assert len(compact) < len(full) * 0.7
    assert "… 8 more: file_outline" in compact and 'symbol("Hub", full=True)' in compact
    assert "m19" in full and "m19" not in compact
    assert "pkg.py: caller" in compact, "callers beyond ten are grouped by file"
    assert "called by (30)" in compact and "called by (30)" in full
    small = atlas.symbol_detail("leaf")
    assert fmt.symbol_detail(small, compact=True) == fmt.symbol_detail(small), "small symbols are untouched"
    im = atlas.impact("Hub.m0")
    full_i, compact_i = fmt.impact(im), fmt.impact(im, compact=True)
    assert len(compact_i) < len(full_i) * 0.7 and "pkg.py: caller" in compact_i and 'impact("Hub.m0"' in compact_i
    small_i = atlas.impact("leaf")
    assert fmt.impact(small_i, compact=True) == fmt.impact(small_i)
    ix.close()
