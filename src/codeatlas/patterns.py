"""Source-backed programming lessons, extracted from an existing syntax tree.

These are observations about syntax, not an LLM interpretation or a type checker.
Closure candidates require an actual reference to an outer function binding;
anonymous functions alone are deliberately not called closures. Supported syntax
is listed in the catalogue. Comments, strings and parser error nodes are excluded.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .languages import COMMENT_TYPES, STRING_TYPES, get_parser

if TYPE_CHECKING:
    from .indexer import Index

DETECTOR_VERSION = 1

_CATALOGUE = (
    ("generics", "Generics", "Type parameters let one definition work with several types while preserving type relationships.",
     ("python", "typescript", "tsx", "go", "rust", "java", "csharp", "cpp")),
    ("closures", "Lexical closures", "A function references a binding from its enclosing function. Captured state can outlive the outer call.",
     ("python", "javascript", "typescript", "tsx")),
    ("decorators", "Decorators and annotations", "Metadata or wrappers attached to declarations alter behavior or inform frameworks and tooling; the effect depends on the decorator or annotation.",
     ("python", "javascript", "typescript", "tsx", "java", "csharp", "rust")),
    ("async_await", "Async and await", "Async functions and await expressions support asynchronous work; await is a suspension point rather than proof of parallel execution.",
     ("python", "javascript", "typescript", "tsx", "rust", "csharp")),
    ("generators", "Generators", "Yield-based iteration produces values incrementally, retaining execution state between requests for the next value.",
     ("python", "javascript", "typescript", "tsx", "csharp")),
    ("context_managers", "Scoped resource management", "A scoped resource construct arranges cleanup when control leaves the scope, including exceptional exits.",
     ("python", "java", "csharp")),
    ("pattern_matching", "Pattern matching", "A match expression dispatches on the shape or value of input and may bind parts of the matched value.",
     ("python", "rust", "csharp")),
    ("interfaces_traits", "Interfaces and traits", "A contract describes operations implementations must provide. The language determines structural versus nominal conformance.",
     ("typescript", "tsx", "go", "rust", "java", "csharp", "php")),
    ("inheritance", "Inheritance and conformance", "A declaration relates a type to a base type or interface, allowing behavior reuse or contract conformance.",
     ("python", "javascript", "typescript", "tsx", "java", "csharp", "cpp", "php")),
    ("error_handling", "Exception and error handling", "An explicit handler, propagation operator or raise/throw site changes the error path; inspect the surrounding cleanup and caller contract.",
     ("python", "javascript", "typescript", "tsx", "rust", "java", "csharp", "cpp", "php")),
    ("comprehensions", "Comprehensions", "A comprehension builds or streams a collection by combining iteration, transformation and optional filtering.",
     ("python",)),
    ("goroutines_channels", "Goroutines and channels", "Go starts concurrent work with go statements and exchanges values through channels; buffering and synchronization affect blocking.",
     ("go",)),
)
_CAT = {p[0]: p for p in _CATALOGUE}
_JS = {"javascript", "typescript", "tsx"}
_FUNCTIONS = {
    "function_definition", "function_declaration", "function_expression", "function",
    "arrow_function", "lambda", "method_definition", "generator_function_declaration",
    "generator_function", "method_declaration", "function_item", "func_literal",
    "closure_expression", "lambda_expression", "local_function_statement",
}
_CLASS = {"class_definition", "class_declaration", "interface_declaration", "struct_item", "impl_item"}
_SKIP = COMMENT_TYPES | STRING_TYPES | {"ERROR", "character_literal", "regex", "regex_pattern"}


def catalogue() -> list[dict]:
    """Return the twelve supported concepts and the deliberately bounded coverage."""
    return [{"id": pid, "pattern": pid, "title": title, "explanation": explanation, "description": explanation,
             "languages": list(languages), "detection": "lexical-capture heuristic" if pid == "closures" else "syntax"}
            for pid, title, explanation, languages in _CATALOGUE]


def _text(node: Any, src: bytes, limit: int = 240) -> str:
    raw = src[node.start_byte:min(node.end_byte, node.start_byte + limit * 4)].decode("utf-8", "replace")
    text = re.sub(r"\s+", " ", raw).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _walk(node: Any, *, prune_functions: bool = False):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in _SKIP or current.is_missing:
            continue
        yield current
        if prune_functions and current is not node and current.type in _FUNCTIONS | _CLASS:
            continue
        stack.extend(reversed(current.named_children))


def _identifiers(node: Any) -> set[str]:
    if node is None:
        return set()
    # Identifier.text is the parser's byte slice; no parsing or source rescanning.
    return {n.text.decode("utf-8", "replace") for n in _walk(node)
            if n.type in {"identifier", "shorthand_property_identifier_pattern"}}


def _bound_names(node: Any, lang: str) -> set[str]:
    """Conservative local bindings, excluding nested-function bodies and types."""
    names: set[str] = set()
    params = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
    if params is not None:
        for p in params.named_children if params.type not in {"identifier"} else [params]:
            target = p.child_by_field_name("pattern") or p.child_by_field_name("name")
            if target is None and p.type in {"typed_parameter", "default_parameter", "typed_default_parameter"}:
                target = p.named_children[0] if p.named_children else None
            if target is None and p.type in {"identifier", "list_splat_pattern", "dictionary_splat_pattern", "rest_pattern", "object_pattern", "array_pattern", "tuple_pattern"}:
                target = p
            names.update(_identifiers(target))
    body = node.child_by_field_name("body")
    if body is None:
        return names
    for current in _walk(body, prune_functions=True):
        target = None
        if current.type in _FUNCTIONS or current.type in _CLASS:
            target = current.child_by_field_name("name")
        elif lang == "python" and current.type in {"assignment", "augmented_assignment", "for_statement", "for_in_clause", "named_expression"}:
            target = current.child_by_field_name("left") or current.child_by_field_name("name")
        elif lang in _JS and current.type == "variable_declarator":
            target = current.child_by_field_name("name")
        # Assignment to obj.field does not bind obj in the local scope.
        if target is not None and target.type not in {"attribute", "subscript", "member_expression", "subscript_expression"}:
            names.update(_identifiers(target))
    return names


def _capture_names(node: Any, lang: str, outer: set[str]) -> list[str]:
    if not outer:
        return []
    body = node.child_by_field_name("body")
    if body is None:
        return []
    free = outer - _bound_names(node, lang)
    # A Python global explicitly bypasses enclosing lexical scopes.
    for n in _walk(body, prune_functions=True):
        if n.type == "global_statement":
            free.difference_update(_identifiers(n))
    captured = set()
    for n in _walk(body, prune_functions=True):
        if n.type not in {"identifier", "shorthand_property_identifier"}:
            continue
        name = n.text.decode("utf-8", "replace")
        if name not in free:
            continue
        parent = n.parent
        if parent is not None:
            if parent.type in {"attribute", "member_expression"}:
                field = parent.child_by_field_name("attribute") or parent.child_by_field_name("property")
                if field is not None and field.id == n.id:
                    continue
            if parent.type in {"pair", "pair_pattern"}:
                key = parent.child_by_field_name("key")
                if key is not None and key.id == n.id:
                    continue
            if parent.type in _FUNCTIONS or parent.type in _CLASS:
                continue
        captured.add(name)
    return sorted(captured)


def _syntax_patterns(lang: str, node: Any) -> list[str]:
    """Only grammar node types/keyword tokens; never substring-match source text."""
    kind = node.type
    out: list[str] = []
    if ((lang in {"typescript", "tsx", "rust", "java"} and kind == "type_parameters")
            or (lang in {"go", "csharp"} and kind == "type_parameter_list")
            or (lang == "python" and kind == "type_parameter")
            or (lang == "cpp" and kind == "template_declaration")):
        out.append("generics")
    if ((lang in {"python"} | _JS and kind == "decorator")
            or (lang == "java" and kind in {"annotation", "marker_annotation"})
            or (lang == "csharp" and kind == "attribute_list")
            or (lang == "rust" and kind == "attribute_item")):
        out.append("decorators")
    if lang in {"python", "rust", "csharp"} | _JS:
        if kind in {"await", "await_expression"}:
            out.append("async_await")
        elif kind in _FUNCTIONS:
            # Python/JS anonymous keyword; Rust/C# place it inside a modifier node.
            modifiers = [c for c in node.children if c.type in {"function_modifiers", "modifier"}]
            if any(c.type == "async" for c in node.children) or any(
                    any(c.type == "async" for c in m.children) or m.text == b"async" for m in modifiers):
                out.append("async_await")
    if ((lang == "python" and kind == "yield") or (lang in _JS and kind in {"yield_expression", "generator_function_declaration", "generator_function"})
            or (lang == "csharp" and kind == "yield_statement")):
        out.append("generators")
    if ((lang == "python" and kind == "with_statement") or (lang == "java" and kind == "try_with_resources_statement")
            or (lang == "csharp" and kind == "using_statement")):
        out.append("context_managers")
    if ((lang == "python" and kind == "match_statement") or (lang == "rust" and kind == "match_expression")
            or (lang == "csharp" and kind in {"switch_expression", "is_pattern_expression"})):
        out.append("pattern_matching")
    if ((lang in {"typescript", "tsx", "java", "csharp", "php"} and kind == "interface_declaration")
            or (lang == "go" and kind == "interface_type") or (lang == "rust" and kind == "trait_item")):
        out.append("interfaces_traits")
    if ((lang == "python" and kind == "class_definition" and node.child_by_field_name("superclasses") is not None
         and any(c.type != "keyword_argument" for c in node.child_by_field_name("superclasses").named_children))
            or (lang in _JS and kind == "class_heritage")
            or (lang == "java" and kind in {"superclass", "super_interfaces", "extends_interfaces"})
            or (lang == "csharp" and kind == "base_list")
            or (lang == "cpp" and kind == "base_class_clause")
            or (lang == "php" and kind in {"base_clause", "class_interface_clause"})):
        out.append("inheritance")
    if lang in {"python", "rust", "java", "csharp", "cpp", "php"} | _JS:
        if kind in {"try_statement", "try_with_resources_statement", "try_expression", "raise_statement", "throw_statement", "throw_expression"}:
            out.append("error_handling")
    if lang == "python" and kind in {"list_comprehension", "dictionary_comprehension", "set_comprehension", "generator_expression"}:
        out.append("comprehensions")
    if lang == "go" and (kind in {"go_statement", "channel_type", "send_statement", "receive_statement"}
                         or (kind == "unary_expression" and any(c.type == "<-" for c in node.children))):
        out.append("goroutines_channels")
    return out


def extract_patterns(lang: str, src: bytes, tree: Any = None) -> list[dict]:
    """Extract lessons once per file, reusing ``tree`` without invoking a parser."""
    if isinstance(src, str):
        src = src.encode("utf-8")
    if lang not in {l for p in _CATALOGUE for l in p[3]}:
        return []
    if tree is None:
        tree = get_parser(lang).parse(src)
    results: list[dict] = []
    seen: set[tuple] = set()
    # Each stack item carries the enclosing definition label and lexical bindings.
    stack: list[tuple] = [(tree.root_node, "module", set())]
    while stack:
        node, owner, outer = stack.pop()
        if node.type in _SKIP or node.is_missing:
            continue
        found = _syntax_patterns(lang, node)
        captures: list[str] = []
        if lang in {"python"} | _JS and node.type in _FUNCTIONS:
            captures = _capture_names(node, lang, outer)
            if captures:
                found.append("closures")
        name = node.child_by_field_name("name") if node.type in _FUNCTIONS | _CLASS else None
        if node.type == "decorator" and node.parent is not None and node.parent.type == "decorated_definition":
            declaration = node.parent.child_by_field_name("definition")
            if declaration is not None:
                name = declaration.child_by_field_name("name")
        current_owner = _text(name, src, 80) if name is not None else owner
        for pid in found:
            key = (pid, node.start_byte, node.end_byte)
            if key in seen:
                continue
            seen.add(key)
            explanation = _CAT[pid][2]
            confidence = 1.0
            if pid == "closures":
                explanation = ("References enclosing-function binding(s) " + ", ".join(captures[:8])
                               + ". Lexical capture is inferred from syntax; runtime binding and block-level shadowing are not proven.")
                confidence = 0.85
            results.append({"id": pid, "pattern": pid, "title": _CAT[pid][1],
                            "line": node.start_point[0] + 1, "end_line": node.end_point[0] + 1,
                            "owner": current_owner, "explanation": f"In {current_owner}: {explanation}",
                            "evidence": _text(node, src), "confidence": confidence,
                            "detection": "lexical-capture heuristic" if captures and pid == "closures" else "syntax"})
        next_owner = current_owner
        next_outer = outer
        if node.type in _FUNCTIONS and lang in {"python"} | _JS:
            next_outer = outer | _bound_names(node, lang)
        # Class namespace names are not Python closure bindings. Existing enclosing
        # function bindings still remain visible inside methods of a nested class.
        stack.extend((child, next_owner, next_outer) for child in reversed(node.named_children))
    return sorted(results, key=lambda p: (p["line"], p["pattern"], p["end_line"]))


def ensure_schema(ix: Index) -> None:
    """No commits: pattern writes participate in the indexer's file transaction."""
    ix.conn.execute("""CREATE TABLE IF NOT EXISTS code_patterns(
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        pattern TEXT NOT NULL, line INTEGER NOT NULL, end_line INTEGER NOT NULL,
        owner TEXT NOT NULL, explanation TEXT NOT NULL, evidence TEXT NOT NULL,
        confidence REAL NOT NULL, detection TEXT NOT NULL,
        UNIQUE(file_id, pattern, line, end_line, evidence))""")
    ix.conn.execute("CREATE INDEX IF NOT EXISTS idx_code_patterns_kind ON code_patterns(pattern, file_id, line)")
    ix.conn.execute("CREATE INDEX IF NOT EXISTS idx_code_patterns_file ON code_patterns(file_id, line)")


def store_patterns(ix: Index, fid: int, lang: str, src: bytes, tree: Any = None) -> int:
    """Replace this file's facts, including when the new file has no patterns."""
    ensure_schema(ix)
    found = extract_patterns(lang, src, tree)
    ix.conn.execute("DELETE FROM code_patterns WHERE file_id=?", (fid,))
    ix.conn.executemany("INSERT OR IGNORE INTO code_patterns VALUES(?,?,?,?,?,?,?,?,?)", [
        (fid, p["pattern"], p["line"], p["end_line"], p["owner"], p["explanation"], p["evidence"], p["confidence"], p["detection"])
        for p in found])
    return len(found)


def patterns(ix: Index, path: str | None = None, pattern: str | None = None,
             limit: int = 50, offset: int = 0, start_line: int | None = None,
             end_line: int | None = None) -> dict:
    """Page over stored evidence; ``path`` is an exact file or directory prefix."""
    ensure_schema(ix)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    base = {"patterns": [], "items": [], "total": 0, "offset": offset, "limit": limit, "next_offset": None,
            "truncated": False, "catalogue": catalogue(), "detector_version": DETECTOR_VERSION,
            "notes": ["Syntax evidence, not runtime behavior. Closure capture is heuristic. Detection coverage varies by language."]}
    if pattern is not None and pattern not in _CAT:
        return {**base, "error": f"Unknown pattern {pattern!r}; use a catalogue id."}
    conditions: list[str] = []
    args: list[Any] = []
    if path:
        rel = ix.rel(path).rstrip("/")
        conditions.append("(f.path=? OR substr(f.path,1,?)=?)")
        args.extend((rel, len(rel) + 1, rel + "/"))
    if pattern:
        conditions.append("p.pattern=?")
        args.append(pattern)
    if start_line is not None:
        conditions.append("p.line>=?")
        args.append(max(1, int(start_line)))
    if end_line is not None:
        conditions.append("p.line<=?")
        args.append(max(1, int(end_line)))
    query = " FROM code_patterns p JOIN files f ON f.id=p.file_id"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    total = ix.conn.execute("SELECT COUNT(*)" + query, args).fetchone()[0]
    rows = ix.conn.execute("SELECT p.*, f.path, f.lang" + query
                           + " ORDER BY f.path,p.line,p.pattern,p.end_line,p.evidence LIMIT ? OFFSET ?", (*args, limit, offset))
    items = []
    for row in rows:
        item = dict(row)
        item.pop("file_id", None)
        item.update(id=item["pattern"], title=_CAT[item["pattern"]][1])
        items.append(item)
    next_offset = offset + len(items) if offset + len(items) < total else None
    return {**base, "patterns": items, "items": items, "total": total, "next_offset": next_offset, "truncated": next_offset is not None}


def render(result: dict, token_budget: int = 2000) -> str:
    """Compact source anchors and explanations under the project's token estimate."""
    from .rank import estimate_tokens

    budget = max(0, int(token_budget))
    if not budget:
        return ""
    if result.get("error"):
        header = result["error"]
    else:
        header = f"Programming patterns: {result.get('total', 0)} occurrence(s); syntax evidence, closures heuristic."
    chunks = [header]
    omitted = False
    displayed = 0
    for item in result.get("patterns", []):
        block = (f"{item['title']} [{item['pattern']}] {item.get('path', '?')}:{item['line']}-{item['end_line']}"
                 f" confidence={item['confidence']:g}\n  {item['explanation']}\n  {item['evidence']}")
        # Reserve room for a continuation notice rather than silently truncating.
        if estimate_tokens("\n".join(chunks + [block])) + 24 > budget:
            omitted = True
            break
        chunks.append(block)
        displayed += 1
    if omitted or result.get("truncated"):
        next_offset = result.get("offset", 0) + displayed
        chunks.append(f"More results: offset={next_offset}; narrow path/pattern or increase token_budget.")
    elif not result.get("patterns") and not result.get("error"):
        chunks.append("Catalogue: " + ", ".join(p["id"] for p in result.get("catalogue", catalogue())))
    text = "\n".join(chunks)
    if estimate_tokens(text) > budget:
        # Tiny budgets still obey the same estimate used by repo_map/task_context.
        while text and estimate_tokens(text) > budget:
            text = text[:-1]
    return text
