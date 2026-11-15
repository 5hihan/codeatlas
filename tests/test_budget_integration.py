"""Adaptive budgets must survive the public retrieval and rendering paths."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from codeatlas import context, rank
from codeatlas.budgeting import AUTO_MAX, AUTO_MIN, plan_context
from codeatlas.graph import Atlas
from codeatlas.indexer import Index


@pytest.fixture
def indexed(tmp_path: Path):
    files = {
        "app/service.py": 'def refresh_token(value):\n    """Refresh an expired authentication token."""\n    return value.strip()\n',
        "application/unrelated.py": 'def refresh_token_unrelated(value):\n    """Refresh a different application token."""\n    return value\n',
        "tests/test_service.py": 'from app.service import refresh_token\ndef test_refresh_token():\n    assert refresh_token(" value ") == "value"\n',
        "config/settings.json": '{"schema": "../schema/user.schema.json"}',
        "schema/user.schema.json": '{"type":"object"}',
    }
    for rel, source in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source, encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    yield ix, Atlas(ix)
    ix.close()


def test_real_relationship_inventory_does_not_double_count_source_as_artifacts(indexed):
    ix, _ = indexed
    plan = plan_context(ix)
    assert plan["repo"]["files"] == 3
    assert plan["repo"]["artifact_files"] == 2
    assert plan["repo"]["config_files"] == 1 and plan["repo"]["schema_files"] == 1


def test_task_context_default_budget_is_automatic_and_explained(indexed):
    ix, atlas = indexed
    result = context.task_context(ix, atlas, "refresh token", scope="app")
    assert result["budget_plan"]["policy"] == "auto"
    assert result["budget"] == result["budget_plan"]["budget"]
    assert AUTO_MIN <= result["budget"] <= AUTO_MAX
    assert result["budget_plan"]["scoped"]["files"] == 1
    assert result["budget_plan"]["reasons"]
    assert result["items"]
    assert rank.estimate_tokens(context.render(result)) <= result["budget"]


@pytest.mark.parametrize("budget", [1, 50, 200, 650, 16000])
def test_task_context_preserves_explicit_budget_through_render(indexed, budget):
    ix, atlas = indexed
    result = context.task_context(ix, atlas, "refresh token", token_budget=budget)
    rendered = context.render(result)
    assert result["budget"] == budget
    assert result["budget_plan"]["policy"] == "explicit"
    assert rank.estimate_tokens(rendered) <= budget
    assert result["tokens_used"] <= budget


def test_scope_uses_directory_boundaries_for_lexical_candidates(indexed):
    ix, atlas = indexed
    result = context.task_context(ix, atlas, "refresh token", scope="app", include_source=False)
    assert any(it["path"] == "app/service.py" for it in result["items"])
    # Relevant tests may sit outside the scope; the unrelated lexical prefix may not.
    assert not any(it["path"] == "application/unrelated.py" for it in result["items"])


@pytest.mark.parametrize("budget", [None, 2500])
def test_common_budget_keeps_relevant_tests_signatures_and_source(indexed, budget):
    ix, atlas = indexed
    result = context.task_context(ix, atlas, "refresh token", scope="app", token_budget=budget)
    output = context.render(result)
    assert "def refresh_token(value)" in output
    assert "tests/test_service.py" in output
    assert "return value.strip()" in output
    assert "expand:" in output
    assert rank.estimate_tokens(output) <= result["budget"]


def test_lexical_ambiguity_does_not_invent_dependency_fanout(tmp_path: Path):
    (tmp_path / "handlers.py").write_text("\n".join(
        f"def shared_{i}():\n    return {i}\n" for i in range(30)), encoding="utf-8")
    ix = Index(tmp_path)
    try:
        ix.update()
        result = context.task_context(ix, Atlas(ix), "shared", include_source=False)
        assert result["budget_plan"]["inputs"]["candidate_count"] >= 20
        assert result["budget_plan"]["inputs"]["fanout"] == 0
    finally:
        ix.close()


def test_scoped_fts_retrieval_is_not_starved_by_outside_top_400(tmp_path: Path):
    target = tmp_path / "scoped" / "service.py"
    target.parent.mkdir()
    target.write_text('def only_target(shared=None):\n    return shared\n', encoding="utf-8")
    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text("\n".join(f"def shared_{i}():\n    return {i}\n" for i in range(430)), encoding="utf-8")
    ix = Index(tmp_path)
    try:
        ix.update()
        result = context.task_context(ix, Atlas(ix), "shared", scope="scoped", include_source=False)
        assert any(item["qualified"] == "only_target" for item in result["items"])
    finally:
        ix.close()


@pytest.mark.parametrize("task", ["the and", "unfindable_identifier_xyz"])
def test_empty_or_unmatched_task_still_returns_bounded_budget_metadata(indexed, task):
    ix, atlas = indexed
    result = context.task_context(ix, atlas, task, token_budget=50)
    assert result["budget_plan"]["policy"] == "explicit"
    assert rank.estimate_tokens(context.render(result)) <= 50


@pytest.mark.parametrize("budget", [1, 20, 100, 700])
def test_repo_map_honors_small_explicit_budget(indexed, budget):
    ix, _ = indexed
    output = rank.repo_map(ix, token_budget=budget, focus_paths=["app"])
    assert rank.estimate_tokens(output) <= budget


def test_python_and_mcp_defaults_select_auto_budget(indexed):
    ix, _ = indexed
    assert inspect.signature(context.task_context).parameters["token_budget"].default is None
    assert inspect.signature(rank.repo_map).parameters["token_budget"].default is None
    from codeatlas.server import App, build_server
    app = App(ix.repo, watch=False, dashboard=False)
    try:
        mcp = build_server(app)
        definitions = asyncio.run(mcp.list_tools())
        for name in ("task_context", "repo_map", "change_context"):
            tool = next(t for t in definitions if t.name == name)
            schema = tool.input_schema
            assert "token_budget" not in schema.get("required", [])
            assert schema["properties"]["token_budget"].get("default") in (None, 0)  # 0 and None both select the automatic budget
    finally:
        app.close()


@pytest.mark.parametrize("command,args", [("context", ["refresh token"]), ("map", []), ("changes", [])])
def test_cli_defaults_forward_auto_and_explicit_budgets(monkeypatch, command, args):
    from codeatlas import cli
    captured = []
    monkeypatch.setattr(cli, "cmd_" + command, lambda opts: captured.append(opts.budget))
    cli.main([command, *args])
    cli.main([command, *args, "--budget", "1700"])
    assert captured == [None, 1700]


def test_real_index_and_relationship_updates_invalidate_budget_counts(indexed):
    ix, _ = indexed
    plan_context(ix)
    assert plan_context(ix)["inputs"]["stats_cache_hit"]
    (ix.repo / "app" / "another.py").write_text("def another():\n    return 2\n", encoding="utf-8")
    ix.update(paths=["app/another.py"])
    source_update = plan_context(ix)
    assert not source_update["inputs"]["stats_cache_hit"]
    assert source_update["repo"]["files"] == 4
    (ix.repo / "schema" / "next.proto").write_text('syntax = "proto3";\nmessage Next {}\n', encoding="utf-8")
    ix.update(paths=["schema/next.proto"])
    artifact_update = plan_context(ix)
    assert not artifact_update["inputs"]["stats_cache_hit"]
    assert artifact_update["repo"]["schema_files"] == 2
