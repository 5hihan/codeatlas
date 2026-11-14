"""PageRank over the symbol reference graph + token-budgeted repo maps.

Nodes are symbols; an edge A -> B exists when symbol A references (calls,
constructs, passes as callback, or mentions) the name of symbol B.  Names that
are defined in several places distribute the edge weight between definitions
(preferring definitions in the same file or in files the caller imports).
The resulting rank is cached in the ``symbol_rank`` table and refreshed lazily
after the index changes.

The same pass persists every resolved edge in ``symbol_edges`` (source symbol or,
for module-level code, only the source file; target symbol; kind; confidence; first
line) so that path queries (:mod:`codeatlas.connect`) can walk the graph without
resolving references again.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Iterable, Optional

from .indexer import Index

RANK_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbol_rank(symbol_id INTEGER PRIMARY KEY, rank REAL, in_deg INTEGER, out_deg INTEGER);
CREATE TABLE IF NOT EXISTS file_rank(file_id INTEGER PRIMARY KEY, rank REAL);
CREATE TABLE IF NOT EXISTS symbol_refs(symbol_id INTEGER PRIMARY KEY, ref_files INTEGER, ref_count INTEGER, confidence TEXT);
CREATE TABLE IF NOT EXISTS symbol_edges(src INTEGER, src_file INTEGER NOT NULL, dst INTEGER NOT NULL, kind TEXT,
  confidence TEXT, line INTEGER, count INTEGER);
CREATE INDEX IF NOT EXISTS idx_edges_src ON symbol_edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON symbol_edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_src_file ON symbol_edges(src_file);
"""

# ambiguous (low confidence) resolutions with more candidates than this are not useful as path evidence
EDGE_MAX_LOW_TARGETS = 4


def compute_pagerank(ix: Index, damping: float = 0.85, iters: int = 40) -> dict:
    """(Re)compute PageRank for all symbols and files. Returns stats."""
    t0 = time.time()
    c = ix.conn
    with ix.lock:
        c.executescript(RANK_SCHEMA)
        syms = c.execute("SELECT id, name, file_id, kind, exported, start_line FROM symbols").fetchall()
        if not syms:
            c.execute("DELETE FROM symbol_rank")
            c.execute("DELETE FROM file_rank")
            c.execute("DELETE FROM symbol_refs")
            c.execute("DELETE FROM symbol_edges")
            now = str(time.time())
            ix.set_meta("rank_updated", now)
            ix.set_meta("edges_version", now)
            c.commit()
            return {"symbols": 0, "edges": 0, "seconds": 0.0}
        id_index = {s["id"]: i for i, s in enumerate(syms)}
        n = len(syms)
        file_info = {r["id"]: (r["lang"], r["path"]) for r in c.execute("SELECT id, lang, path FROM files")}
        from .graph import is_test_path
        is_test = [is_test_path(file_info.get(s["file_id"], ("?", ""))[1]) for s in syms]
        by_name: dict[str, list[int]] = defaultdict(list)
        file_of = [0] * n
        for i, s in enumerate(syms):
            by_name[s["name"]].append(i)
            file_of[i] = s["file_id"]

        # weighted edges: src symbol -> {dst: weight}, resolved with the shared NameResolver
        from .resolve import NameResolver
        resolver = NameResolver(ix)
        out: list[dict[int, float]] = [dict() for _ in range(n)]
        weights = {"call": 1.0, "new": 1.0, "arg": 0.9, "ref": 0.5, "import": 0.3}
        conf_w = {"high": 1.0, "medium": 0.6, "low": 0.15}
        ref_files: dict[int, set[int]] = defaultdict(set)
        ref_count: dict[int, int] = defaultdict(int)
        best_conf: dict[int, str] = {}
        rank_order = {"high": 3, "medium": 2, "low": 1}
        sym_start = {s["id"]: s["start_line"] for s in syms}
        sym_fid = {s["id"]: s["file_id"] for s in syms}
        edge_rows: list[tuple] = []
        q = ("SELECT r.enclosing_id, r.name, r.kind, r.file_id, r.qualifier, COUNT(*) AS cnt, MIN(r.line) AS line "
             "FROM refs r GROUP BY r.enclosing_id, r.name, r.kind, r.file_id, r.qualifier")
        for enc, name, kind, fid, qualifier, cnt, line in c.execute(q):
            res = resolver.resolve(name, fid, qualifier, enc, kind)
            if not res.targets or res.confidence is None:
                continue
            # reference bookkeeping (dead-code detection, outline counts)
            for t in res.targets:
                ref_files[t].add(fid)
                ref_count[t] += cnt
                if rank_order[res.confidence] > rank_order.get(best_conf.get(t, ""), 0):
                    best_conf[t] = res.confidence
            # persisted edges for path queries: module-level code keeps only its file as the source
            if res.confidence != "low" or len(res.targets) <= EDGE_MAX_LOW_TARGETS:
                for t in res.targets:
                    if t == enc:
                        continue
                    if kind == "ref" and sym_fid.get(t) == fid and sym_start.get(t) == line:
                        continue  # the definition's own name
                    edge_rows.append((enc, fid, t, kind, res.confidence, line, cnt))
            src = id_index.get(enc) if enc is not None else None
            if src is None:
                continue
            chosen = [id_index[t] for t in res.targets if t in id_index and id_index[t] != src]
            if not chosen:
                continue
            w = weights.get(kind, 0.5) * min(cnt, 5) ** 0.5 * conf_w[res.confidence]
            if res.confidence == "low":
                w /= len(chosen)
            share = w / len(chosen)
            o = out[src]
            for t in chosen:
                o[t] = o.get(t, 0.0) + share
        c.execute("DELETE FROM symbol_refs")
        c.executemany("INSERT INTO symbol_refs(symbol_id, ref_files, ref_count, confidence) VALUES(?,?,?,?)",
                      [(s["id"], len(ref_files.get(s["id"], ())), ref_count.get(s["id"], 0), best_conf.get(s["id"]))
                       for s in syms])
        # `from m import foo` yields an import ref and identifier refs (names, module path, aliases) on the same
        # line: keep the import edges only, so a module called `resolve` never becomes a reference to `X.resolve`
        import_lines = {(e[1], e[5]) for e in edge_rows if e[3] == "import" and e[0] is None}
        edge_rows = [e for e in edge_rows if not (e[3] == "ref" and e[0] is None and (e[1], e[5]) in import_lines)]
        c.execute("DELETE FROM symbol_edges")
        c.executemany("INSERT INTO symbol_edges(src, src_file, dst, kind, confidence, line, count) VALUES(?,?,?,?,?,?,?)",
                      edge_rows)
        # file-level import edges contribute a little to top-level symbols too (keeps hubs ranked)
        in_deg = [0] * n
        out_deg = [0] * n
        for i, o in enumerate(out):
            out_deg[i] = len(o)
            for t in o:
                in_deg[t] += 1
        # PageRank power iteration
        rank = [1.0 / n] * n
        base = (1.0 - damping) / n
        # small prior for exported symbols
        prior = [(1.0 + (0.5 if s["exported"] else 0.0)) * (0.15 if is_test[i] else 1.0) for i, s in enumerate(syms)]
        psum = sum(prior)
        prior = [p / psum for p in prior]
        for _ in range(iters):
            new = [base * n * p for p in prior]  # teleport proportional to prior
            dangling = 0.0
            for i, o in enumerate(out):
                if not o:
                    dangling += rank[i]
                    continue
                tot = sum(o.values())
                ri = rank[i] * damping / tot
                for t, w in o.items():
                    new[t] += ri * w
            if dangling:
                dshare = dangling * damping
                for i in range(n):
                    new[i] += dshare * prior[i]
            rank = new
        # tests are important to know about but should not dominate a repo map
        rank = [r * (0.3 if is_test[i] else 1.0) for i, r in enumerate(rank)]
        rows = [(syms[i]["id"], rank[i], in_deg[i], out_deg[i]) for i in range(n)]
        c.execute("DELETE FROM symbol_rank")
        c.executemany("INSERT INTO symbol_rank(symbol_id, rank, in_deg, out_deg) VALUES(?,?,?,?)", rows)
        frank: dict[int, float] = defaultdict(float)
        for i in range(n):
            frank[file_of[i]] += rank[i]
        c.execute("DELETE FROM file_rank")
        c.executemany("INSERT INTO file_rank(file_id, rank) VALUES(?,?)", list(frank.items()))
        now = str(time.time())
        ix.set_meta("rank_updated", now)
        ix.set_meta("edges_version", now)
        c.commit()
    return {"symbols": n, "edges": sum(out_deg), "stored_edges": len(edge_rows), "seconds": round(time.time() - t0, 2)}


def rank_is_stale(ix: Index) -> bool:
    ru = ix.get_meta("rank_updated")
    lu = ix.get_meta("last_update")
    if ru is None:
        return True
    if ix.get_meta("edges_version") != ru:
        return True  # index built before symbol_edges existed: one recompute fills it
    if lu is None:
        return False
    try:
        return float(lu) > float(ru)
    except ValueError:
        return True


def ensure_rank(ix: Index) -> None:
    if rank_is_stale(ix):
        compute_pagerank(ix)
    else:
        ix.conn.executescript(RANK_SCHEMA)


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.6))


def repo_map(ix: Index, token_budget: Optional[int] = None, focus_paths: Optional[Iterable[str]] = None,
             focus_names: Optional[Iterable[str]] = None, exported_only: bool = False) -> str:
    """Render the symbols of the repository grouped by file, as a tree with signatures.

    Without ``token_budget`` the map is complete: every indexed file and every symbol, files ordered by
    importance (sum of PageRank, boosted when they live in / are named by the focus set). A partial map
    silently hides the code an agent then fails to account for, so nothing is cut unless a budget is
    given explicitly; with one, files are included in rank order until it is exhausted and only the top
    ranked symbols of each file are shown.
    """
    ensure_rank(ix)
    c = ix.conn
    focus_paths = [ix.rel(p) for p in (focus_paths or [])]
    focus_names = set(focus_names or [])
    full = token_budget is None
    if full:
        budget = None
        char_limit = None
        policy = "complete"
    else:
        from .budgeting import plan_context
        plan = plan_context(ix, task=" ".join(sorted(focus_names)), requested_budget=token_budget,
                            scope=focus_paths[0] if len(focus_paths) == 1 else None, mode="map")
        budget = plan["budget"]
        char_limit = max(1, int(budget * 3.6))
        policy = f"{plan['policy']} budget ~{budget} tokens"
    rows = c.execute(
        "SELECT s.id, s.name, s.kind, s.qualified, s.parent_id, s.start_line, s.end_line, s.signature, s.exported, "
        "f.path, COALESCE(r.rank, 0) AS rank, COALESCE(r.in_deg,0) AS in_deg FROM symbols s JOIN files f ON f.id=s.file_id "
        "LEFT JOIN symbol_rank r ON r.symbol_id=s.id" + (" WHERE s.exported=1" if exported_only else "")).fetchall()
    if not rows:
        return "(index is empty - run index_repo first)"[:char_limit or None]
    # boost
    scored = []
    for r in rows:
        score = r["rank"]
        if focus_paths and any(r["path"].startswith(fp.rstrip("/") + "/") or r["path"] == fp for fp in focus_paths):
            score *= 8
        if r["name"] in focus_names:
            score *= 12
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    max_score = scored[0][0] or 1.0
    # importance of a file = sum of its top symbol scores
    file_score: dict[str, float] = defaultdict(float)
    per_file: dict[str, list] = defaultdict(list)
    for sc, r in scored:
        per_file[r["path"]].append((sc, r))
        file_score[r["path"]] += sc
    ordered_files = sorted(per_file, key=lambda p: -file_score[p])
    lines: list[str] = []
    used = 0
    header = (f"# Repo map ({len(rows)} symbols in {len(per_file)} files; "
              f"{'complete, files ordered by PageRank' if full else 'showing top by PageRank; ' + policy})\n")
    used += estimate_tokens(header)
    lines.append(header.rstrip("\n"))
    shown_files = 0
    for path in ordered_files:
        syms = per_file[path]
        # how many symbols to show for this file: all of them, or proportional to importance under a budget
        k = len(syms) if full else max(3, min(24, int(2 + 30 * (file_score[path] / (file_score[ordered_files[0]] or 1)))))
        top = sorted(syms[:k], key=lambda x: x[1]["start_line"])
        # keep parents of shown children
        ids = {r["id"] for _, r in top}
        parents_needed = {r["parent_id"] for _, r in top if r["parent_id"] and r["parent_id"] not in ids}
        if parents_needed:
            for sc, r in syms:
                if r["id"] in parents_needed:
                    top.append((sc, r))
            top.sort(key=lambda x: x[1]["start_line"])
        block = [path]
        for sc, r in top:
            depth = 1 if r["parent_id"] else 0
            star = "*" if sc >= 0.2 * max_score else ""
            sig = (r["signature"] or r["name"]).replace("\n", " ")
            if len(sig) > 110:
                sig = sig[:107] + "..."
            block.append(f"{'  ' * depth}{'├─ ' if depth else ''}{sig}{star}  [{r['start_line']}]")
        text = "\n".join(block) + "\n"
        cost = estimate_tokens(text)
        if budget is not None and used + cost > budget:
            if shown_files == 0:
                # always show at least the first file, trimmed
                block = block[: max(2, int(len(block) * (budget - used) / cost))]
                lines.extend(block)
                shown_files += 1
            break
        lines.extend(block)
        used += cost
        shown_files += 1
    lines.append(f"\n({shown_files}/{len(per_file)} files shown, ~{used} tokens; * = top-ranked. "
                 f"Use file_outline / symbol for details.)")
    answer = "\n".join(lines)
    if char_limit is not None and len(answer) > char_limit:
        suffix = "\n(truncated; increase token_budget or focus the map)"
        body = answer[:max(0, char_limit - len(suffix))].rsplit("\n", 1)[0]
        answer = (body + suffix)[:char_limit]
    return answer


def top_symbols(ix: Index, limit: int = 30, kind: Optional[str] = None) -> list[dict]:
    ensure_rank(ix)
    ksql = " WHERE s.kind=?" if kind else ""
    rows = ix.conn.execute(
        "SELECT s.id, s.qualified, s.kind, f.path, s.start_line, r.rank, r.in_deg, r.out_deg FROM symbol_rank r "
        "JOIN symbols s ON s.id=r.symbol_id JOIN files f ON f.id=s.file_id" + ksql + " ORDER BY r.rank DESC LIMIT ?",
        ((kind, limit) if kind else (limit,))).fetchall()
    return [dict(r) for r in rows]


def top_files(ix: Index, limit: int = 30) -> list[dict]:
    ensure_rank(ix)
    rows = ix.conn.execute(
        "SELECT f.path, f.lines, r.rank FROM file_rank r JOIN files f ON f.id=r.file_id ORDER BY r.rank DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]
