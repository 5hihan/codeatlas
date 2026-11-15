"""Feature contracts through the index, agent tools, CLI and normal context views."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codeatlas import cli, context, fmt, indexer, patterns
from codeatlas.graph import Atlas
from codeatlas.indexer import Index
from codeatlas.server import App, build_server
from codeatlas.watcher import Watcher


@pytest.fixture
def project(tmp_path):
    files = {
        "app/reader.py": 'def records():\n    with open("schemas/invoice.schema.json") as source:\n        for row in source:\n            yield row\n',
        "app/other.py": 'def unrelated():\n    return 42\n',
        "schemas/invoice.schema.json": '{"type":"object","$ref":"common.schema.json"}',
        "schemas/common.schema.json": '{"type":"object"}',
        "config/runtime.json": '{"schema":"../schemas/invoice.schema.json"}',
    }
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


@pytest.fixture
def app(project):
    runtime = App(project, watch=False, dashboard=False, profile="nav")
    runtime.ix.update()
    yield runtime
    runtime.close()


def _tool_text(result):
    if isinstance(result, tuple):
        result = result[0]
    blocks = result if isinstance(result, list) else result.content
    return "\n".join(block.text for block in blocks if hasattr(block, "text"))


def test_context_resolves_entity_names_without_env_values(tmp_path):
    (tmp_path / ".env").write_text("DATABASE_URL=never-emit-this-secret\n", encoding="utf-8")
    (tmp_path / "db.sql").write_text("CREATE TABLE invoices (id INTEGER);", encoding="utf-8")
    (tmp_path / "service.py").write_text(
        'import os\ndef connection():\n    return os.getenv("DATABASE_URL")\n'
        'def records(db):\n    return db.execute("SELECT * FROM invoices")\n', encoding="utf-8")
    ix = Index(tmp_path)
    try:
        ix.update()
        atlas = Atlas(ix)
        env = context.task_context(ix, atlas, "DATABASE_URL", include_source=False)
        sql = context.task_context(ix, atlas, "invoices", include_source=False)
        assert any(e["kind"] == "uses_env" for e in env["relationships"])
        assert any(e["kind"] == "uses_table" for e in sql["relationships"])
        assert any(i["qualified"] == "connection" for i in env["items"])
        assert any(i["qualified"] == "records" for i in sql["items"])
        assert "never-emit-this-secret" not in context.render(env)
    finally:
        ix.close()


def test_nav_mcp_exposes_and_executes_new_tools(app):
    mcp = build_server(app)

    async def exercise():
        tools = {t.name: t for t in await mcp.list_tools()}
        assert {"relationships", "programming_patterns", "change_context", "task_context"} <= tools.keys()
        assert "replace_symbol" not in tools and "next_snipe" not in tools
        rels = _tool_text(await mcp.call_tool("relationships", {
            "path": "schemas/invoice.schema.json", "direction": "in"}))
        assert "app/reader.py:2" in rels and "uses_schema" in rels
        hints = _tool_text(await mcp.call_tool("programming_patterns", {"path": "app/reader.py"}))
        assert "Generators" in hints and "app/reader.py" in hints
        catalogue = json.loads(_tool_text(await mcp.call_tool("programming_patterns", {"catalogue": True})))
        assert len(catalogue) == 12
        properties = tools["change_context"].input_schema["properties"]
        assert {"comparison", "merge_base", "depth", "token_budget"} <= properties.keys()

    asyncio.run(exercise())


def test_cli_relationships_patterns_and_changes_help(project, capsys):
    cli.main(["relationships", "schemas/invoice.schema.json", "--direction", "in", "--limit", "1", "--json", "--repo", str(project)])
    first = json.loads(capsys.readouterr().out)
    assert first["total"] == 2 and first["next_offset"] == 1
    assert not Path(first["items"][0]["source_path"]).is_absolute()
    cli.main(["patterns", "app/reader.py", "--pattern", "generators", "--json", "--repo", str(project)])
    found = json.loads(capsys.readouterr().out)
    assert found["total"] == 1 and found["items"][0]["path"] == "app/reader.py"
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["changes", "--help"])
    assert exit_info.value.code == 0
    assert "--comparison" in capsys.readouterr().out


def test_hints_and_relationship_retrieval_in_normal_agent_views(app):
    outline = app.atlas.outline("app/reader.py")
    assert {p["pattern"] for p in outline["patterns"]["items"]} >= {"generators", "context_managers"}
    assert "Patterns in context" in fmt.outline(outline)
    assert "uses_schema" in fmt.outline(outline)
    detail = app.atlas.symbol_detail("records", "app/reader.py")
    assert "generators" in {p["pattern"] for p in detail["patterns"]["items"]}
    assert "Generators" in fmt.symbol_detail(detail)
    # pattern explanations are rendered when the task asks about constructs ("patterns"); they stay in the result either way
    result = context.task_context(app.ix, app.atlas, "invoice schema patterns", include_source=False, token_budget=2500)
    reader = next(item for item in result["items"] if item["qualified"] == "records")
    assert "uses_schema schemas/invoice.schema.json" in reader["why"]
    assert "generators" in {p["pattern"] for p in reader["patterns"]}
    assert any(e["source_path"] == "app/reader.py" for e in result["relationships"])
    assert "Generators" in context.render(result)


def test_config_only_update_notifies_and_preserves_code_version(app, monkeypatch):
    previous_code = app.ix.get_meta("last_update")
    previous_relationships = app.ix.get_meta("relationships_updated")
    app.events.clear()
    changed = app.repo / "config/runtime.json"
    changed.write_text('{"schema":"../schemas/common.schema.json"}', encoding="utf-8")
    monkeypatch.setattr(indexer, "extract", lambda *_args, **_kw: pytest.fail("config edit reparsed source"))
    stats = app.ix.update(paths=["config/runtime.json"])
    assert stats.parsed == 0 and stats.relationship_files >= 1
    assert app.ix.get_meta("last_update") == previous_code
    assert app.ix.get_meta("relationships_updated") != previous_relationships
    assert any(e["type"] == "index" and e["relationship_files"] for e in app.events)
    assert app.atlas.relationships("config/runtime.json", direction="out")["items"][0]["target_path"] == "schemas/common.schema.json"
    version = app.ix.get_meta("relationships_updated")
    assert app.ix.update(paths=["config/runtime.json"]).relationship_files == 0
    assert app.ix.get_meta("relationships_updated") == version


def test_cached_queries_and_noop_reopen_do_not_reparse_or_read_sources(app, monkeypatch):
    monkeypatch.setattr(indexer, "extract", lambda *_args, **_kw: pytest.fail("unexpected AST reparse"))
    monkeypatch.setattr(patterns, "get_parser", lambda *_args: pytest.fail("unexpected pattern parse"))
    monkeypatch.setattr(Path, "read_bytes", lambda *_args: pytest.fail("unexpected source content read"))
    for _ in range(3):
        assert app.atlas.programming_patterns("app/reader.py")["total"] >= 2
        assert app.atlas.relationships("schemas/invoice.schema.json", direction="in")["total"] == 2
        assert app.atlas.outline("app/reader.py")["patterns"]["items"]
    warm = Index(app.repo)
    try:
        stats = warm.update()
        assert stats.parsed == 0 and stats.relationship_files == 0
        assert Atlas(warm).programming_patterns("app/reader.py")["total"] >= 2
        assert Atlas(warm).relationships("schemas/invoice.schema.json", direction="in")["total"] == 2
    finally:
        warm.close()


def test_watcher_config_events_and_deleted_dependency_keep_incoming_evidence(app):
    notices = []
    watcher = Watcher(app.ix, on_change=notices.append)
    target = app.repo / "schemas/invoice.schema.json"
    target.unlink()
    watcher._queue(str(target))
    assert "schemas/invoice.schema.json" in watcher._pending
    stats = app.ix.update(paths=sorted(watcher._pending))
    watcher._notify(stats.as_dict(), "watch")
    assert notices and notices[0]["relationship_files"] >= 1
    incoming = app.atlas.relationships("schemas/invoice.schema.json", direction="in")
    assert incoming["total"] == 2 and all(not r["resolved"] for r in incoming["items"])
    assert app.atlas.relationships("schemas/invoice.schema.json", direction="out")["total"] == 0


def test_queries_bound_sql_and_paginate_without_absolute_paths(app):
    traces = []
    app.ix.conn.set_trace_callback(traces.append)
    try:
        page = app.atlas.relationships("schemas/invoice.schema.json", direction="in", limit=1)
        next_page = app.atlas.relationships("schemas/invoice.schema.json", direction="in", limit=1, offset=page["next_offset"])
        hints = app.atlas.programming_patterns("app/reader.py", limit=1)
        assert len(page["items"]) == len(next_page["items"]) == len(hints["items"]) == 1
        assert page["items"] != next_page["items"]
        assert all(not Path(row["source_path"]).is_absolute() for row in page["items"] + next_page["items"])
        assert any("ca_relationships" in sql and "LIMIT 1 OFFSET 1" in sql for sql in traces)
        assert any("code_patterns" in sql and "LIMIT 1 OFFSET 0" in sql for sql in traces)
        assert app.atlas.relationships("x' OR 1=1 --.json")["total"] == 0
    finally:
        app.ix.conn.set_trace_callback(None)
