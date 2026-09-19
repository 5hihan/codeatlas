"""Explainable, bounded context budgets using indexed facts rather than source scans.

``signals`` can contain candidate_count, exact_match, specificity (0..1), fanout,
depth, complexity (maximum of relevant symbols), changed_files, relationship_count,
schema_changes, config_changes, test_count and test_relevance (0..1). File/change
signals accept a count, a bool, or a collection of paths. The caller supplies facts
from retrieval; absence is not treated as evidence of zero matches or no tests.

Allocations are token ceilings for packing, not promises that useful evidence fills
every section. Prefer signatures, relationships and tests before optional bodies;
unused allocations can be reassigned to relevant evidence by the caller.
"""
from __future__ import annotations

import math
import re
from collections import OrderedDict
from numbers import Integral, Real
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .indexer import Index

AUTO_MIN = 1500
AUTO_MAX = 12000
_CACHE_SCOPES = 64
_STOP = frozenset("a an the and or of to in on for with from by is are this that it its fix add remove update change code file files function functions method class please explain show how what where why does do find".split())
_RISK_TERMS = frozenset({"authentication", "authorization", "permissions", "security", "payment", "payments", "transaction", "transactions", "migration", "migrations", "encryption"})
_BASE = {"task": 2400, "changes": 2800, "change": 2800, "diff": 2800,
         "repo_map": 1800, "map": 1800, "symbol": 1900, "source": 1900}
_TEST_SQL = """(f.path GLOB 'test_*' OR f.path GLOB '*/test_*'
    OR f.path GLOB '*_test.*' OR f.path GLOB '*.test.*' OR f.path GLOB '*.spec.*'
    OR f.path LIKE 'tests/%' OR f.path LIKE '%/tests/%'
    OR f.path LIKE '__tests__/%' OR f.path LIKE '%/__tests__/%'
    OR f.path LIKE 'spec/%' OR f.path LIKE '%/spec/%')"""


def _count(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value)
    if isinstance(value, Real) and math.isfinite(float(value)):
        return max(0, int(value))
    return default


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, Real) and math.isfinite(float(value)):
        return max(0.0, float(value))
    return default


def _scope(ix: Index, scope: str | None) -> str:
    if not scope or not scope.strip() or scope.strip() == ".":
        return ""
    normalized = ix.rel(scope.strip()).replace("\\", "/").rstrip("/")
    return "" if normalized == "." else normalized


def _where(scope: str, column: str = "f.path") -> tuple[str, tuple]:
    if not scope:
        return "", ()
    prefix = scope + "/"
    # Literal, boundary-aware prefixes: '_' and '%' are not SQL wildcards here.
    return f" WHERE ({column}=? OR ({column}>=? AND {column}<?))", (scope, prefix, prefix + "\U0010ffff")


def _read_stats(ix: Index, scope: str, tables: set[str]) -> dict:
    where, params = _where(scope)
    files = ix.conn.execute("SELECT COUNT(*), COALESCE(SUM(f.lines),0), "
                            f"COALESCE(SUM(CASE WHEN {_TEST_SQL} THEN 1 ELSE 0 END),0) FROM files f" + where,
                            params).fetchone()
    symbols = ix.conn.execute("SELECT COUNT(*), COALESCE(AVG(CASE WHEN s.complexity>0 THEN s.complexity END),0), "
                              "COALESCE(MAX(s.complexity),0) FROM symbols s JOIN files f ON f.id=s.file_id" + where,
                              params).fetchone()
    stats = {"files": int(files[0]), "lines": int(files[1]), "test_files": int(files[2]),
             "symbols": int(symbols[0]), "mean_complexity": round(float(symbols[1]), 2),
             "max_complexity": int(symbols[2]), "artifact_files": 0, "schema_files": 0,
             "config_files": 0, "relationships": 0}
    if "ca_artifact_files" in tables:
        artifact_where, artifact_params = _where(scope, "path")
        for row in ix.conn.execute("SELECT category,COUNT(*) FROM ca_artifact_files" + artifact_where + " GROUP BY category", artifact_params):
            if row[0] in {"schema", "config"}:
                stats["artifact_files"] += int(row[1])
            if row[0] == "schema":
                stats["schema_files"] += int(row[1])
            elif row[0] == "config":
                stats["config_files"] += int(row[1])
    if "ca_relationships" in tables:
        if scope:
            source_where, source_params = _where(scope, "source_path")
            target_where, target_params = _where(scope, "target_path")
            rel_where = " WHERE (" + source_where.removeprefix(" WHERE ") + " OR " + target_where.removeprefix(" WHERE ") + ")"
            row = ix.conn.execute("SELECT COUNT(*) FROM ca_relationships" + rel_where,
                                  (*source_params, *target_params)).fetchone()
        else:
            row = ix.conn.execute("SELECT COUNT(*) FROM ca_relationships").fetchone()
        stats["relationships"] = int(row[0])
    return stats


def _stats(ix: Index, scope: str) -> tuple[dict, dict, bool, tuple]:
    """Cache aggregate SQL by index versions, retaining at most 64 scoped entries."""
    version = (ix.get_meta("last_update"), ix.get_meta("relationships_updated"))
    cache = getattr(ix, "_context_budget_stats", None)
    if cache is None or cache["version"] != version:
        tables = {row[0] for row in ix.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cache = {"version": version, "tables": tables, "entries": OrderedDict()}
        ix._context_budget_stats = cache
    entries = cache["entries"]
    hit = "" in entries and scope in entries
    for key in ("", scope):
        if key not in entries:
            entries[key] = _read_stats(ix, key, cache["tables"])
        entries.move_to_end(key)
    while len(entries) > _CACHE_SCOPES + 1:
        oldest = next(iter(entries))
        if oldest == "":
            entries.move_to_end(oldest)
        else:
            del entries[oldest]
    return dict(entries[""]), dict(entries[scope]), hit, version


def _specificity(task: str, scope: str, signals: dict, stats: dict) -> tuple[float, list[str]]:
    terms = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_]*", task) if w.lower() not in _STOP]
    terms = list(dict.fromkeys(terms))[:32]
    if "specificity" in signals:
        return min(1.0, _number(signals["specificity"])), terms
    if signals.get("exact_match"):
        return 1.0, terms
    value = 0.0
    if scope:
        value += 0.25
        if stats["files"] <= 2:
            value += 0.25
    if re.search(r"`[^`\n]+`|\b\w+\.\w+\b|\b[a-z]+[A-Z]\w*\b|\b\w+_\w+\b|[/\\]", task):
        value += 0.35
    count = signals.get("candidate_count")
    if count is not None:
        n = _count(count)
        value += 0.30 if 0 < n <= 3 else (-0.2 if n > 30 else 0)
    # More prose does not automatically mean a more specific task.
    if 2 <= len(terms) <= 6:
        value += 0.10
    return max(0.0, min(1.0, value)), terms


def _allocations(budget: int, specificity: float, risk: bool, test_relevance: float,
                 no_tests: bool, mode: str) -> dict[str, int]:
    weights = {"symbols": 0.30, "source": 0.24, "relationships": 0.18,
               "tests": 0.15, "patterns": 0.04, "overhead": 0.09}
    if specificity >= 0.75:
        weights.update(symbols=0.28, source=0.30, relationships=0.15, tests=0.15, patterns=0.03)
    if mode in {"map", "repo_map"}:
        weights.update(symbols=0.48, source=0.08, relationships=0.23, tests=0.08, patterns=0.04)
    if risk:
        weights["tests"] += 0.09
        weights["relationships"] += 0.06
        # A tiny map source allocation must never become negative.
        reduction = min(0.10, weights["source"] - 0.02)
        weights["source"] -= reduction
        weights["symbols"] -= 0.14 - reduction
        weights["patterns"] -= 0.01
    if test_relevance >= 0.5:
        weights["tests"] += 0.05
        weights["source"] -= min(0.03, weights["source"] - 0.01)
        weights["symbols"] -= 0.02
    if no_tests and not risk:
        freed = max(0, weights["tests"] - 0.04)
        weights["tests"] -= freed
        weights["symbols"] += freed * 0.6
        weights["relationships"] += freed * 0.4
    total = sum(weights.values())
    raw = {name: budget * weight / total for name, weight in weights.items()}
    result = {name: int(value) for name, value in raw.items()}
    # Largest remainder makes even an explicit budget of one token sum exactly.
    for name in sorted(raw, key=lambda key: raw[key] - result[key], reverse=True)[:budget - sum(result.values())]:
        result[name] += 1
    return result


def plan_context(ix: Index, *, task: str = "", scope: str | None = None,
                 requested_budget: int | None = None, mode: str = "task",
                 signals: dict | None = None) -> dict:
    """Plan an automatic 1,500–12,000-token context, or honor an explicit budget.

    Scope filters match an exact path or directory boundary. Repository counts are
    reported for context, but only scoped counts affect the budget when scoped.
    Reads SQLite aggregates once per version/scope; never reads source files.
    """
    if requested_budget is not None and (isinstance(requested_budget, bool)
            or not isinstance(requested_budget, Integral) or requested_budget <= 0):
        raise ValueError("requested_budget must be a positive integer, or None for automatic budgeting")
    signals = dict(signals or {})
    normalized = _scope(ix, scope)
    with ix.lock:
        repo, scoped, cache_hit, version = _stats(ix, normalized)
    specificity, terms = _specificity(task, normalized, signals, scoped)
    candidates = _count(signals.get("candidate_count"))
    fanout = _count(signals.get("fanout"))
    depth = max(1, _count(signals.get("depth"), 1))
    complexity = _number(signals.get("complexity"), scoped["mean_complexity"])
    changed = _count(signals.get("changed_files"))
    schema_changes = _count(signals.get("schema_changes"))
    config_changes = _count(signals.get("config_changes"))
    relationships = _count(signals.get("relationship_count"))
    relevant_tests = _count(signals.get("test_count"))
    test_relevance = min(1.0, _number(signals.get("test_relevance")))
    risk_terms = sorted(set(terms) & _RISK_TERMS)
    risk = bool(schema_changes or config_changes or complexity >= 20 or risk_terms)
    reasons: list[str] = []
    contributions: dict[str, int] = {}

    def add(name: str, amount: float, reason: str) -> None:
        value = round(amount)
        contributions[name] = value
        if value:
            reasons.append(f"{reason} ({value:+,} tokens).")

    base = _BASE.get(mode, _BASE["task"])
    effective_files = scoped["files"] + scoped["artifact_files"] * 0.5
    size = min(1500.0, 150 * math.log2(1 + effective_files)
               + 65 * math.log2(1 + scoped["symbols"] / 20)
               + 60 * math.log2(1 + scoped["lines"] / 1000))
    add("scope_size", size, f"Indexed {'scope' if normalized else 'repository'} has {scoped['files']:,} source files and {scoped['symbols']:,} symbols; size growth is capped")
    add("specificity", -700 * specificity, "Specific targets need less discovery context")
    add("candidates", min(1400, 240 * math.log2(1 + candidates)), f"Retrieval found {candidates:,} candidates")
    add("fanout", min(1800, 300 * math.log2(1 + fanout)), f"Relevant dependency fanout is {fanout:,}")
    add("depth", min(1000, max(0, depth - 1) * 250) if fanout or relationships else 0,
        f"Requested dependency depth is {depth}")
    add("relationships", min(650, 130 * math.log2(1 + relationships)), f"{relationships:,} relevant relationship edges need evidence")
    add("complexity", min(1000, max(0, complexity - 8) * 45), f"Relevant complexity is {complexity:g}")
    add("changed_files", min(1600, 270 * math.log2(1 + changed)), f"{changed:,} changed files widen the review")
    add("schema_changes", min(1800, 1000 * math.log2(1 + schema_changes)), f"{schema_changes:,} schema changes require consumer and compatibility checks")
    add("config_changes", min(900, 500 * math.log2(1 + config_changes)), f"{config_changes:,} configuration changes require dependency evidence")
    add("risk_terms", 450 if risk_terms else 0, "Task mentions sensitive behavior: " + ", ".join(risk_terms))
    add("tests", min(550, 150 * math.log2(1 + relevant_tests)) + 250 * test_relevance,
        "Relevant tests need room for verification")
    automatic = max(AUTO_MIN, min(AUTO_MAX, int(math.ceil((base + sum(contributions.values())) / 100) * 100)))
    policy = "explicit" if requested_budget is not None else "auto"
    budget = int(requested_budget) if requested_budget is not None else automatic
    if policy == "explicit":
        reasons.insert(0, f"Explicit budget of {budget:,} tokens is respected without automatic floors or caps.")
    else:
        reasons.insert(0, f"Automatic budget is bounded to {AUTO_MIN:,}–{AUTO_MAX:,} tokens; base for {mode} is {base:,}.")
    return {"budget": budget, "policy": policy,
            "allocations": _allocations(budget, specificity, risk, test_relevance,
                                         signals.get("test_count") is not None and relevant_tests == 0, mode),
            "allocation_order": ["symbols", "relationships", "tests", "patterns", "source"],
            "reasons": reasons, "repo": repo, "scoped": scoped, "scope": normalized or None,
            "inputs": {"mode": mode, "specificity": round(specificity, 3), "terms": terms,
                       "candidate_count": candidates, "fanout": fanout, "depth": depth,
                       "complexity": complexity, "changed_files": changed, "relationship_count": relationships,
                       "schema_changes": schema_changes, "config_changes": config_changes,
                       "test_count": relevant_tests, "test_relevance": test_relevance, "risk_terms": risk_terms,
                       "index_version": version[0], "relationships_version": version[1],
                       "stats_cache_hit": cache_hit, "contributions": contributions,
                       "automatic_budget": automatic},
            "notes": ["Allocations reserve evidence space; unused space may be reassigned to relevant sections.",
                      "Estimated tokens, not a tokenizer-specific guarantee. Missing retrieval signals are unknown, not proof of no dependencies."]}
