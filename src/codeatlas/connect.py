"""Connections: how what the agent already knows links to what it wants to know.

A weighted shortest-path search over everything the index knows about the repository:

* symbol -> symbol: resolved calls, constructions, callback registrations (``arg``), identifier references and
  symbol-level imports, from the ``symbol_edges`` table that PageRank persists (:mod:`codeatlas.rank`);
* file -> symbol: module-level code calling or registering a symbol (tests, route tables, ``main`` scripts), and
  the symbols a file defines;
* class -> member;
* file -> file: resolved imports;
* file -> config / schema artifact: ``uses_env``, ``uses_table``, ``loads_config``, ... (:mod:`codeatlas.relationships`).

The search starts from several *known* anchors at once (symbols, files or ``path:line`` positions the agent has
already looked at) and runs Dijkstra outward. Edge costs prefer tight evidence: a resolved call costs 1, a class
membership 0.8, "same file" 1.6 per hop, an import 1.5; medium-confidence edges cost more and ambiguous ones much
more. Two penalties keep routes meaningful: entering a hub (a logger called from 300 places) costs extra, and a
route that reverses direction (``A calls X, B also calls X``) pays a small flip penalty, so straight call chains win
over "they share a helper" when both exist, while the latter is still reported when nothing better exists.

Crossing from production code into a test also costs extra: a test that calls both A and B is not how A reaches B,
unless the agent started from that test. Every step of a path is labelled with its kind and the line of the evidence
(``login --calls@134--> rotateToken``),
so the agent can open exactly the hop it needs with ``symbol_source``. ``connections()`` answers the explicit
question ("how does A reach B"), ``explore()`` + ``Exploration.path_to()`` back the ``known=`` mode of
``task_context`` and :class:`WorkingSet` remembers what an agent looked at in a session so the linking is automatic.
"""
from __future__ import annotations

import heapq
import math
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .graph import Atlas, is_example_path, is_test_path
from .indexer import Index
from .rank import ensure_rank

Node = tuple  # ("s", symbol_id) | ("f", file_id) | ("a", artifact_path)

# cost of one hop per edge kind; lower = tighter connection
COST = {"call": 1.0, "new": 1.0, "arg": 1.1, "ref": 1.7, "import": 1.8,
        "file_import": 1.5, "defines": 1.6, "contains": 0.8, "relationship": 1.5}
CONF_MULT = {"high": 1.0, "medium": 1.25, "low": 2.2}
FLIP_PENALTY = 0.5
TEST_PENALTY = 0.8       # stepping from production code into a test or example: they call everything, so weak glue
HUB_PENALTY_MAX = 2.0
# edges with a data/control-flow direction; containment and membership never flip a route
FLOW_KINDS = frozenset({"call", "new", "arg", "ref", "import", "file_import"})
LABEL = {"call": "calls", "new": "constructs", "arg": "registers", "ref": "references", "import": "imports",
         "file_import": "imports", "defines": "defines", "contains": "contains"}
MAX_FANOUT = 48          # symbol-level edges followed per node (hubs beyond this are reported as truncated)
MAX_STRUCTURAL = 256     # containment / import edges per node (cheap, and the target must stay reachable)
DEFAULT_MAX_HOPS = 6
HOP_COST = 1.15          # one "hop" of the max_hops budget in cost units (calls cost 1, structure a bit more)
PROXIMITY_MAX_HOPS = 6   # how far task_context looks from the known anchors
PROXIMITY_MAX_COST = PROXIMITY_MAX_HOPS * HOP_COST
SOURCE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".kts", ".swift",
              ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".rb", ".php", ".scala", ".lua", ".sh", ".bash")


@dataclass(frozen=True)
class Edge:
    kind: str
    line: Optional[int]
    confidence: str = "high"
    top_level: bool = False   # module-level code of a file is the source (tests, route tables, scripts)

    @property
    def cost(self) -> float:
        base = COST.get(self.kind, COST["relationship"])
        return base * CONF_MULT.get(self.confidence, 1.0)

    @property
    def label(self) -> str:
        base = LABEL.get(self.kind, self.kind)
        return base + " at top level" if self.top_level and self.kind != "import" else base


@dataclass
class Path:
    nodes: list[Node]
    edges: list[tuple[Edge, bool]]   # (edge, forward) ; forward = traversed in the edge's stored direction
    cost: float

    @property
    def hops(self) -> int:
        return len(self.edges)


class Graph:
    """Lazy, memoised neighbour expansion over the index (one instance per query)."""

    def __init__(self, ix: Index, atlas: Atlas):
        ensure_rank(ix)
        self.ix = ix
        self.atlas = atlas
        self.c = ix.conn
        self._sym: dict[int, Optional[dict]] = {}
        self._fpath: dict[int, str] = {}
        self._fid: dict[str, Optional[int]] = {}
        self._nbrs: dict[Node, list[tuple[Node, Edge, bool]]] = {}
        self._deg: Optional[dict[Node, int]] = None
        self._is_test: dict[Node, bool] = {}
        self._fanout: dict[Node, int] = {}   # top-level symbols per file / members per class
        self.truncated_nodes = 0
        tables = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self._has_rel = "ca_relationships" in tables

    # ------------------------------------------------------------------ lookups
    def sym(self, sid: int) -> Optional[dict]:
        if sid not in self._sym:
            r = self.c.execute(
                "SELECT s.id, s.name, s.kind, s.qualified, s.parent_id, s.file_id, s.start_line, s.end_line, s.signature, "
                "f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.id=?", (sid,)).fetchone()
            self._sym[sid] = dict(r) if r else None
        return self._sym[sid]

    def file_path(self, fid: int) -> str:
        if fid not in self._fpath:
            r = self.c.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
            self._fpath[fid] = r[0] if r else "?"
        return self._fpath[fid]

    def file_id(self, path: str) -> Optional[int]:
        if path not in self._fid:
            r = self.c.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            self._fid[path] = r[0] if r else None
        return self._fid[path]

    def path_node(self, path: str) -> Node:
        fid = self.file_id(path)
        return ("f", fid) if fid is not None else ("a", path)

    def artifact_path(self, text: str) -> Optional[str]:
        """A config/schema file known to the relationship index (exact path or unique suffix)."""
        if not self._has_rel:
            return None
        rel = self.ix.rel(text).lstrip("/")
        row = self.c.execute("SELECT path FROM ca_artifact_files WHERE path=?", (rel,)).fetchone()
        if row:
            return row[0]
        rows = self.c.execute("SELECT path FROM ca_artifact_files WHERE path LIKE ? ESCAPE '\\' LIMIT 2",
                              ("%" + rel.replace("%", "\\%").replace("_", "\\_"),)).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def describe(self, node: Node) -> dict:
        t, key = node
        if t == "s":
            s = self.sym(key)
            if not s:
                return {"type": "symbol", "label": f"#{key}", "path": None, "line": None, "id": key, "kind": None}
            return {"type": "symbol", "label": s["qualified"], "path": s["path"], "line": s["start_line"], "id": key,
                    "kind": s["kind"], "end_line": s["end_line"]}
        if t == "f":
            p = self.file_path(key)
            return {"type": "file", "label": p, "path": p, "line": None, "id": key, "kind": "file"}
        return {"type": "artifact", "label": key, "path": key, "line": None, "id": None, "kind": "artifact"}

    # ------------------------------------------------------------------ hubs
    def degree(self, node: Node) -> int:
        if self._deg is None:
            deg: dict[Node, int] = {}
            for sid, i, o in self.c.execute("SELECT symbol_id, in_deg, out_deg FROM symbol_rank"):
                deg[("s", sid)] = (i or 0) + (o or 0)
            for fid, n in self.c.execute("SELECT resolved_file_id, COUNT(*) FROM imports WHERE resolved_file_id IS NOT NULL "
                                         "GROUP BY resolved_file_id"):
                deg[("f", fid)] = n
            self._deg = deg
        return self._deg.get(node, 0)

    def hub_penalty(self, node: Node) -> float:
        return hub_penalty(self.degree(node))

    def container_cost(self, node: Node, base: float) -> float:
        """Containment is weaker evidence the bigger the container: sharing a 2,000-line file with 160 symbols
        says little, sharing a 60-line file says a lot."""
        if node not in self._fanout:
            t, key = node
            if t == "f":
                n = self.c.execute("SELECT COUNT(*) FROM symbols WHERE file_id=? AND parent_id IS NULL", (key,)).fetchone()[0]
            elif t == "s":
                n = self.c.execute("SELECT COUNT(*) FROM symbols WHERE parent_id=?", (key,)).fetchone()[0]
            else:
                n = 0
            self._fanout[node] = n
        n = self._fanout[node]
        return base + (0.35 * math.log2(n / 8) if n > 8 else 0.0)

    def is_test(self, node: Node) -> bool:
        """Test, example, benchmark or docs code: weak glue between production symbols."""
        if node not in self._is_test:
            d = self.describe(node)
            path = d.get("path") or ""
            self._is_test[node] = bool(path) and (is_test_path(path) or is_example_path(path))
        return self._is_test[node]

    # ------------------------------------------------------------------ neighbours
    def neighbours(self, node: Node) -> list[tuple[Node, Edge, bool]]:
        """(other node, edge, forward). forward=True means the stored direction is node -> other."""
        if node in self._nbrs:
            return self._nbrs[node]
        t, key = node
        out: list[tuple[Node, Edge, bool]] = []
        if t == "s":
            self._symbol_neighbours(key, out)
        elif t == "f":
            self._file_neighbours(key, out)
        else:
            self._artifact_neighbours(key, out)
        self._nbrs[node] = out
        return out

    _CONF_ORDER = "CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"

    def _capped(self, rows: list, cap: int) -> list:
        if len(rows) > cap:
            self.truncated_nodes += 1
            return rows[:cap]
        return rows

    def _symbol_neighbours(self, sid: int, out: list) -> None:
        c = self.c
        rows = self._capped(c.execute(
            f"SELECT dst, kind, confidence, line FROM symbol_edges WHERE src=? ORDER BY {self._CONF_ORDER}, count DESC, line "
            "LIMIT ?", (sid, MAX_FANOUT + 1)).fetchall(), MAX_FANOUT)
        for dst, kind, conf, line in rows:
            out.append((("s", dst), Edge(kind, line, conf), True))
        rows = self._capped(c.execute(
            f"SELECT src, src_file, kind, confidence, line FROM symbol_edges WHERE dst=? ORDER BY {self._CONF_ORDER}, "
            "count DESC, line LIMIT ?", (sid, MAX_FANOUT + 1)).fetchall(), MAX_FANOUT)
        s = self.sym(sid)
        for src, src_file, kind, conf, line in rows:
            if src is None:
                if s and src_file == s["file_id"]:
                    continue  # the symbol's own file is reached through "defines" below
                out.append((("f", src_file), Edge(kind, line, conf, top_level=True), False))
            else:
                out.append((("s", src), Edge(kind, line, conf), False))
        if not s:
            return
        out.append((("f", s["file_id"]), Edge("defines", s["start_line"]), False))
        if s["parent_id"] is not None:
            out.append((("s", s["parent_id"]), Edge("contains", s["start_line"]), False))
        for cid, line in self._capped(c.execute("SELECT id, start_line FROM symbols WHERE parent_id=? ORDER BY start_line LIMIT ?",
                                                (sid, MAX_STRUCTURAL + 1)).fetchall(), MAX_STRUCTURAL):
            out.append((("s", cid), Edge("contains", line), True))

    def _file_neighbours(self, fid: int, out: list) -> None:
        c = self.c
        for rid, line in self._capped(c.execute(
                "SELECT resolved_file_id, MIN(line) FROM imports WHERE file_id=? AND resolved_file_id IS NOT NULL "
                "GROUP BY resolved_file_id LIMIT ?", (fid, MAX_STRUCTURAL + 1)).fetchall(), MAX_STRUCTURAL):
            out.append((("f", rid), Edge("file_import", line), True))
        for sfid, line in self._capped(c.execute(
                "SELECT file_id, MIN(line) FROM imports WHERE resolved_file_id=? GROUP BY file_id LIMIT ?",
                (fid, MAX_STRUCTURAL + 1)).fetchall(), MAX_STRUCTURAL):
            out.append((("f", sfid), Edge("file_import", line), False))
        for sid, line in self._capped(c.execute(
                "SELECT id, start_line FROM symbols WHERE file_id=? AND parent_id IS NULL ORDER BY start_line LIMIT ?",
                (fid, MAX_STRUCTURAL + 1)).fetchall(), MAX_STRUCTURAL):
            out.append((("s", sid), Edge("defines", line), True))
        for dst, kind, conf, line in self._capped(c.execute(
                f"SELECT dst, kind, confidence, line FROM symbol_edges WHERE src IS NULL AND src_file=? "
                f"ORDER BY {self._CONF_ORDER}, count DESC, line LIMIT ?", (fid, MAX_FANOUT + 1)).fetchall(), MAX_FANOUT):
            out.append((("s", dst), Edge(kind, line, conf, top_level=True), True))
        if self._has_rel:
            self._relationship_neighbours(self.file_path(fid), out)

    def _artifact_neighbours(self, path: str, out: list) -> None:
        if self._has_rel:
            self._relationship_neighbours(path, out)

    def _relationship_neighbours(self, path: str, out: list) -> None:
        c = self.c
        for tgt, kind, line in c.execute(
                "SELECT target_path, kind, MIN(line) FROM ca_relationships WHERE source_path=? AND resolved=1 "
                "GROUP BY target_path, kind LIMIT ?", (path, MAX_STRUCTURAL)):
            out.append((self.path_node(tgt), Edge(kind, line), True))
        for src, kind, line in c.execute(
                "SELECT source_path, kind, MIN(line) FROM ca_relationships WHERE target_path=? AND resolved=1 "
                "GROUP BY source_path, kind LIMIT ?", (path, MAX_STRUCTURAL)):
            out.append((self.path_node(src), Edge(kind, line), False))


def hub_penalty(degree: int) -> float:
    """Extra cost for entering a node with many resolved edges: routes through loggers and utilities are weak evidence."""
    if degree <= 2:
        return 0.0
    return min(HUB_PENALTY_MAX, 0.25 * math.log2(1 + degree - 2))


# ---------------------------------------------------------------------- search
@dataclass
class Exploration:
    graph: Graph
    anchors: dict[Node, str]                                   # anchor node -> how the caller named it
    max_cost: float
    dist: dict[Node, float] = field(default_factory=dict)      # best cost per node over both directions
    best_state: dict[Node, tuple] = field(default_factory=dict)
    parent: dict[tuple, tuple] = field(default_factory=dict)   # state -> (prev state, edge, forward)
    expanded: int = 0
    truncated: bool = False
    reached: set = field(default_factory=set)

    def distance(self, node: Node) -> Optional[float]:
        return self.dist.get(node)

    def path_to(self, node: Node) -> Optional[Path]:
        state = self.best_state.get(node)
        if state is None:
            return None
        nodes = [node]
        edges: list[tuple[Edge, bool]] = []
        cur = state
        while cur in self.parent:
            prev, edge, forward = self.parent[cur]
            edges.append((edge, forward))
            nodes.append(prev[0])
            cur = prev
        nodes.reverse()
        edges.reverse()
        return Path(nodes, edges, self.dist[node])

    def anchor_of(self, node: Node) -> Optional[Node]:
        p = self.path_to(node)
        return p.nodes[0] if p else None


def explore(graph: Graph, anchors: Iterable[tuple[Node, str]], *, max_cost: float = DEFAULT_MAX_HOPS * HOP_COST,
            max_nodes: int = 2000, targets: Optional[set] = None, blocked: Iterable[Node] = (),
            exempt: Optional[set] = None) -> Exploration:
    """Multi-source Dijkstra from the anchors. State = (node, direction of the last flow edge: 0 / +1 / -1).

    ``targets`` stop the search once all are settled; ``exempt`` nodes (targets, or every candidate when ranking)
    pay no hub or test penalty, since the question is how to reach them, not whether they are good glue.
    """
    anchor_map: dict[Node, str] = {}
    for node, label in anchors:
        anchor_map.setdefault(node, label)
    exp = Exploration(graph, anchor_map, max_cost)
    blocked = set(blocked) - set(anchor_map)
    free = set(anchor_map) | set(targets or ()) | set(exempt or ())
    inf = float("inf")
    dist_state: dict[tuple, float] = {}
    heap: list[tuple[float, int, tuple]] = []
    tick = 0
    for node in anchor_map:
        state = (node, 0)
        dist_state[state] = 0.0
        heap.append((0.0, tick, state))
        tick += 1
    heapq.heapify(heap)
    settled: set[tuple] = set()
    while heap:
        cost, _, state = heapq.heappop(heap)
        if state in settled:
            continue
        settled.add(state)
        node, direction = state
        if cost < exp.dist.get(node, inf):
            exp.dist[node] = cost
            exp.best_state[node] = state
        if targets and node in targets:
            exp.reached.add(node)
            if len(exp.reached) >= len(targets):
                break
        exp.expanded += 1
        if exp.expanded > max_nodes:
            exp.truncated = True
            break
        for other, edge, forward in graph.neighbours(node):
            if other in blocked:
                continue
            step = edge.cost
            if edge.kind in ("defines", "contains"):
                # the container is `node` when forward (file --defines--> symbol), else `other`
                step = graph.container_cost(node if forward else other, step)
            ndir = direction
            if edge.kind in FLOW_KINDS:
                d = 1 if forward else -1
                if direction and direction != d:
                    step += FLIP_PENALTY
                ndir = d
            if other not in free:
                step += graph.hub_penalty(other)
                if graph.is_test(other) and not graph.is_test(node):
                    step += TEST_PENALTY
            nstate = (other, ndir)
            ncost = cost + step
            if ncost <= max_cost + 1e-9 and ncost < dist_state.get(nstate, inf):
                dist_state[nstate] = ncost
                exp.parent[nstate] = (state, edge, forward)
                heapq.heappush(heap, (ncost, tick, nstate))
                tick += 1
    return exp


# ---------------------------------------------------------------------- anchors
_LOC_RE = re.compile(r"^(.+?):(\d+)(?:-(\d+))?$")


def resolve_anchors(graph: Graph, items: Iterable[str]) -> tuple[list[tuple[Node, str]], list[dict]]:
    """Map agent-supplied names to graph nodes.

    Accepts qualified or plain symbol names, ``path:line`` positions (the smallest enclosing symbol, or the file when
    the line is outside every declaration) and file paths (exact or unique suffix). Returns resolved
    ``(node, input)`` pairs (deduplicated, in input order) and the inputs that could not be resolved with suggestions.
    """
    atlas = graph.atlas
    resolved: list[tuple[Node, str]] = []
    unresolved: list[dict] = []
    seen: set[Node] = set()

    def add(node: Node, text: str) -> None:
        if node not in seen:
            seen.add(node)
            resolved.append((node, text))

    for raw in items or ():
        text = (raw or "").strip().strip("`'\"")
        if not text:
            continue
        m = _LOC_RE.match(text)
        if m and atlas.file_id(m.group(1)) is not None:
            fid = atlas.file_id(m.group(1))
            line = int(m.group(2))
            row = graph.c.execute(
                "SELECT id FROM symbols WHERE file_id=? AND start_line<=? AND end_line>=? ORDER BY (end_line-start_line) LIMIT 1",
                (fid, line, line)).fetchone()
            add(("s", row[0]) if row else ("f", fid), text)
            continue
        looks_like_file = "/" in text or "\\" in text or text.lower().endswith(SOURCE_EXT)
        order = ("file", "symbol") if looks_like_file else ("symbol", "file")
        node: Optional[Node] = None
        for how in order:
            if how == "file":
                fid = atlas.file_id(text)
                if fid is not None:
                    node = ("f", fid)
                    break
            else:
                cands = atlas.resolve_symbol(text)
                if cands:
                    node = ("s", cands[0]["id"])
                    break
        if node is None:
            art = graph.artifact_path(text)
            if art is not None:
                node = ("a", art)
        if node is None:
            similar = atlas.find_symbols(text, limit=3)
            if len(similar) == 1:
                node = ("s", similar[0]["id"])
            else:
                unresolved.append({"input": text, "similar": [f"{s['qualified']} {s['path']}:{s['start_line']}" for s in similar]})
                continue
        add(node, text)
    return resolved, unresolved


# ---------------------------------------------------------------------- paths as data / text
def path_dict(graph: Graph, path: Path) -> dict:
    nodes = [graph.describe(n) for n in path.nodes]
    edges = [{"kind": e.kind, "label": e.label, "line": e.line, "confidence": e.confidence, "forward": fwd}
             for e, fwd in path.edges]
    return {"anchor": nodes[0], "target": nodes[-1], "nodes": nodes, "edges": edges, "hops": path.hops,
            "cost": round(path.cost, 2)}


def _node_text(n: dict, prev_path: Optional[str]) -> str:
    if n["type"] != "symbol":
        return n["label"]
    if n["line"] is None:
        return n["label"]
    if prev_path == n["path"]:
        return f"{n['label']} (:{n['line']})"
    return f"{n['label']} ({n['path']}:{n['line']})"


def _arrow(e: dict) -> str:
    label = e["label"]
    if e["line"] is not None and e["kind"] not in ("defines", "contains"):
        label += f"@{e['line']}"
    if e.get("confidence") in ("medium", "low"):
        label += f" ({e['confidence']})"
    return f"--{label}-->" if e["forward"] else f"<--{label}--"


def format_path(p: dict) -> str:
    """``main (app/main.py:3) --calls@4--> handle (app/handlers.py:3) --calls@4--> process (app/service.py:4)``"""
    parts = [_node_text(p["nodes"][0], None)]
    prev = p["nodes"][0]["path"]
    for e, n in zip(p["edges"], p["nodes"][1:]):
        parts.append(_arrow(e))
        parts.append(_node_text(n, prev))
        prev = n["path"]
    return " ".join(parts)


def format_route_tree(paths: list[dict]) -> list[str]:
    """Routes that share a prefix, merged into an indented tree (one line per hop).

    All routes of one exploration are shortest paths with a unique parent per node, so their union is a tree
    rooted at the anchors: ``main`` -> ``handle`` -> {``process`` -> {``Store.save``}, ``log``} prints each hop once.
    """
    roots: dict = {}
    for p in paths:
        level = roots
        for i, n in enumerate(p["nodes"]):
            e = p["edges"][i - 1] if i else None
            key = (n["type"], n["id"] if n["id"] is not None else n["label"],
                   (e["kind"], e["line"], e["forward"]) if e else None)
            level = level.setdefault(key, {"node": n, "edge": e, "children": {}})["children"]
    lines: list[str] = []

    def emit(level: dict, depth: int, prev_path: Optional[str]) -> None:
        for entry in level.values():
            n, e = entry["node"], entry["edge"]
            lines.append(_node_text(n, None) if e is None else "  " * depth + f"{_arrow(e)} {_node_text(n, prev_path)}")
            emit(entry["children"], depth + 1, n["path"])

    emit(roots, 0, None)
    return lines


def expand_handle(n: dict) -> str:
    if n["type"] == "symbol":
        return f"symbol_source(\"{n['label']}\", path=\"{n['path']}\")"
    if n["type"] == "file":
        return f"file_outline(\"{n['path']}\")"
    return f"relationships(path=\"{n['path']}\")"


# ---------------------------------------------------------------------- the connections query
def connections(ix: Index, atlas: Atlas, target, known, *, max_hops: int = DEFAULT_MAX_HOPS, limit: int = 5,
                max_nodes: int = 4000, known_source: str = "given") -> dict:
    """Shortest labelled routes from the known anchors to each target, up to ``limit`` distinct routes per target.

    Diversity: after a route is found, its interior nodes are blocked and the search runs again (a direct edge
    blocks its anchor instead), so later routes go through other code or start from other anchors.
    """
    targets = [target] if isinstance(target, str) else list(target or [])
    known_items = [known] if isinstance(known, str) else list(known or [])
    max_hops = max(1, min(12, int(max_hops)))
    limit = max(1, min(12, int(limit)))
    g = Graph(ix, atlas)
    known_nodes, bad_known = resolve_anchors(g, known_items)
    target_nodes, bad_targets = resolve_anchors(g, targets)
    result = {"known": [{"input": inp, **g.describe(n)} for n, inp in known_nodes],
              "known_source": known_source,
              "targets": [{"input": inp, **g.describe(n)} for n, inp in target_nodes],
              "unresolved_known": bad_known, "unresolved_targets": bad_targets,
              "paths": [], "unreached": [], "bridges": [], "max_hops": max_hops, "searched": 0,
              "truncated": False, "hubs_capped": 0, "notes": [], "index_version": ix.get_meta("last_update")}
    if not target_nodes:
        result["notes"].append("no target could be resolved; pass a symbol name, qualified name, path or path:line")
        return result
    if not known_nodes:
        result["notes"].append("nothing known to start from: pass known=[...] (symbols, files or path:line you have "
                               "already looked at)" if not known_items else "none of the known items could be resolved")
        return result
    max_cost = max_hops * HOP_COST
    anchor_set = {n for n, _ in known_nodes}
    bridge_counts: Counter = Counter()
    for tnode, _ in target_nodes:
        if tnode in anchor_set:
            result["notes"].append(f"{g.describe(tnode)['label']} is itself one of the known items")
            continue
        blocked: set[Node] = set()
        found: list[Path] = []
        seen_routes: set[tuple] = set()
        for _attempt in range(limit):
            exp = explore(g, known_nodes, max_cost=max_cost, max_nodes=max_nodes, targets={tnode}, blocked=blocked)
            result["searched"] += exp.expanded
            result["truncated"] = result["truncated"] or exp.truncated
            p = exp.path_to(tnode)
            if p is None or tuple(p.nodes) in seen_routes:
                break
            seen_routes.add(tuple(p.nodes))
            found.append(p)
            interior = p.nodes[1:-1]
            if interior:
                blocked.update(interior)
            else:
                blocked.add(p.nodes[0])  # a direct edge: look for routes from the other anchors next
            if not interior and anchor_set <= blocked:
                break
        if not found:
            result["unreached"].append(g.describe(tnode))
        for p in found:
            result["paths"].append(path_dict(g, p))
            for n in p.nodes[1:-1]:
                if n[0] == "s":
                    bridge_counts[n] += 1
    result["paths"].sort(key=lambda p: (p["cost"], p["hops"]))
    result["bridges"] = [{**g.describe(n), "paths": c} for n, c in bridge_counts.most_common(8)]
    result["hubs_capped"] = g.truncated_nodes
    if result["truncated"]:
        result["notes"].append(f"search stopped at {max_nodes} nodes per route: a longer route may exist")
    if g.truncated_nodes and result["unreached"]:
        result["notes"].append(f"{g.truncated_nodes} hub(s) with more than {MAX_FANOUT} edges were not fully expanded")
    return result


def render(d: dict) -> str:
    L = []
    tgt = ", ".join(_node_text(t, None) for t in d["targets"]) or "?"
    kn = ", ".join(_node_text(k, None) for k in d["known"]) or "?"
    src = "" if d.get("known_source", "given") == "given" else f" ({d['known_source']})"
    L.append(f"Connections to {tgt} from{src} {kn}: {len(d['paths'])} route(s) within ~{d['max_hops']} hops"
             + (f", {d['searched']} nodes searched" if d.get("searched") else ""))
    for u in d.get("unresolved_known", []) + d.get("unresolved_targets", []):
        L.append(f"  not found: {u['input']}" + (f"  (similar: {'; '.join(u['similar'])})" if u.get("similar") else ""))
    for n in d.get("notes", []):
        L.append(f"  note: {n}")
    for i, p in enumerate(d["paths"], 1):
        L.append(f"{i}. {format_path(p)}  [{p['hops']} hop{'s' if p['hops'] != 1 else ''}]")
    if d.get("unreached"):
        L.append("no route within ~%d hops: %s" % (d["max_hops"], ", ".join(_node_text(u, None) for u in d["unreached"])))
    if d.get("bridges"):
        L.append("on the way (open these to follow the route):")
        for b in d["bridges"][:6]:
            L.append(f"  {b['label']}  {b['path']}:{b['line']}  ({b['paths']} route{'s' if b['paths'] != 1 else ''})"
                     f"  expand: {expand_handle(b)}")
    if d["paths"]:
        L.append("arrows show the real direction: A --calls@12--> B means A calls B at line 12; A <--calls@12-- B means B calls A.")
    L.append(f"index {d.get('index_version')}")
    return "\n".join(L)


# ---------------------------------------------------------------------- session memory
class WorkingSet:
    """What an agent has already looked at in this session: the default ``known`` anchors.

    Entries are exact positions (``path:line`` for symbols, ``path`` for files) so they resolve unambiguously
    later even if a same-named symbol exists elsewhere. Most recent first; bounded.
    """

    def __init__(self, limit: int = 12):
        self.limit = limit
        self._items: "OrderedDict[str, dict]" = OrderedDict()

    def _put(self, key: str, label: str, kind: str) -> None:
        if key in self._items:
            del self._items[key]
        self._items[key] = {"ref": key, "label": label, "kind": kind}
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def note_symbol(self, s: Optional[dict]) -> None:
        if s and s.get("path") and s.get("start_line") is not None:
            self._put(f"{s['path']}:{s['start_line']}", s.get("qualified") or s.get("name") or "?", "symbol")

    def note_file(self, path: Optional[str]) -> None:
        if path:
            self._put(path, path, "file")

    def note_position(self, path: Optional[str], line: Optional[int]) -> None:
        if path and line is not None:
            self._put(f"{path}:{line}", f"{path}:{line}", "position")

    def anchors(self) -> list[str]:
        return [it["ref"] for it in reversed(self._items.values())]

    def labels(self) -> list[str]:
        return [it["label"] for it in reversed(self._items.values())]

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)
