"""Name resolution: which definition does a reference actually point at?

Tree-sitter gives syntax, not types, so resolution is heuristic but layered:

1. ``this``/``self``/``super`` receivers resolve to members of the enclosing class.
2. ``Foo.bar`` / ``Foo::bar`` / ``Foo.shared.bar`` resolve to members of class-like symbol ``Foo``.
3. Otherwise: same file > files the referencing file imports > unique definition in the same
   project (nearest package manifest) and language > several candidates in the project (low).
4. A member access with an unknown receiver whose name is a common builtin (``size``, ``push``,
   ``get``...) only resolves through same-file or import evidence: otherwise every ``map.size``
   in the repo would count as a use of some unrelated ``OverlayStack.size``.
5. Nothing ever resolves across languages or across project boundaries.

Confidence: ``high`` (rules 1-2, same file, import), ``medium`` (unique in project),
``low`` (ambiguous in project). Containers such as Rust ``impl`` blocks and Swift ``extension``s are
never targets.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from .indexer import Index
from .languages import CLASS_KINDS

RECEIVER_SELF = {"this", "self", "super", "Self", "cls", "static", "$this", "self()", "this()"}
CONTAINER_KINDS = {"impl", "extension", "module"}

# member names that are overwhelmingly builtins/library calls when the receiver is unknown
BUILTIN_MEMBERS = frozenset("""
size length count push pop shift unshift get set has add remove delete clear keys values entries map filter
forEach reduce find findIndex some every includes indexOf join split slice splice concat sort reverse trim
toString toJSON valueOf equals hashCode compareTo then catch finally close open read write send start stop
cancel resume pause next done error data value key id name type status message url path text json body
headers method params query first last append insert contains isEmpty toList toMap toSet toArray copy
apply call bind on off once emit subscribe unsubscribe dispatch connect disconnect update render init
setup destroy dispose load save fetch parse stringify format now date time min max abs floor ceil round
lock unlock wait notify run execute submit schedule log info warn debug print println flush encode decode
add_all remove_all put putAll getOrDefault getOrNull orEmpty let also run with takeIf takeUnless forEachIndexed
mapNotNull filterNotNull firstOrNull lastOrNull sortedBy groupBy joinToString isBlank isNotBlank isNotEmpty
lowercase uppercase lowercased uppercased replace replacingOccurrences components contains hasPrefix hasSuffix
description debugDescription localizedDescription frame bounds center width height x y z w h top left right
bottom items item element elements children parent child index offset limit page total success failure result
response request client server session user users token tokens config options settings state props context
async await sleep delay tick current previous focus blur select enable disable show hide toggle mount unmount
dp sp px em rem pt title label icon color colors style styles padding margin font fonts weight background
foreground border radius alpha opacity scale rotation position anchor visible hidden enabled disabled selected
checked active loading pending ready connected closed empty full valid invalid dirty pristine content
""".split())


@dataclass
class Resolution:
    targets: list[int]
    confidence: Optional[str]  # high | medium | low | None (external / builtin / unknown)
    how: str = ""


class NameResolver:
    """In-memory view of definitions for fast, repeatable resolution."""

    def __init__(self, ix: Index):
        c = ix.conn
        self.version = ix.get_meta("last_update") or "0"
        self.file_info: dict[int, tuple[str, str, str]] = {}  # fid -> (lang, project, path)
        for r in c.execute("SELECT id, lang, project, path FROM files"):
            self.file_info[r[0]] = (r[1], r[2] or "", r[3])
        import json
        self.imports: dict[int, set[int]] = defaultdict(set)
        self.bindings: dict[int, dict[str, tuple[str, Optional[int]]]] = defaultdict(dict)
        # reverse: (original name, defining file) -> [(importing file, local alias)] for aliased imports
        self.alias_rev: dict[tuple[str, int], list[tuple[int, str]]] = defaultdict(list)
        for a, b, bindings in c.execute("SELECT file_id, resolved_file_id, bindings FROM imports"):
            if b is not None:
                self.imports[a].add(b)
            if bindings:
                try:
                    for local, orig in json.loads(bindings).items():
                        self.bindings[a][local] = (orig, b)
                        if b is not None and orig not in ("*", "default") and local != orig:
                            self.alias_rev[(orig, b)].append((a, local))
                except ValueError:
                    pass
        # symbol tables
        self.sym_file: dict[int, int] = {}
        self.sym_kind: dict[int, str] = {}
        self.sym_parent: dict[int, Optional[int]] = {}
        self.sym_name: dict[int, str] = {}
        self.defs_by_name: dict[str, list[int]] = defaultdict(list)
        for r in c.execute("SELECT id, name, kind, file_id, parent_id FROM symbols"):
            sid, name, kind, fid, pid = r
            self.sym_file[sid] = fid
            self.sym_kind[sid] = kind
            self.sym_parent[sid] = pid
            self.sym_name[sid] = name
            if kind not in CONTAINER_KINDS:
                self.defs_by_name[name].append(sid)
        # class-like symbols by (project, name) -> ids; members by class id -> {name: [sid]}
        self.classes: dict[tuple[str, str], list[int]] = defaultdict(list)
        self.members: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for sid, kind in self.sym_kind.items():
            if kind in CLASS_KINDS or kind == "extension":
                fid = self.sym_file[sid]
                self.classes[(self.file_info[fid][1], self.sym_name[sid])].append(sid)
        for sid, pid in self.sym_parent.items():
            if pid is not None and self.sym_kind[sid] not in CONTAINER_KINDS:
                self.members[pid][self.sym_name[sid]].append(sid)
                # Swift extensions / Rust impls: members belong to the type they extend too
                pk = self.sym_kind.get(pid)
                if pk in ("extension", "impl"):
                    tname = self.sym_name[pid].split(" for ")[-1].strip()
                    for cid in self.classes.get((self.file_info[self.sym_file[pid]][1], tname), []):
                        self.members[cid][self.sym_name[sid]].append(sid)

    # ------------------------------------------------------------------ helpers
    def enclosing_class(self, sid: Optional[int]) -> Optional[int]:
        cur = sid
        for _ in range(8):
            if cur is None:
                return None
            k = self.sym_kind.get(cur)
            if k in CLASS_KINDS or k == "extension":
                return cur
            cur = self.sym_parent.get(cur)
        return None

    def _class_members_named(self, cid: int, name: str) -> list[int]:
        out = list(self.members.get(cid, {}).get(name, []))
        # extension/impl blocks of the same type in the same project
        fid = self.sym_file[cid]
        proj = self.file_info[fid][1]
        for other in self.classes.get((proj, self.sym_name[cid]), []):
            if other != cid:
                out += self.members.get(other, {}).get(name, [])
        return out

    # ------------------------------------------------------------------ resolution
    def resolve(self, name: str, from_fid: int, qualifier: Optional[str] = None,
                enclosing_sid: Optional[int] = None, kind: str = "call") -> Resolution:
        finfo = self.file_info.get(from_fid)
        if finfo is None:
            return Resolution([], None, "unknown-file")
        lang, proj, _ = finfo
        q = (qualifier or "").strip()
        qlast = q.replace("::", ".").split(".")[-1].strip("()!? ") if q else ""
        qfirst = q.replace("::", ".").split(".")[0].strip("()!? ") if q else ""
        # 0. import bindings: `import { foo as bar }` / `from m import foo as bar` / `import * as ns`
        fb = self.bindings.get(from_fid)
        if fb:
            if not q and name in fb:
                orig, tfid = fb[name]
                if tfid is not None and orig not in ("*", "default"):
                    hits = [s for s in self.defs_by_name.get(orig, []) if self.sym_file[s] == tfid]
                    if hits:
                        return Resolution(hits, "high", "import-alias")
                elif tfid is not None and orig == "default":
                    hits = [s for s in self.defs_by_name.get(name, []) if self.sym_file[s] == tfid]
                    if hits:
                        return Resolution(hits, "high", "import-default")
            if q and qfirst in fb and fb[qfirst][0] == "*" and fb[qfirst][1] is not None:
                tfid = fb[qfirst][1]
                hits = [s for s in self.defs_by_name.get(name, []) if self.sym_file[s] == tfid]
                if hits:
                    return Resolution(hits, "high", "import-namespace")
        cands = self.defs_by_name.get(name)
        if not cands:
            return Resolution([], None, "no-definition")
        # never across language or project
        same = [s for s in cands if self.file_info[self.sym_file[s]][0] == lang and self.file_info[self.sym_file[s]][1] == proj]
        if not same:
            return Resolution([], None, "other-project")
        if not q:
            # an unqualified name cannot be a method of some *other* class (those need a receiver);
            # methods of the enclosing class (and its extensions) stay eligible
            enc_cls = self.enclosing_class(enclosing_sid)
            enc_names = {self.sym_name[enc_cls]} if enc_cls is not None else set()
            free = [s for s in same if self.sym_parent.get(s) is None
                    or self.sym_kind.get(self.sym_parent[s]) not in CLASS_KINDS
                    or self.sym_parent[s] == enc_cls
                    or self.sym_name.get(self.sym_parent[s]) in enc_names]
            if free:
                same = free
        # 1. self / this receivers -> members of the enclosing class
        if q and (q in RECEIVER_SELF or qfirst in RECEIVER_SELF):
            cid = self.enclosing_class(enclosing_sid)
            if cid is not None:
                hits = [s for s in self._class_members_named(cid, name) if s in same]
                if hits:
                    return Resolution(hits, "high", "self")
            return Resolution([], None, "self-unresolved")
        # 2. Foo.bar / Foo.shared.bar -> members of class-like Foo in this project
        if q:
            for cname in (qlast, qfirst):
                if not cname or cname[0].islower() and cname not in ("app", "api"):
                    continue
                for cid in self.classes.get((proj, cname), []):
                    hits = [s for s in self._class_members_named(cid, name) if s in same]
                    if hits:
                        return Resolution(hits, "high", "class")
        # 3. same file, imports, project
        in_file = [s for s in same if self.sym_file[s] == from_fid]
        if in_file:
            # prefer members of the enclosing class over module-level duplicates
            cid = self.enclosing_class(enclosing_sid)
            if cid is not None:
                mem = [s for s in in_file if self.sym_parent.get(s) == cid]
                if mem:
                    return Resolution(mem, "high", "same-class")
            return Resolution(in_file, "high", "same-file")
        imp = self.imports.get(from_fid, ())
        via = [s for s in same if self.sym_file[s] in imp]
        if via:
            return Resolution(via, "high", "import")
        # unknown receiver + builtin-looking member name: no evidence -> treat as external
        if q and name in BUILTIN_MEMBERS:
            return Resolution([], None, "builtin-member")
        if q and kind in ("call", "ref") and len(same) > 1:
            return Resolution(same, "low", "ambiguous-member")
        if len(same) == 1:
            # a unique project definition is a decent bet for calls/constructions; a bare identifier
            # (`Text(text)`, `x.text`) is usually a local variable or property, so it only counts as low
            if kind in ("ref", "arg") and not name[:1].isupper():
                return Resolution(same, "low", "identifier-project-unique")
            return Resolution(same, "medium", "project-unique")
        # several definitions in the project: prefer ones whose class is not obviously different
        return Resolution(same, "low", "ambiguous")


_cache: dict[str, tuple[str, NameResolver, float]] = {}


def get_resolver(ix: Index) -> NameResolver:
    """Resolver cached per index; rebuilt when the index changed."""
    key = str(ix.db_path)
    ver = ix.get_meta("last_update") or "0"
    hit = _cache.get(key)
    if hit and hit[0] == ver:
        return hit[1]
    r = NameResolver(ix)
    _cache[key] = (ver, r, time.time())
    return r
