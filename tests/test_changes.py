"""Git integration tests for bounded, explainable change impact."""
import os
import subprocess
from pathlib import Path

import pytest

from codeatlas import changes
from codeatlas.graph import Atlas
from codeatlas.indexer import Index
from codeatlas.rank import estimate_tokens


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout.decode("utf-8").strip()


def put(repo, path, source):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "CodeAtlas tests")
    git(tmp_path, "config", "core.autocrlf", "false")
    put(tmp_path, ".gitignore", ".codeatlas/\n")
    put(tmp_path, "package.json", "{}\n")
    return tmp_path


def commit(repo, message="fixture"):
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def index(repo):
    ix = Index(repo)
    yield ix
    ix.close()


def context(index, **kwargs):
    index.update()
    return changes.change_context(index, Atlas(index), **kwargs)


def test_transitive_imports_resolved_callers_and_anonymous_tests(repo, index):
    put(repo, "core.ts", "export function calculate() { return 1; }\n")
    put(repo, "service.ts", "import { calculate } from './core';\nexport function serve() { return calculate(); }\n")
    put(repo, "service.test.ts", "import { serve } from './service';\ntest('works', () => serve());\n")
    put(repo, "other.ts", "export function calculate() { return 500; }\n")
    put(repo, "other.test.ts", "import { calculate } from './other';\ntest('other', () => calculate());\n")
    commit(repo)
    put(repo, "core.ts", "export function calculate() { return 2; }\n")
    result = context(index)
    assert [s["qualified"] for s in result["changed_symbols"]] == ["calculate"]
    assert {t["path"] for t in result["tests"]} == {"service.test.ts"}
    assert any(c["qualified"] == "serve" and c["depth"] == 1 for c in result["consumers"])
    assert any(c["qualified"] == "<top-level>" and c["path"] == "service.test.ts" for c in result["consumers"])
    assert any(e["source_path"] == "service.test.ts" and e["target_path"] == "service.ts" and e["depth"] == 2 for e in result["evidence"])
    assert not any(e["source_path"] == "other.test.ts" for e in result["evidence"])
    assert result["index_version"] and len(result["base_commit"]) == 40


def test_configuration_schema_chain_and_test_import(repo, index):
    put(repo, "schemas/common.schema.json", '{"type":"string"}\n')
    put(repo, "schemas/user.schema.json", '{"$ref":"common.schema.json"}\n')
    put(repo, "config/app.json", '{"schema":"../schemas/user.schema.json"}\n')
    put(repo, "app.ts", 'import settings from "./config/app.json";\nexport function app() { return settings; }\n')
    put(repo, "app.test.ts", 'import { app } from "./app";\ntest("app", () => app());\n')
    commit(repo)
    put(repo, "schemas/common.schema.json", '{"type":"number"}\n')
    result = context(index, depth=4)
    paths = {f["path"]: f["depth"] for f in result["blast_radius"]["files"]}
    assert paths == {"schemas/common.schema.json": 0, "schemas/user.schema.json": 1,
                     "config/app.json": 2, "app.ts": 3, "app.test.ts": 4}
    assert {t["path"] for t in result["tests"]} == {"app.test.ts"}
    assert result["files"][0]["artifact_indexed"] and result["files"][0]["indexed"]
    assert {e["kind"] for e in result["evidence"]} >= {"schema_ref", "uses_schema", "loads_config", "import"}
    assert all(e["line"] > 0 and e["evidence"] and e["confidence"] for e in result["evidence"])
    assert result["budget_plan"]["budget"] == result["token_budget"]
    assert any("schema changes" in reason for reason in result["budget_plan"]["reasons"])


def test_deleted_symbols_and_deleted_file_dependents(repo, index):
    put(repo, "core.py", "def retained():\n    return 1\n\ndef removed():\n    return 2\n")
    put(repo, "gone.py", "def deleted():\n    return 3\n")
    put(repo, "test_gone.py", "from gone import deleted\n\ndef test_call():\n    assert deleted() == 3\n")
    commit(repo)
    index.update()
    put(repo, "core.py", "def retained():\n    return 1\n")
    (repo / "gone.py").unlink()
    result = context(index)
    old = {s["qualified"]: s for s in result["changed_symbols"] if s["side"] == "before"}
    assert set(old) == {"removed", "deleted"}
    assert old["removed"]["start_line"] == 4 and old["deleted"]["path"] == "gone.py"
    assert all(s["id"] is None and s["status"] == "deleted" for s in old.values())
    assert any(t["path"] == "test_gone.py" for t in result["tests"])
    edge = next(e for e in result["evidence"] if e["target_path"] == "gone.py")
    assert edge["confidence"] == "medium" and edge["historical_target"] and not edge["resolved"]


def test_rename_unicode_literal_pathspec_and_untracked_artifact(repo, index):
    old, new = "old [x] café.ts", "new [x] café.ts"
    put(repo, old, "export function work() { return 1; }\n")
    put(repo, "old x café.ts", "export function unrelated() { return 1; }\n")
    commit(repo)
    git(repo, "mv", old, new)
    put(repo, "settings.json", '{"schema":"new.schema.json"}\n')
    result = context(index)
    files = {f["path"]: f for f in result["files"]}
    assert set(files) == {new, "settings.json"}
    assert files[new]["status"] == "renamed" and files[new]["old_path"] == old
    assert files["settings.json"]["status"] == "untracked"
    assert [(s["qualified"], s["old_path"]) for s in result["changed_symbols"]] == [("work", old)]
    assert git(repo, "diff", "--cached", "--name-only", "-z").rstrip("\0") == new


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create tab/newline filenames")
def test_nul_delimited_paths_preserve_tabs_and_newlines(repo, index):
    name = "odd\tline\nname.py"
    put(repo, name, "def changed():\n    return 1\n")
    commit(repo)
    put(repo, name, "def changed():\n    return 2\n")
    result = context(index)
    assert result["files"][0]["path"] == name
    assert result["changed_symbols"][0]["qualified"] == "changed"


def test_staged_snapshot_is_separate_from_working_tree(repo, index):
    put(repo, "app.py", "def original():\n    return 1\n")
    commit(repo)
    put(repo, "app.py", "def staged_only():\n    return 2\n")
    git(repo, "add", "app.py")
    put(repo, "app.py", "def working_only():\n    return 3\n")
    put(repo, "untracked.json", "{}\n")
    staged = context(index, comparison="staged")
    assert {s["qualified"] for s in staged["changed_symbols"]} == {"original", "staged_only"}
    assert {f["path"] for f in staged["files"]} == {"app.py"}
    assert any("working-tree index" in text for text in staged["limitations"])
    working = context(index, comparison="working")
    assert {s["qualified"] for s in working["changed_symbols"]} == {"original", "working_only"}
    assert "untracked.json" in {f["path"] for f in working["files"]}
    assert (repo / "app.py").read_text() == "def working_only():\n    return 3\n"


def test_branch_merge_base_excludes_other_branch_changes(repo, index):
    put(repo, "root.py", "def root():\n    return 1\n")
    base = commit(repo)
    git(repo, "checkout", "-qb", "feature")
    put(repo, "feature.py", "def feature():\n    return 2\n")
    feature = commit(repo, "feature")
    git(repo, "checkout", "-qb", "upstream", base)
    put(repo, "upstream.py", "def upstream():\n    return 3\n")
    commit(repo, "upstream")
    git(repo, "checkout", "-q", "feature")
    put(repo, "dirty.py", "def dirty():\n    return 4\n")
    result = context(index, base_ref="upstream", comparison="branch", merge_base=True)
    assert result["base_commit"] == base and result["head_commit"] == feature
    assert {f["path"] for f in result["files"]} == {"feature.py"}
    assert result["changed_symbols"][0]["qualified"] == "feature"
    assert git(repo, "branch", "--show-current") == "feature"


@pytest.mark.parametrize("ref", ["not-a-real-ref", "--output=unexpected", "HEAD\0bad", ""])
def test_invalid_ref_is_safe_and_explicit(repo, index, ref):
    commit(repo)
    result = changes.change_context(index, Atlas(index), base_ref=ref)
    assert "error" in result and "reference" in result["error"]
    assert not (repo / "unexpected").exists()


def test_caps_and_render_budget_are_explicit(repo, index):
    put(repo, "root.ts", "export function root() { return 1; }\n")
    for n in range(8):
        put(repo, f"use{n}.ts", f"import {{ root }} from './root';\nexport function use{n}() {{ return root(); }}\n")
    commit(repo)
    put(repo, "root.ts", "export function root() { return 2; }\n")
    result = context(index, max_edges=3, token_budget=150)
    assert len(result["evidence"]) <= 3
    assert result["truncated"] and "edges" in result["truncation_reasons"]
    rendered = changes.render(result)
    assert estimate_tokens(rendered) <= 150 and "truncated" in rendered
    assert result["token_budget"] == 150
    no_traversal = context(index, depth=0)
    assert no_traversal["evidence"] == [] and "depth" in no_traversal["truncation_reasons"]
    tiny = context(index, token_budget=1)
    assert tiny["token_budget"] == 1 and estimate_tokens(changes.render(tiny)) <= 1


def test_symbol_cap_does_not_hide_late_changed_definition(repo, index):
    source = "\n".join(f"def f{n}():\n    return {n}\n" for n in range(10))
    put(repo, "many.py", source)
    commit(repo)
    put(repo, "many.py", source.replace("return 9", "return 90"))
    result = context(index, max_symbols=1)
    assert [(s["qualified"], s["id"] is not None) for s in result["changed_symbols"]] == [("f9", True)]
    assert "changed_symbols" not in result["truncation_reasons"]


def test_relationship_pagination_and_deleted_artifact(repo, index):
    put(repo, "shared.schema.json", '{"type":"string"}\n')
    for n in range(205):
        put(repo, f"config{n:03}.json", '{"schema":"shared.schema.json"}\n')
    commit(repo)
    index.update()
    (repo / "shared.schema.json").unlink()
    result = context(index, max_files=300, max_edges=300)
    edges = [e for e in result["evidence"] if e["target_path"] == "shared.schema.json"]
    assert len(edges) == 205 and all(not e["resolved"] for e in edges)
    assert not result["truncated"]


def test_no_changes_is_not_a_truncated_graph(repo, index):
    put(repo, "app.py", "def app():\n    pass\n")
    commit(repo)
    result = context(index)
    assert not result["files"] and not result["evidence"] and not result["truncated"]


def test_rejected_nodes_do_not_allow_unbounded_relationship_pagination(repo, index, monkeypatch):
    from codeatlas import relationships
    put(repo, "schema.json", '{"type":"string"}\n')
    commit(repo)
    put(repo, "schema.json", '{"type":"number"}\n')
    calls = []

    def incoming(ix, path=None, direction="in", limit=50, offset=0):
        calls.append((path, limit, offset))
        if path != "schema.json":
            return {"items": [], "truncated": False, "next_offset": None}
        return {"items": [{"source_path": f"config{n}.json", "target_path": path, "kind": "uses_schema",
                           "line": 1, "evidence": "literal path", "confidence": "high", "resolved": True}
                          for n in range(offset, offset + limit)],
                "truncated": True, "next_offset": offset + limit}

    monkeypatch.setattr(relationships, "relationships", incoming)
    result = context(index, max_files=1, max_edges=100)
    assert len(result["blast_radius"]["files"]) == 2
    assert "affected_files" in result["truncation_reasons"]
    assert "relationship_candidates" in result["truncation_reasons"]
    assert len(calls) <= 9


def test_render_reserves_tests_and_evidence_for_large_change_lists(repo, index):
    put(repo, "core.ts", "export function core() { return 1; }\n")
    put(repo, "service.ts", "import { core } from './core';\nexport function service() { return core(); }\n")
    put(repo, "service.test.ts", "import { service } from './service';\ntest('works', () => service());\n")
    commit(repo)
    put(repo, "core.ts", "export function core() { return 2; }\n")
    result = context(index, token_budget=600)
    result["files"] = [{"status": "modified", "path": f"src/long_module_name_{n:04}.ts"} for n in range(500)]
    result["changed_symbols"] = [{"status": "modified", "qualified": f"changedFunction{n}", "path": f"src/module{n}.ts",
                                  "start_line": 1, "end_line": 5, "side": "after"} for n in range(500)]
    result["total_files"] = 500
    rendered = changes.render(result)
    assert estimate_tokens(rendered) <= 600
    # signal first: tests and symbol-level consumers survive a huge change list, file/symbol lists are capped
    assert "service.test.ts" in rendered and "Tests to run" in rendered
    assert "Consumers to re-check" in rendered and "core <- service" in rendered
    assert "more" in rendered and "/500 files" in rendered  # capped lists say "+N more" instead of listing everything
    assert "budget 600 (explicit)" in rendered
    assert rendered.count("long_module_name_") <= 28 and rendered.count("changedFunction") <= 48
