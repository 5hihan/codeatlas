"""Bounded, evidence-bearing Git change context over the current code index."""
from __future__ import annotations

import posixpath
import re
import subprocess
from collections import defaultdict, deque
from typing import Optional

from .graph import Atlas, is_test_path
from .indexer import Index, MAX_FILE_BYTES, extract
from .languages import SPECS, lang_for_path

HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "copied", "T": "type_changed"}


def _git(repo, *args: str) -> tuple[int, bytes]:
    try:
        p = subprocess.run(["git", "-C", str(repo), "--literal-pathspecs", *args], capture_output=True, timeout=30)
        return p.returncode, p.stdout if p.returncode == 0 else p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc).encode("utf-8", "replace")


def _bound(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _revision(repo, ref: str) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(ref, str) or not ref or "\0" in ref or len(ref) > 4096:
        return None, "base_ref must be a nonempty Git commit reference"
    code, out = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    sha = out.strip().decode("ascii", "replace")
    if code or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        return None, f"invalid Git commit reference: {ref!r}"
    return sha, None


def changed_files(ix: Index, base_ref: str = "HEAD", include_untracked: bool = True, *,
                  comparison: str = "working", merge_base: bool = False, max_files: int = 200) -> dict:
    """Compare a commit with working files, the staging index, or HEAD.

    Read NUL-delimited names and parse hunks per path, never patch filenames.
    """
    if comparison not in ("working", "staged", "branch"):
        return {"error": "comparison must be working, staged, or branch"}
    max_files = _bound(max_files, 1, 2000)
    code, _ = _git(ix.repo, "rev-parse", "--is-inside-work-tree")
    if code:
        return {"error": "not a git repository"}
    base, error = _revision(ix.repo, base_ref)
    if error:
        return {"error": error}
    head, _ = _revision(ix.repo, "HEAD")
    if merge_base:
        code, out = _git(ix.repo, "merge-base", base, head or "HEAD")
        if code:
            return {"error": "no merge base between base_ref and HEAD"}
        base = out.strip().decode("ascii")
    args = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames"]
    if comparison == "staged":
        args.append("--cached")
    revisions = [base] + ([head] if comparison == "branch" and head else [])
    code, out = _git(ix.repo, *args, "--name-status", "-z", *revisions, "--")
    if code:
        return {"error": "git diff failed: " + out.decode("utf-8", "replace").strip()[:200]}
    fields = out.split(b"\0")
    files = {}
    total, at = 0, 0
    while at < len(fields) and fields[at]:
        status = fields[at].decode("ascii", "replace")
        at += 1
        old_path = fields[at].decode("utf-8", "surrogateescape")
        at += 1
        path = old_path
        if status[:1] in ("R", "C"):
            path = fields[at].decode("utf-8", "surrogateescape")
            at += 1
        total += 1
        if len(files) < max_files:
            files[path] = {"status": STATUS.get(status[:1], status), "old_path": old_path, "hunks": [], "old_hunks": []}
    limitations = []
    if include_untracked and comparison == "working":
        code, out = _git(ix.repo, "ls-files", "--others", "--exclude-standard", "-z")
        if code:
            limitations.append("Git could not enumerate untracked files.")
        else:
            for raw in out.split(b"\0"):
                if not raw:
                    continue
                path = raw.decode("utf-8", "surrogateescape")
                if path.startswith(".codeatlas/") or path in files:
                    continue
                total += 1
                if len(files) < max_files:
                    files[path] = {"status": "untracked", "old_path": None, "hunks": [], "old_hunks": []}
    for path, info in files.items():
        if info["status"] == "untracked":
            continue
        paths = list(dict.fromkeys([info["old_path"], path]))
        code, patch = _git(ix.repo, *args, "--unified=0", *revisions, "--", *paths)
        if code:
            info["hunks_incomplete"] = True
            limitations.append(f"Could not read changed line ranges for {path!r}.")
            continue
        for m in HUNK_RE.finditer(patch):
            old, old_n, new, new_n = int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)
            if old_n:
                info["old_hunks"].append((old, old + old_n - 1))
            if new_n:
                info["hunks"].append((new, new + new_n - 1))
    return {"base_ref": base_ref, "base_commit": base, "head_commit": head, "comparison": comparison,
            "merge_base": bool(merge_base), "files": files, "total_files": total,
            "truncated": total > len(files), "limitations": limitations}


def _snapshot(ix: Index, revision: Optional[str], path: str) -> Optional[bytes]:
    if revision is None:
        try:
            target = ix.repo / path
            if (target.is_symlink() or not target.resolve().is_relative_to(ix.repo.resolve())
                    or target.stat().st_size > MAX_FILE_BYTES):
                return None
            return target.read_bytes()
        except OSError:
            return None
    spec = f"{revision}:{path}" if revision else f":{path}"
    code, size = _git(ix.repo, "cat-file", "-s", spec)
    if code or not size.strip().isdigit() or int(size) > MAX_FILE_BYTES:
        return None
    code, src = _git(ix.repo, "show", spec)
    return src if code == 0 else None


def _symbols(src: Optional[bytes], path: str) -> list[dict]:
    lang = lang_for_path(path)
    if src is None or lang not in SPECS:
        return []
    return [{"name": s.name, "qualified": s.qualified, "kind": s.kind, "start_line": s.start_line,
             "end_line": s.end_line, "complexity": s.complexity} for s in extract(SPECS[lang], src).symbols]


def _overlaps(symbol, hunks) -> bool:
    return any(a <= symbol["end_line"] and b >= symbol["start_line"] for a, b in hunks)


def _deleted_import(module: str, importer: str, lang: str, target: str) -> bool:
    """Reconstruct explicit old paths; do not guess from matching symbol names."""
    directory = posixpath.dirname(importer)
    if lang == "python":
        dots = len(module) - len(module.lstrip("."))
        if dots:
            for _ in range(dots - 1):
                directory = posixpath.dirname(directory)
            stem = posixpath.join(directory, module[dots:].replace(".", "/"))
        else:
            stem = module.replace(".", "/")
        candidates = [stem + ".py", stem + ".pyi", stem + "/__init__.py"]
    elif lang in ("typescript", "javascript", "tsx") and module.startswith("."):
        stem = posixpath.normpath(posixpath.join(directory, module))
        extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
        candidates = [stem] + [stem + ext for ext in extensions] + [stem + "/index" + ext for ext in extensions]
    elif lang in ("c", "cpp"):
        candidates = [posixpath.join(directory, module), module]
    else:
        return False
    return target in {posixpath.normpath(p) for p in candidates}


def change_context(ix: Index, atlas: Atlas, base_ref: str = "HEAD", include_untracked: bool = True,
                   max_symbols: int = 60, *, depth: int = 3, max_files: int = 200,
                   max_edges: int = 1000, comparison: str = "working", merge_base: bool = False,
                   token_budget: Optional[int] = None) -> dict:
    """Return bounded potential impact, not a claim of exhaustive runtime coverage."""
    depth, max_files = _bound(depth, 0, 10), _bound(max_files, 1, 2000)
    max_symbols, max_edges = _bound(max_symbols, 1, 2000), _bound(max_edges, 1, 10000)
    ch = changed_files(ix, base_ref, include_untracked, comparison=comparison, merge_base=merge_base, max_files=max_files)
    if "error" in ch:
        return ch
    atlas.ensure_fresh()
    touched = set(ch["files"]) | {v["old_path"] for v in ch["files"].values() if v["old_path"]}
    if touched:
        ix.update(paths=sorted(touched))
    c = ix.conn
    has_artifacts = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ca_artifact_files'").fetchone() is not None
    limitations = list(ch["limitations"])
    limitations.append("Static evidence identifies potential impact; it does not prove runtime breakage or test coverage.")
    if comparison != "working":
        limitations.append("Compared source is staged/committed; dependency edges describe the current working-tree index.")
    truncation = {"changed_files"} if ch["truncated"] else set()
    changed_syms, current_ids = [], set()
    for path, info in ch["files"].items():
        row = c.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        artifact = c.execute("SELECT status FROM ca_artifact_files WHERE path=?", (path,)).fetchone() if has_artifacts else None
        info["source_indexed"] = row is not None
        info["artifact_indexed"] = bool(artifact and artifact[0] == "indexed")
        info["indexed"] = info["source_indexed"] or info["artifact_indexed"]
        fid = row[0] if row else None
        if not lang_for_path(path):
            continue
        revision = None if comparison == "working" else ("" if comparison == "staged" else ch["head_commit"])
        new_src = None if info["status"] == "deleted" else _snapshot(ix, revision, path)
        old_src = None if info["status"] in ("untracked", "added") else _snapshot(ix, ch["base_commit"], info["old_path"])
        new, old = _symbols(new_src, path), _symbols(old_src, info["old_path"] or path)
        if new_src is None and info["status"] != "deleted":
            limitations.append(f"Source {path!r} was unavailable, a symlink, or exceeded the parser size limit.")
        if old_src is None and info["status"] not in ("untracked", "added"):
            limitations.append(f"Base source {info['old_path']!r} was unavailable or exceeded the parser size limit.")
        old_changed = {(s["qualified"], s["kind"]) for s in old if _overlaps(s, info["old_hunks"])}
        new_keys = {(s["qualified"], s["kind"]) for s in new}
        selected = [(s, "after") for s in new if info["status"] in ("added", "untracked", "renamed", "copied")
                    or _overlaps(s, info["hunks"]) or (s["qualified"], s["kind"]) in old_changed]
        selected += [(s, "before") for s in old if (info["status"] == "deleted" or _overlaps(s, info["old_hunks"]))
                     and (s["qualified"], s["kind"]) not in new_keys]
        for s, side in selected:
            if len(changed_syms) >= max_symbols:
                truncation.add("changed_symbols")
                break
            matching = c.execute("SELECT id FROM symbols WHERE file_id=? AND qualified=? AND kind=? LIMIT 2",
                                 (fid, s["qualified"], s["kind"])).fetchall() if side == "after" and fid is not None else []
            sid = matching[0]["id"] if len(matching) == 1 else None
            entry = {**s, "id": sid, "path": path if side == "after" else info["old_path"],
                     "status": info["status"] if side == "after" else "deleted", "side": side,
                     "confidence": "high", "evidence": "Git changed line range and parsed definition"}
            if info["status"] == "renamed":
                entry["old_path"] = info["old_path"]
            changed_syms.append(entry)
            if sid is not None:
                current_ids.add(sid)
    impacted, edges, edge_keys, consumers = {}, [], set(), {}
    dependents, tests = defaultdict(set), defaultdict(set)
    frontier, queued_files, queued_symbols = deque(), set(), set()
    roots = set(ch["files"])
    roots.update(i["old_path"] for i in ch["files"].values() if i["status"] == "renamed")
    for path in sorted(roots):
        impacted[path] = {"path": path, "depth": 0, "confidence": "high", "reasons": ["changed"]}
        frontier.append(("file", path, 0))
        queued_files.add(path)
        if is_test_path(path) and lang_for_path(path):  # only runnable source tests, not docs under specs/
            tests[path].add("changed")
    for sid in sorted(current_ids):
        frontier.append(("symbol", sid, 0))
        queued_symbols.add(sid)
    candidate_budget = min(50000, max(100, max_edges * 8))
    candidates_seen, resolver, boundary = 0, None, set()

    def add_edge(edge, level):
        key = (edge["source_path"], edge["target_path"], edge["kind"], edge.get("line"), edge.get("target_symbol"))
        if key in edge_keys:
            return True
        if len(edges) >= max_edges:
            truncation.add("edges")
            return False
        source = edge["source_path"]
        if source not in impacted and len(impacted) >= max_files + len(roots):
            truncation.add("affected_files")
            return False
        edge_keys.add(key)
        edge["depth"] = level
        edges.append(edge)
        reason = f"{edge['kind']} {edge['target_path']}"
        if source not in impacted:
            impacted[source] = {"path": source, "depth": level, "confidence": edge.get("confidence", "medium"), "reasons": [reason]}
        elif reason not in impacted[source]["reasons"] and len(impacted[source]["reasons"]) < 6:
            impacted[source]["reasons"].append(reason)
        if is_test_path(source) and lang_for_path(source):
            tests[source].add(reason)
        if source not in queued_files:
            queued_files.add(source)
            frontier.append(("file", source, level))
        return True

    deleted_paths = {p for p in roots if not c.execute("SELECT 1 FROM files WHERE path=?", (p,)).fetchone() and lang_for_path(p)}
    deleted_incoming = defaultdict(list)
    if deleted_paths and depth:
        rows = c.execute("SELECT f.path,f.lang,i.module,i.line,i.raw FROM imports i JOIN files f ON f.id=i.file_id "
                         "WHERE i.resolved_file_id IS NULL ORDER BY f.path,i.line LIMIT ?", (candidate_budget + 1,)).fetchall()
        if len(rows) > candidate_budget:
            truncation.add("deleted_import_candidates")
        candidates_seen += min(len(rows), candidate_budget)
        for r in rows[:candidate_budget]:
            for path in deleted_paths:
                if _deleted_import(r["module"] or "", r["path"], r["lang"], path):
                    deleted_incoming[path].append({"source_path": r["path"], "target_path": path, "kind": "import",
                                                  "line": r["line"], "evidence": r["raw"], "confidence": "medium",
                                                  "resolved": False, "historical_target": True})
        limitations.append("Deleted-target imports use explicit path reconstruction; historical call edges are unavailable.")
    try:
        from .relationships import relationships
    except ImportError:
        relationships = None
        limitations.append("Artifact relationship indexing is unavailable.")
    while frontier and len(edges) < max_edges:
        kind, target, level = frontier.popleft()
        if level >= depth:
            boundary.add(str(target))
            continue
        remaining = max_edges - len(edges)
        if kind == "file":
            path = str(target)
            incoming = [dict(r) for r in c.execute(
                "SELECT f.path AS source_path,target.path AS target_path,'import' AS kind,i.line,i.raw AS evidence "
                "FROM files target JOIN imports i ON i.resolved_file_id=target.id JOIN files f ON f.id=i.file_id "
                "WHERE target.path=? ORDER BY f.path,i.line LIMIT ?", (path, remaining + 1))]
            incoming += deleted_incoming.get(path, [])
            if len(incoming) > remaining:
                truncation.add("edges")
            for edge in incoming[:remaining]:
                edge.setdefault("confidence", "high")
                edge.setdefault("resolved", True)
                if add_edge(edge, level + 1):
                    dependents[edge["source_path"]].add(path)
            remaining = max_edges - len(edges)
            if relationships is not None and remaining:
                offset = 0
                while remaining:
                    allowance = candidate_budget - candidates_seen
                    if allowance <= 0:
                        truncation.add("relationship_candidates")
                        break
                    result = relationships(ix, path=path, direction="in", limit=min(200, remaining, allowance), offset=offset)
                    candidates_seen += len(result.get("items", []))
                    for edge in result.get("items", []):
                        add_edge(dict(edge), level + 1)
                    next_offset = result.get("next_offset")
                    if not result.get("truncated") or next_offset is None or next_offset <= offset:
                        break
                    remaining = max_edges - len(edges)
                    if not remaining:
                        truncation.add("relationships")
                        break
                    offset = next_offset
        else:
            sid = int(target)
            symbol = c.execute("SELECT s.name,s.qualified,s.file_id,s.start_line,f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.id=?", (sid,)).fetchone()
            if not symbol:
                continue
            if resolver is None:
                resolver = atlas.resolver()
            aliases = resolver.alias_rev.get((symbol["name"], symbol["file_id"]), [])
            if len(aliases) > candidate_budget:
                truncation.add("reference_candidates")
            queries = [(symbol["name"], None)] + [(local, fid) for fid, local in aliases[:candidate_budget]]
            for name, alias_file in queries:
                allowance = candidate_budget - candidates_seen
                if allowance <= 0:
                    truncation.add("reference_candidates")
                    break
                params, condition = [name], ""
                if alias_file is not None:
                    condition = " AND r.file_id=?"
                    params.append(alias_file)
                rows = c.execute("SELECT r.*,f.path,s.qualified FROM refs r JOIN files f ON f.id=r.file_id "
                                 "LEFT JOIN symbols s ON s.id=r.enclosing_id WHERE r.name=?" + condition +
                                 " ORDER BY f.path,r.line,r.id LIMIT ?", (*params, allowance + 1)).fetchall()
                if len(rows) > allowance:
                    truncation.add("reference_candidates")
                for r in rows[:allowance]:
                    candidates_seen += 1
                    if r["enclosing_id"] == sid or (r["kind"] == "ref" and r["file_id"] == symbol["file_id"] and r["line"] == symbol["start_line"]):
                        continue
                    resolution = resolver.resolve(r["name"], r["file_id"], r["qualifier"], r["enclosing_id"], r["kind"])
                    if sid not in resolution.targets or resolution.confidence not in ("high", "medium"):
                        continue
                    edge = {"source_path": r["path"], "target_path": symbol["path"], "kind": r["kind"], "line": r["line"],
                            "evidence": f"{r['name']} via {resolution.how}", "confidence": resolution.confidence,
                            "resolved": True, "ambiguous": len(resolution.targets) > 1,
                            "source_symbol": r["qualified"] or "<top-level>", "target_symbol": symbol["qualified"]}
                    if not add_edge(edge, level + 1):
                        continue
                    if r["enclosing_id"] not in current_ids and r["kind"] in ("call", "new", "arg"):
                        key = (r["enclosing_id"], r["path"], r["line"] if r["enclosing_id"] is None else None)
                        item = consumers.setdefault(key, {"id": r["enclosing_id"], "qualified": r["qualified"] or "<top-level>",
                                                          "path": r["path"], "line": r["line"], "uses": set(),
                                                          "confidence": resolution.confidence, "depth": level + 1})
                        item["uses"].add(symbol["qualified"])
                    caller = r["enclosing_id"]
                    if caller is not None and caller not in queued_symbols:
                        if len(queued_symbols) >= max_symbols + len(current_ids):
                            truncation.add("affected_symbols")
                        else:
                            queued_symbols.add(caller)
                            frontier.append(("symbol", caller, level + 1))
                    if len(edges) >= max_edges:
                        break
                if len(edges) >= max_edges:
                    break
    if frontier:
        truncation.add("edges")
    if boundary:
        truncation.add("depth")
    if any(e.get("ambiguous") for e in edges):
        limitations.append("Some references have multiple candidate targets; inspect their ambiguous evidence.")
    limitations.append("Dynamic dispatch, runtime configuration and unresolved references may add consumers outside this graph.")
    from .budgeting import plan_context
    schema_changes = sum(p.endswith((".proto", ".graphql", ".gql", ".sql", ".avsc", ".xsd", ".schema.json"))
                         or posixpath.basename(p) == "schema.json" for p in roots)
    config_changes = sum(p.endswith((".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf")) for p in roots) - sum(
        p.endswith(".schema.json") or posixpath.basename(p) == "schema.json" for p in roots)
    budget_plan = plan_context(ix, requested_budget=token_budget, mode="diff", signals={
        "changed_files": ch["total_files"], "fanout": max(0, len(impacted) - len(roots)),
        "relationship_count": len(edges), "schema_changes": schema_changes, "config_changes": config_changes,
        "test_count": len(tests), "depth": depth, "complexity": max((s["complexity"] for s in changed_syms), default=0)})
    return {"base_ref": base_ref, "base_commit": ch["base_commit"], "head_commit": ch["head_commit"],
            "comparison": comparison, "merge_base": bool(merge_base), "total_files": ch["total_files"],
            "files": [{"path": p, **{k: v for k, v in i.items() if k not in ("hunks", "old_hunks")},
                       "hunks": len(i["hunks"]), "old_hunks": len(i["old_hunks"])} for p, i in ch["files"].items()],
            "changed_symbols": changed_syms,
            "consumers": sorted(({**v, "uses": sorted(v["uses"])} for v in consumers.values()), key=lambda x: (x["depth"], x["path"], x["line"])),
            "dependents": [{"path": p, "imports": sorted(s), "depth": impacted[p]["depth"], "confidence": impacted[p]["confidence"]}
                           for p, s in sorted(dependents.items()) if not is_test_path(p)],
            "tests": [{"path": p, "reasons": sorted(r)[:6], "depth": impacted[p]["depth"], "confidence": impacted[p]["confidence"]}
                      for p, r in sorted(tests.items())],
            "blast_radius": {"files": sorted(impacted.values(), key=lambda x: (x["depth"], x["path"])), "edges": edges, "depth": depth},
            "evidence": edges, "truncated": bool(truncation), "truncation_reasons": sorted(truncation),
            "limits": {"max_files": max_files, "max_symbols": max_symbols, "max_edges": max_edges, "depth": depth},
            "limitations": list(dict.fromkeys(limitations)), "token_budget": budget_plan["budget"], "budget_plan": budget_plan,
            "index_version": ix.get_meta("last_update"), "relationships_version": ix.get_meta("relationships_updated")}


def _risk(d: dict) -> tuple[str, list[str]]:
    consumers = d.get("consumers", [])
    direct = [c for c in consumers if c.get("depth", 1) <= 1]
    deeper = [c for c in consumers if c.get("depth", 1) > 1]
    tests = d.get("tests", [])
    cfg = [f for f in d["files"] if f.get("category") in ("config", "schema") or
           f["path"].endswith((".sql", ".prisma", ".graphql", ".proto", ".env", ".yaml", ".yml", ".toml"))]
    reasons = []
    score = 0
    if len(direct) > 30:
        score += 2; reasons.append(f"{len(direct)} direct consumers")
    elif len(direct) > 8:
        score += 1; reasons.append(f"{len(direct)} direct consumers")
    if deeper:
        score += 1; reasons.append(f"{len(deeper)} consumers at depth 2+")
    if cfg:
        score += 1; reasons.append(f"{len(cfg)} config/schema file(s) changed")
    if not tests:
        score += 1; reasons.append("no tests reference the changed code")
    if d.get("truncated"):
        reasons.append("traversal capped")
    level = "high" if score >= 3 else ("medium" if score >= 1 else "low")
    return level, reasons


def _group_tests(tests: list[dict], limit: int) -> list[str]:
    lines = []
    ordered = sorted(tests, key=lambda t: (t.get("depth", 1), t.get("confidence") != "high", t["path"]))
    for t in ordered[:limit]:
        why = ", ".join(t.get("reasons", [])[:2])
        why = re.sub(r"[A-Za-z0-9_./-]+/", "", why)  # strip directories from evidence paths: the test path is enough
        lines.append(f"  {t['path']}  d{t.get('depth', 1)} {t.get('confidence', 'medium')}: {why[:70]}")
    if len(tests) > limit:
        lines.append(f"  +{len(tests) - limit} more (raise max_symbols / query impact per symbol)")
    return lines


def render(d: dict, token_budget: Optional[int] = None) -> str:
    """Signal first: risk, tests, symbol-level consumers, then changed symbols and files, all capped."""
    if "error" in d:
        return str(d["error"])
    budget = token_budget if token_budget is not None else d.get("token_budget", 3500)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError("token_budget must be a positive integer")
    cap = max(1, int(budget * 3.6))
    policy = "explicit" if token_budget is not None else d.get("budget_plan", {}).get("policy", "auto")
    scale = max(0.5, min(2.0, budget / 3500))  # caps grow with the budget, never with the diff
    n_tests, n_cons, n_syms, n_files, n_deps = (int(x * scale) for x in (14, 18, 24, 14, 8))
    level, reasons = _risk(d)
    files = d["files"]
    syms = d["changed_symbols"]
    consumers = d.get("consumers", [])
    dependents = d.get("dependents", [])
    tests = d.get("tests", [])
    L = [f"Changes vs {d['base_ref']!r} ({d.get('comparison', 'working')}): {len(files)}/{d.get('total_files', len(files))} files, "
         f"{len(syms)} symbols; base {str(d.get('base_commit', ''))[:10]}; budget {budget} ({policy}); index {d.get('index_version')}",
         f"Risk: {level}" + (f" ({'; '.join(reasons)})" if reasons else " (small, tested surface)")]
    if d.get("truncated"):
        L.append("Traversal capped: " + ", ".join(d.get("truncation_reasons", []))[:120])
    # tests
    if tests:
        L.append(f"Tests to run ({len(tests)}):")
        L += _group_tests(tests, n_tests)
    else:
        L.append("Tests to run: none reference the changed code (write one, or run the package suite)")
    # symbol-level consumers grouped per changed symbol
    if consumers:
        by_target: dict[str, list[dict]] = {}
        for c in consumers:
            for u in c.get("uses", []) or ["?"]:
                by_target.setdefault(u, []).append(c)
        L.append(f"Consumers to re-check ({len(consumers)} symbols, {len(by_target)} changed targets):")
        shown = 0
        for target, cs in sorted(by_target.items(), key=lambda kv: -len(kv[1])):
            if shown >= n_cons:
                L.append(f"  +{len(by_target) - shown} more targets")
                break
            names = []
            for c in cs[:5]:
                q = c.get("qualified") or c.get("path", "?")
                names.append(f"{q} {c['path'].rsplit('/', 1)[-1]}:{c.get('line', 0)}" + (" (d2+)" if c.get("depth", 1) > 1 else ""))
            L.append(f"  {target} <- " + "; ".join(names) + (f" (+{len(cs) - 5})" if len(cs) > 5 else ""))
            shown += 1
    elif syms:
        L.append("Consumers: no resolved symbol-level consumers (new or private code, or dynamic dispatch)")
    if dependents:
        L.append(f"Files importing changed files ({len(dependents)}): " +
                 ", ".join(x["path"] for x in dependents[:n_deps]) + (f" +{len(dependents) - n_deps} more" if len(dependents) > n_deps else ""))
    # changed symbols (non-test first)
    if syms:
        ordered = sorted(syms, key=lambda s_: (is_test_path(s_["path"]), s_["path"], s_["start_line"]))
        L.append(f"Changed symbols ({len(syms)}):")
        L += [f"  {s_['status'][:3]} {s_['qualified']}  {s_['path']}:{s_['start_line']}-{s_['end_line']}" for s_ in ordered[:n_syms]]
        if len(syms) > n_syms:
            L.append(f"  +{len(syms) - n_syms} more")
    # changed files: only the ones without symbols listed above (config, docs, assets) + a count
    sym_files = {s_["path"] for s_ in syms}
    other = [f for f in files if f["path"] not in sym_files]
    L.append(f"Changed files: {len(files)} ({len(sym_files)} with symbols above" + (f", {len(other)} other)" if other else ")"))
    L += [f"  {f['status']} {f['path']}" + (f" <- {f['old_path']}" if f.get("old_path") and f["old_path"] != f["path"] else "")
          for f in other[:n_files]]
    if len(other) > n_files:
        L.append(f"  +{len(other) - n_files} more")
    if d.get("limitations"):
        L.append("Note: " + "; ".join(d["limitations"][:2])[:200])
    text = "\n".join(L)
    if len(text) > cap:
        note = f"\n(truncated at {budget} tokens; raise token_budget or query impact per symbol)"
        if cap <= len(note) + 40:
            return "truncated"[:cap]
        text = text[:cap - len(note)].rsplit("\n", 1)[0] + note
    return text
