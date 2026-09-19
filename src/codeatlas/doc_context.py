"""Bounded, warm documentation/configuration snippets for task context.

Inventory is rebuilt after the index's next scan or version change. Known paths
are stat-checked on every query, so ordinary edits are visible immediately without
rereading unchanged content. Deliberately preserving every stat field is outside
this cache's freshness guarantee. Content and per-query evidence use separate LRU
caches with a combined eight MiB payload limit; evicted queries may require reads.
"""
from __future__ import annotations

import os
import re
import stat
import sys
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .indexer import Index

MAX_CANDIDATES = 1500
MAX_DIRECTORIES = 5000
MAX_FILE_BYTES = 200_000
CONTENT_CACHE_BYTES = 4 * 1024 * 1024
QUERY_CACHE_BYTES = 4 * 1024 * 1024
MAX_QUERIES = 16
DOC_EXTENSIONS = frozenset({".md", ".rst", ".txt", ".json", ".jsonc", ".yaml", ".yml", ".toml",
                            ".ini", ".cfg", ".conf", ".sql", ".graphql", ".gql", ".proto"})
_LOCK_NAMES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
                         "uv.lock", "poetry.lock", "cargo.lock", "gemfile.lock", "composer.lock"})


def _eligible(path: Path) -> bool:
    name = path.name.lower()
    return (path.suffix.lower() in DOC_EXTENSIONS and name not in _LOCK_NAMES
            and not name.endswith(".lock") and name != ".env" and not name.startswith(".env."))


def _reparse(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _safe_stat(root: Path, path: Path) -> tuple | None:
    try:
        # Cached directory entries can be replaced by links between queries.
        path.resolve().relative_to(root)
        st = path.lstat()
        if _reparse(st) or not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ctime_ns, st.st_ino, st.st_dev)
    except (OSError, ValueError, RuntimeError):
        return None


def _scope_path(ix: Index, scope: str) -> tuple[Path, str, bool]:
    root = ix.repo.resolve()
    raw = str(scope or "").strip().replace("\\", "/")
    if "\x00" in raw:
        raise ValueError("scope must stay within the repository")
    path = Path(raw) if Path(raw).is_absolute() else root / raw
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(root).as_posix()
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError("scope must stay within the repository") from exc
    if relative == ".":
        relative = ""
    is_file = resolved.is_file()
    # File-scoped source queries still need nearby documentation; an explicit
    # documentation/config file means precisely that file.
    if is_file and not _eligible(resolved):
        resolved = resolved.parent
        relative = resolved.relative_to(root).as_posix()
        relative = "" if relative == "." else relative
        is_file = False
    return resolved, relative, is_file


def _inventory(ix: Index, start: Path, scope: str, is_file: bool) -> tuple[list[str], bool, int]:
    from .relationships import _Ignore

    root = ix.repo.resolve()
    ignore = _Ignore(root)
    paths: list[str] = []
    directories = 0
    if is_file:
        if _eligible(start) and not ignore.ignored(scope) and _safe_stat(root, start) is not None:
            paths.append(scope)
        return paths, False, 0
    if not start.is_dir() or (scope and ignore.ignored(scope, directory=True)):
        return paths, False, 0
    # A caller cannot opt an ignored/symlink directory back in by selecting it.
    probe = start
    while probe != root:
        try:
            if _reparse(probe.lstat()):
                return paths, False, 0
        except OSError:
            return paths, False, 0
        probe = probe.parent
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        directories += 1
        if directories > MAX_DIRECTORIES:
            return paths, True, MAX_DIRECTORIES
        directory = Path(dirpath)
        kept = []
        for name in sorted(dirnames):
            child = directory / name
            relative = child.relative_to(root).as_posix()
            try:
                if _reparse(child.lstat()) or ignore.ignored(relative, directory=True):
                    continue
            except OSError:
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = directory / name
            if not _eligible(path):
                continue
            rel = path.relative_to(root).as_posix()
            if ignore.ignored(rel) or _safe_stat(root, path) is None:
                continue
            paths.append(rel)
            if len(paths) >= MAX_CANDIDATES:
                # Return exits the entire walk, not just this directory's loop.
                return paths, True, directories
    return paths, False, directories


def _new_cache() -> dict:
    return {"inventory_key": None, "paths": [], "truncated": False, "directories": 0,
            "contents": OrderedDict(), "content_bytes": 0,
            "queries": OrderedDict(), "query_bytes": 0}


def _drop_content(cache: dict, rel: str) -> None:
    previous = cache["contents"].pop(rel, None)
    if previous is not None:
        cache["content_bytes"] -= previous[2]


def _content(cache: dict, root: Path, rel: str, signature: tuple) -> str | None:
    contents = cache["contents"]
    previous = contents.get(rel)
    if previous is not None and previous[0] == signature:
        contents.move_to_end(rel)
        return previous[1]
    _drop_content(cache, rel)
    try:
        # A bound on the read also protects against growth after the stat check.
        with (root / rel).open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
        return None
    if _safe_stat(root, root / rel) != signature:
        # Avoid caching a mixed observation while a writer is changing the file.
        return None
    text = raw.decode("utf-8", "replace")
    cost = sys.getsizeof(text) + sys.getsizeof(rel) + sys.getsizeof(signature) + 256
    if cost <= CONTENT_CACHE_BYTES:
        while contents and cache["content_bytes"] + cost > CONTENT_CACHE_BYTES:
            _, (_, _, previous_cost) = contents.popitem(last=False)
            cache["content_bytes"] -= previous_cost
        contents[rel] = (signature, text, cost)
        cache["content_bytes"] += cost
    return text


def _record_size(rel: str, record: tuple) -> int:
    signature, _, snippets = record
    return (sys.getsizeof(rel) + sys.getsizeof(record) + sys.getsizeof(signature)
            + sum(sys.getsizeof(value) for value in signature) + sys.getsizeof(snippets)
            + sum(sys.getsizeof(s) for s in snippets) + 192)


def docs_mentioning(ix: Index, terms: list[str], scope: str = "", limit: int = 6) -> list[dict]:
    """Return line-numbered matches with literal scope boundaries and bounded caches.

    Inventory discovery runs once per scope/scan/version. Warm calls still stat
    known files so edits/deletions are detected. Repeated queries reuse per-file
    evidence even when the underlying content has been evicted from the text LRU.
    """
    words = tuple(sorted({str(term).strip().casefold()[:120] for term in terms if str(term).strip()}))[:32]
    limit = max(0, min(int(limit), 50))
    if not words or not limit:
        return []
    with ix.lock:
        root = ix.repo.resolve()
        start, normalized, is_file = _scope_path(ix, scope)
        version = (ix.last_scan, ix.get_meta("last_update"), ix.get_meta("relationships_updated"))
        inventory_key = (version, normalized, is_file)
        cache = getattr(ix, "_doc_context_cache", None)
        if cache is None:
            cache = _new_cache()
            ix._doc_context_cache = cache
        if cache["inventory_key"] != inventory_key:
            cache["paths"], cache["truncated"], cache["directories"] = _inventory(ix, start, normalized, is_file)
            cache["inventory_key"] = inventory_key
        query_key = (normalized, is_file, words)
        previous = cache["queries"].pop(query_key, None)
        old_records = previous[0] if previous else {}
        if previous is not None:
            cache["query_bytes"] -= previous[1]
        rx = re.compile("|".join(re.escape(word) for word in words), re.I)
        records: dict[str, tuple] = {}
        matches = []
        for rel in cache["paths"]:
            signature = _safe_stat(root, root / rel)
            if signature is None:
                _drop_content(cache, rel)
                continue
            old = old_records.get(rel)
            if old is not None and old[0] == signature:
                record = old
            else:
                text = _content(cache, root, rel, signature)
                if text is None:
                    continue
                hits = 0
                snippets = []
                for line_number, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        hits += 1
                        if len(snippets) < 3:
                            snippets.append(f"{line_number}: {line.strip()[:120]}")
                record = (signature, hits, tuple(snippets))
            records[rel] = record
            if record[1]:
                matches.append({"path": rel, "hits": record[1], "snippets": list(record[2])})
        size = sys.getsizeof(records) + sys.getsizeof(query_key) + sum(_record_size(rel, record) for rel, record in records.items())
        if size <= QUERY_CACHE_BYTES:
            queries = cache["queries"]
            while queries and (cache["query_bytes"] + size > QUERY_CACHE_BYTES or len(queries) >= MAX_QUERIES):
                _, (_, old_size) = queries.popitem(last=False)
                cache["query_bytes"] -= old_size
            queries[query_key] = (records, size)
            cache["query_bytes"] += size
        matches.sort(key=lambda item: (-item["hits"], item["path"]))
        return matches[:limit]


def coverage(ix: Index) -> dict:
    """Diagnostics for callers that want to disclose bounded discovery coverage."""
    cache = getattr(ix, "_doc_context_cache", None)
    return {"candidate_files": len(cache["paths"]) if cache else 0,
            "inventory_truncated": cache["truncated"] if cache else False,
            "directories_visited": cache["directories"] if cache else 0,
            "content_cache_bytes": cache["content_bytes"] if cache else 0,
            "query_cache_bytes": cache["query_bytes"] if cache else 0,
            "max_candidates": MAX_CANDIDATES, "max_file_bytes": MAX_FILE_BYTES}
