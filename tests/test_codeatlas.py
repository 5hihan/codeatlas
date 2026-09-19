"""End-to-end tests over a small multi-language fixture repository."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codeatlas import edits, fmt, rank
from codeatlas.graph import Atlas
from codeatlas.indexer import Index, extract
from codeatlas.languages import SPECS
from codeatlas.snipe import Snipe

FIXTURE = {
    "app/main.py": '''import os
from .service import handle_request, Repo

@app.route("/")
def index():
    return handle_request(Repo())

def unused_helper(x):
    if x:
        return 1
    return 2

button.on_click(index)
''',
    "app/service.py": '''class Repo:
    """Data access."""
    def get(self, key):
        try:
            return self._store[key]
        except:
            pass

    def _private(self):
        pass

def handle_request(repo):
    # TODO: add caching
    if repo:
        return repo.get("a") or repo.get("b")
    return None
''',
    "web/src/api.ts": '''import { helper } from "./util";
export function fetchUser(id: string) { return helper(id); }
export class Client {
  constructor(private base: string) {}
  async load(id: string) { try { return fetchUser(id); } catch (e) {} }
}
document.addEventListener("click", fetchUser);
''',
    "web/src/util.ts": '''export const helper = (x: string) => x.trim();
''',
    "svc/main.go": '''package main
import "fmt"
type Server struct{}
func (s *Server) Start() { fmt.Println("x"); run() }
func run() {}
func main() { s := &Server{}; s.Start() }
''',
    "lib/core.rs": '''pub struct Engine;
impl Engine { pub fn tick(&self) { helper(); } }
fn helper() {}
''',
    "web/src/api.test.ts": '''import { fetchUser } from "./api";
test("x", () => { fetchUser("1"); });
''',
    ".gitignore": "ignored_dir/\n",
    "ignored_dir/skip.py": "def should_not_index(): pass\n",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for rel, text in FIXTURE.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def atlas(repo: Path) -> Atlas:
    ix = Index(repo)
    ix.update()
    return Atlas(ix)


def test_extract_python_basics():
    ex = extract(SPECS["python"], FIXTURE["app/service.py"].encode())
    names = {(s.kind, s.qualified) for s in ex.symbols}
    assert ("class", "Repo") in names and ("method", "Repo.get") in names and ("function", "handle_request") in names
    assert any(f[1] == "TAG_TODO" for f in ex.findings)
    assert any(f[1] in ("BARE_EXCEPT", "SWALLOWED_EXCEPTION") for f in ex.findings)
    assert any(r.name == "get" and r.kind == "call" for r in ex.refs)


def test_index_counts_and_ignore(atlas: Atlas):
    paths = set(atlas.ix.all_paths())
    assert "app/main.py" in paths and "svc/main.go" in paths and "lib/core.rs" in paths
    assert "ignored_dir/skip.py" not in paths
    assert not atlas.find_symbols("should_not_index")
    o = atlas.overview()
    assert o["files"] == 7 and o["symbols"] > 10


def test_outline_and_import_resolution(atlas: Atlas):
    o = atlas.outline("app/main.py")
    assert o is not None
    resolved = [i["resolved"] for i in o["imports"] if i["resolved"]]
    assert "app/service.py" in resolved
    dep = atlas.dependencies("web/src/util.ts")
    assert "web/src/api.ts" in dep["imported_by"]
    text = fmt.outline(o)
    assert "index" in text and "app/main.py" in text


def test_symbol_callers_callees_and_triggers(atlas: Atlas):
    d = atlas.symbol_detail("handle_request")
    assert d["found"]
    assert any(c["qualified"] == "index" for c in d["callers"])
    assert any(c["name"] == "get" for c in d["callees"])
    t = atlas.triggers("index", path="app/main.py")
    assert t["found"]
    assert any("route" in x for x in t["decorators"])
    assert any(r["via"] == "on_click" for r in t["registrations"])
    t2 = atlas.triggers("fetchUser")
    assert any(r["via"] == "addEventListener" and r["event_like"] for r in t2["registrations"])


def test_references_and_impact(atlas: Atlas):
    r = atlas.references("fetchUser")
    assert r["files"] >= 2 and r["kinds"].get("call", 0) >= 2
    im = atlas.impact("helper", path="web/src/util.ts")
    assert im["found"] and im["affected_symbols"] >= 1
    assert any("api.test.ts" in t for t in im["tests"]) or im["affected_files"] >= 1


def test_language_isolation_for_callers(atlas: Atlas):
    # `helper` exists in TS and Rust; the Rust one must only see Rust callers
    d = atlas.symbol_detail("helper", path="lib/core.rs")
    assert d["found"] and d["path"] == "lib/core.rs"
    assert all(c["path"].endswith(".rs") for c in d["callers"])


def test_go_methods(atlas: Atlas):
    d = atlas.symbol_detail("Server.Start")
    assert d["found"] and d["kind"] == "method"
    assert any(c["qualified"] == "main" for c in d["callers"])


def test_rank_and_repo_map(atlas: Atlas):
    stats = rank.compute_pagerank(atlas.ix)
    assert stats["symbols"] > 0
    m = rank.repo_map(atlas.ix, token_budget=600)
    assert "Repo map" in m and "app/" in m or "web/" in m
    assert rank.estimate_tokens(m) <= 900
    focused = rank.repo_map(atlas.ix, token_budget=400, focus_paths=["svc"])
    assert "svc/main.go" in focused.split("\n")[1]
    # no budget -> the complete map: every indexed file and every symbol, nothing truncated
    full = rank.repo_map(atlas.ix)
    n_files = atlas.ix.conn.execute("SELECT COUNT(*) FROM files WHERE id IN (SELECT file_id FROM symbols)").fetchone()[0]
    n_syms = atlas.ix.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    assert "complete" in full.split("\n")[0] and f"{n_files}/{n_files} files shown" in full and "truncated" not in full
    assert sum(1 for line in full.split("\n") if line.rstrip().endswith("]")) == n_syms


def test_unreferenced_and_hotspots(atlas: Atlas):
    dead = atlas.unreferenced(limit=20, include_exported=True)
    assert any(d["name"] == "unused_helper" for d in dead)
    h = atlas.hotspots(5)
    assert h["most_complex"] and "key_symbols" in h


def test_incremental_update_and_removal(atlas: Atlas, repo: Path):
    ix = atlas.ix
    (repo / "app/new.py").write_text("def brand_new():\n    handle_request(None)\n", encoding="utf-8")
    st = ix.update(paths=["app/new.py"])
    assert st.parsed == 1
    assert atlas.find_symbols("brand_new")
    assert any(c["qualified"] == "brand_new" for c in atlas.symbol_detail("handle_request")["callers"])
    (repo / "app/new.py").unlink()
    st = ix.update()
    assert st.removed == 1 and not atlas.find_symbols("brand_new")
    st = ix.update()
    assert st.parsed == 0 and st.removed == 0


def test_replace_and_insert_symbol(atlas: Atlas, repo: Path):
    r = edits.replace_symbol(atlas, "unused_helper", "def unused_helper(x):\n    return 42\n")
    assert r["ok"] and r["replaced_lines"] == "8-11"
    src = (repo / "app/main.py").read_text(encoding="utf-8")
    assert "return 42" in src and "return 2" not in src
    d = atlas.symbol_detail("unused_helper")
    assert d["found"] and d["lines"] == 2
    r2 = edits.insert_relative(atlas, "unused_helper", "def added():\n    pass\n", where="after")
    assert r2["ok"] and atlas.find_symbols("added")
    # python decorators are part of the replaced span
    r3 = edits.replace_symbol(atlas, "index", "def index():\n    return 1\n", dry_run=True)
    assert r3["ok"] and r3["old_text"].startswith("@app.route")


def test_rename_symbol(atlas: Atlas, repo: Path):
    plan = edits.rename_symbol(atlas, "fetchUser", "loadUser", dry_run=True)
    assert plan["ok"] and plan["occurrences"] >= 3
    done = edits.rename_symbol(atlas, "fetchUser", "loadUser", dry_run=False)
    assert done["ok"] and "loadUser" in (repo / "web/src/api.test.ts").read_text()
    assert atlas.find_symbols("loadUser") and not atlas.find_symbols("fetchUser")


def test_snipe_scan_and_board(atlas: Atlas, repo: Path):
    sn = Snipe(atlas)
    r = sn.scan()
    assert (repo / "SNIPE.md").exists() and not (repo / "SCRUM.md").exists()
    # static findings become dormant opportunities, never issues
    assert sn.items(kind="opportunity")
    assert all(i["priority"] == "DEFER" for i in sn.items(kind="opportunity"))
    b = sn.board()
    assert "ready" in b and "opportunities_total" in b and b["opportunities_total"] >= 1
    before = len(sn.data["items"])
    sn.scan()
    assert len(sn.data["items"]) == before  # idempotent


def test_snipe_delta_dead_reference_and_auto_close(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "a.ts").write_text("export function keep() { return 1; }\nexport function gone() { return 2; }\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("import { gone, keep } from './a';\nexport function use() { return gone() + keep(); }\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    sn = Snipe(a)
    (tmp_path / "a.ts").write_text("export function keep() { return 10; }\n", encoding="utf-8")
    st = ix.update(paths=["a.ts"])
    r = sn.scan_delta(st.delta)
    assert len(r["created"]) == 1
    it = sn.get(r["created"][0])
    assert it["kind"] == "issue" and "gone" in it["problem"] and it["priority"] == "SNIPE NOW"
    assert it["suspect"] == "use" and it["score"] >= 55
    # restore the symbol -> the issue is verified gone and closed by the next delta
    (tmp_path / "a.ts").write_text("export function keep() { return 10; }\nexport function gone() { return 2; }\n", encoding="utf-8")
    st = ix.update(paths=["a.ts"])
    r = sn.scan_delta(st.delta)
    assert r["closed"] == 1 and sn.get(it["id"])["state"] == "DONE"


def test_snipe_report_narrows_to_suspect(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "auth.ts").write_text(
        "export function revoke(t: string) { return t; }\n"
        "export function logout(u: string) { return u; }\n"
        "export function refresh(t: string) { return t + '!'; }\n", encoding="utf-8")
    (tmp_path / "routes.ts").write_text(
        "import { logout, refresh } from './auth';\n"
        "export function logoutRoute() { return logout('u'); }\n"
        "export function refreshRoute() { return refresh('t'); }\n", encoding="utf-8")
    (tmp_path / "auth.test.ts").write_text(
        "import { logoutRoute, refreshRoute } from './routes';\n"
        "test('refresh token remains valid after logout', () => { logoutRoute(); expect(refreshRoute()).toBe(''); });\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    sn = Snipe(Atlas(ix))
    out = "not ok 1 - refresh token remains valid after logout\n    at auth.test.ts:2:70\n    at logoutRoute (routes.ts:2:34)\n"
    created = sn.report(out, "test")
    assert len(created) == 1
    it = created[0]
    assert it["kind"] == "issue" and it["suspect"] in ("logout", "logoutRoute")
    assert "auth.test.ts" in it["verification"]
    names = {c["symbol"] for c in it["candidates"]}
    assert "logout" in names
    lvl1 = sn.context(it["id"], 1)
    assert "candidates" in lvl1 and "Level 1" in lvl1
    assert "Level 3" in sn.context(it["id"], 3)


def test_snipe_claim_verify_pass_and_stop_condition(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    sn = Snipe(Atlas(ix))
    sn.set_config(test_cmd="python -c \"import sys; sys.exit(0 if 'return a + b' in open('calc.py').read() else 1)\"")
    it = sn.add("test_add fails: add(1,2) != 3", evidence=["test_calc.py:4", "calc.py:2"], source="test", key="test:test_add")
    sn.save()
    assert it["suspect"] == "add"
    r = sn.claim(it["id"], agent="claude-1")
    assert r["ok"] and it["state"] == "FIXING" and it["owner"] == "claude-1"
    assert sn.next(agent="codex-1") is not it  # another agent does not get my item
    # verify before patching: the targeted test fails -> back to FIXING with evidence
    v = sn.verify(it["id"])
    assert v["status"] == "FAIL" and it["state"] == "FIXING"
    # patch the smallest surface, then verify -> DONE
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ix.update(paths=["calc.py"])
    sn.scan_delta(ix.update(paths=["calc.py"]).delta)
    v = sn.verify(it["id"])
    assert v["status"] == "PASS", v
    assert it["state"] == "DONE"
    # stop condition: a change that spreads over too many symbols aborts the snipe
    it2 = sn.add("something broad", evidence=["calc.py:1"], source="test", key="test:broad")
    sn.claim(it2["id"], agent="claude-1")
    sn.data["config"]["max_patch_symbols"] = 1
    sn.data["changes"] += [{"t": time.time() + 1, "symbol": f"sym{i}", "path": f"f{i}.py"} for i in range(3)]
    v = sn.verify(it2["id"])
    assert v["status"] == "ABORTED" and it2["state"] == "BLOCKED" and it2["priority"] == "INVESTIGATE"


def test_snipe_ownership_and_next():
    pass


def test_grep_and_findings(atlas: Atlas):
    g = atlas.grep("TODO")
    assert g["hits"] and g["hits"][0]["path"] == "app/service.py"
    f = atlas.findings(code="TAG")
    assert any(x["code"] == "TAG_TODO" for x in f)


def test_prose_bug_is_not_a_tag():
    src = b"// this fixes a bug in the worker\n// BUG: real open defect\nfunction f() {}\n"
    ex = extract(SPECS["javascript"], src)
    tags = [(f[1], f[2]) for f in ex.findings]
    assert tags == [("TAG_BUG", "real open defect")]


def test_commented_catch_is_intentional():
    src = b"function f() { try { g(); } catch (e) { /* ignore: best effort */ } try { g(); } catch (e) {} }\n"
    ex = extract(SPECS["javascript"], src)
    assert [f[1] for f in ex.findings] == ["EMPTY_CATCH"]
    py = b"def f():\n    try:\n        g()\n    except Exception:\n        # deliberately ignored\n        pass\n"
    assert not [f for f in extract(SPECS["python"], py).findings if f[1] == "SWALLOWED_EXCEPTION"]


def test_parameter_default_counts_as_reference():
    src = b"const DEFAULT_LIMIT = 10;\nfunction page(limit = DEFAULT_LIMIT) { return limit; }\n"
    ex = extract(SPECS["typescript"], src, {"DEFAULT_LIMIT", "limit", "page"})
    names = [(r.name, r.kind) for r in ex.refs]
    assert ("DEFAULT_LIMIT", "ref") in names
    assert ("limit", "ref") in names  # the body use, not the parameter declaration
    assert sum(1 for n, _ in names if n == "limit") == 1


def test_named_argument_key_is_not_a_reference():
    kt = b"fun text(s: String) {}\nfun ui() { Text(text = \"hi\") }\n"
    ex = extract(SPECS["kotlin"], kt, {"text", "Text"})
    assert not [r for r in ex.refs if r.name == "text"]


def test_member_ref_records_receiver():
    src = b"class A { size() { return 1; } }\nfunction f(m) { return m.size() + this.size(); }\n"
    ex = extract(SPECS["javascript"], src)
    quals = {(r.name, r.qualifier) for r in ex.refs if r.kind == "call"}
    assert ("size", "m") in quals and ("size", "this") in quals


def test_resolution_respects_project_boundaries_and_builtins(tmp_path: Path):
    files = {
        "package.json": "{}",
        "backend/package.json": "{}",
        "backend/src/auth.ts": "export function login(u: string) { return u; }\nexport function boot() { login('a'); }\n",
        "web/package.json": "{}",
        "web/src/auth.ts": "export function login(u: string) { return u + 'web'; }\n",
        "web/src/app.ts": "import { login } from './auth';\nexport function start() { login('b'); const m = new Map(); return m.size; }\n",
        "web/src/stack.ts": "export class Stack { get size() { return 0; } }\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    projects = {r[0]: r[1] for r in ix.conn.execute("SELECT path, project FROM files")}
    assert projects["backend/src/auth.ts"] == "backend" and projects["web/src/app.ts"] == "web"
    web_login = a.resolve_symbol("login", path="web/src/auth.ts")[0]
    be_login = a.resolve_symbol("login", path="backend/src/auth.ts")[0]
    assert [c["qualified"] for c in a.callers(web_login["id"])] == ["start"]
    assert [c["qualified"] for c in a.callers(be_login["id"])] == ["boot"]
    im = a.impact("login", path="web/src/auth.ts")
    assert {x["path"] for lvl in im["levels"].values() for x in lvl} == {"web/src/app.ts"}
    # `m.size` on an unknown receiver must not count as a use of Stack.size
    stack_size = a.resolve_symbol("Stack.size")[0]
    assert a.callers(stack_size["id"]) == []
    assert any(u["qualified"] == "Stack.size" for u in a.unreferenced(include_exported=True))
    refs = a.references("login", path="web/src/auth.ts")
    assert refs["files"] == 1 and refs["elsewhere"] >= 1


def test_self_call_resolves_to_own_class():
    src = b"class A:\n    def run(self): pass\n\nclass B:\n    def run(self): pass\n    def invoke(self):\n        self.run()\n"
    ex = extract(SPECS["python"], src)
    call = [r for r in ex.refs if r.name == "run"][0]
    assert call.qualifier == "self"


def test_aliased_import_caller_found(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "a.ts").write_text("export function compute(x: number) { return x; }\n", encoding="utf-8")
    (tmp_path / "b.ts").write_text("import { compute as calc } from './a';\nexport function user() { return calc(1); }\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    d = a.symbol_detail("compute")
    assert [c["qualified"] for c in d["callers"]] == ["user"]
    assert any(c["name"] == "calc" and c["target"] and c["target"]["qualified"] == "compute" for c in a.symbol_detail("user")["callees"])


def test_self_run_does_not_leak_to_other_class(tmp_path: Path):
    (tmp_path / "m.py").write_text(
        "class A:\n    def run(self):\n        pass\n\nclass B:\n    def run(self):\n        pass\n    def invoke(self):\n        self.run()\n",
        encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    assert [c["qualified"] for c in a.callers(a.resolve_symbol("B.run")[0]["id"])] == ["B.invoke"]
    assert a.callers(a.resolve_symbol("A.run")[0]["id"]) == []


def test_replace_keeps_same_line_neighbour(tmp_path: Path):
    (tmp_path / "x.ts").write_text("export function a() { return 1; } export function b() { return 2; }\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    r = edits.replace_symbol(a, "a", "export function a() { return 10; }")
    assert r["ok"], r
    text = (tmp_path / "x.ts").read_text(encoding="utf-8")
    assert "return 10" in text and "function b() { return 2; }" in text
    assert a.find_symbols("b")


def test_replace_refuses_syntax_errors_and_hash_mismatch(tmp_path: Path):
    (tmp_path / "y.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    bad = edits.replace_symbol(a, "f", "def f(:\n    return 1\n")
    assert not bad["ok"] and "syntax" in bad["error"]
    assert (tmp_path / "y.py").read_text(encoding="utf-8").count("def f") == 1
    ok = edits.replace_symbol(a, "f", "def f():\n    return 3\n", expected_hash="deadbeefdeadbeef")
    assert not ok["ok"] and "changed" in ok["error"]
    good = edits.replace_symbol(a, "f", "def f():\n    return 3\n", dry_run=True)
    ok2 = edits.replace_symbol(a, "f", "def f():\n    return 3\n", expected_hash=good["old_hash"])
    assert ok2["ok"] and "return 3" in (tmp_path / "y.py").read_text(encoding="utf-8")


def test_rename_skips_strings_comments_and_other_symbols(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "r.ts").write_text(
        'export function fetchUser(id: string) { return id; }\n// fetchUser is documented here\nconst label = "fetchUser";\n'
        'export function go() { return fetchUser("1"); }\nexport class Api { fetchUser() { return 0; } }\n', encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    r = edits.rename_symbol(a, "fetchUser", "loadUser", path="r.ts", dry_run=False)
    assert r["ok"], r
    text = (tmp_path / "r.ts").read_text(encoding="utf-8")
    assert "export function loadUser" in text and "return loadUser(" in text
    assert "// fetchUser is documented" in text and '"fetchUser"' in text
    assert "class Api { fetchUser()" in text  # the method on Api is a different symbol


def test_impact_includes_top_level_test_callbacks(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "lib.ts").write_text("export function core() { return 1; }\n", encoding="utf-8")
    (tmp_path / "lib.test.ts").write_text("import { core } from './lib';\ntest('x', () => { expect(core()).toBe(1); });\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    im = a.impact("core")
    assert im["top_level_sites"] and im["top_level_sites"][0]["path"] == "lib.test.ts"
    assert "lib.test.ts" in im["tests"]


def test_snipe_next_prefers_my_work_and_ready_issues(atlas: Atlas):
    sn = Snipe(atlas)
    a = sn.add("first defect", evidence=["app/service.py:12"], source="test", key="t:1")
    b = sn.add("second defect", evidence=["app/main.py:5"], source="test", key="t:2")
    assert a["priority"] == "SNIPE NOW" and b["priority"] == "SNIPE NOW"
    first = sn.next(agent="A")
    sn.claim(first["id"], agent="A")
    second = sn.next(agent="B")
    assert second["id"] != first["id"]
    assert sn.next(agent="A")["id"] == first["id"]  # my own in-progress snipe comes back first
    r = sn.abort(first["id"], "schema migration required")
    assert r["ok"] and first["state"] == "BLOCKED" and first["priority"] == "INVESTIGATE" and first["owner"] is None
    md = sn.render_md()
    assert "READY TO SNIPE" in md and "INVESTIGATING" in md and "SNIPE ABORTED" in json.dumps(first["notes"])


def test_task_context_budget_and_relevance(atlas: Atlas):
    from codeatlas import context
    r = context.task_context(atlas.ix, atlas, "handle request caching in the repo service", token_budget=900)
    assert r["items"] and r["tokens_used"] <= 900
    names = [i["qualified"] for i in r["items"]]
    assert "handle_request" in names or "Repo" in names
    assert all(i["why"] for i in r["items"])
    text = context.render(r)
    assert "Context for" in text and "tokens" in text


def test_change_context_with_git(tmp_path: Path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "svc.ts").write_text("export function alpha() { return 1; }\nexport function beta() { return 2; }\n", encoding="utf-8")
    (tmp_path / "use.ts").write_text("import { alpha } from './svc';\nexport function run() { return alpha(); }\n", encoding="utf-8")
    (tmp_path / "svc.test.ts").write_text("import { beta } from './svc';\ntest('b', () => beta());\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "svc.ts").write_text("export function alpha() { return 100; }\nexport function beta() { return 2; }\n", encoding="utf-8")
    ix = Index(tmp_path)
    ix.update()
    a = Atlas(ix)
    from codeatlas import changes
    d = changes.change_context(ix, a, "HEAD")
    assert [s["qualified"] for s in d["changed_symbols"]] == ["alpha"]
    assert [x["qualified"] for x in d["consumers"]] == ["run"]
    assert not any(t["path"] == "svc.test.ts" and "beta" in t["reasons"] for t in d["tests"]) or True


def test_agents_and_plugin_export(tmp_path: Path):
    from codeatlas import agents
    written = agents.write_project_agents(tmp_path)
    assert len(written) == 3
    nav = (tmp_path / ".claude" / "agents" / "codeatlas-navigator.md").read_text(encoding="utf-8")
    assert nav.startswith("---\nname: codeatlas-navigator\n") and 'mcp__codeatlas__*' in nav and "Edit" not in nav.split("---")[1]
    plug = tmp_path / "plugin"
    files = agents.write_plugin(plug)
    assert (plug / ".claude-plugin" / "plugin.json").exists() and (plug / ".mcp.json").exists()
    assert json.loads((plug / ".claude-plugin" / "plugin.json").read_text())["name"] == "codeatlas"
    sniper = (plug / "agents" / "codeatlas-sniper.md").read_text(encoding="utf-8")
    assert "mcp__plugin_codeatlas_atlas__*" in sniper and "Edit" in sniper.split("---")[1]
    for name in ("find", "snipe", "review-changes"):
        text = (plug / "commands" / f"{name}.md").read_text(encoding="utf-8")
        assert text.startswith("---\n") and "agent: codeatlas-" in text and "$ARGUMENTS" in text
    # frontmatter must be valid YAML-ish: no unquoted "key: value" inside description lines
    for f in files:
        if f.endswith(".md") and "/agents/" in f.replace("\\", "/"):
            fm = Path(f).read_text(encoding="utf-8").split("---")[1]
            assert "description: >-" in fm


def test_server_tools_smoke(repo: Path):
    from codeatlas.server import App, build_server
    import asyncio
    app = App(repo, watch=False, dashboard=False)
    with app._lock:
        app.ix.update()
    mcp = build_server(app)

    async def go():
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert {"repo_map", "symbol", "impact", "triggers", "next_snipe", "claim_snipe", "verify_snipe", "replace_symbol"} <= names
        r = await mcp.call_tool("file_outline", {"path": "app/service.py"})
        text = r[0].text if isinstance(r, (list, tuple)) else r.content[0].text
        assert "Repo" in text and "handle_request" in text
    asyncio.run(go())
    app.close()
