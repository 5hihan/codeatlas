"""Connections: routes from what an agent knows to what it wants, and `known` anchors in task_context."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from codeatlas import cli, connect, context, rank
from codeatlas.budgeting import plan_context
from codeatlas.connect import WorkingSet, hub_penalty
from codeatlas.graph import Atlas
from codeatlas.indexer import Index

FIXTURE = {
    # main -> handle -> process -> Store.save -> Store._write, plus a module-level registration of main
    "app/main.py": "from .handlers import handle\n\ndef main():\n    return handle('x')\n\nbutton.on_click(main)\n",
    "app/handlers.py": ("from .service import process, log\n\ndef handle(req):\n    log('h')\n    return process(req)\n\n"
                        "def other():\n    log('o')\n    return 1\n"),
    "app/service.py": ("import os\nfrom .store import Store\n\ndef process(req):\n    s = Store()\n    log('p')\n"
                       "    return s.save(req)\n\ndef log(msg):\n    pass\n\ndef connection():\n    return os.getenv('DB_URL')\n"),
    "app/store.py": "class Store:\n    def save(self, x):\n        return self._write(x)\n\n    def _write(self, x):\n        return x\n",
    "tests/test_store.py": "from app.store import Store\n\ndef test_save():\n    assert Store().save(1) == 1\n",
    ".env": "DB_URL=postgres://example\n",
    # a same-named decoy that is better referenced but not connected to app/
    "legacy/process.py": "def process(req):\n    return req\n",
    "legacy/a.py": "from .process import process\n\ndef a():\n    return process(1)\n",
    "legacy/b.py": "from .process import process\n\ndef b():\n    return process(2)\n",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for rel, text in FIXTURE.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def atlas(repo: Path):
    ix = Index(repo)
    ix.update()
    a = Atlas(ix)
    yield a
    ix.close()


def _routes(result: dict) -> list[list[str]]:
    return [[n["label"] for n in p["nodes"]] for p in result["paths"]]


# ---------------------------------------------------------------------- persisted edges
def test_pagerank_persists_resolved_edges_including_top_level(atlas: Atlas):
    stats = rank.compute_pagerank(atlas.ix)
    assert stats["stored_edges"] > 0
    rows = atlas.conn.execute(
        "SELECT s1.qualified, s2.qualified, e.kind, e.confidence, e.line FROM symbol_edges e "
        "LEFT JOIN symbols s1 ON s1.id=e.src JOIN symbols s2 ON s2.id=e.dst").fetchall()
    edges = {(r[0], r[1], r[2]) for r in rows}
    assert ("main", "handle", "call") in edges
    assert ("process", "Store.save", "call") in edges
    assert ("Store.save", "Store._write", "call") in edges
    # module-level `button.on_click(main)` keeps only its file as the source
    assert (None, "main", "arg") in edges
    # `from .service import process` is one import edge per importing file (handlers.py, legacy/a.py, legacy/b.py),
    # never an extra identifier edge for the same line
    kinds = [r[2] for r in rows if r[0] is None and r[1] == "process" and r[4] == 1]
    assert kinds.count("import") == 3 and "ref" not in kinds
    assert atlas.ix.get_meta("edges_version") == atlas.ix.get_meta("rank_updated")


def test_rank_is_stale_until_edges_exist(atlas: Atlas):
    rank.ensure_rank(atlas.ix)
    assert not rank.rank_is_stale(atlas.ix)
    atlas.ix.set_meta("edges_version", "0")  # an index built before symbol_edges existed
    assert rank.rank_is_stale(atlas.ix)
    rank.ensure_rank(atlas.ix)
    assert not rank.rank_is_stale(atlas.ix)
    assert atlas.conn.execute("SELECT COUNT(*) FROM symbol_edges").fetchone()[0] > 0


# ---------------------------------------------------------------------- routes
def test_call_chain_route_with_labelled_hops(atlas: Atlas):
    r = connect.connections(atlas.ix, atlas, "Store.save", ["main"])
    assert _routes(r)[0] == ["main", "handle", "process", "Store.save"]
    first = r["paths"][0]
    assert [e["label"] for e in first["edges"]] == ["calls", "calls", "calls"]
    assert [e["line"] for e in first["edges"]] == [4, 5, 7]
    assert all(e["forward"] for e in first["edges"]) and first["hops"] == 3
    text = connect.render(r)
    assert "main (app/main.py:3) --calls@4--> handle (app/handlers.py:3) --calls@5--> process (app/service.py:4) " \
           "--calls@7--> Store.save (app/store.py:2)" in text
    assert 'symbol_source("handle", path="app/handlers.py")' in text  # bridges are expandable
    assert r["bridges"][0]["label"] in ("handle", "process")


def test_reverse_direction_is_shown_with_reversed_arrows(atlas: Atlas):
    r = connect.connections(atlas.ix, atlas, "main", ["Store._write"])
    assert _routes(r)[0] == ["Store._write", "Store", "process", "handle", "main"]
    text = connect.render(r)
    assert "<--contains-- Store" in text and "<--calls@5-- process" in text and "<--calls@4-- main" in text
    assert not any(e["forward"] for e in r["paths"][0]["edges"])


def test_file_anchor_reaches_symbol_through_its_definitions_not_only_imports(atlas: Atlas):
    r = connect.connections(atlas.ix, atlas, "process", ["app/main.py"], limit=3)
    routes = _routes(r)
    assert routes[0] == ["app/main.py", "handle", "process"]
    assert r["paths"][0]["edges"][0]["label"] == "imports" and r["paths"][0]["edges"][1]["label"] == "calls"
    # the alternative route goes file -> file -> symbol
    assert ["app/main.py", "app/handlers.py", "process"] in routes


def test_test_file_route_prefers_the_actual_call_over_the_import(atlas: Atlas):
    r = connect.connections(atlas.ix, atlas, "Store.save", ["tests/test_store.py", "main"], limit=4)
    routes = _routes(r)
    assert routes[0] == ["tests/test_store.py", "test_save", "Store.save"]
    assert r["paths"][0]["edges"][0]["label"] == "defines"
    assert ["main", "handle", "process", "Store.save"] in routes
    assert ["tests/test_store.py", "Store", "Store.save"] in routes  # the import route is still reported
    assert len(routes) == len({tuple(x) for x in routes})


def test_limit_bounds_routes_and_no_route_is_honest(atlas: Atlas):
    one = connect.connections(atlas.ix, atlas, "Store.save", ["tests/test_store.py", "main"], limit=1)
    assert len(one["paths"]) == 1
    none = connect.connections(atlas.ix, atlas, "Store._write", ["other"], max_hops=1)
    assert none["paths"] == [] and [u["label"] for u in none["unreached"]] == ["Store._write"]
    assert "no route within ~1 hops: Store._write (app/store.py:5)" in connect.render(none)
    far = connect.connections(atlas.ix, atlas, "Store._write", ["other"], max_hops=8)
    assert far["paths"], "a longer budget finds the route through the shared file"


def test_config_artifact_targets_resolve_through_relationships(atlas: Atlas):
    r = connect.connections(atlas.ix, atlas, ".env", ["connection"])
    assert _routes(r) == [["connection", "app/service.py", ".env"]]
    assert r["paths"][0]["edges"][1]["kind"] == "uses_env" and r["targets"][0]["type"] == "artifact"
    assert "--uses_env@13--> .env" in connect.render(r)


def test_unresolved_inputs_are_reported_with_suggestions(atlas: Atlas):
    r = connect.connections(atlas.ix, atlas, "nonexistent_thing", ["main"])
    assert r["paths"] == [] and r["unresolved_targets"][0]["input"] == "nonexistent_thing"
    assert "not found: nonexistent_thing" in connect.render(r)
    r = connect.connections(atlas.ix, atlas, "Store.save", [])
    assert "nothing known to start from" in r["notes"][0]
    r = connect.connections(atlas.ix, atlas, "main", ["main"])
    assert r["paths"] == [] and "itself one of the known items" in r["notes"][0]


def test_position_anchors_and_target_lists(atlas: Atlas):
    r = connect.connections(atlas.ix, atlas, ["Store.save", "log"], ["app/main.py:4"])  # inside main()
    assert r["known"][0]["label"] == "main"
    targets = {p["target"]["label"] for p in r["paths"]}
    assert targets == {"Store.save", "log"}
    assert r["paths"] == sorted(r["paths"], key=lambda p: (p["cost"], p["hops"]))


def test_hub_penalty_grows_with_degree_and_is_capped():
    assert hub_penalty(0) == hub_penalty(2) == 0.0
    assert 0 < hub_penalty(5) < hub_penalty(50) <= hub_penalty(5000) == connect.HUB_PENALTY_MAX


def test_route_tree_merges_shared_prefixes(atlas: Atlas):
    g = connect.Graph(atlas.ix, atlas)
    anchors, _ = connect.resolve_anchors(g, ["main"])
    exp = connect.explore(g, [(n, "main") for n, _ in anchors])
    paths = [connect.path_dict(g, exp.path_to(n)) for n in (("s", s["id"]) for s in (
        atlas.resolve_symbol("Store.save")[0], atlas.resolve_symbol("process", "app/service.py")[0], atlas.resolve_symbol("log")[0]))]
    lines = connect.format_route_tree(paths)
    assert lines[0] == "main (app/main.py:3)"
    assert lines[1] == "  --calls@4--> handle (app/handlers.py:3)"
    assert sum("handle" in ln for ln in lines) == 1, "the shared prefix is printed once"
    assert any(ln.strip().startswith("--calls@7--> Store.save (:2)") or ln.strip().endswith("Store.save (app/store.py:2)") for ln in lines)


# ---------------------------------------------------------------------- task_context with known anchors
def test_known_anchor_reranks_the_connected_definition_first(atlas: Atlas):
    without = context.task_context(atlas.ix, atlas, "process request", include_source=False)
    assert without["items"][0]["qualified"] == "process" and without["items"][0]["path"] == "legacy/process.py"
    with_known = context.task_context(atlas.ix, atlas, "process request", known=["main"], include_source=False)
    first = with_known["items"][0]
    assert first["qualified"] == "process" and first["path"] == "app/service.py"
    assert first["why"].startswith("2 hops from main")
    assert with_known["known"][0]["label"] == "main" and with_known["known_source"] == "given"
    assert "main" not in [i["qualified"] for i in with_known["items"]], "known anchors are not repeated"
    assert any(p["target"]["label"] == "process" and p["target"]["path"] == "app/service.py" for p in with_known["paths"])
    assert any(u["path"] == "legacy/process.py" for u in with_known["unreached"])
    assert with_known["budget_plan"]["allocations"]["paths"] > 0
    assert "paths" not in without["budget_plan"]["allocations"]


def test_bridges_and_route_tree_in_rendered_context(atlas: Atlas):
    r = context.task_context(atlas.ix, atlas, "save to the store", known=["main"], token_budget=3000)
    bridges = [i for i in r["items"] if i["bridge"]]
    assert bridges and all(i["source"] is None for i in bridges), "bridges stay signatures"
    assert any(i["qualified"] == "handle" and i["why"].startswith("links main to") for i in bridges)
    text = context.render(r)
    assert "known (given): main (app/main.py:3)" in text
    assert "## How what you know connects to these" in text
    assert "  main (app/main.py:3)\n    --calls@4--> handle (app/handlers.py:3)\n      --calls@5--> process (app/service.py:4)" in text
    assert r["tokens_used"] <= 3000


def test_unknown_known_item_is_noted_and_string_known_accepted(atlas: Atlas):
    r = context.task_context(atlas.ix, atlas, "save to the store", known="no_such_symbol", include_source=False)
    assert any("known item 'no_such_symbol' not found" in n for n in r["notes"])
    assert r["known"] == [] and r["paths"] == []
    assert r["items"], "retrieval still works without a usable anchor"


def test_budget_plan_reserves_paths_only_with_known_anchors(atlas: Atlas):
    plain = plan_context(atlas.ix, task="save")
    anchored = plan_context(atlas.ix, task="save", signals={"known_count": 2})
    assert "paths" not in plain["allocations"] and anchored["allocations"]["paths"] > 0
    assert sum(anchored["allocations"].values()) == anchored["budget"]
    assert all(v >= 0 for v in anchored["allocations"].values())
    assert anchored["allocation_order"].index("paths") < anchored["allocation_order"].index("source")
    assert anchored["budget"] >= plain["budget"]


# ---------------------------------------------------------------------- session memory
def test_working_set_is_recency_ordered_bounded_and_exact():
    ws = WorkingSet(limit=3)
    ws.note_symbol({"path": "a.py", "start_line": 1, "qualified": "a"})
    ws.note_file("b.py")
    ws.note_symbol({"path": "c.py", "start_line": 9, "qualified": "C.m"})
    ws.note_symbol({"path": "a.py", "start_line": 1, "qualified": "a"})  # touched again: most recent
    assert ws.anchors() == ["a.py:1", "c.py:9", "b.py"]
    ws.note_file("d.py")
    assert ws.anchors() == ["d.py", "a.py:1", "c.py:9"] and len(ws) == 3
    ws.note_symbol(None)
    ws.note_symbol({"path": None, "start_line": 1})
    assert len(ws) == 3
    ws.clear()
    assert ws.anchors() == []


def test_server_remembers_what_the_agent_looked_at_and_links_to_it(repo: Path):
    from codeatlas.server import App, build_server
    app = App(repo, watch=False, dashboard=False)
    with app._lock:
        app.ix.update()
    mcp = build_server(app)

    def text(result):
        blocks = result if isinstance(result, (list, tuple)) else result.content
        return "\n".join(b.text for b in blocks if hasattr(b, "text"))

    async def go():
        fresh = text(await mcp.call_tool("connections", {"target": "Store.save"}))
        assert "nothing known to start from" in fresh
        await mcp.call_tool("symbol", {"name": "main"})
        assert app.working.anchors() == ["app/main.py:3"]
        await mcp.call_tool("file_outline", {"path": "app/store.py"})
        await mcp.call_tool("symbol_source", {"target": "handle"})
        assert app.working.anchors() == ["app/handlers.py:3", "app/store.py", "app/main.py:3"]
        auto = text(await mcp.call_tool("connections", {"target": "Store.save"}))
        assert "from (recent)" in auto and "handle (app/handlers.py:3) --calls@5--> process" in auto
        explicit = text(await mcp.call_tool("connections", {"target": ["Store.save"], "known": ["tests/test_store.py"], "limit": 1}))
        assert "tests/test_store.py --defines--> test_save" in explicit and "(recent)" not in explicit
        ctx = text(await mcp.call_tool("task_context", {"task": "process the request", "include_source": False}))
        assert "known (recent):" in ctx and "## How what you know connects to these" in ctx
        plain = text(await mcp.call_tool("task_context", {"task": "process the request", "known": [], "include_source": False}))
        assert "known (" not in plain and "connects to these" not in plain
        await mcp.call_tool("references", {"name": "Store.save"})
        assert app.working.anchors()[0] == "app/store.py:2"
        await mcp.call_tool("impact", {"name": "log"})
        assert app.working.anchors()[0] == "app/service.py:9"
        await mcp.call_tool("replace_symbol", {"name": "other", "new_text": "def other():\n    return 2\n"})
        assert app.working.anchors()[0] == "app/handlers.py:7"
    asyncio.run(go())
    app.close()


# ---------------------------------------------------------------------- CLI
def test_cli_connections_and_context_known(repo: Path, capsys):
    cli.main(["connections", "Store.save", "--known", "main", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "main (app/main.py:3) --calls@4--> handle" in out
    cli.main(["connections", "Store.save", "--known", "main", "--json", "--repo", str(repo)])
    data = json.loads(capsys.readouterr().out)
    assert data["paths"][0]["hops"] == 3
    cli.main(["context", "save to the store", "--known", "main", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert "known (given): main" in out and "--calls@4--> handle" in out


def test_task_context_includes_bodies_only_for_tasks_and_how_questions(atlas: Atlas):
    from codeatlas.context import wants_source
    assert not wants_source("where are dependencies resolved, and which test exercises that code?")
    assert not wants_source("if request_body_to_args changed its signature, what would break? list its callers")
    assert not wants_source("who calls Store.save")
    assert wants_source("how does a websocket route get from registration to the handler")
    assert wants_source("fix the discount so VIP codes stack")
    assert wants_source("process the request and save it to the store")
    loc = context.task_context(atlas.ix, atlas, "where is the request saved to the store")
    assert loc["include_source"] is False and all(i["source"] is None for i in loc["items"])
    assert "signatures only" in context.render(loc)
    forced = context.task_context(atlas.ix, atlas, "where is the request saved to the store", include_source=True)
    assert any(i["source"] for i in forced["items"])
    how = context.task_context(atlas.ix, atlas, "how does the request get saved to the store")
    assert how["include_source"] is True and any(i["source"] for i in how["items"])
