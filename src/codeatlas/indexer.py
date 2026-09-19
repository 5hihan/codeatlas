"""Incremental tree-sitter indexer backed by SQLite.

The index lives in ``<repo>/.codeatlas/index.db`` and stores files, symbols
(functions/classes/...), references (calls, callbacks, plain identifier uses),
imports (resolved to repo files when possible) and findings (TODO tags,
swallowed exceptions, ...).  Updates are incremental: only files whose
size/mtime changed are re-parsed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import pathspec

from .languages import (CLASS_KINDS, COMMENT_TYPES, IDENT_TYPES, SPECS, STRING_TYPES, LangSpec, get_parser,
                        lang_for_path)

DEFAULT_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", "dist", "build", "out", ".next", ".nuxt", ".turbo", ".cache", ".codeatlas",
    "target", "vendor", "Pods", "DerivedData", ".gradle", ".idea", ".vscode", "coverage", ".parcel-cache",
    "bower_components", ".dart_tool", "site-packages", "__snapshots__", ".terraform", ".serverless", ".expo",
    "third_party", "thirdparty", "__generated__", "generated", "wasm", "min", ".yarn", ".pnpm", "Carthage",
}
# files under these dirs are only indexed when small (assets/vendored bundles are noise)
LOW_SIGNAL_DIRS = {"public", "static", "assets", "lib", "libs", "external", "ext"}
LOW_SIGNAL_MAX_BYTES = 120_000
MAX_FILE_BYTES = 4_000_000  # real 40k-line source files exist; generated/minified ones are caught separately
# case-sensitive on purpose: "this is a bug" is prose, "BUG:" is a tag
TAG_RE = re.compile(r"\b(?:(TODO|FIXME|HACK|XXX)\b|(BUG|OPTIMIZE|DEPRECATED|KLUDGE|NOTE|SECURITY|PERF)(?=[:(]))"
                    r"(?:\([^)]{0,40}\))?[:\s-]*(.*)")
SCHEMA_VERSION = 8
MANIFEST_FILES = {"package.json", "pyproject.toml", "setup.py", "setup.cfg", "go.mod", "Cargo.toml", "build.gradle",
                  "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "pom.xml", "composer.json", "Gemfile",
                  "Package.swift", "CMakeLists.txt", "mix.exs", "pubspec.yaml", "Makefile"}
MANIFEST_SUFFIXES = (".csproj", ".xcodeproj", ".sln", ".fsproj")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, lang TEXT NOT NULL, size INTEGER, mtime REAL,
  hash TEXT, lines INTEGER, indexed_at REAL, project TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS symbols(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, qualified TEXT NOT NULL,
  parent_id INTEGER, start_line INTEGER, end_line INTEGER, signature TEXT, doc TEXT, complexity INTEGER,
  decorators TEXT, exported INTEGER, start_byte INTEGER, end_byte INTEGER, span_start INTEGER, span_end INTEGER,
  name_start INTEGER, name_end INTEGER, body_hash TEXT, FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS refs(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, name TEXT NOT NULL, line INTEGER, col INTEGER, kind TEXT,
  qualifier TEXT, enclosing_id INTEGER, FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS imports(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, module TEXT, raw TEXT, resolved_file_id INTEGER, line INTEGER,
  names TEXT, bindings TEXT, FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS file_tokens(
  file_id INTEGER NOT NULL, token TEXT NOT NULL, FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_tokens_token ON file_tokens(token);
CREATE INDEX IF NOT EXISTS idx_tokens_file ON file_tokens(file_id);
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, line INTEGER, code TEXT, message TEXT,
  FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified);
CREATE INDEX IF NOT EXISTS idx_refs_name ON refs(name);
CREATE INDEX IF NOT EXISTS idx_refs_file ON refs(file_id);
CREATE INDEX IF NOT EXISTS idx_refs_enclosing ON refs(enclosing_id);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS idx_imports_resolved ON imports(resolved_file_id);
CREATE INDEX IF NOT EXISTS idx_findings_file ON findings(file_id);
"""


# --------------------------------------------------------------------------------------
# Extraction data structures
# --------------------------------------------------------------------------------------
@dataclass
class SymbolRec:
    name: str
    kind: str
    qualified: str
    parent_idx: Optional[int]
    start_line: int
    end_line: int
    signature: str
    doc: str
    complexity: int
    decorators: list[str]
    exported: bool
    db_id: Optional[int] = None
    start_byte: int = 0
    end_byte: int = 0
    span_start: int = 0      # includes decorators / export keyword / `const x =` holder
    span_end: int = 0
    name_start: int = 0
    name_end: int = 0
    body_hash: str = ""


@dataclass
class RefRec:
    name: str
    line: int
    col: int
    kind: str  # call | new | arg | ref | import
    qualifier: Optional[str]
    enclosing_idx: Optional[int]


@dataclass
class ImportRec:
    module: str
    raw: str
    line: int
    names: list[str] = field(default_factory=list)
    bindings: dict[str, str] = field(default_factory=dict)  # local name -> original name ("*" = namespace)


@dataclass
class Extraction:
    symbols: list[SymbolRec] = field(default_factory=list)
    refs: list[RefRec] = field(default_factory=list)
    ident_refs: list[RefRec] = field(default_factory=list)   # unfiltered identifier uses (collect_all mode)
    imports: list[ImportRec] = field(default_factory=list)
    findings: list[tuple[int, str, str]] = field(default_factory=list)
    lines: int = 0
    tokens: set[str] = field(default_factory=set)
    tree: object = None
    error_count: int = 0


def _text(node, src: bytes, limit: int = 0) -> str:
    t = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
    if limit and len(t) > limit:
        return t[:limit] + "…"
    return t


def _first_ident_desc(node, src: bytes):
    """Depth-first search for the first identifier-like descendant."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in IDENT_TYPES:
            return n
        stack.extend(reversed(n.children))
    return None


def _last_ident_desc(node):
    """Right-most identifier-like descendant (used for callee names like a.b.c())."""
    while node is not None and node.type not in IDENT_TYPES:
        nxt = None
        for c in reversed(node.children):
            if c.is_named and c.type not in ("arguments", "argument_list", "type_arguments", "generic_type") \
                    and c.type not in STRING_TYPES:
                nxt = c
                break
        if nxt is None:
            # maybe an unnamed token like `self`/`this`
            return None
        node = nxt
    return node


def _c_declarator_name(node):
    """Follow declarator chain for C/C++ function definitions."""
    cur = node
    for _ in range(12):
        if cur is None:
            return None
        if cur.type in IDENT_TYPES:
            return cur
        if cur.type in ("qualified_identifier", "scoped_identifier"):
            nxt = cur.child_by_field_name("name")
            if nxt is None:
                return _first_ident_desc(cur, b"")
            cur = nxt
            continue
        if cur.type in ("destructor_name", "operator_name", "template_function", "field_identifier"):
            return cur
        nxt = cur.child_by_field_name("declarator")
        if nxt is None:
            nxt = cur.child_by_field_name("name")
        if nxt is None:
            return _first_ident_desc(cur, b"")
        cur = nxt
    return None


def _string_content(node, src: bytes) -> Optional[str]:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in STRING_TYPES or n.type == "system_lib_string":
            t = _text(n, src).strip()
            t = t.strip("'\"`<>")
            if t:
                return t
        stack.extend(reversed(n.children))
    return None


def _strip_comment(t: str) -> str:
    t = t.strip()
    t = re.sub(r"^(/\*\*?|\*/|///?|#|--|\*)\s?", "", t, flags=re.M)
    t = re.sub(r"\*/\s*$", "", t)
    lines = [ln.strip().lstrip("*").strip() for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    return " ".join(lines)


def _is_exported(spec: LangSpec, node, name: str, src: bytes) -> bool:
    lang = spec.name
    if lang == "python":
        return not name.startswith("_")
    if lang in ("javascript", "typescript", "tsx"):
        p = node.parent
        for _ in range(3):
            if p is None:
                break
            if p.type in spec.export_wrappers:
                return True
            p = p.parent
        return False
    if lang == "go":
        return name[:1].isupper()
    if lang == "rust":
        return any(c.type == "visibility_modifier" for c in node.children)
    if lang in ("java", "csharp", "php", "cpp", "c", "scala"):
        for c in node.children:
            if c.type in ("modifiers", "modifier", "visibility_modifier", "access_specifier"):
                t = _text(c, src)
                if "public" in t or "protected" in t:
                    return True
            if c.type == "modifier" and _text(c, src) == "public":
                return True
        return lang in ("c", "cpp")  # C has no access modifiers; treat as exported unless static
    if lang in ("kotlin", "swift"):
        for c in node.children:
            if c.type == "modifiers":
                t = _text(c, src)
                if "private" in t or "fileprivate" in t:
                    return False
        return True
    return True


def _decorators(spec: LangSpec, node, src: bytes) -> list[str]:
    if not spec.decorators:
        return []
    out: list[str] = []
    for c in node.children:
        if c.type in spec.decorators:
            out.append(_text(c, src, 120))
        elif c.type in ("modifiers", "attributes", "attribute_list"):
            for cc in c.children:
                if cc.type in spec.decorators or cc.type in ("attribute", "annotation", "marker_annotation"):
                    out.append(_text(cc, src, 120))
            if c.type == "attribute_list" and not out:
                out.append(_text(c, src, 120))
    p = node.parent
    if p is not None and p.type == "decorated_definition":
        for c in p.children:
            if c.type in spec.decorators:
                out.append(_text(c, src, 120))
    sib = node.prev_named_sibling
    while sib is not None and sib.type in spec.decorators:
        out.append(_text(sib, src, 120))
        sib = sib.prev_named_sibling
    # dedupe preserving order
    seen: set[str] = set()
    res = []
    for d in out:
        if d not in seen:
            seen.add(d)
            res.append(d)
    return res


def _doc_for(spec: LangSpec, node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    if spec.name == "python" and body is not None:
        for c in body.children:
            if c.type == "expression_statement" and c.named_child_count and c.named_children[0].type == "string":
                t = _text(c.named_children[0], src).strip()
                t = t.strip("rRbBuU").strip("\"'").strip()
                return re.sub(r"\s+", " ", t)[:400]
            if c.is_named:
                break
        return ""
    # look for a preceding comment
    target = node
    if node.parent is not None and node.parent.type in ("decorated_definition", "export_statement",
                                                          "template_declaration", "lexical_declaration",
                                                          "variable_declaration", "expression_statement"):
        target = node.parent
        if target.parent is not None and target.parent.type == "export_statement":
            target = target.parent
    sib = target.prev_named_sibling
    docs = []
    while sib is not None and sib.type in COMMENT_TYPES and len(docs) < 4:
        if sib.end_point[0] < target.start_point[0] - 2 - len(docs):
            break
        docs.append(_strip_comment(_text(sib, src, 600)))
        sib = sib.prev_named_sibling
    docs.reverse()
    return " ".join(docs)[:400]


def _signature(node, src: bytes) -> str:
    body = node.child_by_field_name("body")
    if body is None:
        for c in node.children:
            if c.type in ("block", "statement_block", "compound_statement", "function_body", "class_body",
                          "declaration_list", "body_statement", "field_declaration_list", "template_body",
                          "enum_body", "interface_body"):
                body = c
                break
    end = body.start_byte if body is not None and body.start_byte > node.start_byte else None
    if end is None:
        t = _text(node, src, 400)
    else:
        t = src[node.start_byte:end].decode("utf-8", "replace")
    t = re.sub(r"\s+", " ", t).strip().rstrip("{:=").strip()
    return t[:240]


def _complexity(spec: LangSpec, node, src: bytes) -> int:
    n = 1
    stack = [node]
    while stack:
        cur = stack.pop()
        for c in cur.children:
            if not c.is_named:
                continue
            if c.type in spec.branches:
                n += 1
            elif c.type == "binary_expression":
                for cc in c.children:
                    if not cc.is_named and src[cc.start_byte:cc.end_byte] in (b"&&", b"||", b"and", b"or", b"??"):
                        n += 1
                        break
            stack.append(c)
    return n


_PARAM_TYPES = frozenset({"parameter", "required_parameter", "optional_parameter", "parameter_declaration",
                          "typed_parameter", "default_parameter", "typed_default_parameter", "simple_parameter",
                          "property_promotion_parameter", "variadic_parameter", "class_parameter", "keyword_argument",
                          "identifier_parameter", "rest_parameter", "optional_type_parameter", "type_parameter"})
_KEYED_TYPES = frozenset({"pair", "property_signature", "public_field_definition", "field_definition",
                          "keyword_argument", "named_argument", "value_argument", "object_creation_expression"})
_MEMBER_TYPES = frozenset({"attribute", "member_expression", "field_expression", "selector_expression",
                           "member_access_expression", "navigation_expression", "scoped_identifier",
                           "qualified_identifier", "field_access", "member_call_expression", "scoped_type_identifier",
                           "nullsafe_member_access_expression", "conditional_member_access_expression",
                           "optional_chaining_expression"})

_IMPORT_KEYWORDS = re.compile(r"^(import|from|use|using|require(_relative|_once)?|include(_once)?|#include|export)\b")


def _import_info(spec: LangSpec, node, src: bytes) -> Optional[ImportRec]:
    raw = re.sub(r"\s+", " ", _text(node, src, 300)).strip().rstrip(";")
    line = node.start_point[0] + 1
    lang = spec.name
    names: list[str] = []
    module = ""
    if lang == "python":
        if node.type == "import_statement":
            bindings: dict[str, str] = {}
            for c in node.named_children:
                if c.type == "dotted_name":
                    names.append(_text(c, src))
                    bindings[_text(c, src).split(".")[0]] = "*"
                elif c.type == "aliased_import":
                    nm = c.child_by_field_name("name")
                    al = c.child_by_field_name("alias")
                    names.append(_text(nm, src) if nm is not None else _text(c, src))
                    if al is not None:
                        bindings[_text(al, src)] = "*"
            module = names[0] if names else raw
            return ImportRec(module=module, raw=raw, line=line, names=[n.split(".")[-1] for n in names],
                             bindings=bindings)
        mod = node.child_by_field_name("module_name")
        module = _text(mod, src) if mod is not None else ""
        bindings: dict[str, str] = {}
        for c in node.named_children:
            if mod is not None and c.id == mod.id:
                continue
            if c.type == "dotted_name":
                names.append(_text(c, src))
                bindings[_text(c, src)] = _text(c, src)
            elif c.type == "aliased_import":
                nm = c.child_by_field_name("name")
                al = c.child_by_field_name("alias")
                orig = _text(nm, src) if nm is not None else _text(c, src)
                names.append(orig)
                bindings[_text(al, src) if al is not None else orig] = orig
            elif c.type == "wildcard_import":
                names.append("*")
        return ImportRec(module=module, raw=raw, line=line, names=names, bindings=bindings)
    if lang in ("javascript", "typescript", "tsx"):
        s = node.child_by_field_name("source")
        module = _string_content(s, src) if s is not None else (_string_content(node, src) or "")
        bindings: dict[str, str] = {}
        stack = list(node.named_children)
        while stack:
            c = stack.pop()
            if c.type == "import_specifier":
                nm = c.child_by_field_name("name")
                al = c.child_by_field_name("alias")
                if nm is not None:
                    local = _text(al, src) if al is not None else _text(nm, src)
                    names.append(local)
                    bindings[local] = _text(nm, src)
            elif c.type == "namespace_import":
                i = _first_ident_desc(c, src)
                if i is not None:
                    names.append(_text(i, src))
                    bindings[_text(i, src)] = "*"
            elif c.type == "import_clause":
                for cc in c.named_children:
                    if cc.type == "identifier":
                        names.append(_text(cc, src))
                        bindings[_text(cc, src)] = "default"
                stack.extend(c.named_children)
            elif c.type == "named_imports":
                stack.extend(c.named_children)
        return ImportRec(module=module or raw, raw=raw, line=line, names=names, bindings=bindings)
    if lang == "go":
        p = node.child_by_field_name("path")
        module = _string_content(p, src) if p is not None else (_string_content(node, src) or raw)
        alias = node.child_by_field_name("name")
        local = _text(alias, src) if alias is not None else module.rsplit("/", 1)[-1]
        return ImportRec(module=module, raw=raw, line=line, names=[local], bindings={local: "*"})
    if lang in ("c", "cpp"):
        module = _string_content(node, src) or raw
        return ImportRec(module=module, raw=raw, line=line, names=[])
    if lang in ("ruby", "php"):
        sc = _string_content(node, src)
        if sc:
            return ImportRec(module=sc, raw=raw, line=line, names=[])
        module = re.sub(r"^(use|namespace)\s+", "", raw).split(" as ")[0].strip()
        return ImportRec(module=module, raw=raw, line=line, names=[module.rsplit("\\", 1)[-1]])
    # java / kotlin / scala / csharp / swift / rust
    module = _IMPORT_KEYWORDS.sub("", raw).strip()
    module = re.sub(r"^(static|typealias)\s+", "", module).strip()
    last = re.split(r"[.:\\/]+", module.replace("::", "."))[-1].strip("{} ")
    bindings = {last: last} if last and last != "*" else {}
    if " as " in raw:
        alias = raw.rsplit(" as ", 1)[-1].strip(" ;")
        if alias:
            bindings = {alias: last}
    return ImportRec(module=module, raw=raw, line=line, names=[last] if last and last != "*" else [],
                     bindings=bindings)


def _span(node, spec: LangSpec, lang: str) -> tuple[int, int]:
    """Byte span an edit should replace: the node plus decorators / export keyword / declaration holder."""
    start, end = node.start_byte, node.end_byte
    p = node.parent
    if p is None:
        return start, end
    if lang == "python" and p.type == "decorated_definition":
        return p.start_byte, p.end_byte
    holder = node
    if node.type in spec.value_defs and p.type in ("lexical_declaration", "variable_declaration") \
            and len([c for c in p.named_children if c.type == "variable_declarator"]) == 1:
        holder = p
        start, end = p.start_byte, p.end_byte
        p = p.parent
    if p is not None and p.type in spec.export_wrappers:
        start, end = p.start_byte, p.end_byte
    # leading decorators/annotations that are siblings (JS/TS decorators, Java annotations inside modifiers are inside)
    sib = holder.prev_named_sibling
    while sib is not None and sib.type in spec.decorators and sib.end_point[0] >= holder.start_point[0] - 1:
        start = sib.start_byte
        sib = sib.prev_named_sibling
    return start, end


def count_errors(root) -> int:
    n = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        if cur.type == "ERROR" or cur.is_missing:
            n += 1
            continue
        if cur.has_error:
            stack.extend(cur.children)
    return n


def extract(spec: LangSpec, src: bytes, ident_filter: Optional[set[str]] = None, tree=None,
            collect_all: bool = False) -> Extraction:
    """Walk the syntax tree once. With ``ident_filter`` collect plain identifier references (only for names in
    the filter); otherwise collect definitions, calls, imports, findings and the identifier-token set. With
    ``collect_all`` the definition pass also records every identifier use in ``ident_refs`` so the caller can
    filter them once the global name set is known (no second walk). ``tree`` reuses a parse of ``src``."""
    if tree is None:
        parser = get_parser(spec.name)
        tree = parser.parse(src)
    root = tree.root_node
    ex = Extraction(lines=src.count(b"\n") + (1 if src and not src.endswith(b"\n") else 0))
    ex.tree = tree
    tokens = ex.tokens
    lang = spec.name
    symbols = ex.symbols
    refs = ex.refs
    collect_idents = ident_filter is not None
    ident_out = refs if collect_idents else ex.ident_refs
    cx: dict[int, int] = {}            # branch counts per enclosing symbol (cyclomatic complexity)
    enc_of: list[Optional[int]] = []   # enclosing symbol at definition time, per symbol index

    # stack items: (node, enclosing_symbol_idx, parent_class_idx, arg_callee)
    stack: list[tuple] = [(root, None, None, None)]
    def_name_nodes: set[int] = set()

    while stack:
        node, enc, parent_cls, arg_callee = stack.pop()
        ntype = node.type
        new_enc = enc
        new_parent = parent_cls
        new_arg = arg_callee
        if not collect_idents and ntype in IDENT_TYPES and node.end_byte - node.start_byte >= 3:
            tokens.add(_text(node, src, 200))
        if enc is not None and not collect_idents and (ntype in spec.branches or (
                ntype == "binary_expression" and any(
                    (not ch.is_named) and src[ch.start_byte:ch.end_byte] in (b"&&", b"||", b"and", b"or", b"??")
                    for ch in node.children))):
            cx[enc] = cx.get(enc, 0) + 1

        # ---- definitions -------------------------------------------------------------
        kind = spec.definitions.get(ntype)
        name_node = None
        if kind is None and ntype in spec.value_defs:
            vfield, mapping = spec.value_defs[ntype]
            val = node.child_by_field_name(vfield)
            if val is not None and val.type in mapping:
                kind = mapping[val.type]
        if kind is not None:
            nf = spec.name_field.get(ntype, "name")
            if lang in ("c", "cpp") and ntype == "function_definition":
                name_node = _c_declarator_name(node.child_by_field_name("declarator"))
            elif lang in ("c", "cpp") and ntype == "type_definition":
                d = node.child_by_field_name("declarator")
                name_node = _c_declarator_name(d) if d is not None else None
            else:
                name_node = node.child_by_field_name(nf)
                if name_node is None and lang == "kotlin":
                    for c in node.children:
                        if c.type in ("simple_identifier", "type_identifier"):
                            name_node = c
                            break
                if name_node is None:
                    for c in node.children:
                        if c.type in IDENT_TYPES:
                            name_node = c
                            break
                if name_node is not None and name_node.type not in IDENT_TYPES:
                    inner = _last_ident_desc(name_node) if lang == "lua" else _first_ident_desc(name_node, src)
                    name_node = inner or name_node
            if name_node is not None:
                name = _text(name_node, src, 200)
                if lang == "csharp" and ntype in ("namespace_declaration", "file_scoped_namespace_declaration"):
                    name = _text(node.child_by_field_name("name") or name_node, src, 200)
                if name and (ntype not in ("struct_specifier", "class_specifier", "enum_specifier", "union_specifier")
                             or node.child_by_field_name("body") is not None):
                    if kind == "function" and parent_cls is not None and lang not in ("go",):
                        kind = "method"
                    if lang == "rust" and ntype == "impl_item":
                        tr = node.child_by_field_name("trait")
                        if tr is not None:
                            name = f"{_text(tr, src, 80)} for {name}"
                    if lang == "swift" and ntype == "class_declaration" and any(
                            not c.is_named and src[c.start_byte:c.end_byte] == b"extension" for c in node.children):
                        kind = "extension"
                    if kind == "class" and ntype == "class_declaration" and lang == "swift":
                        head = src[node.start_byte:name_node.start_byte]
                        if b"struct" in head:
                            kind = "struct"
                        elif b"enum" in head:
                            kind = "enum"
                        elif b"protocol" in head:
                            kind = "interface"
                    qual = name
                    if parent_cls is not None:
                        qual = f"{symbols[parent_cls].qualified}.{name}"
                    idx = len(symbols)
                    sp_start, sp_end = _span(node, spec, lang)
                    symbols.append(SymbolRec(
                        name=name, kind=kind, qualified=qual, parent_idx=parent_cls,
                        start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                        signature=_signature(node, src), doc=_doc_for(spec, node, src),
                        complexity=1 if kind in ("function", "method") else 0,
                        decorators=_decorators(spec, node, src),
                        exported=_is_exported(spec, node, name, src),
                        start_byte=node.start_byte, end_byte=node.end_byte, span_start=sp_start, span_end=sp_end,
                        name_start=name_node.start_byte, name_end=name_node.end_byte,
                        body_hash=hashlib.blake2b(src[node.start_byte:node.end_byte], digest_size=8).hexdigest()))
                    def_name_nodes.add(name_node.id)
                    enc_of.append(enc)
                    new_enc = idx
                    if kind in CLASS_KINDS:
                        new_parent = idx
                    elif kind in ("function", "method"):
                        new_parent = None  # closures inside functions are plain functions
                    # findings: long / complex handled at scan time from symbols table

        # ---- imports -----------------------------------------------------------------
        if ntype in spec.imports:
            rec = _import_info(spec, node, src)
            if rec is not None:
                ex.imports.append(rec)
                if collect_idents or collect_all:
                    for nm in rec.names:
                        if collect_all or nm in ident_filter:
                            ident_out.append(RefRec(nm, rec.line, node.start_point[1], "import", rec.module, enc))
        elif lang == "ruby" and ntype == "call":
            m = node.child_by_field_name("method")
            if m is not None and _text(m, src) in ("require", "require_relative", "load"):
                sc = _string_content(node, src)
                if sc:
                    ex.imports.append(ImportRec(module=sc, raw=_text(node, src, 200), line=node.start_point[0] + 1))

        # ---- calls -------------------------------------------------------------------
        callee_field = spec.calls.get(ntype)
        callee_node = None
        callee_name = None
        if callee_field is not None:
            if callee_field == "__first":
                callee_node = node.named_children[0] if node.named_child_count else None
            else:
                callee_node = node.child_by_field_name(callee_field)
            if callee_node is not None:
                leaf = callee_node if callee_node.type in IDENT_TYPES else _last_ident_desc(callee_node)
                if leaf is not None and leaf.type in IDENT_TYPES:
                    callee_name = _text(leaf, src, 200)
                    if lang == "bash" and callee_node.type == "command_name":
                        callee_name = _text(callee_node, src, 200)
                    qualifier = None
                    if leaf is not callee_node:
                        q = _text(callee_node, src, 120)
                        qualifier = q[: max(0, len(q) - len(callee_name) - 1)] or None
                    rkind = "new" if ntype in ("new_expression", "object_creation_expression") else "call"
                    if ntype == "macro_invocation":
                        callee_name += "!"
                    if not collect_idents:
                        refs.append(RefRec(callee_name, leaf.start_point[0] + 1, leaf.start_point[1], rkind,
                                           qualifier, enc))
                    def_name_nodes.add(leaf.id)

        # ---- identifier refs (pass 2) ------------------------------------------------
        if (collect_idents or collect_all) and ntype in IDENT_TYPES and node.id not in def_name_nodes:
            nm = _text(node, src, 200)
            if collect_all or nm in ident_filter:
                p = node.parent
                ptype = p.type if p is not None else ""
                skip = False
                qualifier = None
                if ptype in _PARAM_TYPES:
                    # parameter *names* are declarations; default values / type names are references
                    nn = p.child_by_field_name("name") or p.child_by_field_name("pattern")
                    if nn is not None:
                        skip = nn.id == node.id
                    else:
                        skip = bool(p.named_children) and p.named_children[0].id == node.id
                elif ptype in ("formal_parameters", "parameters", "parameter_list", "lambda_parameters"):
                    skip = True
                elif ptype in _KEYED_TYPES:
                    kn = (p.child_by_field_name("key") or p.child_by_field_name("name") or
                          p.child_by_field_name("property"))
                    if kn is not None:
                        skip = kn.id == node.id
                    else:  # grammars without field names (Kotlin `text = ...`): key is followed by '=' / ':'
                        ns = node.next_sibling
                        skip = ns is not None and not ns.is_named and src[ns.start_byte:ns.end_byte] in (b"=", b":", b"=>")
                elif ptype in ("argument", "value_argument", "named_argument", "labeled_argument"):
                    ns = node.next_sibling
                    skip = ns is not None and not ns.is_named and src[ns.start_byte:ns.end_byte] in (b"=", b":")
                elif ptype in ("shorthand_property_identifier_pattern", "label", "statement_identifier"):
                    skip = True
                elif ptype in _MEMBER_TYPES or ptype in ("navigation_suffix", "scoped_call_expression"):
                    holder = p.parent if ptype == "navigation_suffix" and p.parent is not None else p
                    obj = holder.named_children[0] if holder.named_child_count else None
                    if obj is not None and obj.id != node.id:
                        qualifier = _text(obj, src, 60)
                    # `foo.bar`: `foo` itself is a plain reference (module/class/variable); keep it
                if not skip:
                    rk = "arg" if arg_callee else "ref"
                    ident_out.append(RefRec(nm, node.start_point[0] + 1, node.start_point[1], rk,
                                            arg_callee if arg_callee else qualifier, enc))

        # ---- findings ----------------------------------------------------------------
        if not collect_idents:
            if ntype in COMMENT_TYPES:
                t = _text(node, src, 4000)
                for m in TAG_RE.finditer(t):
                    tag = (m.group(1) or m.group(2)).upper()
                    if tag == "NOTE":
                        continue
                    msg = _strip_comment(m.group(3))[:160]
                    ln = node.start_point[0] + 1 + t[:m.start()].count("\n")
                    ex.findings.append((ln, f"TAG_{tag}", msg))
            elif ntype in spec.bare_except:
                named = [c for c in node.named_children if c.type not in COMMENT_TYPES]
                body = [c for c in named if c.type == "block"]
                others = [c for c in named if c.type != "block"]
                if not others:
                    ex.findings.append((node.start_point[0] + 1, "BARE_EXCEPT", "bare `except:` catches everything"))
                if body:
                    stmts = [c for c in body[0].named_children if c.type not in COMMENT_TYPES]
                    commented = any(c.type in COMMENT_TYPES for c in body[0].named_children) or \
                        any(c.type in COMMENT_TYPES for c in node.named_children) or \
                        any(c.type in COMMENT_TYPES for c in stmts[0].children) if stmts else False
                    if len(stmts) == 1 and stmts[0].type == "pass_statement" and not commented:
                        ex.findings.append((node.start_point[0] + 1, "SWALLOWED_EXCEPTION",
                                            "exception handler only does `pass`"))
            elif ntype in spec.catch_clause:
                body = node.child_by_field_name("body")
                if body is None:
                    for c in node.named_children:
                        if c.type in ("statement_block", "block", "compound_statement", "statements"):
                            body = c
                            break
                if body is not None:
                    stmts = [c for c in body.named_children if c.type not in COMMENT_TYPES]
                    commented = any(c.type in COMMENT_TYPES for c in body.named_children) or \
                        any(c.type in COMMENT_TYPES for c in node.named_children)
                    if not stmts and not commented:
                        ex.findings.append((node.start_point[0] + 1, "EMPTY_CATCH", "empty catch block swallows errors"))

        # ---- recurse -----------------------------------------------------------------
        children = node.children
        if not children:
            continue
        args_node = None
        if callee_name is not None:
            args_node = node.child_by_field_name("arguments")
        for c in reversed(children):
            if c.is_named or c.type in IDENT_TYPES:
                if args_node is not None and c.id == args_node.id:
                    stack.append((c, new_enc, new_parent, callee_name))
                else:
                    stack.append((c, new_enc, new_parent, new_arg if (callee_node is None or c.id != callee_node.id) else None))
    if not collect_idents:
        # complexity = 1 + branches in the function's subtree (nested closures included, like a subtree walk)
        for idx in range(len(symbols) - 1, -1, -1):
            sy = symbols[idx]
            n = cx.get(idx, 0)
            if sy.kind in ("function", "method"):
                sy.complexity += n
                n = sy.complexity - 1
            if n:
                par = enc_of[idx]
                while par is not None and symbols[par].kind not in ("function", "method"):
                    par = enc_of[par]
                if par is not None:
                    cx[par] = cx.get(par, 0) + n
        ex.error_count = count_errors(root) if root.has_error else 0
    return ex


# --------------------------------------------------------------------------------------
# Repository walking
# --------------------------------------------------------------------------------------
class IgnoreRules:
    def __init__(self, root: Path, extra_patterns: Iterable[str] = ()):
        self.root = root
        self.specs: list[tuple[str, pathspec.PathSpec]] = []
        pats = list(extra_patterns)
        gi = root / ".gitignore"
        if gi.exists():
            pats += gi.read_text(encoding="utf-8", errors="replace").splitlines()
        ca = root / ".codeatlas" / "ignore"
        if ca.exists():
            pats += ca.read_text(encoding="utf-8", errors="replace").splitlines()
        if pats:
            self.specs.append(("", pathspec.GitIgnoreSpec.from_lines(pats)))

    def add_nested(self, rel_dir: str, gitignore: Path) -> None:
        try:
            lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        if lines:
            self.specs.append((rel_dir, pathspec.GitIgnoreSpec.from_lines(lines)))

    def ignored(self, rel: str, is_dir: bool = False) -> bool:
        probe = rel + "/" if is_dir else rel
        for base, spec in self.specs:
            if base and not rel.startswith(base + "/"):
                continue
            sub = probe[len(base) + 1:] if base else probe
            if spec.match_file(sub):
                return True
        return False


def looks_minified(path: str) -> bool:
    """True for generated/minified sources (very long lines), which are noise for a code map."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    if not head:
        return False
    nl = head.count(b"\n")
    return nl < 8 or len(head) / max(1, nl) > 400


def _is_manifest_dir(filenames: Iterable[str], dirnames: Iterable[str]) -> bool:
    for f in filenames:
        if f in MANIFEST_FILES or f.endswith(MANIFEST_SUFFIXES):
            return True
    return any(d.endswith(".xcodeproj") for d in dirnames)


def project_for(root: Path, rel: str) -> str:
    """Nearest ancestor directory (relative, '' = repo root) holding a package manifest."""
    parts = rel.split("/")[:-1]
    for n in range(len(parts), -1, -1):
        d = root.joinpath(*parts[:n]) if n else root
        try:
            names = os.listdir(d)
        except OSError:
            continue
        if _is_manifest_dir(names, names):
            return "/".join(parts[:n])
    return ""


def walk_repo(root: Path, ignore: Optional[IgnoreRules] = None) -> Iterable[tuple[str, str, os.stat_result, str]]:
    """Yield (relative_path, lang, stat, project) for indexable source files."""
    ignore = ignore or IgnoreRules(root)
    root_str = str(root)
    project_dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_str):
        rel_dir = os.path.relpath(dirpath, root_str).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        if _is_manifest_dir(filenames, dirnames):
            project_dirs.append(rel_dir)
        project = ""
        for pd in project_dirs:
            if pd == "" or rel_dir == pd or rel_dir.startswith(pd + "/"):
                if len(pd) >= len(project):
                    project = pd
        if ".gitignore" in filenames and rel_dir:
            ignore.add_nested(rel_dir, Path(dirpath) / ".gitignore")
        keep = []
        for d in dirnames:
            if d in DEFAULT_IGNORE_DIRS or d.startswith(".git"):
                continue
            rd = f"{rel_dir}/{d}" if rel_dir else d
            if ignore.ignored(rd, is_dir=True):
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        for fn in filenames:
            lang = lang_for_path(fn)
            if lang is None:
                continue
            low = fn.lower()
            if low.endswith((".min.js", ".min.css", ".bundle.js", ".d.ts.map")) or ".generated." in low:
                continue
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            if ignore.ignored(rel):
                continue
            try:
                st = os.stat(os.path.join(dirpath, fn))
            except OSError:
                continue
            if st.st_size > MAX_FILE_BYTES or st.st_size == 0:
                continue
            if st.st_size > 20_000 and looks_minified(os.path.join(dirpath, fn)):
                continue
            if st.st_size > LOW_SIGNAL_MAX_BYTES and LOW_SIGNAL_DIRS.intersection(rel.split("/")[:-1]):
                continue
            yield rel, lang, st, project


# --------------------------------------------------------------------------------------
# Import resolution
# --------------------------------------------------------------------------------------
_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".d.ts")


class Resolver:
    def __init__(self, paths: Iterable[str]):
        self.paths: set[str] = set(paths)
        self.by_name: dict[str, list[str]] = {}
        for p in self.paths:
            self.by_name.setdefault(os.path.basename(p).lower(), []).append(p)
        self.no_ext: dict[str, list[str]] = {}
        for p in self.paths:
            stem = p.rsplit(".", 1)[0]
            self.no_ext.setdefault(stem, []).append(p)

    def _suffix_match(self, suffix: str) -> Optional[str]:
        """Find the unique (or shortest) repo path that ends with the suffix."""
        name = os.path.basename(suffix).lower()
        cands = [p for p in self.by_name.get(name, []) if p.lower().endswith(suffix.lower())]
        if not cands:
            return None
        cands.sort(key=len)
        return cands[0]

    def resolve(self, lang: str, module: str, from_file: str) -> Optional[str]:
        if not module:
            return None
        from_dir = os.path.dirname(from_file)
        try:
            if lang == "python":
                return self._py(module, from_dir)
            if lang in ("javascript", "typescript", "tsx"):
                return self._js(module, from_dir)
            if lang in ("c", "cpp"):
                return self._rel_or_suffix(module, from_dir)
            if lang == "go":
                parts = module.strip("/").split("/")
                for n in range(min(3, len(parts)), 0, -1):
                    suffix = "/".join(parts[-n:])
                    dirs = {os.path.dirname(p) for p in self.paths if p.endswith(".go")}
                    for d in sorted(dirs, key=len):
                        if d == suffix or d.endswith("/" + suffix):
                            return d + "/"
                return None
            if lang == "rust":
                m = module.replace("::", "/").strip("/")
                m = re.sub(r"\{.*$", "", m).strip("/")
                m = re.sub(r"^(crate|self|super)/?", "", m)
                parts = [p for p in m.split("/") if p]
                for n in range(len(parts), 0, -1):
                    sub = "/".join(parts[:n])
                    for cand in (f"src/{sub}.rs", f"src/{sub}/mod.rs", f"{sub}.rs", f"{sub}/mod.rs"):
                        hit = self._suffix_match(cand)
                        if hit:
                            return hit
                return None
            if lang in ("java", "kotlin", "scala"):
                m = module.split(" as ")[0].strip()
                parts = [p for p in m.replace("\\", ".").split(".") if p and p != "*"]
                for n in range(len(parts), 0, -1):
                    base = "/".join(parts[:n])
                    for ext in (".java", ".kt", ".scala", ".kts"):
                        hit = self._suffix_match(base + ext)
                        if hit:
                            return hit
                return None
            if lang == "csharp":
                return None
            if lang == "php":
                if module.endswith(".php"):
                    return self._rel_or_suffix(module, from_dir)
                parts = [p for p in module.replace("\\", "/").split("/") if p]
                for n in range(len(parts), 0, -1):
                    hit = self._suffix_match("/".join(parts[-n:]) + ".php")
                    if hit:
                        return hit
                return None
            if lang == "ruby":
                m = module if module.endswith(".rb") else module + ".rb"
                return self._rel_or_suffix(m, from_dir) or self._suffix_match("lib/" + m)
            if lang == "swift":
                return None
        except Exception:
            return None
        return None

    def _norm(self, p: str) -> str:
        return os.path.normpath(p).replace("\\", "/").lstrip("./")

    def _rel_or_suffix(self, module: str, from_dir: str) -> Optional[str]:
        cand = self._norm(os.path.join(from_dir, module))
        if cand in self.paths:
            return cand
        return self._suffix_match(module.lstrip("./"))

    def _py(self, module: str, from_dir: str) -> Optional[str]:
        module = module.strip()
        if module.startswith("."):
            dots = len(module) - len(module.lstrip("."))
            rest = module[dots:]
            base = from_dir
            for _ in range(dots - 1):
                base = os.path.dirname(base)
            rel = rest.replace(".", "/")
            for cand in (f"{base}/{rel}.py", f"{base}/{rel}/__init__.py", f"{base}/__init__.py" if not rel else ""):
                if cand:
                    cand = self._norm(cand)
                    if cand in self.paths:
                        return cand
            return None
        rel = module.replace(".", "/")
        parts = rel.split("/")
        for n in range(len(parts), 0, -1):
            sub = "/".join(parts[:n])
            for cand in (sub + ".py", sub + "/__init__.py"):
                if cand in self.paths:
                    return cand
                hit = self._suffix_match("/" + cand) if "/" in cand else None
                if hit:
                    return hit
                hit = self._suffix_match(cand)
                if hit and (hit == cand or hit.endswith("/" + cand)):
                    return hit
        return None

    def _js(self, module: str, from_dir: str) -> Optional[str]:
        m = module.split("?")[0]
        if m.startswith("."):
            base = self._norm(os.path.join(from_dir, m))
        elif m.startswith(("@/", "~/", "#/", "src/", "$lib/")):
            base = m.split("/", 1)[1]
            hits = [p for p in self._candidates(base) if p]
            return hits[0] if hits else None
        else:
            return None
        for c in self._candidates(base):
            if c:
                return c
        return None

    def _candidates(self, base: str) -> Iterable[Optional[str]]:
        if base in self.paths:
            yield base
        stem = base
        for ext in _JS_EXT:
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        for ext in _JS_EXT:
            c = stem + ext
            if c in self.paths:
                yield c
        for ext in _JS_EXT:
            c = f"{stem}/index{ext}"
            if c in self.paths:
                yield c
        hits = self.no_ext.get(stem)
        if hits:
            yield sorted(hits, key=len)[0]
        # suffix match for alias roots like @/components/x
        name = os.path.basename(stem)
        for p in self.by_name.get(name.lower() + ".ts", []) + self.by_name.get(name.lower() + ".tsx", []):
            if p.rsplit(".", 1)[0].endswith(stem):
                yield p


# --------------------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------------------
@dataclass
class UpdateStats:
    scanned: int = 0
    parsed: int = 0
    removed: int = 0
    unchanged: int = 0
    ref_files: int = 0
    relationship_files: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    # AST delta of this update: what SNIPE's scan reacts to
    delta: dict = field(default_factory=lambda: {"files": [], "added": [], "removed": [], "changed": [],
                                                 "parse_errors": [], "new_tags": [], "broken_imports": []})

    def as_dict(self) -> dict:
        d = self.delta
        return {"scanned": self.scanned, "parsed": self.parsed, "removed": self.removed,
                "unchanged": self.unchanged, "ref_files": self.ref_files,
                "relationship_files": self.relationship_files, "seconds": round(self.seconds, 2),
                "errors": self.errors[:10],
                "delta": {"files": len(d["files"]), "added": len(d["added"]), "removed": len(d["removed"]),
                          "changed": len(d["changed"]), "parse_errors": len(d["parse_errors"]),
                          "new_tags": len(d["new_tags"]), "broken_imports": len(d["broken_imports"])}}


class Index:
    """SQLite-backed code index for one repository."""

    def __init__(self, repo: Path, db_path: Optional[Path] = None):
        self.repo = Path(repo).resolve()
        self.dir = self.repo / ".codeatlas"
        self.dir.mkdir(exist_ok=True)
        self.db_path = db_path or (self.dir / "index.db")
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        ver = self.get_meta("schema_version")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(symbols)")}
        if ver != str(SCHEMA_VERSION) or "body_hash" not in cols:
            self._reset()
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.last_scan: float = 0.0
        self.on_update: list[Callable[[UpdateStats], None]] = []
        gi = self.dir / ".gitignore"
        if not gi.exists():
            gi.write_text("index.db*\n*.log\n", encoding="utf-8")

    # -- meta ----------------------------------------------------------------------
    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
            self.conn.commit()

    def _reset(self) -> None:
        """Schema changed: drop everything and recreate (a full re-index follows on the next update)."""
        with self.lock:
            for t in ("code_patterns", "ca_relationships", "ca_entity_links", "ca_artifact_entities", "ca_artifact_files",
                      "findings", "imports", "refs", "symbols", "files", "symbol_rank", "file_rank", "symbol_refs",
                      "file_tokens", "symbols_fts"):
                self.conn.execute(f"DROP TABLE IF EXISTS {t}")
            self.conn.execute("DELETE FROM meta")
            self.conn.commit()
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # -- helpers --------------------------------------------------------------------
    def rel(self, path: str | os.PathLike) -> str:
        p = Path(path)
        if p.is_absolute():
            try:
                p = p.resolve().relative_to(self.repo)
            except ValueError:
                return str(path).replace("\\", "/")
        return str(p).replace("\\", "/")

    def abs(self, rel: str) -> Path:
        return self.repo / rel

    def read_source(self, rel: str) -> Optional[bytes]:
        try:
            return self.abs(rel).read_bytes()
        except OSError:
            return None

    def all_paths(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT path FROM files")]

    def file_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    # -- update ---------------------------------------------------------------------
    def update(self, full: bool = False, paths: Optional[Iterable[str]] = None,
               progress: Optional[Callable[[str], None]] = None) -> UpdateStats:
        """Bring the index up to date. ``paths`` limits the check to specific files (watcher);
        otherwise the whole tree is stat-scanned (cheap) and only changed files are parsed."""
        t0 = time.time()
        stats = UpdateStats()
        paths = list(paths) if paths is not None else None
        with self.lock:
            existing = {r["path"]: (r["id"], r["size"], r["mtime"]) for r in
                        self.conn.execute("SELECT id, path, size, mtime FROM files")}
            to_parse: list[tuple[str, str]] = []
            removed: list[str] = []
            if paths is None:
                seen: set[str] = set()
                ignore = IgnoreRules(self.repo)
                for rel, lang, st, project in walk_repo(self.repo, ignore):
                    seen.add(rel)
                    stats.scanned += 1
                    ex = existing.get(rel)
                    if full or ex is None or ex[1] != st.st_size or abs(ex[2] - st.st_mtime) > 1e-6:
                        to_parse.append((rel, lang, project))
                    else:
                        stats.unchanged += 1
                removed = [p for p in existing if p not in seen]
            else:
                ignore = IgnoreRules(self.repo)
                for p in paths:
                    rel = self.rel(p)
                    lang = lang_for_path(rel)
                    ap = self.abs(rel)
                    if lang is None:
                        continue
                    if not ap.exists():
                        if rel in existing:
                            removed.append(rel)
                        continue
                    if ignore.ignored(rel) or any(part in DEFAULT_IGNORE_DIRS for part in rel.split("/")[:-1]):
                        continue
                    st = ap.stat()
                    if st.st_size > MAX_FILE_BYTES or st.st_size == 0:
                        continue
                    if (st.st_size > 20_000 and looks_minified(str(ap))) or (
                            st.st_size > LOW_SIGNAL_MAX_BYTES and LOW_SIGNAL_DIRS.intersection(rel.split("/")[:-1])):
                        if rel in existing:
                            removed.append(rel)
                        continue
                    ex = existing.get(rel)
                    stats.scanned += 1
                    if full or ex is None or ex[1] != st.st_size or abs(ex[2] - st.st_mtime) > 1e-6:
                        to_parse.append((rel, lang, project_for(self.repo, rel)))
                    else:
                        stats.unchanged += 1

            delta = stats.delta
            if removed:
                for p in removed:
                    rid = existing[p][0]
                    for r in self.conn.execute("SELECT qualified, name FROM symbols WHERE file_id=?", (rid,)):
                        delta["removed"].append((r[0], p, r[1]))
                    for r in self.conn.execute(
                            "SELECT f.path, i.module FROM imports i JOIN files f ON f.id=i.file_id WHERE i.resolved_file_id=?", (rid,)):
                        delta["broken_imports"].append((r[0], r[1]))
                delta["files"].extend(removed)
                self.conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in removed])
                stats.removed = len(removed)

            if not to_parse and not removed:
                self._sync_relationships(stats, paths, full)
                self.last_scan = time.time()
                stats.seconds = time.time() - t0
                if stats.relationship_files:
                    for cb in list(self.on_update):
                        try:
                            cb(stats)
                        except Exception:
                            pass
                return stats

            names_before = {r[0] for r in self.conn.execute("SELECT DISTINCT name FROM symbols")}

            # ---- pass 1: definitions / calls / imports / findings / tokens ---------
            parsed_files: list[tuple[str, str, int]] = []  # (rel, lang, file_id)
            new_names: set[str] = set()
            old_res_by_fid: dict[int, tuple[str, set[str]]] = {}
            keep_trees = len(to_parse) <= 400  # reuse parses in pass 2 for small batches (typical edits)
            trees: dict[int, tuple[bytes, object]] = {}
            for i, (rel, lang, project) in enumerate(to_parse):
                if progress and (i % 200 == 0):
                    progress(f"parsing {i}/{len(to_parse)}: {rel}")
                src = self.read_source(rel)
                if src is None:
                    continue
                spec = SPECS[lang]
                try:
                    ex = extract(spec, src, None, collect_all=keep_trees)
                except Exception as e:  # pragma: no cover - grammar edge cases
                    stats.errors.append(f"{rel}: {e}")
                    continue
                # old state of this file (for the AST delta)
                old_syms: dict[str, tuple[str, str]] = {}
                old_tags: set[tuple] = set()
                old_res: set[str] = set()
                if rel in existing:
                    ofid = existing[rel][0]
                    old_syms = {r[0]: (r[1] or "", r[2]) for r in self.conn.execute(
                        "SELECT qualified, body_hash, name FROM symbols WHERE file_id=?", (ofid,))}
                    old_tags = {(r[0], r[1]) for r in self.conn.execute(
                        "SELECT code, message FROM findings WHERE file_id=? AND code LIKE 'TAG_%'", (ofid,))}
                    old_res = {r[0] for r in self.conn.execute(
                        "SELECT module FROM imports WHERE file_id=? AND resolved_file_id IS NOT NULL", (ofid,))}
                st = self.abs(rel).stat()
                fid = self._store_file(rel, lang, st, src, ex, project)
                parsed_files.append((rel, lang, fid))
                old_res_by_fid[fid] = (rel, old_res)
                delta["files"].append(rel)
                new_syms = {s.qualified: s for s in ex.symbols}
                for q, s in new_syms.items():
                    if q not in old_syms:
                        delta["added"].append((q, rel))
                    elif old_syms[q][0] != s.body_hash:
                        delta["changed"].append((q, rel))
                for q, (h, nm) in old_syms.items():
                    if q not in new_syms:
                        delta["removed"].append((q, rel, nm))
                if ex.error_count:
                    delta["parse_errors"].append((rel, ex.error_count))
                for ln, code, msg in ex.findings:
                    if code.startswith("TAG_") and (code, msg) not in old_tags:
                        delta["new_tags"].append((rel, ln, code, msg))
                if keep_trees:
                    trees[fid] = (src, ex)
                for s in ex.symbols:
                    if s.name not in names_before:
                        new_names.add(s.name)
                stats.parsed += 1
            self.conn.commit()

            # ---- resolve imports for parsed files + files importing changed files --
            resolver = Resolver(self.all_paths())
            self._resolve_imports(resolver, [f[2] for f in parsed_files])
            # Other files' unresolved imports can only start resolving when a file appears (or stop when one
            # disappears); a modified file never changes them, so a plain save skips this repo-wide pass.
            added_any = any(rel not in existing for rel, _, _ in parsed_files)
            if added_any or removed:
                rows = self.conn.execute(
                    "SELECT DISTINCT file_id FROM imports WHERE resolved_file_id IS NULL "
                    "OR resolved_file_id NOT IN (SELECT id FROM files)").fetchall()
                self._resolve_imports(resolver, [r[0] for r in rows])
            for fid, (rel, old_res) in old_res_by_fid.items():
                if not old_res:
                    continue
                for r in self.conn.execute("SELECT module FROM imports WHERE file_id=? AND resolved_file_id IS NULL", (fid,)):
                    if r[0] in old_res:
                        delta["broken_imports"].append((rel, r[0]))
            self.conn.commit()

            # ---- pass 2: identifier references (same trees, no re-parse) -----------
            all_names = {r[0] for r in self.conn.execute("SELECT DISTINCT name FROM symbols")}
            ref_files = {fid: (rel, lang) for rel, lang, fid in parsed_files}
            if new_names and not full:
                # unchanged files that mention a newly defined name (token index, no file reads)
                extra = self._files_mentioning(new_names, exclude={f[2] for f in parsed_files})
                ref_files.update(extra)
            for i, (fid, (rel, lang)) in enumerate(ref_files.items()):
                if progress and (i % 200 == 0):
                    progress(f"references {i}/{len(ref_files)}: {rel}")
                cached = trees.get(fid)
                if cached is not None:
                    # identifiers were collected during the definition walk: filter, no second walk
                    src, ex1 = cached
                    ex = Extraction(symbols=ex1.symbols, refs=[r for r in ex1.ident_refs if r.name in all_names])
                else:
                    src = self.read_source(rel)
                    if src is None:
                        continue
                    try:
                        ex = extract(SPECS[lang], src, all_names)
                    except Exception as e:  # pragma: no cover
                        stats.errors.append(f"{rel}: {e}")
                        continue
                self._store_ident_refs(fid, ex)
                stats.ref_files += 1
            trees.clear()
            self.conn.commit()
            self._sync_relationships(stats, paths, full)
            self.set_meta("last_update", str(time.time()))
            self.last_scan = time.time()
        stats.seconds = time.time() - t0
        for cb in list(self.on_update):
            try:
                cb(stats)
            except Exception:
                pass
        return stats

    def _sync_relationships(self, stats: UpdateStats, paths, full: bool) -> None:
        from .relationships import sync_relationships
        # A full artifact inventory stats every config/schema file. Targeted (watcher) syncs are cheap and
        # event-driven, so the full inventory is only repeated every 60s: a no-op refresh stays ~free.
        now = time.time()
        if paths is None and not full and stats.parsed == 0 and stats.removed == 0 \
                and now - getattr(self, "_rel_inventory_at", 0.0) < 60.0:
            return
        if paths is None:
            self._rel_inventory_at = now
        result = sync_relationships(self, paths=paths, full=full)
        changed = result.get("changed_files", [])
        stats.relationship_files = len(changed)
        if changed:
            self.set_meta("relationships_updated", str(time.time()))
        stats.errors.extend(result.get("errors", []))

    def _files_mentioning(self, names: set[str], exclude: set[int]) -> dict[int, tuple[str, str]]:
        """Files whose identifier-token index contains any of ``names`` (no source reads)."""
        out: dict[int, tuple[str, str]] = {}
        names_l = sorted(n for n in names if len(n) >= 3)
        for i in range(0, len(names_l), 400):
            chunk = names_l[i:i + 400]
            q = ("SELECT DISTINCT f.id, f.path, f.lang FROM file_tokens t JOIN files f ON f.id=t.file_id "
                 "WHERE t.token IN (%s)" % ",".join("?" * len(chunk)))
            for r in self.conn.execute(q, chunk):
                if r[0] not in exclude:
                    out[r[0]] = (r[1], r[2])
        return out

    def _store_file(self, rel: str, lang: str, st: os.stat_result, src: bytes, ex: Extraction, project: str = "") -> int:
        c = self.conn
        h = hashlib.blake2b(src, digest_size=12).hexdigest()
        row = c.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()
        if row:
            fid = row[0]
            c.execute("UPDATE files SET lang=?, size=?, mtime=?, hash=?, lines=?, indexed_at=?, project=? WHERE id=?",
                      (lang, st.st_size, st.st_mtime, h, ex.lines, time.time(), project, fid))
            for t in ("symbols", "refs", "imports", "findings", "file_tokens"):
                c.execute(f"DELETE FROM {t} WHERE file_id=?", (fid,))
        else:
            cur = c.execute("INSERT INTO files(path, lang, size, mtime, hash, lines, indexed_at, project) "
                            "VALUES(?,?,?,?,?,?,?,?)", (rel, lang, st.st_size, st.st_mtime, h, ex.lines, time.time(), project))
            fid = cur.lastrowid
        # symbols (parents first: they always precede children in extraction order)
        for s in ex.symbols:
            parent_id = ex.symbols[s.parent_idx].db_id if s.parent_idx is not None else None
            cur = c.execute(
                "INSERT INTO symbols(file_id,name,kind,qualified,parent_id,start_line,end_line,signature,doc,"
                "complexity,decorators,exported,start_byte,end_byte,span_start,span_end,name_start,name_end,body_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, s.name, s.kind, s.qualified, parent_id, s.start_line, s.end_line, s.signature, s.doc,
                 s.complexity, json.dumps(s.decorators) if s.decorators else None, 1 if s.exported else 0,
                 s.start_byte, s.end_byte, s.span_start, s.span_end, s.name_start, s.name_end, s.body_hash))
            s.db_id = cur.lastrowid
        c.executemany(
            "INSERT INTO refs(file_id,name,line,col,kind,qualifier,enclosing_id) VALUES(?,?,?,?,?,?,?)",
            [(fid, r.name, r.line, r.col, r.kind, r.qualifier,
              ex.symbols[r.enclosing_idx].db_id if r.enclosing_idx is not None else None) for r in ex.refs])
        c.executemany("INSERT INTO imports(file_id,module,raw,line,names,bindings) VALUES(?,?,?,?,?,?)",
                      [(fid, im.module, im.raw, im.line, json.dumps(im.names) if im.names else None,
                        json.dumps(im.bindings) if im.bindings else None) for im in ex.imports])
        c.executemany("INSERT INTO file_tokens(file_id, token) VALUES(?,?)", [(fid, t) for t in ex.tokens])
        c.executemany("INSERT INTO findings(file_id,line,code,message) VALUES(?,?,?,?)",
                      [(fid, ln, code, msg) for ln, code, msg in ex.findings])
        from .patterns import store_patterns
        store_patterns(self, fid, lang, src, tree=ex.tree)
        return fid

    def _store_ident_refs(self, fid: int, ex: Extraction) -> None:
        c = self.conn
        c.execute("DELETE FROM refs WHERE file_id=? AND kind IN ('ref','arg','import')", (fid,))
        # map enclosing symbol by (start_line, name) -> db id
        m = {(r["start_line"], r["name"]): r["id"] for r in
             c.execute("SELECT id, start_line, name FROM symbols WHERE file_id=?", (fid,))}
        rows = []
        seen: set[tuple] = set()
        for r in ex.refs:
            key = (r.name, r.line, r.col)
            if key in seen:
                continue
            seen.add(key)
            enc = None
            if r.enclosing_idx is not None:
                s = ex.symbols[r.enclosing_idx]
                enc = m.get((s.start_line, s.name))
            rows.append((fid, r.name, r.line, r.col, r.kind, r.qualifier, enc))
        c.executemany("INSERT INTO refs(file_id,name,line,col,kind,qualifier,enclosing_id) VALUES(?,?,?,?,?,?,?)", rows)

    def _resolve_imports(self, resolver: Resolver, file_ids: Iterable[int]) -> None:
        c = self.conn
        for fid in file_ids:
            frow = c.execute("SELECT path, lang FROM files WHERE id=?", (fid,)).fetchone()
            if not frow:
                continue
            for im in c.execute("SELECT id, module FROM imports WHERE file_id=?", (fid,)).fetchall():
                target = resolver.resolve(frow["lang"], im["module"], frow["path"])
                tid = None
                if target:
                    if target.endswith("/"):
                        trow = c.execute("SELECT id FROM files WHERE path LIKE ? ORDER BY length(path) LIMIT 1",
                                         (target + "%",)).fetchone()
                    else:
                        trow = c.execute("SELECT id FROM files WHERE path=?", (target,)).fetchone()
                    tid = trow[0] if trow else None
                c.execute("UPDATE imports SET resolved_file_id=? WHERE id=?", (tid, im["id"]))
