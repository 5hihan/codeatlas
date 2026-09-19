"""Syntax-aware, symbol-level editing.

Every edit re-parses the file first, so spans come from the *current* syntax tree (never from stale
line numbers), and only the target declaration's byte span is touched: a neighbour sharing the same
line survives. Before writing, the new text is parsed and refused if it introduces syntax errors
(unless ``force``). Callers may pass ``expected_hash`` (returned by previous calls / ``symbol``) to
guard against concurrent modification. Rename replaces identifier nodes only (never strings or
comments) and only where the reference resolves to the target symbol.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

from .graph import Atlas
from .indexer import SPECS, SymbolRec, count_errors, extract
from .languages import get_parser, lang_for_path


def _hash(b: bytes) -> str:
    return hashlib.blake2b(b, digest_size=8).hexdigest()


def _eol(src: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in src[:5000] else b"\n"


def _line_start(src: bytes, pos: int) -> int:
    return src.rfind(b"\n", 0, pos) + 1


def _line_end(src: bytes, pos: int) -> int:
    i = src.find(b"\n", pos)
    return len(src) if i < 0 else i


def _indent_at(src: bytes, pos: int) -> bytes:
    ls = _line_start(src, pos)
    m = re.match(rb"[ \t]*", src[ls:pos + 1])
    return m.group(0) if m else b""


def _reindent(text: str, indent: bytes, eol: bytes) -> bytes:
    raw = text.strip("\n").splitlines()
    nonblank = [ln for ln in raw if ln.strip()]
    common = min((len(ln) - len(ln.lstrip()) for ln in nonblank), default=0)
    out = [(indent + ln[common:].encode("utf-8")) if ln.strip() else b"" for ln in raw]
    return eol.join(out)


def locate(atlas: Atlas, name: str, path: Optional[str]) -> dict:
    cands = atlas.resolve_symbol(name, path)
    if not cands:
        return {"ok": False, "error": f"symbol {name!r} not found", "similar": atlas.find_symbols(name, limit=6)}
    distinct = {(c["path"], c["qualified"]) for c in cands}
    if len(distinct) > 1 and cands[0]["qualified"] != name and not path:
        alts = "; ".join(f"{c['qualified']} {c['path']}:{c['start_line']}" for c in cands[:6])
        return {"ok": False, "error": f"symbol {name!r} is ambiguous; pass path= or a qualified name. Candidates: {alts}"}
    return {"ok": True, "symbol": cands[0]}


def _fresh_symbol(atlas: Atlas, sym: dict) -> tuple[Optional[SymbolRec], bytes, str]:
    """Re-parse the file and find the symbol's current node (by qualified name, nearest to the indexed line)."""
    rel = sym["path"]
    src = atlas.ix.read_source(rel)
    if src is None:
        return None, b"", rel
    lang = lang_for_path(rel)
    ex = extract(SPECS[lang], src)
    same = [s for s in ex.symbols if s.qualified == sym["qualified"]]
    if not same:
        same = [s for s in ex.symbols if s.name == sym["name"] and s.kind == sym["kind"]]
    if not same:
        return None, src, rel
    same.sort(key=lambda s: abs(s.start_line - sym["start_line"]))
    return same[0], src, rel


def _validate(rel: str, new_src: bytes, old_errors: int) -> Optional[str]:
    lang = lang_for_path(rel)
    tree = get_parser(lang).parse(new_src)
    n = count_errors(tree.root_node) if tree.root_node.has_error else 0
    if n > old_errors:
        # find the first error location for the message
        stack = [tree.root_node]
        where = "?"
        while stack:
            cur = stack.pop()
            if cur.type == "ERROR" or cur.is_missing:
                where = f"line {cur.start_point[0] + 1}"
                break
            if cur.has_error:
                stack.extend(reversed(cur.children))
        return f"new text introduces {n - old_errors} syntax error(s) (first near {where}); not written. Use force=True to override."
    return None


def replace_symbol(atlas: Atlas, name: str, new_text: str, path: Optional[str] = None, dry_run: bool = False,
                   expected_hash: Optional[str] = None, force: bool = False) -> dict:
    loc = locate(atlas, name, path)
    if not loc["ok"]:
        return loc
    rec, src, rel = _fresh_symbol(atlas, loc["symbol"])
    if rec is None:
        return {"ok": False, "error": f"{loc['symbol']['qualified']} no longer found in {rel} (file changed?); re-run symbol"}
    a, b = rec.span_start, rec.span_end
    old = src[a:b]
    h = _hash(old)
    if expected_hash and expected_hash != h:
        return {"ok": False, "error": f"content changed since you looked (expected {expected_hash}, now {h}); re-read with symbol_source"}
    eol = _eol(src)
    indent = _indent_at(src, a)
    new = _reindent(new_text, indent, eol)
    new_src = src[:a] + new + src[b:]
    ex_old_errors = extract(SPECS[lang_for_path(rel)], src).error_count
    err = _validate(rel, new_src, ex_old_errors)
    result = {"ok": True, "path": rel, "symbol": rec.qualified, "replaced_lines": f"{rec.start_line}-{rec.end_line}",
              "replaced_bytes": f"{a}-{b}", "old_hash": h, "new_hash": _hash(new),
              "old_text": old.decode("utf-8", "replace") if len(old) < 6000 else old[:6000].decode("utf-8", "replace") + "\n...(truncated)"}
    if err and not force:
        result.update(ok=False, error=err, preview=new.decode("utf-8", "replace")[:2000])
        return result
    if err:
        result["warning"] = err
    if dry_run:
        result["dry_run"] = True
        result["preview"] = new.decode("utf-8", "replace")
        return result
    atlas.ix.abs(rel).write_bytes(new_src)
    result["reindexed"] = atlas.ix.update(paths=[rel]).as_dict()
    o = atlas.outline(rel)
    result["symbols_in_file_now"] = len(o["symbols"]) if o else 0
    return result


def insert_relative(atlas: Atlas, name: str, new_text: str, where: str = "after", path: Optional[str] = None,
                    dry_run: bool = False, force: bool = False) -> dict:
    loc = locate(atlas, name, path)
    if not loc["ok"]:
        return loc
    rec, src, rel = _fresh_symbol(atlas, loc["symbol"])
    if rec is None:
        return {"ok": False, "error": f"{loc['symbol']['qualified']} no longer found in {rel}"}
    eol = _eol(src)
    indent = _indent_at(src, rec.span_start)
    block = _reindent(new_text, indent, eol)
    if where == "before":
        at = _line_start(src, rec.span_start)
        new_src = src[:at] + block + eol + eol + src[at:]
        line = rec.start_line
    else:
        at = _line_end(src, rec.span_end)
        new_src = src[:at] + eol + eol + block + src[at:]
        line = src.count(b"\n", 0, at) + 3
    old_errors = extract(SPECS[lang_for_path(rel)], src).error_count
    err = _validate(rel, new_src, old_errors)
    result = {"ok": True, "path": rel, "inserted_at_line": line, "relative_to": rec.qualified, "where": where}
    if err and not force:
        result.update(ok=False, error=err, preview=block.decode("utf-8", "replace")[:2000])
        return result
    if dry_run:
        result["dry_run"] = True
        result["preview"] = block.decode("utf-8", "replace")
        return result
    atlas.ix.abs(rel).write_bytes(new_src)
    result["reindexed"] = atlas.ix.update(paths=[rel]).as_dict()
    return result


def rename_symbol(atlas: Atlas, name: str, new_name: str, path: Optional[str] = None, dry_run: bool = True,
                  include_tests: bool = True) -> dict:
    """Rename a symbol at its definition(s) and at every reference that resolves to it.

    Only identifier nodes are touched (found via the reference index + fresh parses); strings, comments
    and same-named symbols elsewhere are left alone. References that resolve ambiguously are reported
    and skipped.
    """
    if not re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", new_name):
        return {"ok": False, "error": "new_name must be an identifier"}
    loc = locate(atlas, name, path)
    if not loc["ok"]:
        return loc
    sym = loc["symbol"]
    bare = sym["name"]
    c = atlas.conn
    res = atlas.resolver()
    # 1. references that resolve to this symbol, grouped by file
    edits: dict[str, list[tuple[int, int, str]]] = {}  # rel -> [(line, col, kind)]
    skipped: list[str] = []
    rows = c.execute("SELECT r.line, r.col, r.kind, r.qualifier, r.enclosing_id, f.id AS fid, f.path FROM refs r "
                     "JOIN files f ON f.id=r.file_id WHERE r.name=?", (bare,)).fetchall()
    for r in rows:
        if not include_tests and re.search(r"(^|/)(tests?|__tests__)/|\.(test|spec)\.", r["path"]):
            continue
        rs = res.resolve(bare, r["fid"], r["qualifier"], r["enclosing_id"], r["kind"])
        if sym["id"] not in rs.targets:
            continue
        if len(rs.targets) > 1 or rs.confidence == "low":
            skipped.append(f"{r['path']}:{r['line']} ({rs.how}, {len(rs.targets)} candidates)")
            continue
        edits.setdefault(r["path"], []).append((r["line"], r["col"], r["kind"]))
    # 2. the definition itself (name node) - plus other definitions in same class hierarchy are NOT touched
    edits.setdefault(sym["path"], []).append((sym["start_line"], -1, "def"))
    plan = []
    total = 0
    written = []
    for rel, locs in sorted(edits.items()):
        src = atlas.ix.read_source(rel)
        if src is None:
            continue
        lang = lang_for_path(rel)
        ex = extract(SPECS[lang], src)
        # identifier nodes in this file named `bare`, by (line, col)
        ident_at: dict[tuple[int, int], tuple[int, int]] = {}
        stack = [ex.tree.root_node]
        from .languages import IDENT_TYPES
        while stack:
            n = stack.pop()
            if n.type in IDENT_TYPES and src[n.start_byte:n.end_byte] == bare.encode():
                ident_at[(n.start_point[0] + 1, n.start_point[1])] = (n.start_byte, n.end_byte)
            stack.extend(n.children)
        spans: set[tuple[int, int]] = set()
        for line, col, kind in locs:
            if kind == "def":
                for s in ex.symbols:
                    if s.qualified == sym["qualified"] and abs(s.start_line - sym["start_line"]) <= 3:
                        spans.add((s.name_start, s.name_end))
            elif kind == "import":
                # import refs are recorded at the statement start; rename identifiers on that line only
                for (ln, cl), sp in ident_at.items():
                    if ln == line:
                        spans.add(sp)
            else:
                sp = ident_at.get((line, col))
                if sp:
                    spans.add(sp)
                else:
                    # tolerate small drift: same line
                    for (ln, cl), sp2 in ident_at.items():
                        if ln == line:
                            spans.add(sp2)
                            break
        if not spans:
            continue
        plan.append({"path": rel, "occurrences": len(spans)})
        total += len(spans)
        if not dry_run:
            out = bytearray(src)
            for a, b in sorted(spans, reverse=True):
                out[a:b] = new_name.encode("utf-8")
            new_src = bytes(out)
            err = _validate(rel, new_src, ex.error_count)
            if err:
                plan[-1]["error"] = err
                continue
            atlas.ix.abs(rel).write_bytes(new_src)
            written.append(rel)
    result = {"ok": True, "renamed": f"{bare} -> {new_name}", "symbol": sym["qualified"], "files": plan,
              "occurrences": total, "skipped_ambiguous": skipped[:20], "dry_run": dry_run}
    if dry_run:
        result["hint"] = "call again with dry_run=False to apply"
    elif written:
        result["reindexed"] = atlas.ix.update(paths=written).as_dict()
    return result
