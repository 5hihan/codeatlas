"""Compact, token-efficient text renderings of query results for LLM agents.

Every renderer returns a short plain-text block.  Dense line-oriented output
beats JSON for agents: fewer tokens, easier to scan, and file:line anchors
are directly usable with the agent's own editing tools.
"""
from __future__ import annotations

import time
from typing import Iterable, Optional

KIND_ICON = {"class": "C", "struct": "S", "interface": "I", "trait": "T", "enum": "E", "function": "f",
             "method": "m", "type": "t", "module": "M", "object": "O", "record": "R", "impl": "impl",
             "constant": "k", "macro": "#", "property": "p", "variable": "v"}


def _sig(s: dict, width: int = 110) -> str:
    t = (s.get("signature") or s.get("name") or "").replace("\n", " ")
    return t if len(t) <= width else t[: width - 3] + "..."


def _pattern_hints(result: dict) -> list[str]:
    items = result.get("items", [])
    if not items:
        return []
    lines = ["Patterns in context:"]
    # One explanation per concept; more occurrences remain available by pagination.
    seen = set()
    for item in items:
        key = item["pattern"]
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {item['title']} :{item['line']} — {item['explanation']}")
    if result.get("truncated"):
        lines.append("  More occurrences: programming_patterns(path=..., offset=...)")
    return lines


def _age(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    if d < 60:
        return f"{int(d)}s ago"
    if d < 3600:
        return f"{int(d / 60)}m ago"
    return f"{d / 3600:.1f}h ago"


def overview(o: dict) -> str:
    L = [f"Repo: {o['repo']}",
         f"{o['files']} files, {o['lines']:,} lines, {o['symbols']:,} symbols  (index updated {_age(o.get('last_update'))})",
         "Languages: " + ", ".join(f"{x['lang']} {x['files']}f/{x['lines']:,}L" for x in o["languages"]),
         "Symbols: " + ", ".join(f"{x['kind']} {x['count']}" for x in o["kinds"]),
         "", "Top directories (files / lines):"]
    L += [f"  {d['dir']:<40} {d['files']:>5} / {d['lines']:>8,}" for d in o["top_dirs"][:15]]
    if o.get("packages") and len(o["packages"]) > 1:
        L += ["", "Packages / services (nearest manifest):"]
        L += [f"  {p['project']:<40} {p['files']:>5} files  {p['lines']:>8,} lines  {p['langs']}" for p in o["packages"][:15]]
    if o["hubs"]:
        L += ["", "Hub files (imported by N files):"]
        L += [f"  {h['imported_by']:>3}  {h['path']}" for h in o["hubs"]]
    if o["entry_points"]:
        L += ["", "Entry-point candidates:"]
        L += [f"  {e['symbol']} ({e['kind']})  {e['path']}:{e['line']}" for e in o["entry_points"][:10]]
    if o.get("key_symbols"):
        L += ["", "Key symbols (PageRank, in-degree):"]
        L += [f"  {m['name']} ({m['in_deg']})  {m['path']}" for m in o["key_symbols"][:12]]
    if o["findings"]:
        L += ["", "Findings: " + ", ".join(f"{f['code']} {f['count']}" for f in o["findings"])]
    L += ["", "Next: task_context <task>, repo_map, file_outline <path>, symbol <name>, next_snipe."]
    return "\n".join(L)


def outline(o: dict, show_imports: bool = True) -> str:
    L = [f"{o['path']}  [{o['lang']}, {o['lines']} lines, {len(o['symbols'])} symbols]"]
    by_parent: dict = {}
    for s in o["symbols"]:
        by_parent.setdefault(s["parent_id"], []).append(s)

    def emit(parent, depth):
        kids = by_parent.get(parent, [])
        for i, s in enumerate(kids):
            last = i == len(kids) - 1
            pre = ("  " * depth + ("└─ " if last else "├─ ")) if depth else ""
            icon = KIND_ICON.get(s["kind"], s["kind"][:1])
            flags = ""
            if s.get("ref_files"):
                flags += f" ←{s['ref_files']}f"
            if s.get("complexity", 0) >= 15:
                flags += f" cx{s['complexity']}"
            if not s.get("exported", 1):
                flags += " (private)"
            dec = (" " + " ".join(s["decorators"][:2])) if s.get("decorators") else ""
            tags = (" {" + ",".join(s["pattern_tags"][:4]) + "}") if s.get("pattern_tags") else ""
            L.append(f"{pre}{icon} {_sig(s)}  :{s['start_line']}-{s['end_line']}{flags}{tags}{dec}")
            if s.get("doc"):
                L.append(f"{'  ' * (depth + 1)}   \"{s['doc'][:120]}\"")
            emit(s["id"], depth + 1)

    emit(None, 0)
    if show_imports and o["imports"]:
        internal = [i for i in o["imports"] if i.get("resolved")]
        external = [i for i in o["imports"] if not i.get("resolved")]
        if internal:
            L.append("imports (repo): " + ", ".join(
                f"{i['resolved']}" + (f"[{','.join(i['names'][:4])}]" if i["names"] else "") for i in internal[:25]))
        if external:
            L.append("imports (external): " + ", ".join(i["module"] for i in external[:25]))
    if o.get("imported_by"):
        L.append(f"imported by ({len(o['imported_by'])}): " + ", ".join(o["imported_by"][:15]) +
                 (" ..." if len(o["imported_by"]) > 15 else ""))
    if o.get("findings"):
        L.append("findings: " + "; ".join(f"{f['code']}@{f['line']} {f['message'][:60]}" for f in o["findings"][:8]))
    L += _pattern_hints(o.get("patterns", {}))
    rels = o.get("relationships", {})
    if rels.get("items"):
        L.append("Config/schema relationships (source consumes target):")
        L += [f"  {r['source_path']}:{r['line']} --{r['kind']}--> {r['target_path']}"
              + (" (unresolved)" if not r['resolved'] else "") for r in rels["items"]]
        if rels.get("truncated"):
            L.append("  More: relationships(path=...)")
    return "\n".join(L)


def symbol_list(rows: Iterable[dict], header: str = "") -> str:
    L = [header] if header else []
    for s in rows:
        icon = KIND_ICON.get(s["kind"], s["kind"][:1])
        extra = f" ←{s['ref_files']}f" if s.get("ref_files") is not None else ""
        L.append(f"{icon} {s.get('qualified') or s['name']}  {s['path']}:{s['start_line']}-{s.get('end_line', s['start_line'])}{extra}")
        sig = _sig(s, 100)
        if sig and sig != s.get("qualified"):
            L.append(f"    {sig}")
    return "\n".join(L) if L else "(no matches)"


def symbol_detail(d: dict) -> str:
    if not d.get("found"):
        return "Symbol not found." + ("\nSimilar:\n" + symbol_list(d["similar"]) if d.get("similar") else "")
    L = [f"{KIND_ICON.get(d['kind'], '?')} {d['qualified']}  {d['path']}:{d['start_line']}-{d['end_line']}  "
         f"({d['lines']} lines, complexity {d['complexity']}, {'exported' if d['exported'] else 'private'}, "
         f"referenced from {d['ref_files']} files)",
         f"  {_sig(d, 200)}"]
    if d.get("decorators"):
        L.append("  decorators: " + " ".join(d["decorators"][:5]))
    if d.get("pattern_tags"):
        L.append("  patterns: " + ", ".join(d["pattern_tags"]) + "  (programming_patterns for evidence)")
    if d.get("doc"):
        L.append(f"  doc: {d['doc'][:300]}")
    if d.get("ambiguous"):
        L.append("  other definitions with this name: " + "; ".join(
            f"{a['qualified']} {a['path']}:{a['line']}" for a in d["ambiguous"]))
    if d.get("children"):
        L.append(f"  members ({len(d['children'])}):")
        L += [f"    {KIND_ICON.get(c['kind'], '?')} {_sig(c, 90)}  :{c['start_line']}" for c in d["children"][:40]]
    if d.get("callers"):
        L.append(f"  called by ({len(d['callers'])}):")
        L += [f"    {c['qualified']}  {c['path']}:{c['line']}" + (f"  [{c['via']}]" if c['via'] != 'call' else "")
              + ("  (medium confidence)" if c.get("confidence") == "medium" else "")
              for c in d["callers"][:25]]
    else:
        L.append("  called by: (no internal callers found - entry point, exported API, dynamic dispatch, or dead code)")
    if d.get("callees"):
        parts = []
        for c in d["callees"][:30]:
            t = c.get("target")
            if t and t.get("path"):
                parts.append(f"{c['name']}→{t['path']}:{t['line']}")
            elif t:
                parts.append(f"{(c['qualifier'] + '.') if c.get('qualifier') else ''}{c['name']}?{t['resolution']}")
            else:
                parts.append(f"{(c['qualifier'] + '.') if c.get('qualifier') else ''}{c['name']}")
        L.append("  calls: " + ", ".join(parts))
    L += _pattern_hints(d.get("patterns", {}))
    return "\n".join(L)


def references(r: dict) -> str:
    L = [f"References to `{r['name']}`: {r['total']} total, showing {r.get('shown', r['total'])} in {r['files']} files  " +
         ", ".join(f"{k}:{v}" for k, v in r["kinds"].items()) +
         (f"  (truncated; continue with offset={r['next_offset']})" if r.get("truncated") else "")]
    if r.get("resolved_to"):
        L.append(f"resolved to {r['resolved_to']}" + (f"; {r['elsewhere']} same-named uses resolve elsewhere (other "
                                                      f"definitions/projects/builtins) and are not listed" if r.get("elsewhere") else ""))
    if r["definitions"]:
        L.append("defined: " + "; ".join(f"{d['qualified']} ({d['kind']}) {d['path']}:{d['line']}" for d in r["definitions"]))
    for f in r["by_file"]:
        items = []
        for x in f["refs"]:
            tag = "" if x["kind"] in ("call", "ref") else f"/{x['kind']}"
            enc = f"@{x['enclosing']}" if x.get("enclosing") else ""
            items.append(f"{x['line']}{tag}{enc}")
        L.append(f"  {f['path']}: " + ", ".join(items[:30]) + (" ..." if len(items) > 30 else ""))
    return "\n".join(L)


def impact(d: dict) -> str:
    if not d.get("found"):
        return "Symbol not found." + ("\nSimilar:\n" + symbol_list(d["similar"]) if d.get("similar") else "")
    r = d["root"]
    L = [f"Impact of changing {r['qualified']} ({r['path']}:{r['start_line']}): "
         f"{d['affected_symbols']} symbols in {d['affected_files']} files within {d['depth']} hops"]
    for lvl, items in d["levels"].items():
        L.append(f"  depth {lvl} ({len(items)}):")
        L += [f"    {i['qualified']}  {i['path']}:{i['line']}" for i in items[:25]]
        if len(items) > 25:
            L.append(f"    ... {len(items) - 25} more")
    if d.get("top_level_sites"):
        L.append(f"  module-level / callback call sites ({len(d['top_level_sites'])}): " +
                 ", ".join(f"{t['path']}:{t['line']}" for t in d["top_level_sites"][:12]))
    if d["tests"]:
        L.append("  tests touching this: " + ", ".join(d["tests"][:10]))
    else:
        L.append("  tests touching this: none found")
    if d.get("truncated"):
        L.append("  (traversal capped at 60 callers per symbol: some callers were not followed; query impact of the listed symbols)")
    elif d.get("deeper"):
        L.append(f"  (callers exist beyond depth {d['depth']}; raise depth to follow them)")
    return "\n".join(L)


def triggers(d: dict) -> str:
    if not d.get("found"):
        return "Symbol not found." + ("\nSimilar:\n" + symbol_list(d["similar"]) if d.get("similar") else "")
    s = d["symbol"]
    L = [f"What triggers {s['qualified']} ({s['path']}:{s['start_line']}):"]
    if d["trigger_decorators"]:
        L.append("  framework hooks/decorators: " + " ".join(d["trigger_decorators"]))
    elif d["decorators"]:
        L.append("  decorators: " + " ".join(d["decorators"]))
    if d["registrations"]:
        L.append("  registered as callback/argument:")
        L += [f"    {'⚡ ' if r['event_like'] else ''}{r['via'] or '?'}(...)  {r['path']}:{r['line']}"
              + (f"  in {r['enclosing']}" if r.get("enclosing") else "") for r in d["registrations"][:20]]
    if d["callers"]:
        L.append(f"  direct callers ({len(d['callers'])}):")
        L += [f"    {c['qualified']}  {c['path']}:{c['line']}" for c in d["callers"][:20]]
    if d.get("parent"):
        p = d["parent"]
        L.append(f"  member of {p['qualified']} ({p['kind']}, {p['callers']} callers"
                 + (", " + " ".join(p["decorators"][:3]) if p["decorators"] else "") + ")")
    if d["entry_point"]:
        L.append("  looks like an entry point (name)")
    if d["exported_no_internal_callers"]:
        L.append("  exported with no internal callers: public API, framework-invoked, or unused")
    if not (d["registrations"] or d["callers"] or d["trigger_decorators"]):
        L.append("  no static trigger found: check dynamic dispatch, reflection, config/route tables, or grep its name")
    return "\n".join(L)


def dependencies(d: dict) -> str:
    L = [f"Dependencies of {d['path']}:"]
    if d["imports_internal"]:
        L.append(f"  imports (repo, {len(d['imports_internal'])}):")
        L += [f"    {i['path']}" + (f"  [{', '.join(i['names'][:6])}]" if i["names"] else "") for i in d["imports_internal"]]
    if d["imports_external"]:
        L.append("  imports (external): " + ", ".join(d["imports_external"][:30]))
    L.append(f"  imported by ({len(d['imported_by'])}): " + (", ".join(d["imported_by"][:30]) or "nobody"))
    if d["imported_by_transitive"]:
        L.append(f"  transitively depends on it ({len(d['imported_by_transitive'])}): " +
                 ", ".join(d["imported_by_transitive"][:20]) + (" ..." if len(d["imported_by_transitive"]) > 20 else ""))
    return "\n".join(L)


def hotspots(h: dict) -> str:
    L = ["Most complex functions (cyclomatic):"]
    L += [f"  cx{x['complexity']:<4} {x['lines']:>5}L  {x['qualified']}  {x['path']}:{x['start_line']}" for x in h["most_complex"]]
    L.append("Longest functions:")
    L += [f"  {x['lines']:>5}L cx{x['complexity']:<4} {x['qualified']}  {x['path']}:{x['start_line']}" for x in h["longest"]]
    L.append("Largest files:")
    L += [f"  {x['lines']:>6}L {x['symbols']:>4}sym  {x['path']}" for x in h["largest_files"]]
    L.append("Key symbols (PageRank):")
    L += [f"  ←{x['in_deg']:<4} {x['qualified']}  {x['path']}:{x['start_line']}" for x in h["key_symbols"]]
    if h["cycles"]:
        L.append("Import cycles:")
        L += ["  " + " -> ".join(c) for c in h["cycles"]]
    return "\n".join(L)


def module_graph(g: dict, limit: int = 60) -> str:
    L = ["Modules (files): " + ", ".join(f"{n['id']}({n['files']})" for n in g["nodes"][:40])]
    L.append("Edges (imports, count):")
    L += [f"  {e['from']} -> {e['to']}  x{e['count']}" for e in g["edges"][:limit]]
    return "\n".join(L)


def grep(r: dict) -> str:
    if r.get("error"):
        return r["error"]
    L = [f"grep /{r['pattern']}/: {len(r['hits'])} hits in {r['files']} files" + (" (truncated)" if r["truncated"] else "")]
    L += [f"  {h['path']}:{h['line']}: {h['text']}" for h in r["hits"]]
    return "\n".join(L)


def findings(rows: list[dict]) -> str:
    if not rows:
        return "(no findings)"
    return "\n".join(f"  {r['code']:<20} {r['path']}:{r['line']}  {r['message'][:100]}" for r in rows)


def unreferenced(rows: list[dict]) -> str:
    if not rows:
        return "(no unreferenced symbols found)"
    return "\n".join(f"  {KIND_ICON.get(r['kind'], '?')} {r['qualified']}  {r['path']}:{r['start_line']}  ({r['lines']}L"
                     f"{', exported' if r['exported'] else ''})" for r in rows)


def source(d: dict) -> str:
    if not d.get("found"):
        return (d.get("error") or "Not found.") + ("\nSimilar:\n" + symbol_list(d["similar"]) if d.get("similar") else "")
    head = f"{d['label']}" + (f"  (truncated at {d['end']}; file has {d['total_lines']} lines)" if d["truncated"] else "")
    return head + "\n" + d["text"]
