"""SNIPE: Scan, Narrow, Infer, Patch, Evaluate.

The loop that replaces task bureaucracy:

    SCAN      event-driven: every index delta (removed/changed symbols, broken imports, parse errors, new
              FIXME tags) and every reported failure (tests, typecheck, lint, runtime) becomes a candidate
    NARROW    reduce an issue to the smallest symbol graph: seeds from the evidence -> callee chains ->
              intersection -> ranked suspects
    INFER     candidates with confidence, evidence and counter-evidence; the agent works on the best one
    PATCH     the smallest edit that invalidates the hypothesis (the agent, via replace_symbol etc.)
    EVALUATE  verify outward: parse + dead refs + targeted tests -> blast-radius tests -> full suite only if
              the risk justifies it; stop conditions abort snipes that grow into investigations

State lives in ``.codeatlas/snipe.json`` and is mirrored to ``SNIPE.md``. Records are tiny; context is lazy
(levels 0-6). Issues (defects) are attacked automatically; opportunities (refactors, cleanups) stay dormant.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

from .graph import Atlas, is_test_path
from .rank import ensure_rank, estimate_tokens

STATES = ("FOUND", "TARGETED", "FIXING", "VERIFYING", "DONE", "BLOCKED")
PRIORITIES = ("SNIPE NOW", "INVESTIGATE", "DEFER", "IGNORE")
KINDS = ("issue", "opportunity")
LOC_RE = re.compile(r"((?:[A-Za-z]:)?[\w./\\-]+\.(?:tsx?|jsx?|mjs|cjs|py|go|rs|kt|kts|swift|java|cs|rb|php|c|cc|cpp|h|hpp|scala|lua|sh))"
                    r"(?::|\()(\d+)")
FAIL_PATTERNS = [
    re.compile(r"^\s*not ok \d+ - (.+?)\s*$"),                    # node --test / TAP
    re.compile(r"^\s*(?:FAIL|✕|×|✗)\s+(.+?)\s*$"),                # jest/vitest style
    re.compile(r"^FAILED\s+(\S+)"),                               # pytest
    re.compile(r"^--- FAIL: (\S+)"),                              # go
    re.compile(r"^test (\S+) \.\.\. FAILED"),                     # cargo
    re.compile(r"^\s*\d+\) (.+?)\s*$"),                           # mocha
]
ERROR_RE = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Panic))\b[:\s]+(.{0,140})")
HOT_LIMIT = 600


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class Snipe:
    def __init__(self, atlas: Atlas):
        self.atlas = atlas
        self.ix = atlas.ix
        self.path = self.ix.dir / "snipe.json"
        self.md_path = self.ix.repo / "SNIPE.md"
        self.on_save = None
        self.data = self._load()
        self._migrate_scrum()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> dict:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                d.setdefault("changes", [])
                d.setdefault("hot", {})
                d.setdefault("config", {})
                for k, v in self._default_config().items():
                    d["config"].setdefault(k, v)
                return d
            except Exception:
                pass
        return {"project": self.ix.repo.name, "created": _now(), "next_id": 1, "items": [], "log": [],
                "config": self._default_config(), "hot": {}, "changes": []}

    @staticmethod
    def _default_config() -> dict:
        return {"test_cmd": None, "full_test_cmd": None, "typecheck_cmd": None, "lint_cmd": None,
                "max_patch_symbols": 6, "max_patch_files": 3, "test_timeout": 300}

    def _migrate_scrum(self) -> None:
        for p in (self.ix.repo / "SCRUM.md", self.ix.dir / "scrum.json"):
            try:
                if p.exists():
                    p.unlink()
                    self._log("removed legacy SCRUM artefact " + p.name)
            except OSError:
                pass

    def save(self) -> None:
        self.data["updated"] = _now()
        self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        self.md_path.write_text(self.render_md(), encoding="utf-8")
        if self.on_save:
            try:
                self.on_save()
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        self.data.setdefault("log", []).append({"t": _now(), "msg": msg})
        self.data["log"] = self.data["log"][-300:]

    # ------------------------------------------------------------------ records
    def items(self, kind: Optional[str] = None, state: Optional[str] = None, priority: Optional[str] = None) -> list[dict]:
        out = self.data["items"]
        if kind:
            out = [i for i in out if i["kind"] == kind]
        if state:
            st = set(state.split(","))
            out = [i for i in out if i["state"] in st]
        if priority:
            pr = set(priority.split(","))
            out = [i for i in out if i["priority"] in pr]
        return sorted(out, key=lambda i: (-i.get("score", 0), i["id"]))

    def get(self, item_id: str) -> Optional[dict]:
        item_id = item_id.strip().upper()
        if item_id.isdigit():
            item_id = f"BUG-{item_id}"
        for i in self.data["items"]:
            if i["id"] == item_id:
                return i
        return None

    def add(self, problem: str, evidence: Optional[list[str]] = None, kind: str = "issue", suspect: Optional[str] = None,
            related: Optional[list[str]] = None, verification: Optional[list[str]] = None, source: str = "manual",
            key: Optional[str] = None, confidence: Optional[float] = None, priority: Optional[str] = None,
            narrow: bool = True) -> dict:
        kind = kind if kind in KINDS else "issue"
        if key:
            for it in self.data["items"]:
                if it.get("key") == key:
                    if it["state"] in ("DONE",) and source in ("delta", "test", "report"):
                        it["state"] = "FOUND"  # regression: it is back
                        it["notes"].append({"t": _now(), "text": "reopened: detected again"})
                    it["evidence"] = list(dict.fromkeys((it.get("evidence") or []) + (evidence or [])))[:12]
                    it["updated"] = _now()
                    return it
        prefix = "BUG" if kind == "issue" else "OPP"
        rec = {"id": f"{prefix}-{self.data['next_id']}", "kind": kind, "state": "FOUND", "problem": problem.strip()[:200],
               "evidence": [e[:220] for e in (evidence or [])][:12], "suspect": suspect, "suspect_path": None,
               "suspect_line": None, "confidence": confidence, "candidates": [], "related": related or [],
               "patch_surface": {"symbols": 1, "files": 1}, "verification": verification or [],
               "priority": priority, "priority_locked": bool(priority), "score": 0, "factors": {}, "owner": None,
               "created": _now(), "updated": _now(), "notes": [], "key": key, "source": source,
               "context_tokens": 0, "blast": 0}
        self.data["next_id"] += 1
        self.data["items"].append(rec)
        if narrow:
            self.narrow(rec)
        self.score(rec)
        self._log(f"{rec['id']} found: {rec['problem'][:80]}")
        return rec

    # ------------------------------------------------------------------ SCAN (event-driven)
    def scan_delta(self, delta: dict) -> dict:
        """React to one index update. Only the changed surface is analysed."""
        a = self.atlas
        created: list[dict] = []
        closed = 0
        now = time.time()
        # hot graph + change log (used by NARROW and by verification stop conditions)
        for q, path in delta.get("changed", []) + delta.get("added", []):
            self.data["hot"][q] = now
            self.data["changes"].append({"t": now, "symbol": q, "path": path})
        for q, path, name in delta.get("removed", []):
            self.data["changes"].append({"t": now, "symbol": q, "path": path, "removed": True})
        if len(self.data["hot"]) > HOT_LIMIT:
            keep = sorted(self.data["hot"].items(), key=lambda kv: -kv[1])[:HOT_LIMIT]
            self.data["hot"] = dict(keep)
        self.data["changes"] = self.data["changes"][-2000:]
        res = a.resolver()
        # dead references: a symbol disappeared but code still refers to it
        for q, path, name in delta.get("removed", []):
            refs = a.conn.execute(
                "SELECT f.path, r.line, r.kind, r.qualifier, r.enclosing_id, f.id AS fid FROM refs r JOIN files f ON f.id=r.file_id "
                "WHERE r.name=? AND r.kind IN ('call','new','arg','ref','import') AND f.path != ? LIMIT 40", (name, path)).fetchall()
            dead = []
            for r in refs:
                rs = res.resolve(name, r["fid"], r["qualifier"], r["enclosing_id"], r["kind"])
                if not rs.targets:
                    dead.append(f"{r['path']}:{r['line']}")
            if dead:
                enc = a.resolve_symbol(dead[0])
                rec = self.add(f"Dead reference: {name} was removed from {path} but is still used",
                               evidence=[f"{q} removed from {path}"] + [f"used at {d}" for d in dead[:5]],
                               kind="issue", suspect=enc[0]["qualified"] if enc else None, source="delta",
                               key=f"deadref:{path}:{q}", confidence=0.95,
                               verification=self._tests_mentioning(name)[:3], narrow=not enc)
                if enc:
                    rec["suspect_path"], rec["suspect_line"] = enc[0]["path"], enc[0]["start_line"]
                    rec["related"] = [f"{q} (removed)"]
                    self.score(rec)
                created.append(rec)
        for path, module in delta.get("broken_imports", []):
            rec = self.add(f"Broken import '{module}' in {path}", evidence=[f"{path} imports {module}, which no longer resolves"],
                           kind="issue", source="delta", key=f"import:{path}:{module}", confidence=0.95, narrow=False)
            self._suspect_file_head(rec, path)
            created.append(rec)
        for path, n in delta.get("parse_errors", []):
            rec = self.add(f"Syntax errors in {path} ({n})", evidence=[f"tree-sitter reports {n} error node(s) in {path}"],
                           kind="issue", source="delta", key=f"parse:{path}", confidence=0.98, narrow=False)
            self._suspect_file_head(rec, path)
            created.append(rec)
        for path, line, code, msg in delta.get("new_tags", []):
            tag = code[4:]
            if tag in ("FIXME", "BUG", "HACK", "XXX"):
                rec = self.add(f"{tag}: {msg or 'see comment'} ({path.rsplit('/', 1)[-1]}:{line})",
                               evidence=[f"new {tag} comment at {path}:{line}: {msg}"], kind="issue", source="tag",
                               key=f"tag:{path}:{code}:{(msg or '')[:40]}", confidence=0.5, priority="INVESTIGATE")
            else:
                rec = self.add(f"TODO: {msg or 'see comment'} ({path.rsplit('/', 1)[-1]}:{line})",
                               evidence=[f"new TODO at {path}:{line}"], kind="opportunity", source="tag",
                               key=f"tag:{path}:{code}:{(msg or '')[:40]}", confidence=0.3, narrow=False)
            created.append(rec)
        # re-check delta-born issues that touch the changed files: fixed elsewhere?
        touched = set(delta.get("files", []))
        for it in self.data["items"]:
            if it["state"] == "DONE" or not it.get("key"):
                continue
            k = it["key"]
            if k.startswith("parse:") and k[6:] in touched and not any(p == k[6:] for p, _ in delta.get("parse_errors", [])):
                self._close(it, "syntax errors gone after edit")
                closed += 1
            elif k.startswith("import:"):
                _, p, module = k.split(":", 2)
                if p in touched:
                    fid = a.file_id(p)
                    ok = fid is not None and a.conn.execute(
                        "SELECT 1 FROM imports WHERE file_id=? AND module=? AND resolved_file_id IS NOT NULL", (fid, module)).fetchone()
                    if ok or fid is None:
                        self._close(it, "import resolves again")
                        closed += 1
            elif k.startswith("deadref:"):
                name = k.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
                if res.defs_by_name.get(name):
                    self._close(it, f"{name} is defined again")
                    closed += 1
                elif touched and not any(a.conn.execute("SELECT 1 FROM refs WHERE name=? LIMIT 1", (name,)).fetchall()):
                    self._close(it, f"no references to {name} remain")
                    closed += 1
            elif it["state"] in ("FOUND", "TARGETED") and it.get("suspect_path") in touched:
                it["notes"].append({"t": _now(), "text": "suspect file changed since detection; re-narrow before fixing"})
        if created or closed:
            self.save()
        return {"created": [c["id"] for c in created], "closed": closed}

    def _close(self, it: dict, why: str) -> None:
        it["state"] = "DONE"
        it["done"] = _now()
        it["notes"].append({"t": _now(), "text": f"verified: {why}"})
        self._log(f"{it['id']} done ({why})")

    def _suspect_file_head(self, rec: dict, path: str) -> None:
        o = self.atlas.outline(path)
        if o and o["symbols"]:
            s = o["symbols"][0]
            rec["suspect"], rec["suspect_path"], rec["suspect_line"] = s["qualified"], path, s["start_line"]
        else:
            rec["suspect_path"] = path
        rec["verification"] = self._tests_for_file(path)[:3]
        self.score(rec)

    # ------------------------------------------------------------------ SCAN (static + external runners)
    def scan(self, run: Optional[str] = None) -> dict:
        """Static pass over the index (cheap: no file reads) and, optionally, run the configured tests /
        typecheck / lint and turn failures into issues. run: 'tests' | 'typecheck' | 'lint' | None."""
        a = self.atlas
        a.ensure_fresh()
        created: list[dict] = []
        c = a.conn
        # unresolved *relative* imports are real defects (external packages are not)
        for r in c.execute("SELECT f.path, i.module FROM imports i JOIN files f ON f.id=i.file_id "
                           "WHERE i.resolved_file_id IS NULL AND (i.module LIKE './%' OR i.module LIKE '../%') LIMIT 400"):
            if self._import_exists_on_disk(r[0], r[1]):
                continue  # assets, non-indexed files (css/json/webp/oversized) are not broken imports
            rec = self.add(f"Unresolved relative import '{r[1]}' in {r[0]}", evidence=[f"{r[0]} imports {r[1]} but no such file is indexed"],
                           kind="issue", source="static", key=f"import:{r[0]}:{r[1]}", confidence=0.85, narrow=False)
            if rec["created"] == rec["updated"] and rec not in created:
                self._suspect_file_head(rec, r[0])
                created.append(rec)
        # opportunities (dormant): complexity, cycles, swallowed errors, hubs without tests
        h = a.hotspots(limit=10)
        for x in h["most_complex"]:
            if x["complexity"] >= 30 and not is_test_path(x["path"]):
                created.append(self.add(f"Refactor {x['qualified']} (complexity {x['complexity']}, {x['lines']} lines)",
                                        evidence=[f"{x['path']}:{x['start_line']}"], kind="opportunity", source="static",
                                        key=f"complex:{x['path']}:{x['qualified']}", suspect=x["qualified"], narrow=False))
        for cyc in h["cycles"][:5]:
            created.append(self.add("Import cycle: " + " -> ".join(p.rsplit("/", 1)[-1] for p in cyc[:4]), evidence=cyc[:6],
                                    kind="opportunity", source="static", key="cycle:" + "|".join(sorted(cyc)), narrow=False))
        for f in a.findings(limit=300):
            if f["code"] in ("EMPTY_CATCH", "SWALLOWED_EXCEPTION", "BARE_EXCEPT") and not is_test_path(f["path"]):
                created.append(self.add(f"{f['code'].replace('_', ' ').title()} at {f['path'].rsplit('/', 1)[-1]}:{f['line']}",
                                        evidence=[f"{f['path']}:{f['line']} {f['message']}"], kind="opportunity", source="static",
                                        key=f"finding:{f['path']}:{f['code']}:{f['line']}", narrow=False))
        created = [x for x in created if x["created"] == x["updated"]]
        ran = None
        if run:
            ran = self.run_and_report(run)
        for it in self.data["items"]:
            if not it.get("suspect_path") and it.get("suspect"):
                cands = a.resolve_symbol(it["suspect"])
                if cands:
                    it["suspect_path"], it["suspect_line"] = cands[0]["path"], cands[0]["start_line"]
            self.score(it)
        self.save()
        return {"created": [x["id"] for x in created], "opportunities": len(self.items(kind="opportunity")),
                "issues_open": len(self.items(kind="issue", state="FOUND,TARGETED,FIXING,VERIFYING,BLOCKED")), "run": ran}

    # ------------------------------------------------------------------ external evidence
    def report(self, text: str, source: str = "report") -> list[dict]:
        """Turn test/typecheck/lint/runtime output into issues (one per failing test or distinct location)."""
        lines = text.splitlines()
        groups: list[dict] = []
        cur: Optional[dict] = None
        for ln in lines:
            name = None
            for pat in FAIL_PATTERNS:
                m = pat.match(ln)
                if m:
                    name = m.group(1).strip()
                    break
            if name:
                cur = {"name": name[:160], "lines": [ln.strip()[:200]], "locs": [], "errors": []}
                groups.append(cur)
                continue
            locs = [(self.ix.rel(p.replace("\\", "/")), int(n)) for p, n in LOC_RE.findall(ln)]
            err = ERROR_RE.search(ln)
            if locs or err:
                if cur is None:
                    cur = {"name": None, "lines": [], "locs": [], "errors": []}
                    groups.append(cur)
                if locs:
                    cur["locs"].extend(locs)
                if err:
                    cur["errors"].append(f"{err.group(1)}: {err.group(2).strip()}")
                if len(cur["lines"]) < 6:
                    cur["lines"].append(ln.strip()[:200])
        created: list[dict] = []
        for g in groups:
            locs = [(p, n) for p, n in g["locs"] if self.atlas.file_id(p) is not None]
            if not g["name"] and not locs and not g["errors"]:
                continue
            problem = g["name"] or (g["errors"][0] if g["errors"] else f"Failure at {locs[0][0]}:{locs[0][1]}")
            evidence = list(dict.fromkeys([f"{source}: {l}" for l in g["lines"][:3]] +
                                          [f"at {p}:{n}" for p, n in locs[:6]] + g["errors"][:2]))
            test_files = [p for p, _ in locs if is_test_path(p)]
            key = f"{source}:{g['name']}" if g["name"] else f"{source}:{locs[0][0] if locs else problem[:40]}"
            rec = self.add(problem, evidence=evidence, kind="issue", source=source if source != "report" else "test",
                           key=key, confidence=0.85 if source in ("test", "typecheck") else 0.75,
                           verification=list(dict.fromkeys(test_files))[:3])
            created.append(rec)
        if created:
            self.save()
        return created

    # ------------------------------------------------------------------ NARROW + INFER
    def narrow(self, rec: dict) -> dict:
        a = self.atlas
        res = a.resolver()
        seeds: list[dict] = []      # {qualified, path, id, why, is_test}
        seen: set[int] = set()

        def add_seed(s: dict, why: str):
            if s["id"] in seen:
                return
            seen.add(s["id"])
            seeds.append({"qualified": s["qualified"], "path": s["path"], "id": s["id"], "why": why,
                          "is_test": is_test_path(s["path"]), "line": s["start_line"]})

        text = " ".join(rec.get("evidence", []) + [rec["problem"]])
        # 1. locations in the evidence
        for p, n in LOC_RE.findall(text):
            p = self.ix.rel(p.replace("\\", "/"))
            for s in a.resolve_symbol(f"{p}:{n}")[:1]:
                add_seed(s, f"evidence location {p}:{n}")
        # 2. explicit suspect
        if rec.get("suspect"):
            for s in a.resolve_symbol(rec["suspect"], rec.get("suspect_path"))[:1]:
                add_seed(s, "named suspect")
        # the language/project of the located evidence bounds everything else (no cross-language suspects)
        bound = {(res.file_info[a.file_id(sd["path"])][0], res.file_info[a.file_id(sd["path"])][1])
                 for sd in seeds if a.file_id(sd["path"]) is not None}

        def in_bounds(s: dict) -> bool:
            if not bound:
                return True
            fid = a.file_id(s["path"])
            return fid is not None and (res.file_info[fid][0], res.file_info[fid][1]) in bound

        # 3. identifiers in the evidence that are defined symbols (paths stripped first: C:\Users is not a symbol)
        clean = re.sub(r"(?:[A-Za-z]:)?[\w./\\-]+[\\/][\w./\\-]+", " ", text)
        for tok in set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", clean)):
            if tok.lower() in ("test", "error", "expected", "received", "actual", "true", "false", "null", "undefined",
                               "stack", "subtest", "users", "desktop", "anonymous", "context", "assert", "async"):
                continue
            defs = res.defs_by_name.get(tok)
            if defs and len(defs) <= 3:
                for sid in defs:
                    s = a.symbol_by_id(sid)
                    if s and in_bounds(s):
                        add_seed(s, f"named in evidence ({tok})")
        # 4. symbols exercised by the failing test files
        for tf in rec.get("verification", []):
            fid = a.file_id(tf)
            if fid is None:
                continue
            for r in a.conn.execute("SELECT r.name, r.qualifier, r.enclosing_id, r.kind FROM refs r WHERE r.file_id=? "
                                    "AND r.kind IN ('call','new') LIMIT 400", (fid,)):
                rs = res.resolve(r[0], fid, r[1], r[2], r[3])
                if rs.confidence in ("high", "medium") and len(rs.targets) == 1:
                    s = a.symbol_by_id(rs.targets[0])
                    if s and not is_test_path(s["path"]):
                        add_seed(s, f"exercised by {tf}")
                if len(seeds) > 24:
                    break
        # 5. words of the problem statement matched against symbol names (weak; only when nothing better)
        if not seeds:
            for w in re.findall(r"[A-Za-z]{4,}", rec["problem"])[:6]:
                for s in a.find_symbols(w, limit=2):
                    if not is_test_path(s["path"]) and in_bounds(s):
                        add_seed(s, f"name matches '{w}'")
        if not seeds:
            rec["candidates"] = []
            return rec
        # chains: seed -> callees (2 hops)
        chains: dict[int, set[int]] = {}
        for sd in seeds:
            nodes = {sd["id"]}
            frontier = [sd["id"]]
            for _ in range(2):
                nxt = []
                for sid in frontier:
                    for ce in a.callees(sid, limit=20):
                        t = ce.get("target")
                        if t and t.get("id") and t["id"] not in nodes:
                            nodes.add(t["id"])
                            nxt.append(t["id"])
                frontier = nxt
                if len(nodes) > 30:
                    break
            chains[sd["id"]] = nodes
        counts: Counter = Counter()
        for sid, nodes in chains.items():
            for n in nodes:
                counts[n] += 1
        hot = self.data.get("hot", {})
        named = {sd["id"] for sd in seeds if "named" in sd["why"] or "location" in sd["why"]}
        cands = []
        for sid, n in counts.items():
            s = a.symbol_by_id(sid)
            if not s or is_test_path(s["path"]) or s["kind"] not in ("function", "method", "class", "constant", "variable", "property"):
                continue
            ev = []
            counter = []
            for sd in seeds:
                (ev if sid in chains[sd["id"]] else counter).append(sd)
            conf = 0.35 + 0.35 * (n / max(1, len(seeds)))
            if s["qualified"] in hot:
                conf += 0.15
            if sid in named:
                conf += 0.1
            conf = min(0.95, conf)
            cands.append({"symbol": s["qualified"], "path": s["path"], "line": s["start_line"], "id": sid,
                          "confidence": round(conf, 2),
                          "evidence": [f"on path from {sd['qualified']} ({sd['why']})" for sd in ev][:4]
                                      + (["changed recently"] if s["qualified"] in hot else []),
                          "counter": [f"not reached from {sd['qualified']}" for sd in counter][:3],
                          "shared": n, "lines": s["lines"]})
        cands.sort(key=lambda x: (-x["confidence"], -x["shared"], x["lines"]))
        rec["candidates"] = cands[:5]
        if cands:
            top = cands[0]
            rec["suspect"], rec["suspect_path"], rec["suspect_line"] = top["symbol"], top["path"], top["line"]
            rec["confidence"] = max(rec.get("confidence") or 0.0, top["confidence"]) if rec.get("source") in ("delta", "test", "typecheck", "report") else top["confidence"]
            related = [f"{c['symbol']} {c['path']}:{c['line']}" for c in cands[1:4]]
            rec["related"] = related
            tests = set(rec.get("verification") or [])
            for t in self._tests_mentioning(top["symbol"].rsplit(".", 1)[-1]):
                tests.add(t)
            rec["verification"] = sorted(tests)[:4]
            rec["blast"] = len(a.callers(top["id"], limit=50))
            tokens = 0
            src = a.source(top["symbol"], top["path"], max_lines=200)
            if src.get("found"):
                tokens += estimate_tokens(src["text"])
            tokens += 30 * len(related)
            rec["context_tokens"] = tokens
            rec["patch_surface"] = {"symbols": 1, "files": 1}
        return rec

    def _import_exists_on_disk(self, from_path: str, module: str) -> bool:
        base = os.path.normpath(os.path.join(os.path.dirname(str(self.ix.abs(from_path))), module.split("?")[0]))
        if os.path.exists(base):
            return True
        stem = re.sub(r"\.(js|mjs|cjs|jsx)$", "", base)
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".d.ts", ".py", ".json", ".vue", ".svelte"):
            if os.path.exists(stem + ext):
                return True
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
            if os.path.exists(os.path.join(base, "index" + ext)):
                return True
        return os.path.exists(os.path.join(base, "__init__.py"))

    def detect_any_test_cmd(self) -> Optional[str]:
        """First detectable test command across the repo's packages (for status displays)."""
        if self.data["config"].get("test_cmd"):
            return self.data["config"]["test_cmd"]
        for r in self.atlas.conn.execute("SELECT project, MIN(path) FROM files GROUP BY project ORDER BY COUNT(*) DESC LIMIT 12"):
            det = self.detect_test_cmd(r[1])
            if det:
                return det[0] + ("" if not r[0] else f"  (in {r[0]})")
        return None

    def _tests_mentioning(self, name: str) -> list[str]:
        rows = self.atlas.conn.execute(
            "SELECT DISTINCT f.path FROM refs r JOIN files f ON f.id=r.file_id WHERE r.name=? LIMIT 50", (name,)).fetchall()
        return [r[0] for r in rows if is_test_path(r[0])]

    def _tests_for_file(self, path: str) -> list[str]:
        fid = self.atlas.file_id(path)
        if fid is None:
            return []
        rows = self.atlas.conn.execute(
            "SELECT DISTINCT f.path FROM imports i JOIN files f ON f.id=i.file_id WHERE i.resolved_file_id=?", (fid,)).fetchall()
        return [r[0] for r in rows if is_test_path(r[0])]

    # ------------------------------------------------------------------ Snipe score
    def score(self, rec: dict) -> dict:
        conf = rec.get("confidence") or (0.3 if rec["kind"] == "opportunity" else 0.5)
        src = rec.get("source", "manual")
        impact = {"issue": {"delta": 0.9, "test": 1.0, "typecheck": 1.0, "lint": 0.5, "report": 0.9, "static": 0.8,
                            "tag": 0.5, "manual": 0.7}, "opportunity": {}}[rec["kind"]].get(src, 0.4)
        repro = {"delta": 1.0, "static": 1.0, "test": 0.9, "typecheck": 1.0, "lint": 1.0, "report": 0.7, "tag": 0.8,
                 "manual": 0.6}.get(src, 0.5)
        if rec["kind"] == "opportunity":
            repro *= 0.6
        isolation = 1.0 / (1.0 + 0.1 * max(0, len(rec.get("related") or []) - 1))
        if not rec.get("suspect"):
            isolation *= 0.5
        cost = 1.0 + (rec.get("context_tokens") or 0) / 1500.0
        blast = min(4.0, 1.0 + (rec.get("blast") or 0) / 20.0)
        # geometric form keeps the scale readable: 90+ = confirmed isolated defect, <25 = not worth an agent's time
        score = int(round(100 * (conf * impact * repro * isolation) ** 0.5 / (cost * blast) ** 0.5))
        rec["score"] = max(0, min(100, score))
        rec["factors"] = {"confidence": round(conf, 2), "impact": impact, "reproducibility": repro,
                          "isolation": round(isolation, 2), "context_cost": round(cost, 2), "blast_radius": round(blast, 2)}
        if not rec.get("priority_locked"):
            if rec["kind"] == "opportunity":
                rec["priority"] = "DEFER"
            elif rec["score"] >= 55:
                rec["priority"] = "SNIPE NOW"
            elif rec["score"] >= 25:
                rec["priority"] = "INVESTIGATE"
            else:
                rec["priority"] = "DEFER"
        rec["risk"] = "low" if blast < 1.5 else ("medium" if blast < 2.5 else "high")
        return rec

    # ------------------------------------------------------------------ the loop
    def next(self, agent: Optional[str] = None) -> Optional[dict]:
        mine = [i for i in self.items(kind="issue", state="FIXING,VERIFYING") if not agent or i.get("owner") in (None, agent)]
        if mine:
            return mine[0]
        ready = [i for i in self.items(kind="issue", state="FOUND,TARGETED", priority="SNIPE NOW")
                 if not (i.get("owner") and agent and i["owner"] != agent)]
        if ready:
            return ready[0]
        inv = [i for i in self.items(kind="issue", state="FOUND,TARGETED,BLOCKED", priority="INVESTIGATE")]
        return inv[0] if inv else None

    def claim(self, item_id: str, agent: Optional[str] = None) -> dict:
        it = self.get(item_id)
        if not it:
            return {"ok": False, "error": f"no item {item_id}"}
        if it["state"] in ("FIXING", "VERIFYING") and it.get("owner") and agent and it["owner"] != agent:
            return {"ok": False, "error": f"{it['id']} is being worked on by {it['owner']}"}
        if not it.get("candidates") and it["kind"] == "issue":
            self.narrow(it)
            self.score(it)
        it["state"] = "FIXING"
        it["owner"] = agent or it.get("owner")
        it["claimed_at"] = time.time()
        it["claim_files"] = self._snapshot_files(it)
        it["updated"] = _now()
        self._log(f"{it['id']} claimed" + (f" by {agent}" if agent else ""))
        self.save()
        return {"ok": True, "item": it}

    def _snapshot_files(self, it: dict) -> dict:
        paths = set()
        if it.get("suspect_path"):
            paths.add(it["suspect_path"])
        for r in it.get("related", []):
            m = re.search(r"(\S+):\d+$", r)
            if m:
                paths.add(m.group(1))
        out = {}
        for p in paths:
            try:
                out[p] = os.path.getmtime(self.ix.abs(p))
            except OSError:
                pass
        return out

    def context(self, item_id: str, level: int = 1) -> str:
        it = self.get(item_id)
        if not it:
            return f"no item {item_id}"
        a = self.atlas
        L = [render_item(it, brief=True)]
        if level <= 0:
            return "\n".join(L)
        names = [it["suspect"]] if it.get("suspect") else []
        names += [r.split(" ")[0] for r in it.get("related", [])]
        L.append("\nLevel 1 - symbols: " + (", ".join(names) or "(no suspect yet: run snipe_scan or give evidence)"))
        if it.get("candidates"):
            L.append("candidates:")
            for c in it["candidates"]:
                L.append(f"  {c['confidence']:.2f}  {c['symbol']}  {c['path']}:{c['line']}")
                for e in c["evidence"][:3]:
                    L.append(f"        + {e}")
                for e in c["counter"][:2]:
                    L.append(f"        - {e}")
        if it.get("verification"):
            L.append("verification: " + ", ".join(it["verification"]))
        if level <= 1:
            return "\n".join(L)
        L.append("\nLevel 2 - signatures:")
        for n in names[:6]:
            d = a.symbol_detail(n, it.get("suspect_path") if n == it.get("suspect") else None)
            if d.get("found"):
                L.append(f"  {d['qualified']}  {d['path']}:{d['start_line']}-{d['end_line']}  cx{d['complexity']}")
                L.append(f"    {d['signature'][:200]}")
                if d.get("doc"):
                    L.append(f"    doc: {d['doc'][:160]}")
        if level <= 2:
            return "\n".join(L)
        L.append("\nLevel 3 - bodies:")
        for n in names[:3]:
            src = a.source(n, it.get("suspect_path") if n == it.get("suspect") else None, max_lines=160)
            if src.get("found"):
                L.append(src["label"])
                L.append(src["text"])
        if level <= 3:
            return "\n".join(L)
        if it.get("suspect"):
            from . import fmt
            L.append("\nLevel 4 - callers / dependencies:")
            L.append(fmt.impact(a.impact(it["suspect"], it.get("suspect_path"), depth=2)))
            if it.get("suspect_path"):
                dep = a.dependencies(it["suspect_path"])
                if dep:
                    L.append(fmt.dependencies(dep))
        if level <= 4:
            return "\n".join(L)
        if it.get("suspect_path"):
            L.append("\nLevel 5 - full file:")
            src = a.source(f"{it['suspect_path']}:1-100000", max_lines=400)
            if src.get("found"):
                L.append(src["text"])
        if level <= 5:
            return "\n".join(L)
        from . import context as ctx
        scope = os.path.dirname(it.get("suspect_path") or "") or None
        L.append("\nLevel 6 - subsystem:")
        L.append(ctx.render(ctx.task_context(self.ix, a, it["problem"], scope=scope, token_budget=4000,
                                             boost=set(self.data.get("hot", {})))))
        return "\n".join(L)

    # ------------------------------------------------------------------ EVALUATE
    def changed_since_claim(self, it: dict) -> tuple[list[str], list[str]]:
        """(files, symbols) changed since the item was claimed, from the change log + file mtimes."""
        t0 = it.get("claimed_at") or 0
        files: set[str] = set()
        syms: set[str] = set()
        for ch in self.data.get("changes", []):
            if ch["t"] >= t0:
                files.add(ch["path"])
                syms.add(ch["symbol"])
        for p, mt in (it.get("claim_files") or {}).items():
            try:
                if os.path.getmtime(self.ix.abs(p)) > mt + 1e-6:
                    files.add(p)
            except OSError:
                files.add(p)
        return sorted(files), sorted(syms)

    def verify(self, item_id: str, deep: bool = False) -> dict:
        it = self.get(item_id)
        if not it:
            return {"ok": False, "error": f"no item {item_id}"}
        a = self.atlas
        a.ensure_fresh(force=True)
        it["state"] = "VERIFYING"
        files, syms = self.changed_since_claim(it)
        cfg = self.data["config"]
        levels: list[dict] = []
        status = "PASS"

        def check(level: dict, name: str, ok: Optional[bool], detail: str = ""):
            level["checks"].append({"check": name, "status": "pass" if ok else ("skip" if ok is None else "fail"), "detail": detail[:400]})

        # stop condition: the snipe grew into an investigation
        surface = {"files": len(files), "symbols": len(syms)}
        if len(syms) > cfg["max_patch_symbols"] or len(files) > cfg["max_patch_files"]:
            self.abort(it["id"], f"blast radius exceeded threshold: {len(syms)} symbols in {len(files)} files "
                                 f"(limits {cfg['max_patch_symbols']}/{cfg['max_patch_files']})")
            return {"ok": True, "status": "ABORTED", "item": it, "levels": levels, "changed": surface}
        # ---- Level 1: direct ----
        l1 = {"level": 1, "name": "direct", "checks": []}
        levels.append(l1)
        from .indexer import SPECS, extract
        from .languages import lang_for_path
        for p in files:
            src = self.ix.read_source(p)
            lang = lang_for_path(p)
            if src is None or not lang:
                continue
            n = extract(SPECS[lang], src).error_count
            check(l1, f"parse {p}", n == 0, f"{n} syntax error node(s)" if n else "clean")
        res = a.resolver()
        dead = []
        for ch in self.data.get("changes", []):
            if ch["t"] >= (it.get("claimed_at") or 0) and ch.get("removed"):
                name = ch["symbol"].rsplit(".", 1)[-1]
                if not res.defs_by_name.get(name):
                    for r in a.conn.execute("SELECT f.path, r.line FROM refs r JOIN files f ON f.id=r.file_id WHERE r.name=? LIMIT 3", (name,)):
                        dead.append(f"{name} at {r[0]}:{r[1]}")
        check(l1, "no dead references introduced", not dead, "; ".join(dead) if dead else "ok")
        tests = list(it.get("verification") or [])
        ran_any = False
        for tf in tests[:4]:
            r = self.run_test(tf)
            if r is None:
                check(l1, f"test {tf}", None, "no test command configured (snipe_config test_cmd=...)")
                continue
            ran_any = True
            check(l1, f"test {tf}", r["ok"], r["tail"])
        if not tests:
            check(l1, "targeted tests", None, "no test file identified for this issue")
        if any(c["status"] == "fail" for c in l1["checks"]):
            status = "FAIL"
        # ---- Level 2: immediate blast radius ----
        if status == "PASS":
            l2 = {"level": 2, "name": "blast radius", "checks": []}
            levels.append(l2)
            affected = set()
            for q in syms[:10]:
                for s in a.resolve_symbol(q)[:1]:
                    im = a.impact(q, s["path"], depth=1)
                    for t in im.get("tests", []):
                        affected.add(t)
            if it.get("suspect"):
                for t in self._tests_mentioning(it["suspect"].rsplit(".", 1)[-1]):
                    affected.add(t)
            affected -= set(tests)
            if not affected:
                check(l2, "affected tests", None, "no additional tests reference the callers")
            for tf in sorted(affected)[:4]:
                r = self.run_test(tf)
                if r is None:
                    check(l2, f"test {tf}", None, "no test command configured")
                    continue
                ran_any = True
                check(l2, f"test {tf}", r["ok"], r["tail"])
            if any(c["status"] == "fail" for c in l2["checks"]):
                status = "FAIL"
        # ---- Level 3: broader (only when risk justifies it) ----
        if status == "PASS" and (deep or it.get("risk") == "high") and cfg.get("full_test_cmd"):
            l3 = {"level": 3, "name": "full", "checks": []}
            levels.append(l3)
            r = self._run(cfg["full_test_cmd"], self.ix.repo)
            ran_any = True
            check(l3, "full test suite", r["ok"], r["tail"])
            if not r["ok"]:
                status = "FAIL"
        if status == "PASS" and not ran_any and not files:
            status = "INCOMPLETE"
        # ---- outcome ----
        if status == "PASS":
            it["state"] = "DONE"
            it["done"] = _now()
            it["notes"].append({"t": _now(), "text": f"verified: {len(files)} file(s), {len(syms)} symbol(s) changed; "
                                                    + ("tests passed" if ran_any else "structural checks passed (no tests run)")})
            self._log(f"{it['id']} done")
        elif status == "FAIL":
            it["state"] = "FIXING"
            fails = [c for l in levels for c in l["checks"] if c["status"] == "fail"]
            it["evidence"] = (it.get("evidence") or [])[:8] + [f"verify: {c['check']}: {c['detail'][:120]}" for c in fails[:3]]
            it["notes"].append({"t": _now(), "text": "verification failed -> back to INFER"})
            for c in fails:
                if c["check"].startswith("test "):
                    self.report(c["detail"], source="test")
        else:
            it["state"] = "VERIFYING"
            it["notes"].append({"t": _now(), "text": "verification incomplete: nothing changed or no tests runnable"})
        it["updated"] = _now()
        self.save()
        return {"ok": True, "status": status, "item": it, "levels": levels, "changed": surface}

    # ------------------------------------------------------------------ runners
    def detect_test_cmd(self, path: Optional[str] = None) -> Optional[tuple[str, str]]:
        """(command template with {file}, cwd) for the project containing `path`, or configured cmd."""
        cfg = self.data["config"]
        if cfg.get("test_cmd"):
            return cfg["test_cmd"], str(self.ix.repo)
        proj = ""
        if path:
            fid = self.atlas.file_id(path)
            if fid is not None:
                proj = self.atlas.conn.execute("SELECT project FROM files WHERE id=?", (fid,)).fetchone()[0] or ""
        root = self.ix.repo / proj if proj else self.ix.repo
        pj = root / "package.json"
        if pj.exists():
            try:
                data = json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            deps = {**data.get("devDependencies", {}), **data.get("dependencies", {})}
            script = (data.get("scripts") or {}).get("test", "")
            if "vitest" in deps:
                return "npx vitest run {file}", str(root)
            if "jest" in deps:
                return "npx jest {file}", str(root)
            if "--test" in script and "node" in script:
                tmpl = re.sub(r'"?[^\s"]*\*[^\s"]*"?', "{file}", script)
                if "{file}" not in tmpl:
                    tmpl += " {file}"
                return tmpl, str(root)
            if "mocha" in deps:
                return "npx mocha {file}", str(root)
        if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "setup.py").exists() or \
                (path or "").endswith(".py"):
            return "python -m pytest {file} -q -x", str(root)
        if (root / "go.mod").exists():
            return "go test {dir}", str(root)
        if (root / "Cargo.toml").exists():
            return "cargo test", str(root)
        return None

    def run_test(self, test_file: str) -> Optional[dict]:
        found = self.detect_test_cmd(test_file)
        if not found:
            return None
        tmpl, cwd = found
        rel = os.path.relpath(self.ix.abs(test_file), cwd).replace("\\", "/")
        cmd = tmpl.replace("{file}", rel).replace("{dir}", "./" + os.path.dirname(rel) if os.path.dirname(rel) else "./...")
        return self._run(cmd, cwd)

    def _run(self, cmd: str, cwd) -> dict:
        timeout = self.data["config"].get("test_timeout", 300)
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
            out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
            ok = p.returncode == 0
        except subprocess.TimeoutExpired:
            out, ok = f"timed out after {timeout}s", False
        except OSError as e:
            out, ok = str(e), False
        tail = "\n".join(out.strip().splitlines()[-12:])
        return {"ok": ok, "cmd": cmd, "seconds": round(time.time() - t0, 1), "tail": tail, "output": out[-20000:]}

    def run_and_report(self, what: str) -> dict:
        cfg = self.data["config"]
        cmd = {"tests": cfg.get("full_test_cmd") or (cfg.get("test_cmd") or "").replace("{file}", "").strip() or None,
               "typecheck": cfg.get("typecheck_cmd"), "lint": cfg.get("lint_cmd")}.get(what)
        if not cmd:
            det = self.detect_test_cmd() if what == "tests" else None
            if det and "{file}" in det[0]:
                cmd = det[0].replace("{file}", "").strip()
            if not cmd:
                return {"ran": False, "error": f"no {what} command configured (snipe_config {what}_cmd=...)"}
        r = self._run(cmd, self.ix.repo)
        created = [] if r["ok"] else self.report(r["output"], source="test" if what == "tests" else what)
        return {"ran": True, "ok": r["ok"], "cmd": cmd, "seconds": r["seconds"], "issues": [c["id"] for c in created],
                "tail": r["tail"]}

    # ------------------------------------------------------------------ updates
    def update(self, item_id: str, state: Optional[str] = None, note: Optional[str] = None, priority: Optional[str] = None,
               suspect: Optional[str] = None, kind: Optional[str] = None, agent: Optional[str] = None) -> dict:
        it = self.get(item_id)
        if not it:
            return {"ok": False, "error": f"no item {item_id}"}
        if state:
            state = state.upper()
            if state not in STATES:
                return {"ok": False, "error": f"state must be one of {STATES}"}
            it["state"] = state
            if state == "DONE":
                it["done"] = _now()
            if state in ("FIXING", "VERIFYING") and agent:
                it["owner"] = agent
        if note:
            it["notes"].append({"t": _now(), "text": note.strip()[:1000] + (f" ({agent})" if agent else "")})
        if priority:
            priority = priority.upper()
            if priority not in PRIORITIES:
                return {"ok": False, "error": f"priority must be one of {PRIORITIES}"}
            it["priority"], it["priority_locked"] = priority, True
        if suspect:
            it["suspect"] = suspect
            c = self.atlas.resolve_symbol(suspect)
            if c:
                it["suspect_path"], it["suspect_line"] = c[0]["path"], c[0]["start_line"]
                it["blast"] = len(self.atlas.callers(c[0]["id"], limit=50))
        if kind in KINDS:
            it["kind"] = kind
            it["id"] = it["id"]  # ids keep their prefix; kind flips priority handling
        self.score(it)
        it["updated"] = _now()
        self.save()
        return {"ok": True, "item": it}

    def abort(self, item_id: str, reason: str) -> dict:
        it = self.get(item_id)
        if not it:
            return {"ok": False, "error": f"no item {item_id}"}
        it["state"] = "BLOCKED"
        it["priority"], it["priority_locked"] = "INVESTIGATE", True
        it["notes"].append({"t": _now(), "text": f"SNIPE ABORTED: {reason}"})
        it["owner"] = None
        it["updated"] = _now()
        self._log(f"{it['id']} aborted: {reason[:80]}")
        self.save()
        return {"ok": True, "item": it}

    def set_config(self, **kw) -> dict:
        for k, v in kw.items():
            if k in self.data["config"] and v is not None:
                self.data["config"][k] = v if k not in ("max_patch_symbols", "max_patch_files", "test_timeout") else int(v)
        self.save()
        return self.data["config"]

    # ------------------------------------------------------------------ views
    def board(self) -> dict:
        issues = self.items(kind="issue")
        ready = [i for i in issues if i["state"] in ("FOUND", "TARGETED") and i["priority"] == "SNIPE NOW"]
        investigating = [i for i in issues if i["priority"] == "INVESTIGATE" and i["state"] != "DONE" or i["state"] == "BLOCKED"]
        current = [i for i in issues if i["state"] in ("FIXING", "VERIFYING")]
        deferred = [i for i in issues if i["priority"] in ("DEFER", "IGNORE") and i["state"] != "DONE"]
        opps = self.items(kind="opportunity", state="FOUND,TARGETED,FIXING,VERIFYING,BLOCKED")
        done = sorted([i for i in self.data["items"] if i["state"] == "DONE"], key=lambda i: i.get("done", ""), reverse=True)
        return {"project": self.data["project"], "ready": [brief(i) for i in ready[:20]],
                "investigating": [brief(i) for i in investigating[:20]], "current": [brief(i) for i in current],
                "deferred": len(deferred), "opportunities": [brief(i) for i in opps[:20]], "opportunities_total": len(opps),
                "done": [brief(i) for i in done[:10]], "done_total": len(done),
                "hot": len(self.data.get("hot", {})), "config": self.data["config"],
                "test_cmd": self.detect_any_test_cmd(), "log": self.data.get("log", [])[-30:][::-1]}

    def render_md(self) -> str:
        b = self.board()
        L = [f"# SNIPE - {b['project']}", "",
             "_Scan, Narrow, Infer, Patch, Evaluate. Managed by CodeAtlas; source of truth `.codeatlas/snipe.json`. "
             f"Updated {self.data.get('updated', _now())}._", "",
             "## READY TO SNIPE", "| score | id | problem | target | context | risk |", "|---|---|---|---|---|---|"]
        L += [f"| {i['score']} | {i['id']} | {i['problem'].replace('|', '/')} | `{i['suspect'] or '?'}` | "
              f"{i['symbols']} sym / ~{i['tokens']} tok | {i['risk']} |" for i in b["ready"]] or ["| | | _nothing ready: run `snipe_scan` or report a failure_ | | | |"]
        if b["current"]:
            L += ["", "## CURRENT"] + [f"- {i['owner'] or '?'}  {i['id']}  {i['state']}  {i['problem']}" for i in b["current"]]
        if b["investigating"]:
            L += ["", "## INVESTIGATING"] + [f"- {i['score']}  {i['id']}  {i['problem']}" + (f"  ({i['state']})" if i['state'] == 'BLOCKED' else "") for i in b["investigating"]]
        L += ["", f"## OPPORTUNITIES ({b['opportunities_total']}, dormant unless selected)"]
        L += [f"- {i['id']}  {i['problem']}" for i in b["opportunities"][:10]]
        if b["done"]:
            L += ["", f"## DONE (last {len(b['done'])} of {b['done_total']})"] + [f"- {i['id']}  {i['problem']}" for i in b["done"]]
        cfg = b["config"]
        L += ["", "## Verification", f"- test command: `{cfg.get('test_cmd') or b['test_cmd'] or 'not configured (snipe_config)'}`",
              f"- full suite: `{cfg.get('full_test_cmd') or '-'}`   typecheck: `{cfg.get('typecheck_cmd') or '-'}`   lint: `{cfg.get('lint_cmd') or '-'}`",
              f"- stop condition: > {cfg['max_patch_symbols']} symbols or > {cfg['max_patch_files']} files changed -> SNIPE ABORTED",
              "", "## The loop",
              "`next_snipe` -> `claim_snipe` -> `get_snipe_context` (level 1..6, climb only as needed) -> patch the smallest "
              "surface (`replace_symbol`) -> `verify_snipe` (direct -> blast radius -> full only if risky) -> DONE -> `next_snipe`.",
              "Found something else? `snipe_add` (issue) or leave it as an opportunity. Never widen a snipe into a refactor."]
        return "\n".join(L)


# ---------------------------------------------------------------------- rendering helpers
def brief(i: dict) -> dict:
    return {"id": i["id"], "kind": i["kind"], "state": i["state"], "problem": i["problem"], "suspect": i.get("suspect"),
            "suspect_path": i.get("suspect_path"), "score": i.get("score", 0), "priority": i.get("priority"),
            "confidence": i.get("confidence"), "symbols": 1 + len(i.get("related") or []),
            "tokens": i.get("context_tokens") or 0, "risk": i.get("risk", "low"), "owner": i.get("owner"),
            "source": i.get("source"), "updated": i.get("updated")}


def render_item(it: dict, brief: bool = False) -> str:
    L = [f"{it['id']}  [{it['state']}]  {it['priority'] or '-'}  score {it.get('score', 0)}"
         + (f"  owner {it['owner']}" if it.get("owner") else "") + (f"  ({it['kind']})" if it["kind"] != "issue" else ""),
         f"problem: {it['problem']}"]
    for e in it.get("evidence", [])[:6]:
        L.append(f"  evidence: {e}")
    if it.get("suspect"):
        L.append(f"suspect: {it['suspect']}  {it.get('suspect_path') or ''}:{it.get('suspect_line') or ''}  "
                 f"confidence {it.get('confidence') or 0:.2f}")
    if it.get("related"):
        L.append("related: " + ", ".join(it["related"][:4]))
    L.append(f"patch surface: {it.get('patch_surface', {}).get('symbols', 1)} symbol(s); context ~{it.get('context_tokens', 0)} tokens; "
             f"blast {it.get('blast', 0)} callers ({it.get('risk', 'low')} risk)")
    if it.get("verification"):
        L.append("verification: " + ", ".join(it["verification"][:4]))
    if not brief:
        for n in it.get("notes", [])[-3:]:
            L.append(f"  note {n['t']}: {n['text']}")
        f = it.get("factors") or {}
        if f:
            L.append("factors: " + ", ".join(f"{k} {v}" for k, v in f.items()))
    return "\n".join(L)


def render_next(it: Optional[dict]) -> str:
    if not it:
        return "nothing to snipe: run snipe_scan (optionally run='tests') or snipe_report with a failure"
    head = render_item(it, brief=True)
    if it.get("candidates"):
        c = it["candidates"][0]
        head += f"\nlikely cause: {c['symbol']} ({c['confidence']:.0%})" + (": " + c["evidence"][0] if c["evidence"] else "")
    return head + f"\nnext: claim_snipe(\"{it['id']}\") then get_snipe_context(\"{it['id']}\", level=1)"


def render_verify(r: dict) -> str:
    if not r.get("ok"):
        return r.get("error", "verify failed")
    it = r["item"]
    L = [f"{it['id']} verification: {r['status']}  (changed {r['changed']['symbols']} symbol(s) in {r['changed']['files']} file(s))"]
    for lvl in r["levels"]:
        L.append(f"  level {lvl['level']} {lvl['name']}:")
        for c in lvl["checks"]:
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "skip"}[c["status"]]
            L.append(f"    {mark:<4} {c['check']}" + (f"  - {c['detail'].splitlines()[-1][:120]}" if c["detail"] and c["status"] != "pass" else ""))
    if r["status"] == "PASS":
        L.append(f"{it['id']} -> DONE. next_snipe() for the next one.")
    elif r["status"] == "FAIL":
        L.append(f"{it['id']} -> back to INFER: read the failure evidence, re-run get_snipe_context, patch again.")
    elif r["status"] == "ABORTED":
        L.append(f"{it['id']} -> SNIPE ABORTED, promoted to INVESTIGATE. Stop, leave a note, pick the next snipe.")
    else:
        L.append("nothing changed since the claim, or no test command is configured (snipe_config test_cmd=...).")
    return "\n".join(L)


def render_board(b: dict, watcher_alive: bool = True, mcp: bool = True) -> str:
    L = [f"SNIPE - {b['project']}    graph OK   watcher {'OK' if watcher_alive else 'down'}   "
         f"tests {'OK' if b.get('test_cmd') or b['config'].get('test_cmd') else 'unconfigured'}   mcp {'OK' if mcp else '-'}",
         "", "READY TO SNIPE"]
    L += [f"  {i['score']:>3}  {i['id']:<8} {i['problem'][:70]}\n       {i['symbols']} symbol(s) · ~{i['tokens']} tokens · {i['risk']} risk"
          for i in b["ready"][:10]] or ["  (nothing ready: snipe_scan, snipe_scan run='tests', or snipe_report)"]
    if b["investigating"]:
        L += ["", "INVESTIGATING"] + [f"  {i['score']:>3}  {i['id']:<8} {i['problem'][:70]}" for i in b["investigating"][:8]]
    if b["current"]:
        L += ["", "CURRENT"] + [f"  {i['owner'] or '?':<10} {i['id']:<8} {i['state']:<10} {i['problem'][:60]}" for i in b["current"]]
    L += ["", f"deferred {b['deferred']} · opportunities {b['opportunities_total']} (dormant) · done {b['done_total']} · hot symbols {b['hot']}"]
    return "\n".join(L)
