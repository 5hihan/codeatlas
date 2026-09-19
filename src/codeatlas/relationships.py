"""Persisted, evidence-backed file relationships beyond source imports.

An edge always points from a consumer to a dependency. Extraction recognizes
literal paths only: it does not execute configuration, expand environment
variables, or infer that an arbitrary string is a dependency. Evidence contains
the construct and its source location, never a source line or configuration value.
Queries read SQLite only; callers explicitly synchronize after filesystem changes.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import posixpath
import re
import time
from collections import defaultdict, deque
from pathlib import Path, PureWindowsPath
from typing import Iterable, Optional, TYPE_CHECKING

import pathspec

from .indexer import DEFAULT_IGNORE_DIRS, MAX_FILE_BYTES, project_for

if TYPE_CHECKING:
    from .indexer import Index

SCHEMA = """
CREATE TABLE IF NOT EXISTS ca_artifact_files(
  path TEXT PRIMARY KEY, category TEXT NOT NULL, size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL, content_hash TEXT, indexed_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'indexed');
CREATE TABLE IF NOT EXISTS ca_relationships(
  source_path TEXT NOT NULL, target_path TEXT NOT NULL, kind TEXT NOT NULL,
  line INTEGER NOT NULL, evidence TEXT NOT NULL, confidence TEXT NOT NULL,
  resolved INTEGER NOT NULL,
  PRIMARY KEY(source_path,target_path,kind,line));
CREATE INDEX IF NOT EXISTS ca_relationships_target ON ca_relationships(target_path);
CREATE INDEX IF NOT EXISTS ca_relationships_kind ON ca_relationships(kind);
CREATE TABLE IF NOT EXISTS ca_artifact_entities(
  path TEXT NOT NULL, project TEXT NOT NULL, entity_kind TEXT NOT NULL,
  name TEXT NOT NULL, bare_name TEXT NOT NULL, role TEXT NOT NULL, line INTEGER NOT NULL,
  PRIMARY KEY(path,entity_kind,name,role,line));
CREATE INDEX IF NOT EXISTS ca_entities_lookup ON ca_artifact_entities(project,entity_kind,bare_name,role);
CREATE TABLE IF NOT EXISTS ca_entity_links(
  source_path TEXT NOT NULL, project TEXT NOT NULL, entity_kind TEXT NOT NULL,
  name TEXT NOT NULL, line INTEGER NOT NULL, target_path TEXT NOT NULL,
  target_line INTEGER NOT NULL, status TEXT NOT NULL,
  PRIMARY KEY(source_path,entity_kind,name,line,target_path));
CREATE INDEX IF NOT EXISTS ca_entity_links_target ON ca_entity_links(target_path);
"""
EXTRACTOR_VERSION = "2"

CONFIG_SUFFIXES = frozenset({".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"})
SCHEMA_SUFFIXES = frozenset({".proto", ".graphql", ".gql", ".sql", ".avsc", ".xsd"})
ARTIFACT_SUFFIXES = CONFIG_SUFFIXES | SCHEMA_SUFFIXES
KINDS = frozenset({"loads_config", "uses_schema", "config_ref", "schema_ref", "uses_env", "uses_table"})
_LOCKFILES = frozenset({"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock"})
_SENSITIVE = re.compile(r"(?:password|passwd|secret|credential|private.?key|api.?key|(?:^|[._-])token(?:$|[._-]))", re.I)
_KEY = re.compile(r"(?:^|[._-])(?:file|files|path|paths|schema|config|include|includes|extends|import|imports|ref|main|module|types|entry|input|env_file)$", re.I)
_PATH_KEY = re.compile(r"(?:file|path|schema|config)$", re.I)
_QUOTED = r'''["'](?P<value>[^"'\r\n]{1,500})["']'''
_SOURCE_IMPORT = re.compile(
    r'''\b(?:(?:import|export)\s+[^;\n]{0,200}?\bfrom\s*|import\s*|(?:require|import)\s*\(\s*)''' + _QUOTED)
_SOURCE_LOAD = re.compile(
    r'''\b(?P<call>open|readFile|readFileSync|read_to_string|ReadAllText|loadConfig|loadSchema|load_file|include_str!)\s*\(\s*''' + _QUOTED)
_TOKENS = re.compile(
    r'''"""[\s\S]*?"""|''' + "'''[\\s\\S]*?'''" +
    r'''|/\*[\s\S]*?\*/|//[^\n]*|\#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''')
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def is_artifact_path(path: str | os.PathLike) -> bool:
    """Supported configuration/schema artifact; env files are parsed for keys only."""
    p = Path(path)
    return _env_path(path) or p.suffix.lower() in ARTIFACT_SUFFIXES and p.name.lower() not in _LOCKFILES


def _env_path(path: str | os.PathLike) -> bool:
    name = Path(path).name
    return name == ".env" or name.startswith(".env.")


def _schema_path(path: str) -> bool:
    p = Path(path)
    return p.suffix.lower() in SCHEMA_SUFFIXES or ".schema." in p.name.lower() or p.name.lower() == "schema.json"


class _Ignore:
    """Use the same default exclusions, applying ancestor gitignore precedence."""
    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, pathspec.GitIgnoreSpec] = {}
        self.results: dict[tuple[str, bool], bool] = {}
        self.extra = self._read(root / ".codeatlas" / "ignore")

    @staticmethod
    def _read(path: Path):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        return pathspec.GitIgnoreSpec.from_lines(lines)

    def ignored(self, rel: str, directory: bool = False) -> bool:
        cache_key = (rel, directory)
        if cache_key in self.results:
            return self.results[cache_key]
        result = self._ignored(rel, directory)
        self.results[cache_key] = result
        return result

    def _ignored(self, rel: str, directory: bool) -> bool:
        parts = rel.split("/")
        if any(p in DEFAULT_IGNORE_DIRS for p in (parts if directory else parts[:-1])):
            return True
        # A file cannot resurrect itself inside an excluded parent directory.
        if not directory:
            for n in range(1, len(parts)):
                if self.ignored("/".join(parts[:n]), directory=True):
                    return True
        probe = rel + "/" if directory else rel
        ignored = False
        for n in range(len(parts)):
            base = "/".join(parts[:n])
            if base not in self.cache:
                self.cache[base] = self._read(self.root / base / ".gitignore")
            sub = probe[len(base) + 1:] if base else probe
            verdict = self.cache[base].check_file(sub).include
            if verdict is not None:
                ignored = bool(verdict)
        extra = self.extra.check_file(probe).include
        return bool(extra) if extra is not None else ignored


def _relative(root: Path, path: str | os.PathLike) -> Optional[str]:
    try:
        raw = str(path).replace("\\", "/")
        if "\x00" in raw or (PureWindowsPath(raw).drive and os.name != "nt"):
            return None
        absolute = (root / raw).resolve()
        rel = absolute.relative_to(root).as_posix()
        return rel if rel != "." else None
    except (OSError, ValueError, RuntimeError):
        return None


def _literal_references(text: str, category: str, suffix: str):
    """Yield (literal path, kind, line, redacted evidence, confidence, base)."""
    newlines = [m.start() for m in re.finditer("\n", text)]
    def line_at(offset):
        return bisect.bisect_left(newlines, offset) + 1

    if category == "code":
        spans = [(m.start(), m.end()) for m in _TOKENS.finditer(text)]
        starts = [a for a, _ in spans]

        def in_literal_or_comment(offset):
            i = bisect.bisect_right(starts, offset) - 1
            return i >= 0 and offset < spans[i][1]

        for pattern in (_SOURCE_IMPORT, _SOURCE_LOAD):
            for m in pattern.finditer(text):
                if in_literal_or_comment(m.start()):
                    continue
                value = m.group("value")
                if not is_artifact_path(value.split("#", 1)[0]):
                    continue
                load = pattern is _SOURCE_LOAD
                call = m.group("call") if load else "import"
                if call == "open" and re.match(r'''\s*,\s*["'][wax]''', text[m.end():m.end() + 20]):
                    continue
                yield (value, "uses_schema" if _schema_path(value) or call == "loadSchema" else "loads_config",
                       line_at(m.start()), f"literal {call} file reference",
                       "medium" if load else "high", "root" if load else "relative")
        return

    if suffix in {".proto", ".graphql", ".gql"}:
        pattern = (r'''(?m)^\s*import\s+(?:(?:public|weak)\s+)?["']([^"']+)["']'''
                   if suffix == ".proto" else r'''(?m)^\s*\#\s*import\s+(?:[^\n]*?\sfrom\s+)?["']([^"']+)["']''')
        for m in re.finditer(pattern, text):
            yield m.group(1), "schema_ref", line_at(m.start()), "literal schema import", "high", "relative"
        return

    if suffix in {".json", ".jsonc", ".avsc"}:
        # Blanking comments preserves source offsets for evidence locations.
        clean = _TOKENS.sub(lambda m: re.sub(r"[^\n]", " ", m.group())
                            if m.group().startswith(("//", "/*")) else m.group(), text)
        if suffix == ".jsonc":
            # Replace commas only outside quoted strings, preserving locations.
            spans = [(m.start(), m.end()) for m in _JSON_STRING.finditer(clean)]
            starts = [a for a, _ in spans]
            def trailing_comma(match):
                i = bisect.bisect_right(starts, match.start()) - 1
                return match.group() if i >= 0 and match.start() < spans[i][1] else " " + match.group()[1:]
            clean = re.sub(r",\s*[}\]]", trailing_comma, clean)
        value = json.loads(clean)
        positions = defaultdict(deque)
        for m in _JSON_STRING.finditer(clean):
            end = m.end()
            while end < len(clean) and clean[end].isspace():
                end += 1
            if end >= len(clean) or clean[end] != ":":
                positions[json.loads(m.group())].append(m.start())

        def visit(obj, key="", sensitive=False):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield from visit(v, k, sensitive or bool(_SENSITIVE.search(k)))
            elif isinstance(obj, list):
                for v in obj:
                    yield from visit(v, key, sensitive)
            elif isinstance(obj, str):
                offset = positions[obj].popleft() if positions[obj] else 0
                if not sensitive and (key in {"$ref", "$schema"} or _KEY.search(key) or _PATH_KEY.search(key)):
                    kind = "schema_ref" if key in {"$ref", "$schema"} else "uses_schema" if "schema" in key.lower() else "config_ref"
                    yield obj, kind, line_at(offset), \
                        "JSON schema reference" if key in {"$ref", "$schema"} else "configuration file-path field", "high" if key in {"$ref", "$schema"} else "medium", "relative"
        yield from visit(value)
        return

    if suffix not in CONFIG_SUFFIXES:
        return
    # Conservative YAML/TOML/INI literal fields. No interpolation or YAML tags.
    stack: list[tuple[int, str, bool]] = []
    section_sensitive = False
    for number, original in enumerate(text.splitlines(), 1):
        line = re.sub(r'''("(?:\\.|[^"\\])*"|'[^']*')|\s+#.*$''',
                      lambda m: m.group(1) or "", original)
        if line.lstrip().startswith("[") and suffix in {".toml", ".ini", ".cfg", ".conf"}:
            section_sensitive = bool(_SENSITIVE.search(line.split("]", 1)[0]))
            continue
        if not line.strip() or line.lstrip().startswith(("#", ";", "[")):
            continue
        indent = len(line) - len(line.lstrip())
        while stack and indent <= stack[-1][0]:
            stack.pop()
        m = re.match(r'''\s*["']?(?P<key>[\w.$-]+)["']?\s*[:=]\s*(?P<value>.*)$''', line)
        if m:
            key, value = m.group("key"), m.group("value").strip()
            sensitive = section_sensitive or bool(_SENSITIVE.search(key)) or bool(stack and stack[-1][2])
            if not value:
                stack.append((indent, key, sensitive))
                continue
        elif stack and re.match(r"\s*-\s+", line):
            key, sensitive = stack[-1][1], stack[-1][2]
            value = re.sub(r"^\s*-\s+", "", line).strip()
        else:
            continue
        if sensitive or not (key in {"$ref", "$schema"} or _KEY.search(key) or _PATH_KEY.search(key)):
            continue
        values = re.findall(r'''["']([^"']+)["']''', value) if value.startswith(("[", '"', "'")) else [value]
        for item in values:
            kind = "schema_ref" if key in {"$ref", "$schema"} else "uses_schema" if "schema" in key.lower() else "config_ref"
            yield item, kind, number, \
                "schema reference" if key in {"$ref", "$schema"} else "configuration file-path field", "high" if key in {"$ref", "$schema"} else "medium", "relative"


def _target(root: Path, source: str, literal: str, kind: str, base: str, ignore: _Ignore):
    value = literal.strip().replace("\\", "/")
    if not value or len(value) > 500 or any(x in value for x in ("\x00", "\n", "\r", "://", "${", "{{", "?", "*", "!", "@")):
        return None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) or value.startswith(("/", "~")):
        return None
    value = value.split("#", 1)[0]
    if not value:
        return (source, True) if kind == "schema_ref" else None
    if not Path(value).suffix and not value.startswith(("./", "../")):
        return None
    relative = posixpath.join(posixpath.dirname(source), value)
    candidates = [relative] if value.startswith(("./", "../")) else [relative, value]
    if base == "root" and not value.startswith(("./", "../")):
        candidates.reverse()
    primary = None
    for number, candidate in enumerate(dict.fromkeys(candidates)):
        rel = _relative(root, candidate)
        if rel is None or ignore.ignored(rel) or Path(rel).name.startswith(".env"):
            if number == 0:
                return None
            continue
        if number == 0:
            primary = rel
        if (root / rel).is_file():
            return rel, True
    return (primary, False) if primary else None


_ENV_USE = re.compile(
    r'''\bprocess\s*\.\s*env\s*(?:\.\s*(?P<property>[A-Za-z_][A-Za-z0-9_]*)|\[\s*["'](?P<bracket>[A-Za-z_][A-Za-z0-9_]*)["']\s*\])'''
    r'''|\bos\s*\.\s*(?:getenv|environ\s*\.\s*get)\s*\(\s*["'](?P<getenv>[A-Za-z_][A-Za-z0-9_]*)["'](?=\s*[,\)])'''
    r'''|\bos\s*\.\s*environ\s*\[\s*["'](?P<environ>[A-Za-z_][A-Za-z0-9_]*)["']\s*\]''')
_SQL_PART = r'''(?:[A-Za-z_][A-Za-z0-9_$]*|"[A-Za-z_][A-Za-z0-9_$]*"|`[A-Za-z_][A-Za-z0-9_$]*`|\[[A-Za-z_][A-Za-z0-9_$]*\])'''
_SQL_NAME = _SQL_PART + r"(?:\s*\.\s*" + _SQL_PART + r")?"
_SQL_DEF = re.compile(r"\bCREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>" + _SQL_NAME + r")", re.I)
_SQL_USE = re.compile(r"\b(?:FROM|JOIN|INSERT\s+INTO|UPDATE|REFERENCES)\s+(?P<name>" + _SQL_NAME + r")", re.I)
_SQL_COMMENTS_VALUES = re.compile(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|\\.|[^'\\])*'")
_SQL_CALL = re.compile(r"(?:\b(?:execute|executemany|query|raw|prepare|exec|executescript|sql)\s*\(\s*|\b(?:\w*sql\w*|\w*query\w*|statement)\s*=\s*)[rubf]*$", re.I)


def _sql_name(value: str) -> str:
    # Quoted identifier case is significant; do not silently bind it to an
    # unquoted name. Bracket/backtick names use the same exact-name convention.
    return ".".join(part if part.startswith(('"', '`', '[')) else part.lower()
                    for part in re.split(r"\s*\.\s*", value.strip()))


def _sql_entities(text: str, first_line: int = 1):
    clean = _SQL_COMMENTS_VALUES.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
    if any(marker in clean for marker in ("${", "{{")):
        return
    newlines = [m.start() for m in re.finditer("\n", text)]
    ctes = {_sql_name(m.group(1)) for m in re.finditer(r"(?:\bWITH|,)\s+(" + _SQL_NAME + r")\s+AS\s*\(", clean, re.I)}
    for pattern, role in ((_SQL_DEF, "definition"), (_SQL_USE, "use")):
        for match in pattern.finditer(clean):
            name = _sql_name(match.group("name"))
            if name in ctes or name.upper() in {"SELECT", "SET", "VALUES", "IF", "ONLY"}:
                continue
            if role == "use" and not re.search(r"\b(?:SELECT|INSERT|UPDATE|DELETE|REFERENCES)\b", clean, re.I):
                continue
            line = first_line + bisect.bisect_left(newlines, match.start())
            yield "table", name, role, line


def _entities(text: str, path: str, category: str):
    """Yield identifiers and locations only, never literal SQL/env values."""
    if _env_path(path):
        quote = None
        for line, content in enumerate(text.splitlines(), 1):
            if quote:
                if re.search(r"(?<!\\)" + re.escape(quote), content):
                    quote = None
                continue
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", content)
            if not match:
                continue
            yield "env", match.group(1), "definition", line
            value = match.group(2)
            if value.startswith(('"', "'")) and not re.search(r"(?<!\\)" + re.escape(value[0]), value[1:]):
                quote = value[0]
        return
    if Path(path).suffix.lower() == ".sql":
        yield from _sql_entities(text)
        return
    if category != "code":
        return
    tokens = list(_TOKENS.finditer(text))
    starts = [m.start() for m in tokens]
    newlines = [m.start() for m in re.finditer("\n", text)]
    for match in _ENV_USE.finditer(text):
        before = bisect.bisect_right(starts, match.start()) - 1
        if before >= 0 and match.start() < tokens[before].end():
            continue
        name = next(value for value in match.groupdict().values() if value is not None)
        yield "env", name, "use", bisect.bisect_left(newlines, match.start()) + 1
    for token in tokens:
        literal = token.group()
        if not literal.startswith(('"', "'", "`")):
            continue
        prefix = text[max(0, token.start() - 120):token.start()]
        if not _SQL_CALL.search(prefix) and not (literal.startswith("`") and re.search(r"\bsql\s*$", prefix)):
            continue
        delimiter = 3 if literal.startswith(('"""', "'''")) else 1
        body = literal[delimiter:-delimiter]
        # Interpolated SQL may change table identities. Ordinary bound parameters
        # in literal queries remain supported; expression-built strings do not.
        if "${" in body or "{{" in body or re.search(r"\b[rub]*f[rub]*$", prefix, re.I):
            continue
        line = bisect.bisect_left(newlines, token.start()) + 1
        yield from _sql_entities(body, line)


def _entity_keys(ix: Index, path: str) -> set[tuple[str, str, str]]:
    return {tuple(row) for row in ix.conn.execute(
        "SELECT DISTINCT project,entity_kind,bare_name FROM ca_artifact_entities WHERE path=?", (path,))}


def _relink_entities(ix: Index, keys: set[tuple[str, str, str]], consumers: set[str]) -> set[str]:
    """Resolve changed declarations against cached uses, without reading sources."""
    for project, kind, bare in sorted(keys):
        consumers.update(row[0] for row in ix.conn.execute(
            "SELECT DISTINCT path FROM ca_artifact_entities WHERE project=? AND entity_kind=? AND bare_name=? AND role='use'",
            (project, kind, bare)))
    changed = set()
    definitions = {}
    for source in sorted(consumers):
        old_links = defaultdict(list)
        for row in ix.conn.execute("SELECT * FROM ca_entity_links WHERE source_path=?", (source,)):
            old_links[(row["entity_kind"], row["name"], row["line"])].append(dict(row))
        links = []
        for use in ix.conn.execute("SELECT * FROM ca_artifact_entities WHERE path=? AND role='use'", (source,)):
            lookup = (use["project"], use["entity_kind"], use["name"])
            if lookup not in definitions:
                name_column = "name" if use["entity_kind"] == "env" or "." in use["name"] else "bare_name"
                definitions[lookup] = ix.conn.execute(
                    "SELECT path,name,MIN(line) AS line FROM ca_artifact_entities "
                    f"WHERE project=? AND entity_kind=? AND {name_column}=? AND role='definition' GROUP BY path,name",
                    lookup).fetchall()
            defs = definitions[lookup]
            key = (use["entity_kind"], use["name"], use["line"])
            if len(defs) == 1:
                links.append((source, *lookup, use["line"], defs[0]["path"], defs[0]["line"], "resolved"))
            else:
                # Preserve previous evidence for removed declarations so a diff
                # can still find their consumers. Do not invent a target for a
                # use which has never had an unambiguous declaration.
                for old in old_links.get(key, []):
                    links.append((source, *lookup, use["line"], old["target_path"], old["target_line"],
                                  "ambiguous" if defs else "missing"))
        before = {tuple(row) for row in ix.conn.execute(
            "SELECT * FROM ca_relationships WHERE source_path=? AND kind IN ('uses_env','uses_table')", (source,))}
        ix.conn.execute("DELETE FROM ca_entity_links WHERE source_path=?", (source,))
        ix.conn.executemany("INSERT OR IGNORE INTO ca_entity_links VALUES(?,?,?,?,?,?,?,?)", links)
        grouped = defaultdict(list)
        for row in links:
            # source, project, entity kind, name, line, target, target line, status
            grouped[(row[0], row[5], "uses_env" if row[2] == "env" else "uses_table", row[4])].append(row)
        relationships_rows = []
        for (consumer, target, kind, line), values in sorted(grouped.items()):
            names = ", ".join(sorted({v[3] for v in values}))[:180]
            locations = ",".join(str(n) for n in sorted({v[6] for v in values}))
            resolved = all(v[7] == "resolved" for v in values)
            subject = "environment key" if kind == "uses_env" else "SQL table"
            evidence = f"literal {subject} {names}; declaration {target}:{locations}"
            if not resolved:
                evidence += "; declaration missing or ambiguous"
            relationships_rows.append((consumer, target, kind, line, evidence, "medium", int(resolved)))
        ix.conn.execute("DELETE FROM ca_relationships WHERE source_path=? AND kind IN ('uses_env','uses_table')", (source,))
        ix.conn.executemany("INSERT OR REPLACE INTO ca_relationships VALUES(?,?,?,?,?,?,?)", relationships_rows)
        if before != set(relationships_rows):
            changed.add(source)
    return changed


def _inventory(ix: Index, ignore: _Ignore):
    sources = {r[0] for r in ix.conn.execute("SELECT path FROM files")}
    for directory, dirs, files in os.walk(ix.repo):
        rel_dir = Path(directory).relative_to(ix.repo).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirs[:] = sorted(d for d in dirs if not ignore.ignored(prefix + d, directory=True)
                         and _relative(ix.repo, prefix + d) is not None)
        for name in sorted(files):
            rel = prefix + name
            if (rel in sources or is_artifact_path(rel)) and not ignore.ignored(rel) and _relative(ix.repo, rel) == rel:
                yield rel, "schema" if _schema_path(rel) else "config" if is_artifact_path(rel) else "code"


def _ensure_schema(ix: Index):
    # Avoid executescript(): it would implicitly commit the caller's transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            ix.conn.execute(statement)


def sync_relationships(ix: Index, paths: Optional[Iterable[str]] = None, full: bool = False) -> dict:
    """Refresh cached relationships. Targeted calls read only supplied consumers.

    Full inventory scans compare stat metadata before reading content. Targeted
    events verify content even when an editor preserves size/mtime. Missing targets
    retain incoming evidence with resolved=False; removed consumers lose outgoing
    edges. This function never commits an existing caller transaction.
    """
    start = time.monotonic()
    stats = {"scanned": 0, "parsed": 0, "unchanged": 0, "removed": 0, "skipped": 0, "errors": []}
    changed: set[str] = set()
    with ix.lock:
        had_transaction = ix.conn.in_transaction
        _ensure_schema(ix)
        version = ix.conn.execute("SELECT value FROM meta WHERE key='relationships_extractor_version'").fetchone()
        if not version or version[0] != EXTRACTOR_VERSION:
            paths, full = None, True
        entity_keys: set[tuple[str, str, str]] = set()
        entity_consumers: set[str] = set()
        ignore = _Ignore(ix.repo)
        if paths is None:
            candidates = dict(_inventory(ix, ignore))
            old = {r["path"]: dict(r) for r in ix.conn.execute("SELECT * FROM ca_artifact_files")}
            removed = set(old) - candidates.keys()
            touched = set(candidates) | removed
        else:
            normalized = {_relative(ix.repo, p) for p in paths}
            normalized.discard(None)
            candidates, old, removed = {}, {}, set()
            for rel in normalized:
                row = ix.conn.execute("SELECT * FROM ca_artifact_files WHERE path=?", (rel,)).fetchone()
                if row:
                    old[rel] = dict(row)
                source = ix.conn.execute("SELECT 1 FROM files WHERE path=?", (rel,)).fetchone()
                if ignore.ignored(rel) or not (ix.repo / rel).is_file() or not (source or is_artifact_path(rel)):
                    removed.add(rel)
                else:
                    candidates[rel] = "schema" if _schema_path(rel) else "config" if is_artifact_path(rel) else "code"
            touched = normalized
        for rel in sorted(removed):
            entity_keys.update(_entity_keys(ix, rel))
            entity_consumers.add(rel)
            ix.conn.execute("DELETE FROM ca_artifact_entities WHERE path=?", (rel,))
            ix.conn.execute("DELETE FROM ca_relationships WHERE source_path=?", (rel,))
            ix.conn.execute("DELETE FROM ca_artifact_files WHERE path=?", (rel,))
            stats["removed"] += int(rel in old)
            if rel in old:
                changed.add(rel)
        for rel, category in sorted(candidates.items()):
            stats["scanned"] += 1
            previous = old.get(rel)
            st, digest = None, None
            try:
                st = (ix.repo / rel).stat()
                if not full and paths is None and previous and previous["size"] == st.st_size and previous["mtime_ns"] == st.st_mtime_ns:
                    stats["unchanged"] += 1
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    entity_keys.update(_entity_keys(ix, rel))
                    entity_consumers.add(rel)
                    ix.conn.execute("DELETE FROM ca_artifact_entities WHERE path=?", (rel,))
                    ix.conn.execute("DELETE FROM ca_relationships WHERE source_path=?", (rel,))
                    ix.conn.execute("INSERT OR REPLACE INTO ca_artifact_files VALUES(?,?,?,?,?,?,?)",
                                    (rel, category, st.st_size, st.st_mtime_ns, None, time.time(), "too_large"))
                    stats["skipped"] += 1
                    changed.add(rel)
                    continue
                src = (ix.repo / rel).read_bytes()
                after = (ix.repo / rel).stat()
                if (st.st_size, st.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    stats["errors"].append({"path": rel, "reason": "changed_during_read"})
                    continue
                digest = hashlib.blake2b(src, digest_size=16).hexdigest()
                if previous and previous["content_hash"] == digest and not full:
                    ix.conn.execute("UPDATE ca_artifact_files SET size=?,mtime_ns=? WHERE path=?", (st.st_size, st.st_mtime_ns, rel))
                    stats["unchanged"] += 1
                    continue
                text = src.decode("utf-8-sig", "replace")
                extracted = [] if _env_path(rel) else list(_literal_references(text, category, Path(rel).suffix.lower()))
                entity_keys.update(_entity_keys(ix, rel))
                entity_consumers.add(rel)
                entities = list(_entities(text, rel, category))
                project_row = ix.conn.execute("SELECT project FROM files WHERE path=?", (rel,)).fetchone()
                project = project_row[0] if project_row else project_for(ix.repo, rel)
                entity_rows = [(rel, project, kind, name, name.rsplit(".", 1)[-1], role, line)
                               for kind, name, role, line in entities]
                ix.conn.execute("DELETE FROM ca_artifact_entities WHERE path=?", (rel,))
                ix.conn.executemany("INSERT OR IGNORE INTO ca_artifact_entities VALUES(?,?,?,?,?,?,?)", entity_rows)
                entity_keys.update((row[1], row[2], row[4]) for row in entity_rows)
                rows = []
                for literal, kind, line, evidence, confidence, base in extracted:
                    target = _target(ix.repo, rel, literal, kind, base, ignore)
                    if target:
                        target_path, resolved = target
                        rows.append((rel, target_path, kind, line, evidence, confidence, int(resolved)))
                ix.conn.execute("DELETE FROM ca_relationships WHERE source_path=? AND kind NOT IN ('uses_env','uses_table')", (rel,))
                ix.conn.executemany("INSERT OR IGNORE INTO ca_relationships VALUES(?,?,?,?,?,?,?)", rows)
                ix.conn.execute("INSERT OR REPLACE INTO ca_artifact_files VALUES(?,?,?,?,?,?,?)",
                                (rel, category, st.st_size, st.st_mtime_ns, digest, time.time(), "indexed"))
                stats["parsed"] += 1
                changed.add(rel)
            except (OSError, ValueError, RecursionError) as exc:
                stats["errors"].append({"path": rel, "reason": type(exc).__name__})
                entity_keys.update(_entity_keys(ix, rel))
                entity_consumers.add(rel)
                ix.conn.execute("DELETE FROM ca_artifact_entities WHERE path=?", (rel,))
                ix.conn.execute("DELETE FROM ca_relationships WHERE source_path=?", (rel,))
                ix.conn.execute("INSERT OR REPLACE INTO ca_artifact_files VALUES(?,?,?,?,?,?,?)",
                                (rel, category, st.st_size if st else previous["size"] if previous else -1,
                                 st.st_mtime_ns if st else previous["mtime_ns"] if previous else -1,
                                 digest, time.time(), "error"))
                changed.add(rel)
        # Stat dependencies only on full reconciliation; on saves touch only
        # changed targets. No source content is reread for inbound invalidation.
        targets = ([r[0] for r in ix.conn.execute("SELECT DISTINCT target_path FROM ca_relationships")]
                   if paths is None else sorted(touched))
        for target in targets:
            resolved = _relative(ix.repo, target) == target and not ignore.ignored(target) and (ix.repo / target).is_file()
            cursor = ix.conn.execute("UPDATE ca_relationships SET resolved=? WHERE target_path=? AND resolved!=? "
                                     "AND kind NOT IN ('uses_env','uses_table')",
                                     (int(resolved), target, int(resolved)))
            if cursor.rowcount:
                changed.add(target)
        changed.update(_relink_entities(ix, entity_keys, entity_consumers))
        ix.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('relationships_extractor_version',?)", (EXTRACTOR_VERSION,))
        if not had_transaction:
            ix.conn.commit()
    stats["seconds"] = round(time.monotonic() - start, 3)
    stats["changed_files"] = sorted(changed)
    stats["updated"] = stats["parsed"]
    return stats


def relationships(ix: Index, path: Optional[str] = None, kind: Optional[str] = None,
                  direction: str = "both", limit: int = 50, offset: int = 0) -> dict:
    """Query file paths or directory prefixes; ``in`` finds consumers, ``out`` dependencies."""
    aliases = {"incoming": "in", "outgoing": "out"}
    direction = aliases.get(direction, direction)
    if direction not in {"in", "out", "both"}:
        raise ValueError("direction must be in, out, or both")
    if kind and kind not in KINDS:
        raise ValueError("unknown relationship kind")
    limit, offset = max(1, min(200, int(limit))), max(0, int(offset))
    # Normalize lexically: queries must not read or resolve the filesystem.
    rel = ix.rel(path) if path else None
    if rel:
        rel = posixpath.normpath(rel.replace("\\", "/"))
        if rel == ".." or rel.startswith("../") or rel.startswith("/") or PureWindowsPath(rel).drive:
            raise ValueError("path must be inside the repository")
    clauses, params = [], []
    if rel == ".":
        rel = None
    if rel:
        def path_clause(column):
            params.extend((rel, len(rel.rstrip("/")) + 1, rel.rstrip("/") + "/"))
            return f"({column}=? OR substr({column},1,?)=?)"
        if direction == "in":
            clauses.append(path_clause("target_path"))
        elif direction == "out":
            clauses.append(path_clause("source_path"))
        else:
            clauses.append(f"({path_clause('source_path')} OR {path_clause('target_path')})")
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with ix.lock:
        _ensure_schema(ix)
        total = ix.conn.execute("SELECT COUNT(*) FROM ca_relationships" + where, params).fetchone()[0]
        rows = ix.conn.execute("SELECT * FROM ca_relationships" + where +
                               " ORDER BY source_path,line,target_path,kind LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        items = [{**dict(row), "resolved": bool(row["resolved"])} for row in rows]
        coverage = {r[0]: r[1] for r in ix.conn.execute("SELECT status,COUNT(*) FROM ca_artifact_files GROUP BY status")}
    truncated = offset + len(items) < total
    return {"path": rel, "kind": kind, "direction": direction, "items": items, "total": total,
            "limit": limit, "offset": offset, "truncated": truncated,
            "next_offset": offset + len(items) if truncated else None,
            "coverage": coverage, "method": "literal paths and uniquely declared env keys/SQL tables within a project; dynamic references are not inferred"}


def render(result: dict, token_budget: int = 2000) -> str:
    """Compact, conservatively budgeted evidence; never include source contents."""
    # A character cap is deliberately conservative for paths and punctuation.
    cap = max(1, int(token_budget)) * 3
    header = f"File relationships: {result.get('total', 0)} matches (consumer -> dependency)"
    lines = [header]
    shown = 0
    for row in result.get("items", []):
        line = (f"{row['source_path']}:{row['line']} -> {row['target_path']} "
                f"[{row['kind']}; {row['confidence']}; {'resolved' if row['resolved'] else 'missing'}] {row['evidence']}")
        if len("\n".join(lines + [line])) + 90 > cap:
            break
        lines.append(line)
        shown += 1
    if shown < result.get("total", 0) - result.get("offset", 0):
        lines.append(f"Shown {shown}; continue at offset {result.get('offset', 0) + shown} with a larger budget if needed.")
    lines.append("Literal paths/keys/tables only; runtime values and dynamic references are not inferred.")
    return "\n".join(lines)[:cap]
