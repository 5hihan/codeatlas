"""task_context: everything an agent needs for one task, inside a strict token budget.

Pipeline
  1. terms from the task text (CamelCase / snake_case split, stop words removed)
  2. candidate symbols by BM25 over an FTS5 index of names, signatures and docs (scope-filtered)
  3. boost by PageRank, scope, exported-ness; penalise tests
  4. expand one hop through the resolved graph (callers / callees) with a discount
  5. tests that reference the selected symbols, config/docs files mentioning the terms
  6. assemble under the budget: signatures for everything, source bodies for the top items,
     de-duplicated (no child inside an already-included parent), each with a one-line reason and an
     expansion handle (a tool call that gets more)
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from typing import Optional

from .graph import Atlas, is_test_path
from .indexer import Index
from .rank import ensure_rank, estimate_tokens
from .budgeting import plan_context

STOP = set("""a an the and or of to in on for with from by as is are was were be been being this that these those it its
into onto over under at about after before between during without within fix fixing fixed bug bugs issue issues
error errors add adding added remove removing removed update updating updated change changing changed make making
made implement implementing support supporting handle handling when where which what who how why should would could
can cannot not no yes do does did done doing use using used need needs needed want wants wanted new old all any some
so if then else than too very just also only more most less least code file files function functions method methods
class classes module modules please pls we our you your they their them there here""".split())

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(symbol_id UNINDEXED, name, words, signature, doc, path,
  pathwords, tokenize='unicode61');
"""

DOC_EXT = (".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".env", ".ini", ".cfg", ".sql", ".graphql", ".proto")


def split_words(s: str) -> list[str]:
    parts = re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", s)
    return [p.lower() for p in parts if p]


def terms_from(task: str) -> list[str]:
    seen: list[str] = []
    for w in split_words(task):
        if len(w) >= 3 and w not in STOP and w not in seen:
            seen.append(w)
    # also keep identifiers verbatim (refreshToken) so exact names match strongly
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", task):
        if any(c.isupper() for c in ident[1:]) or "_" in ident:
            if ident.lower() not in seen:
                seen.append(ident.lower())
    return seen[:24]


def ensure_fts(ix: Index) -> None:
    """Build/refresh the FTS index when the code index changed (cheap relative to parsing)."""
    c = ix.conn
    with ix.lock:
        c.executescript(FTS_SCHEMA)
        ver = ix.get_meta("last_update") or "0"
        if ix.get_meta("fts_version") == ver:
            return
        cols = {r[1] for r in c.execute("PRAGMA table_info(symbols_fts)")}
        if "pathwords" not in cols:
            c.execute("DROP TABLE IF EXISTS symbols_fts")
            c.executescript(FTS_SCHEMA)
        c.execute("DELETE FROM symbols_fts")
        rows = c.execute("SELECT s.id, s.name, s.qualified, s.signature, s.doc, f.path FROM symbols s "
                         "JOIN files f ON f.id=s.file_id").fetchall()
        c.executemany("INSERT INTO symbols_fts(symbol_id, name, words, signature, doc, path, pathwords) "
                      "VALUES(?,?,?,?,?,?,?)",
                      [(r[0], r[1], " ".join(split_words(r[2])), (r[3] or "")[:300], (r[4] or "")[:400], r[5],
                        " ".join(split_words(r[5].rsplit('/', 1)[-1]))) for r in rows])
        ix.set_meta("fts_version", ver)
        c.commit()


_PATTERN_HINTS = [
    (r"error handling|exception|try/catch|\btry\b|\bcatch\b|\bthrow|\braise\b|\bpanic", "error_handling"),
    (r"\basync|\bawait|\bpromise|concurren|coroutine", "async_await"),
    (r"\bclosure", "closures"),
    (r"\bdecorator|\bannotation", "decorators"),
    (r"\bgeneric|type parameter|\btemplate\b", "generics"),
    (r"\bgenerator|\byield\b|\biterator", "generators"),
    (r"context manager|\bwith statement|\busing\b.*\bdispos", "context_managers"),
    (r"pattern match|\bmatch statement|\bswitch\b", "pattern_matching"),
    (r"\binterface|\btrait\b|\bprotocol\b", "interfaces_traits"),
    (r"\binherit|\bsubclass|\bextends\b|base class", "inheritance"),
    (r"comprehension", "comprehensions"),
    (r"goroutine|\bchannel", "goroutines_channels"),
]


def _pattern_hints(task: str) -> list[str]:
    return [pat for rx, pat in _PATTERN_HINTS if re.search(rx, task, re.I)]


def _fts_query(terms: list[str]) -> str:
    parts = []
    for t in terms:
        t = re.sub(r"[^a-z0-9_]", "", t)
        if not t:
            continue
        parts.append(f'"{t}"' if len(t) < 4 else f'("{t}" OR {t}*)')
    return " OR ".join(parts)


def task_context(ix: Index, atlas: Atlas, task: str, scope: Optional[str] = None, token_budget: Optional[int] = None,
                 include_source: bool = True, max_symbols: int = 40, boost: Optional[set[str]] = None) -> dict:
    t0 = time.time()
    ensure_rank(ix)
    ensure_fts(ix)
    c = ix.conn
    terms = terms_from(task)
    scope = ix.rel(scope).rstrip("/") if scope and scope.strip() != "." else ""
    initial_plan = plan_context(ix, task=task, scope=scope, requested_budget=token_budget)
    result: dict = {"task": task, "terms": terms, "scope": scope or None, "index_version": ix.get_meta("last_update"),
                    "budget": initial_plan["budget"], "budget_plan": initial_plan,
                    "items": [], "tests": [], "docs": [], "relationships": [],
                    "relationships_version": ix.get_meta("relationships_updated"),
                    "truncated": False, "notes": []}
    if not terms:
        result["notes"].append("no searchable terms in the task text")
        return result

    # ---- 1. lexical candidates -------------------------------------------------------
    scores: dict[int, float] = defaultdict(float)
    reasons: dict[int, list[str]] = defaultdict(list)
    try:
        scope_sql = " AND (path=? OR substr(path,1,?)=?)" if scope else ""
        scope_args = (scope, len(scope) + 1, scope + "/") if scope else ()
        rows = c.execute("SELECT symbol_id, bm25(symbols_fts, 0.0, 4.0, 2.5, 1.2, 1.2, 0.0, 0.4) AS s, path, "
                         "name, words, signature, doc FROM symbols_fts WHERE symbols_fts MATCH ?" + scope_sql + " ORDER BY s LIMIT 400",
                         (_fts_query(terms), *scope_args)).fetchall()
    except Exception as e:  # malformed query
        rows = []
        result["notes"].append(f"text search failed: {e}")
    rank_rows = {r[0]: r[1] for r in c.execute("SELECT symbol_id, rank FROM symbol_rank")}
    max_rank = max(rank_rows.values(), default=1.0) or 1.0
    exact = {t for t in terms}
    for sid, bm, path, nm, words, sig, doc in rows:
        s = -bm  # bm25: smaller is better (negative)
        s *= 0.4 if is_test_path(path) else 1.0
        # coverage: how many distinct task terms appear in the symbol's own text (name/signature/doc)
        own = f"{nm} {words} {sig} {doc}".lower()
        covered = sum(1 for t in terms if t in own)
        s *= 1.0 + 0.6 * covered
        scores[sid] += s
        reasons[sid].append(f"matches {covered} task term(s)" if covered else "matches file name")
    # exact-name matches get a strong boost
    for t in exact:
        for r in c.execute("SELECT s.id, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE lower(s.name)=?" +
                           (" AND (f.path=? OR substr(f.path,1,?)=?)" if scope else "") + " LIMIT 20", (t, *scope_args)):
            scores[r[0]] += 8.0
            if "exact name match" not in reasons[r[0]]:
                reasons[r[0]].append("exact name match")
    # A task may name a schema/config rather than a symbol. Resolve explicit consumers
    # from the persisted relationship index instead of guessing from matching names.
    from .relationships import relationships
    related_paths = set()
    for term in terms[:8]:
        for row in c.execute("SELECT path FROM ca_artifact_files WHERE path LIKE ? ESCAPE '\\' LIMIT 12",
                             ("%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)):
            related_paths.add(row[0])
    # Entity names (DATABASE_URL, invoices) may not occur in their declaring path.
    for term in terms:
        for row in c.execute("SELECT DISTINCT path FROM ca_artifact_entities WHERE role='definition' "
                             "AND (lower(name)=? OR lower(bare_name)=?) LIMIT 12", (term, term)):
            related_paths.add(row[0])
    for dependency in sorted(related_paths)[:12]:
        for edge in relationships(ix, path=dependency, direction="in", limit=8)["items"]:
            if not edge["resolved"]:
                continue
            for row in c.execute("SELECT s.id,f.path FROM symbols s JOIN files f ON f.id=s.file_id "
                                 "WHERE f.path=? AND s.parent_id IS NULL LIMIT 8", (edge["source_path"],)):
                if scope and not (row[1] == scope or row[1].startswith(scope.rstrip("/") + "/")):
                    continue
                scores[row[0]] += 4.0
                reasons[row[0]].append(f"{edge['kind']} {dependency}")
    # pattern hints: "error handling", "async", "closures" ... map to stored pattern evidence
    hints = _pattern_hints(task)
    if hints:
        scope_sql, scope_args = ("AND f.path LIKE ?", (scope + "%",)) if scope else ("", ())
        for pat in hints:
            try:
                rows = c.execute(
                    "SELECT DISTINCT s.id, f.path FROM code_patterns p JOIN files f ON f.id=p.file_id "
                    "JOIN symbols s ON s.file_id=p.file_id AND p.line BETWEEN s.start_line AND s.end_line "
                    "WHERE p.pattern=? AND s.kind IN ('function','method','class') " + scope_sql + " LIMIT 400",
                    (pat, *scope_args)).fetchall()
            except Exception:
                rows = []
            for sid, path in rows:
                if is_test_path(path):
                    continue
                if sid in scores:
                    scores[sid] *= 1.5
                else:
                    scores[sid] += 2.0  # weak seed: the pattern alone, ranked by PageRank below
                reasons[sid].append(f"uses {pat.replace('_', ' ')}")
    if re.search(r"\bentry ?point|\bmain\b|\bstartup\b|\bbootstrap", task, re.I):
        from .graph import ENTRY_NAMES
        for r in c.execute("SELECT s.id, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name IN (%s) "
                           "AND s.parent_id IS NULL LIMIT 20" % ",".join("?" * len(ENTRY_NAMES)), tuple(ENTRY_NAMES)):
            if scope and not r[1].startswith(scope):
                continue
            scores[r[0]] += 6.0
            reasons[r[0]].append("entry point")
    if not scores:
        result["notes"].append("no symbols matched; try broader terms or a different scope")
    # rank boost (+ hot graph: recently changed / active symbols)
    for sid in list(scores):
        scores[sid] *= 1.0 + 2.0 * (rank_rows.get(sid, 0.0) / max_rank) ** 0.5
        if boost:
            s = atlas.symbol_by_id(sid)
            if s and s["qualified"] in boost:
                scores[sid] *= 1.4
                reasons[sid].append("recently changed (hot)")

    # ---- 2. graph expansion --------------------------------------------------------
    lexical_ids = set(scores)
    seeds = sorted(scores, key=lambda s: -scores[s])[:10]
    for sid in seeds:
        base = scores[sid]
        for cl in atlas.callers(sid, limit=8):
            if cl["id"] and cl["id"] not in scores:
                scores[cl["id"]] += base * 0.45
                reasons[cl["id"]].append(f"calls {atlas.symbol_by_id(sid)['qualified']}")
        for ce in atlas.callees(sid, limit=12):
            t = ce.get("target")
            if t and t.get("id") and t["id"] not in scores:
                scores[t["id"]] += base * 0.35
                reasons[t["id"]].append(f"called by {atlas.symbol_by_id(sid)['qualified']}")

    # ---- 3. tests & docs -------------------------------------------------------------
    top = sorted(scores, key=lambda s: -scores[s])[:max_symbols]
    names = {atlas.symbol_by_id(s)["name"] for s in top[:15] if atlas.symbol_by_id(s)}
    tests: dict[str, set[int]] = defaultdict(set)
    if names:
        q = "SELECT f.path, r.line FROM refs r JOIN files f ON f.id=r.file_id WHERE r.name IN (%s)" % ",".join("?" * len(names))
        for path, line in c.execute(q, tuple(names)):
            if is_test_path(path):
                tests[path].add(line)
    result["tests"] = [{"path": p, "lines": sorted(ls)[:8], "hits": len(ls)} for p, ls in
                       sorted(tests.items(), key=lambda kv: -len(kv[1]))[:8]]
    result["docs"] = _docs_mentioning(ix, terms[:8], scope, limit=6)

    # Reserve dependency evidence and tests before adding optional source bodies.
    selected = {sid: atlas.symbol_by_id(sid) for sid in top}
    seen_edges = set()
    for path in dict.fromkeys([s["path"] for s in selected.values() if s][:8] + sorted(related_paths)[:8]):
        for edge in relationships(ix, path=path, limit=8)["items"]:
            key = (edge["source_path"], edge["target_path"], edge["kind"])
            if key not in seen_edges and len(result["relationships"]) < 16:
                seen_edges.add(key)
                result["relationships"].append(edge)
    plan = plan_context(ix, task=task, scope=scope, requested_budget=token_budget, signals={
        "candidate_count": len(scores), "exact_match": any("exact name match" in r for r in reasons.values()),
        "fanout": len(set(scores) - lexical_ids), "relationship_count": len(seen_edges),
        "complexity": max((s.get("complexity", 0) or 0 for s in selected.values() if s), default=0),
        "test_count": len(tests), "test_relevance": 1.0 if tests else 0.0})
    result["budget_plan"], result["budget"] = plan, plan["budget"]
    allocations = plan["allocations"]
    used = 0
    source_used = 0
    items = []
    included_spans: list[tuple[str, int, int]] = []
    for sid in top:
        s = selected[sid]
        if not s:
            continue
        # skip children whose parent is already included with source
        if any(p == s["path"] and a <= s["start_line"] and s["end_line"] <= b for p, a, b in included_spans):
            continue
        why = ", ".join(reasons[sid][:2])
        header = f"{s['kind']} {s['qualified']}  {s['path']}:{s['start_line']}-{s['end_line']}  [{why}]"
        sig = (s["signature"] or "")[:200]
        doc = (s["doc"] or "")[:200]
        cost = estimate_tokens(header + sig + doc) + 4
        if used + cost > allocations["symbols"]:
            result["truncated"] = True
            break
        item = {"id": sid, "qualified": s["qualified"], "kind": s["kind"], "path": s["path"],
                "start_line": s["start_line"], "end_line": s["end_line"], "why": why, "signature": sig, "doc": doc,
                "score": round(scores[sid], 2), "source": None,
                "expand": f"symbol_source(\"{s['qualified']}\", path=\"{s['path']}\")"}
        used += cost
        # source bodies for the strongest items while budget allows (cap 120 lines each)
        if include_source and len(items) < 12 and s["lines"] <= 160 and source_used < allocations["source"]:
            src = atlas.source(s["qualified"], s["path"], max_lines=120)
            if src.get("found"):
                scost = estimate_tokens(src["text"])
                if source_used + scost <= allocations["source"]:
                    item["source"] = src["text"]
                    source_used += scost
                    included_spans.append((s["path"], s["start_line"], s["end_line"]))
        items.append(item)
        item["patterns"] = atlas.programming_patterns(s["path"], limit=4,
            start_line=s["start_line"], end_line=s["end_line"])["items"]
    result["items"] = items
    result["tokens_used"] = used + source_used
    result["symbols_considered"] = len(scores)
    result["seconds"] = round(time.time() - t0, 2)
    return result


def _docs_mentioning(ix: Index, terms: list[str], scope: str, limit: int = 6) -> list[dict]:
    """Config/doc files (not in the code index) whose text mentions the task terms."""
    if not terms:
        return []
    from .indexer import DEFAULT_IGNORE_DIRS
    rx = re.compile("|".join(re.escape(t) for t in terms), re.I)
    hits: list[tuple[int, str, list[str]]] = []
    root = ix.repo
    walked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        if scope and not (rel_dir.startswith(scope) or scope.startswith(rel_dir)):
            continue
        for fn in filenames:
            if fn == ".env" or fn.startswith(".env."):
                continue  # env definitions are retrieved as keys via relationships, never as value snippets
            if not fn.lower().endswith(DOC_EXT) or fn in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
                continue
            p = os.path.join(dirpath, fn)
            walked += 1
            if walked > 1500:
                break
            try:
                if os.path.getsize(p) > 200_000:
                    continue
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            snippets = []
            n = 0
            for i, ln in enumerate(lines, 1):
                if rx.search(ln):
                    n += 1
                    if len(snippets) < 3:
                        snippets.append(f"{i}: {ln.strip()[:120]}")
            if n:
                hits.append((n, os.path.relpath(p, root).replace("\\", "/"), snippets))
    hits.sort(key=lambda h: -h[0])
    return [{"path": p, "hits": n, "snippets": s} for n, p, s in hits[:limit]]


def render(r: dict) -> str:
    """Pack evidence first, with bounded optional explanations and source bodies."""
    budget = r["budget"]
    limit = max(1, int(budget * 3.6))
    plan = r.get("budget_plan", {})
    allocations = plan.get("allocations", {})
    policy = plan.get("policy", "explicit")
    footer = (f"\nindex {r.get('index_version')}; relationships {r.get('relationships_version')}; "
              f"{policy} budget ~{budget} tokens")
    omitted = "\n(truncated: narrow scope or increase token_budget)"
    # Reserve the limit notice so optional bodies cannot erase expansion handles.
    room = max(0, limit - len(footer) - len(omitted))
    lines = []
    used = 0
    truncated = r["truncated"]

    def add(block: str, remaining: int | None = None) -> int:
        nonlocal used, truncated
        cost = len(block) + 1
        if used + cost > room or (remaining is not None and cost > remaining):
            truncated = True
            return 0
        lines.append(block)
        used += cost
        return cost

    add(f"# Context for: {r['task']}")
    add(f"terms: {', '.join(r['terms'])}" + (f"   scope: {r['scope']}" if r.get("scope") else ""))
    if plan:
        stats, inputs = plan["scoped"], plan["inputs"]
        add(f"budget: {policy} ~{budget} tokens; scope {stats['files']} files; "
            f"{inputs['candidate_count']} candidates; fanout {inputs['fanout']}")
    for note in r.get("notes", []):
        add(f"note: {note}")
    # Signatures and expansion handles remain visible even when bodies do not fit.
    if r["items"]:
        add(f"\n## Relevant symbols ({len(r['items'])} of {r.get('symbols_considered', 0)} considered)")
        for it in r["items"]:
            block = f"{it['kind']} {it['qualified']}  {it['path']}:{it['start_line']}-{it['end_line']}  [{it['why']}]"
            if it["signature"]:
                block += f"\n  {it['signature']}"
            if it["doc"]:
                block += f"\n  doc: {it['doc']}"
            block += f"\n  expand: {it['expand']}"
            add(block)
    if r.get("relationships"):
        remaining = int(allocations.get("relationships", budget) * 3.6)
        add("\n## Code / config / schema dependencies")
        for e in r["relationships"]:
            remaining -= add(f"  {e['source_path']}:{e['line']} --{e['kind']}--> {e['target_path']}"
                             + (" (unresolved)" if not e['resolved'] else ""), remaining)
    if r["tests"]:
        remaining = int(allocations.get("tests", budget) * 3.6)
        add("\n## Tests touching these symbols")
        for t in r["tests"]:
            remaining -= add(f"  {t['path']}  ({t['hits']} refs, lines {', '.join(map(str, t['lines']))})", remaining)
    remaining = int(allocations.get("patterns", budget) * 3.6)
    seen_patterns = set()
    for it in r["items"]:
        for p in it.get("patterns", []):
            key = (p["pattern"], it["path"])
            if key not in seen_patterns:
                remaining -= add(f"  {p['title']} {it['path']}:{p['line']} — {p['explanation']}", remaining)
                seen_patterns.add(key)
    remaining = int(allocations.get("source", budget) * 3.6)
    for it in r["items"]:
        if it["source"]:
            remaining -= add(f"\n## Source: {it['path']}:{it['start_line']}\n{it['source']}", remaining)
    if r["docs"]:
        add("\n## Config / docs mentioning the terms")
        for d in r["docs"]:
            add(f"  {d['path']}  ({d['hits']} hits)\n" + "\n".join(f"    {s}" for s in d["snippets"]))
    answer = ("\n".join(lines) + (omitted if truncated else "") + footer)[:limit]
    r["truncated"] = truncated
    r["tokens_used"] = estimate_tokens(answer)
    return answer
