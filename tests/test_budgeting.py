"""Behavioral tests of adaptive planning, with indexed data and no source reads."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from codeatlas.budgeting import AUTO_MAX, AUTO_MIN, plan_context


class IndexedRepo:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.lock = threading.RLock()
        self.conn.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO meta VALUES('last_update','1'),('relationships_updated','1');
            CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT UNIQUE,lines INTEGER);
            CREATE TABLE symbols(id INTEGER PRIMARY KEY,file_id INTEGER,complexity INTEGER);
            CREATE INDEX symbol_file ON symbols(file_id);
            CREATE TABLE ca_artifact_files(path TEXT PRIMARY KEY,category TEXT);
            CREATE TABLE ca_relationships(source_path TEXT,target_path TEXT);
        """)

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def rel(self, path):
        return path.replace("\\", "/")

    def read_source(self, *_):
        pytest.fail("budget planning must not read source files")

    def add_files(self, directory, count, complexity=3, lines=100, symbols=4):
        for i in range(count):
            fid = self.conn.execute("INSERT INTO files(path,lines) VALUES(?,?)", (f"{directory}/file{i}.py", lines)).lastrowid
            self.conn.executemany("INSERT INTO symbols(file_id,complexity) VALUES(?,?)", [(fid, complexity)] * symbols)

    def bump(self, key="last_update"):
        self.conn.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key=?", (key,))


@pytest.fixture
def repo():
    value = IndexedRepo()
    value.add_files("apps/small", 2)
    yield value
    value.conn.close()


def test_scoped_budget_ignores_huge_unrelated_folders(repo):
    small = plan_context(repo, task="explain cache", scope="apps/small")
    repo.add_files("apps/unrelated", 10000, symbols=8)
    repo.bump()
    scoped = plan_context(repo, task="explain cache", scope="apps/small")
    global_plan = plan_context(repo, task="explain cache")
    assert scoped["budget"] == small["budget"]
    assert scoped["repo"]["files"] == 10002
    assert scoped["scoped"]["files"] == 2
    assert global_plan["budget"] > scoped["budget"]
    assert global_plan["budget"] < 6000  # size alone must not max out the context window


def test_precise_target_uses_less_discovery_than_ambiguous_query(repo):
    precise = plan_context(repo, task="explain `refreshToken`", scope="apps/small/file0.py",
                           signals={"candidate_count": 1, "exact_match": True})
    broad = plan_context(repo, task="find handlers", signals={"candidate_count": 150, "fanout": 20})
    assert precise["budget"] < broad["budget"]
    assert precise["inputs"]["specificity"] == 1
    assert precise["allocations"]["source"] / precise["budget"] > broad["allocations"]["source"] / broad["budget"]


def test_high_risk_change_reserves_more_relationship_and_test_evidence(repo):
    ordinary = plan_context(repo, task="change names", signals={"candidate_count": 2, "test_count": 0})
    risky = plan_context(repo, task="change authentication migration", mode="changes",
                         signals={"candidate_count": 2, "changed_files": ["schema.sql", "auth.py"],
                                  "schema_changes": True, "config_changes": 1, "complexity": 28,
                                  "fanout": 12, "depth": 3, "relationship_count": 25,
                                  "test_count": 5, "test_relevance": 1.0})
    assert risky["budget"] > ordinary["budget"]
    assert risky["allocations"]["tests"] / risky["budget"] > ordinary["allocations"]["tests"] / ordinary["budget"]
    assert risky["allocations"]["relationships"] > risky["allocations"]["source"]
    assert risky["inputs"]["changed_files"] == 2
    assert any("schema changes" in reason for reason in risky["reasons"])


@pytest.mark.parametrize("signals", [
    {"fanout": 30}, {"changed_files": 12}, {"complexity": 30}, {"schema_changes": 1},
    {"config_changes": 1}, {"candidate_count": 120}, {"relationship_count": 25},
    {"test_count": 20, "test_relevance": 1},
])
def test_each_relevant_signal_can_increase_budget(repo, signals):
    before = plan_context(repo, task="analyze behavior")
    after = plan_context(repo, task="analyze behavior", signals=signals)
    assert after["budget"] > before["budget"]


def test_depth_only_increases_budget_when_a_graph_is_relevant(repo):
    no_graph = plan_context(repo, signals={"depth": 8})
    base = plan_context(repo)
    assert no_graph["budget"] == base["budget"]
    shallow = plan_context(repo, signals={"fanout": 8, "depth": 1})
    deep = plan_context(repo, signals={"fanout": 8, "depth": 4})
    assert deep["budget"] > shallow["budget"]


@pytest.mark.parametrize("budget", [1, 99, 1400, 3000, 15001])
def test_explicit_positive_budgets_are_exact_even_outside_auto_bounds(repo, budget):
    result = plan_context(repo, requested_budget=budget,
                          signals={"schema_changes": 50, "fanout": 1000, "changed_files": 50})
    assert result["policy"] == "explicit" and result["budget"] == budget
    assert sum(result["allocations"].values()) == budget
    assert all(value >= 0 for value in result["allocations"].values())


@pytest.mark.parametrize("budget", [-1, 0, 1.5, True, "2000"])
def test_invalid_explicit_budget_rejected(repo, budget):
    with pytest.raises(ValueError, match="positive integer"):
        plan_context(repo, requested_budget=budget)


def test_automatic_cap_and_bounded_allocations(repo):
    result = plan_context(repo, task="authentication migrations payments", signals={
        "candidate_count": 100000, "fanout": 100000, "depth": 100,
        "complexity": 1000, "changed_files": 10000, "schema_changes": 10000,
        "config_changes": 10000, "test_count": 10000, "test_relevance": 1,
        "relationship_count": 100000,
    })
    assert result["budget"] == AUTO_MAX
    assert sum(result["allocations"].values()) == AUTO_MAX
    assert all(value >= 0 for value in result["allocations"].values())


def test_empty_or_absent_scope_is_honest_and_bounded(repo):
    result = plan_context(repo, task="`specificName`", scope="absent", signals={"exact_match": True})
    assert AUTO_MIN <= result["budget"] <= AUTO_MAX
    assert result["scoped"]["files"] == 0 and result["repo"]["files"] == 2
    assert plan_context(repo, scope=".")["scope"] is None


def test_aggregate_cache_reused_but_signals_are_replanned(repo):
    first = plan_context(repo, scope="apps/small")
    assert not first["inputs"]["stats_cache_hit"]
    statements = []
    repo.conn.set_trace_callback(statements.append)
    second = plan_context(repo, scope="apps/small", signals={"schema_changes": 1})
    repo.conn.set_trace_callback(None)
    assert second["inputs"]["stats_cache_hit"]
    assert not any("COUNT(" in sql.upper() or "SUM(" in sql.upper() for sql in statements)
    assert second["budget"] > first["budget"]
    second["scoped"]["files"] = -50
    assert plan_context(repo, scope="apps/small")["scoped"]["files"] == 2


def test_index_version_invalidates_cached_counts(repo):
    before = plan_context(repo)
    repo.add_files("new", 4)
    repo.bump()
    after = plan_context(repo)
    assert not after["inputs"]["stats_cache_hit"]
    assert after["repo"]["files"] == before["repo"]["files"] + 4


def test_relationship_only_version_invalidates_artifact_statistics(repo):
    plan_context(repo, scope="apps/small")
    repo.conn.execute("INSERT INTO ca_artifact_files VALUES('apps/small/schema.sql','schema')")
    repo.conn.execute("INSERT INTO ca_relationships VALUES('apps/consumer.py','apps/small/schema.sql')")
    repo.bump("relationships_updated")
    after = plan_context(repo, scope="apps/small")
    assert not after["inputs"]["stats_cache_hit"]
    assert after["scoped"]["schema_files"] == 1
    assert after["scoped"]["relationships"] == 1


def test_scopes_are_literal_and_have_path_boundaries(repo):
    repo.add_files("apps/smallish", 5)
    repo.add_files("apps/a_%", 1)
    repo.add_files("apps/abanything", 3)
    repo.bump()
    assert plan_context(repo, scope="apps/small")["scoped"]["files"] == 2
    assert plan_context(repo, scope="apps/a_%")["scoped"]["files"] == 1
    assert plan_context(repo, scope="apps\\small\\")["scoped"]["files"] == 2


def test_cache_cannot_grow_without_bound(repo):
    for number in range(80):
        plan_context(repo, scope=f"nonexistent/{number}")
    assert len(repo._context_budget_stats["entries"]) <= 65
    assert "" in repo._context_budget_stats["entries"]


@pytest.mark.parametrize("mode", ["task", "changes", "repo_map", "symbol", "source"])
def test_allocation_totals_and_signature_first_policy(repo, mode):
    result = plan_context(repo, mode=mode, signals={"schema_changes": 2, "test_relevance": 1})
    assert sum(result["allocations"].values()) == result["budget"]
    assert all(value >= 0 for value in result["allocations"].values())
    assert result["allocation_order"].index("source") > result["allocation_order"].index("tests")
    if mode == "repo_map":
        assert result["allocations"]["symbols"] > result["allocations"]["source"]


def test_invalid_optional_signals_do_not_produce_nan_or_unbounded_plans(repo):
    result = plan_context(repo, signals={"candidate_count": float("nan"), "fanout": -1,
                                       "complexity": float("inf"), "specificity": "high"})
    assert AUTO_MIN <= result["budget"] <= AUTO_MAX
    assert sum(result["allocations"].values()) == result["budget"]
