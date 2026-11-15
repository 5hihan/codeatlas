"""task_context: everything an agent needs for one task, inside a strict token budget.

Pipeline
  1. terms from the task text (CamelCase / snake_case split, stop words removed)
  2. candidate symbols by BM25 over an FTS5 index of names, signatures and docs (scope-filtered)
  3. boost by PageRank, scope, exported-ness; penalise tests
  3b. ``known``: symbols / files / path:line positions the agent has already looked at. A weighted search from
     them over calls, membership, containment, imports and config relationships (:mod:`codeatlas.connect`)
     re-ranks candidates by graph proximity, drops the anchors themselves from the answer, adds the symbols
     that bridge an anchor to a result, and renders the route to each strong result
     (``main --calls@4--> handle --calls@5--> process``)
  4. expand one hop through the resolved graph (callers / callees) with a discount
  5. tests that reference the selected symbols, config/docs files mentioning the terms
  6. assemble under the budget: signatures for everything, source bodies for the top items,
     de-duplicated (no child inside an already-included parent), each with a one-line reason and an
     expansion handle (a tool call that gets more)
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from typing import Iterable, Optional

from .graph import Atlas, is_example_path, is_script_path, is_test_path
from .languages import CLASS_KINDS
from .indexer import Index
from .rank import ensure_rank, estimate_tokens
from .budgeting import plan_context

STOP = set("""a an the and or of to in on for with from by as is are was were be been being this that these those it its
into onto over under at about after before between during without within fix fixing fixed bug bugs issue issues
error errors add adding added remove removing removed update updating updated change changing changed make making
made implement implementing support supporting handle handling when where which what who how why should would could
can cannot not no yes do does did done doing use using used need needs needed want wants wanted new old all any some
so if then else than too very just also only more most less least code file files function functions method methods
class classes module modules please pls we our you your they their them there here
call calls called caller callers test tests tested testing cover covers covered coverage exercise exercises exercised
break breaks broken breaking give list show explain describe tell name names brief briefly anchor anchors line lines
repository repo project answer answers changes through flow flows""".split())
# the second block is how questions talk about code ("who calls it, which tests cover it, give file:line anchors"):
# words about the answer wanted, not about the code, so they never select symbols

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(symbol_id UNINDEXED, name, words, signature, doc, path,
  pathwords, tokenize='unicode61');
"""
FTS_SCHEMA_VERSION = "3"   # bump when the indexed text changes shape (stems, directory words, stop words)

DOC_EXT = (".md", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml", ".env", ".ini", ".cfg", ".sql", ".graphql", ".proto")
# directory names that say nothing about what the code does
_PATHWORD_IGNORE = frozenset("src lib app apps pkg pkgs internal main java com org io net js ts py index mod".split())
# items below this fraction of the best score are a long lexical tail (shown only while the budget is unused)
TAIL_CUTOFF = 0.3
MIN_ITEMS = 8
# the list is what a model reads first and keeps in context for the rest of the session; measured on ten real
# questions, items beyond these were never the ones it followed up on
MAX_ITEMS_LOCATION = 12   # signatures-only answers ("where is X")
MAX_ITEMS_TASK = 18       # answers with bodies
SIG_CHARS = 150
DOC_CHARS = 120
BODY_KINDS = frozenset({"function", "method", "class", "struct", "module"})   # a type's signature already is its body
# scoring weights (tuned on the benchmark corpora: see the development notes in README)
COVERAGE_WEIGHT = 0.6     # per task term the symbol's own text covers
COVERAGE_POWER = 1.5      # covering two task terms is worth more than twice one rare term (MRR 0.70 -> 0.72 on the
                          # nine benchmark tasks; a body-parsing task's request_body_to_args moved from 6th to 3rd)
EXACT_BOOST = 8.0         # exact-name match on a plain word, absolute: a relative boost promoted every `match`/`run`
NAMED_SHARE = 0.6         # an identifier the task spells out (Hono.fetch, request_body_to_args, getPath) is the subject:
                          # its definition gets this share of the best lexical score, and seeds the flow expansion
_IDENT_IN_TASK = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_#][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]{2,}")
FTS_LIMIT = 800
CALLS_TERM_SHARE = 0.15   # per task term a symbol calls (request.body() for "body parsing"), as a share of the best score
CALLS_TERM_MAX_SITES = 80 # a name called from more places than this (get, run, json) says nothing about the task
RANK_WEIGHT = 2.0         # PageRank boost: 1 + RANK_WEIGHT * sqrt(rank / max_rank)
TEST_FACTOR = 0.4
EXAMPLE_FACTOR = 0.5
SCRIPT_FACTOR = 0.5       # scripts/, tools/, ci/: build and release tooling, rarely the subject of a task
MAX_CALLER_ITEMS = 6      # for a change ("which functions would I change, who calls them"): callers of the strongest items
MAX_CALLERS_SHOWN = 5
# "how does X work" is usually answered by callable code; types, constants and properties are reached from it
KIND_FACTORS = {"interface": 0.75, "type": 0.75, "enum": 0.8, "constant": 0.7, "variable": 0.7, "property": 0.7}
PRIVATE_FACTOR = 0.8      # private helpers are reached through their public callers
FILE_DIVERSITY = 0.75     # the k-th candidate from one file counts FILE_DIVERSITY**k: spread the answer, outline the rest
PATH_TERM_WEIGHT = 0.5    # per task term that names the symbol's module (directory or file): "dependencies" -> fastapi/dependencies/
MAX_SOURCE_ITEMS = 4      # bodies are included for the strongest items only; the rest keep a signature and a handle
MAX_SOURCE_LINES = 100    # longer bodies are read on demand with symbol_source


def split_words(s: str) -> list[str]:
    parts = re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", s)
    return [p.lower() for p in parts if p]


# ---- a compact Porter stemmer: `dependencies`, `dependant` and `depends` all become `depend`, `resolved`
# and `resolve` become `resolv`, so task words meet identifier words without a dictionary
def _cons(w: str, i: int) -> bool:
    c = w[i]
    if c in "aeiou":
        return False
    if c == "y":
        return i == 0 or not _cons(w, i - 1)
    return True


def _measure(w: str) -> int:
    n = i = 0
    L = len(w)
    while i < L and _cons(w, i):
        i += 1
    while i < L:
        while i < L and not _cons(w, i):
            i += 1
        if i >= L:
            break
        n += 1
        while i < L and _cons(w, i):
            i += 1
    return n


def _vowel_in(w: str) -> bool:
    return any(not _cons(w, i) for i in range(len(w)))


def _cvc(w: str) -> bool:
    L = len(w)
    return L >= 3 and _cons(w, L - 3) and not _cons(w, L - 2) and _cons(w, L - 1) and w[-1] not in "wxy"


_STEP2 = (("ational", "ate"), ("tional", "tion"), ("enci", "ence"), ("anci", "ance"), ("izer", "ize"), ("bli", "ble"),
          ("alli", "al"), ("entli", "ent"), ("eli", "e"), ("ousli", "ous"), ("ization", "ize"), ("ation", "ate"),
          ("ator", "ate"), ("alism", "al"), ("iveness", "ive"), ("fulness", "ful"), ("ousness", "ous"), ("aliti", "al"),
          ("iviti", "ive"), ("biliti", "ble"), ("logi", "log"))
_STEP3 = (("icate", "ic"), ("ative", ""), ("alize", "al"), ("iciti", "ic"), ("ical", "ic"), ("ful", ""), ("ness", ""))
_STEP4 = ("al", "ance", "ence", "er", "ic", "able", "ible", "ant", "ement", "ment", "ent", "ion", "ou", "ism", "ate",
          "iti", "ous", "ive", "ize")


def stem(word: str) -> str:
    w = word.lower()
    if len(w) <= 2 or not w.isalpha():
        return w
    # 1a
    if w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("ies"):
        w = w[:-2]
    elif w.endswith("ss"):
        pass
    elif w.endswith("s"):
        w = w[:-1]
    # 1b
    if w.endswith("eed"):
        if _measure(w[:-3]) > 0:
            w = w[:-1]
    else:
        for suf in ("ed", "ing"):
            if w.endswith(suf) and _vowel_in(w[:-len(suf)]):
                w = w[:-len(suf)]
                if w.endswith(("at", "bl", "iz")):
                    w += "e"
                elif len(w) >= 2 and w[-1] == w[-2] and _cons(w, len(w) - 1) and w[-1] not in "lsz":
                    w = w[:-1]
                elif _measure(w) == 1 and _cvc(w):
                    w += "e"
                break
    # 1c
    if w.endswith("y") and _vowel_in(w[:-1]):
        w = w[:-1] + "i"
    # 2, 3
    for table in (_STEP2, _STEP3):
        for suf, rep_ in table:
            if w.endswith(suf):
                base = w[:-len(suf)]
                if _measure(base) > 0:
                    w = base + rep_
                break
    # 4
    for suf in _STEP4:
        if w.endswith(suf):
            base = w[:-len(suf)]
            if _measure(base) > 1 and (suf != "ion" or base.endswith(("s", "t"))):
                w = base
            break
    # 5
    if w.endswith("e"):
        base = w[:-1]
        m = _measure(base)
        if m > 1 or (m == 1 and not _cvc(base)):
            w = base
    if len(w) >= 2 and w.endswith("ll") and _measure(w) > 1:
        w = w[:-1]
    return w


def stems_of(words: Iterable[str]) -> list[str]:
    out: list[str] = []
    for w in words:
        st = stem(w)
        if st and st not in out:
            out.append(st)
    return out


def path_words(path: str) -> list[str]:
    """Words of the file name and of its directories (stemmed), so a task about "dependencies" meets
    ``fastapi/dependencies/utils.py``."""
    parts = path.split("/")
    words = split_words(parts[-1].rsplit(".", 1)[0])
    for d in parts[:-1]:
        words += [w for w in split_words(d) if w not in _PATHWORD_IGNORE and w not in STOP and len(w) > 1]
    return stems_of(words)


# "in this FastAPI repository", "the hono codebase": the project is named, not a symbol; its name (and the words
# inside it: Fast, API) would otherwise match everything in the repository
_PROJECT_REF = re.compile(r"\b(?:in|of|for|on|within|inside)?\s*(?:this|the|our|my)?\s*([A-Za-z][\w.-]*)\s+"
                          r"(?:repository|repo|project|codebase|code base|library|package|monorepo)\b", re.I)


def named_identifiers(task: str) -> list[str]:
    """Identifiers the task spells out as code: qualified (Hono.fetch), snake_case (request_body_to_args) or
    camelCase (getPath). Plain words are not identifiers, whatever symbol happens to share their name."""
    task = _PROJECT_REF.sub(" ", task)
    out: list[str] = []
    for m in _IDENT_IN_TASK.findall(task):
        mixed = any(c.isupper() for c in m[1:]) and any(c.islower() for c in m)   # getPath, OpenAPI; not JSX, HTML
        if ("." in m or "_" in m or mixed) and m not in out and m.lower() not in STOP:
            out.append(m)   # case kept: the task spelled the identifier, so `OpenAPI` is not `openapi`
    return out[:8]


def terms_from(task: str) -> list[str]:
    task = _PROJECT_REF.sub(" ", task)
    seen: list[str] = []
    for w in split_words(task):
        if len(w) >= 3 and w not in STOP and w not in seen:
            seen.append(w)
    # also keep identifiers verbatim (refreshToken) so exact names match strongly
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", task):
        if any(c.isupper() for c in ident[1:]) or "_" in ident:
            if ident.lower() not in seen:
                seen.append(ident.lower())
    return seen[:24]


def ensure_fts(ix: Index) -> None:
    """Build/refresh the FTS index when the code index changed (cheap relative to parsing)."""
    c = ix.conn
    with ix.lock:
        c.executescript(FTS_SCHEMA)
        ver = ix.get_meta("last_update") or "0"
        if ix.get_meta("fts_version") == ver and ix.get_meta("fts_schema") == FTS_SCHEMA_VERSION:
            return
        cols = {r[1] for r in c.execute("PRAGMA table_info(symbols_fts)")}
        if "pathwords" not in cols or ix.get_meta("fts_schema") != FTS_SCHEMA_VERSION:
            c.execute("DROP TABLE IF EXISTS symbols_fts")
            c.executescript(FTS_SCHEMA)
        c.execute("DELETE FROM symbols_fts")
        rows = c.execute("SELECT s.id, s.name, s.qualified, s.signature, s.doc, f.path FROM symbols s "
                         "JOIN files f ON f.id=s.file_id").fetchall()
        c.executemany("INSERT INTO symbols_fts(symbol_id, name, words, signature, doc, path, pathwords) "
                      "VALUES(?,?,?,?,?,?,?)",
                      [(r[0], r[1], " ".join(stems_of(split_words(r[2]))), (r[3] or "")[:300], (r[4] or "")[:400], r[5],
                        " ".join(path_words(r[5]))) for r in rows])
        ix.set_meta("fts_version", ver)
        ix.set_meta("fts_schema", FTS_SCHEMA_VERSION)
        c.commit()


_PATTERN_HINTS = [
    (r"error handling|exception|try/catch|\btry\b|\bcatch\b|\bthrow|\braise\b|\bpanic", "error_handling"),
    (r"\basync|\bawait|\bpromise|concurren|coroutine", "async_await"),
    (r"\bclosure", "closures"),
    (r"\bdecorator|\bannotation", "decorators"),
    (r"\bgeneric|type parameter|\btemplate\b", "generics"),
    (r"\bgenerator|\byield\b|\biterator", "generators"),
    (r"context manager|\bwith statement|\busing\b.*\bdispos", "context_managers"),
    (r"pattern match|\bmatch statement|\bswitch\b", "pattern_matching"),
    (r"\binterface|\btrait\b|\bprotocol\b", "interfaces_traits"),
    (r"\binherit|\bsubclass|\bextends\b|base class", "inheritance"),
    (r"comprehension", "comprehensions"),
    (r"goroutine|\bchannel", "goroutines_channels"),
]


def _pattern_hints(task: str) -> list[str]:
    return [pat for rx, pat in _PATTERN_HINTS if re.search(rx, task, re.I)]


def _fts_query(terms: list[str]) -> str:
    """Stemmed terms, as exact tokens and as prefixes (``depend*`` meets dependency, dependencies, dependant)."""
    parts = []
    seen = set()
    for t in terms:
        t = re.sub(r"[^a-z0-9_]", "", stem(t))
        if not t or t in seen:
            continue
        seen.add(t)
        parts.append(f'"{t}"' if len(t) < 4 else f'("{t}" OR {t}*)')
    return " OR ".join(parts)


def _path_factor(path: str) -> float:
    """Tests, documentation samples and tooling are real code but rarely what a task is about."""
    if is_test_path(path):
        return TEST_FACTOR
    if is_example_path(path):
        return EXAMPLE_FACTOR
    if is_script_path(path):
        return SCRIPT_FACTOR
    return 1.0


def _covered(terms: list[str], own: str) -> int:
    """How many task terms the symbol's own text covers, comparing stems (``resolved`` covers ``resolve``)."""
    own_stems = set(stems_of(split_words(own)))
    n = 0
    for t in terms:
        st = stem(t)
        if t in own or st in own_stems or any(w.startswith(st) for w in own_stems if len(st) >= 4):
            n += 1
    return n


_LOCATION_Q = re.compile(r"^\s*(where|which|who|what calls|what uses|what references|what breaks|what would break|list|find|"
                         r"locate|show me where|is there)\b|\b(where is|where are|where does|which (test|file|function|class|module)|"
                         r"who calls|what calls|callers of|references to|what breaks|would break|is defined|defined in)\b", re.I)
_TASK_Q = re.compile(r"\b(fix|implement|add|change|refactor|debug|rewrite|extend|migrate|optimi[sz]e|why|how does|how do|"
                     r"how is|how are|explain|understand|walk me through|make|write|update|rename|remove|replace|trace)\b", re.I)


_FLOW_Q = re.compile(r"\b(how (does|do|is|are)|flow|reach(es)?|path from|goes from|get from|end to end|walk me through|trace)\b", re.I)
_CTOR_NAMES = ("__init__", "constructor", "init", "__new__", "new", "initialize")


def wants_source(task: str) -> bool:
    """Bodies by default only for tasks and "how does it work" questions.

    A location question ("where is X, which test covers it", "what breaks if X changes") is answered by the
    signatures, anchors, callers and tests; the bodies would be read and discarded. Measured in real sessions, they
    were the whole gap between a task_context answer and a handful of grep hits.
    """
    if _LOCATION_Q.search(task) and not _TASK_Q.search(task):
        return False
    return True


def task_context(ix: Index, atlas: Atlas, task: str, scope: Optional[str] = None, token_budget: Optional[int] = None,
                 include_source: Optional[bool] = None, max_symbols: int = 40, boost: Optional[set[str]] = None,
                 known: Optional[list[str]] = None, known_source: str = "given") -> dict:
    t0 = time.time()
    ensure_rank(ix)
    ensure_fts(ix)
    c = ix.conn
    if include_source is None:
        include_source = wants_source(task)
    terms = terms_from(task)
    # "the Hono constructor": the constructor name selects nothing by itself (every class has one); it is resolved
    # through the class the task names, below
    search_terms = [t for t in terms if t not in _CTOR_NAMES]
    scope = ix.rel(scope).rstrip("/") if scope and scope.strip() != "." else ""
    if isinstance(known, str):
        known = [known]
    known = [k for k in (known or []) if k and str(k).strip()]
    initial_plan = plan_context(ix, task=task, scope=scope, requested_budget=token_budget,
                                signals={"known_count": len(known)} if known else None)
    result: dict = {"task": task, "terms": terms, "scope": scope or None, "index_version": ix.get_meta("last_update"),
                    "budget": initial_plan["budget"], "budget_plan": initial_plan, "include_source": include_source,
                    "items": [], "tests": [], "docs": [], "relationships": [],
                    "known": [], "known_source": known_source if known else None, "paths": [], "unreached": [],
                    "relationships_version": ix.get_meta("relationships_updated"),
                    "truncated": False, "notes": []}
    if not terms:
        result["notes"].append("no searchable terms in the task text")
        return result

    # ---- 1. lexical candidates -------------------------------------------------------
    scores: dict[int, float] = defaultdict(float)
    reasons: dict[int, list[str]] = defaultdict(list)
    try:
        scope_sql = " AND (path=? OR substr(path,1,?)=?)" if scope else ""
        scope_args = (scope, len(scope) + 1, scope + "/") if scope else ()
        rows = c.execute("SELECT symbol_id, bm25(symbols_fts, 0.0, 4.0, 2.5, 1.2, 1.2, 0.0, 0.4) AS s, path, "
                         "name, words, signature, doc FROM symbols_fts WHERE symbols_fts MATCH ?" + scope_sql + f" ORDER BY s LIMIT {FTS_LIMIT}",
                         (_fts_query(search_terms), *scope_args)).fetchall()
    except Exception as e:  # malformed query
        rows = []
        result["notes"].append(f"text search failed: {e}")
    rank_rows = {r[0]: r[1] for r in c.execute("SELECT symbol_id, rank FROM symbol_rank")}
    max_rank = max(rank_rows.values(), default=1.0) or 1.0
    exact = {t for t in terms}
    term_stems = {stem(t) for t in search_terms if len(stem(t)) >= 3}
    meta: dict[int, tuple[str, int]] = {}
    ids = [r[0] for r in rows]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        meta.update({r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT id, kind, exported FROM symbols WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)})
    for sid, bm, path, nm, words, sig, doc in rows:
        s = -bm  # bm25: smaller is better (negative)
        if is_test_path(path):
            s *= TEST_FACTOR
        elif is_example_path(path):
            s *= EXAMPLE_FACTOR  # docs samples / benchmarks / fixtures: real code, rarely the subject of a task
        elif is_script_path(path):
            s *= SCRIPT_FACTOR
        kind, exported = meta.get(sid, ("", 1))
        s *= KIND_FACTORS.get(kind, 1.0)
        if not exported:
            s *= PRIVATE_FACTOR
        # the task names the module itself ("dependencies" -> fastapi/dependencies/, "jsx renderer" -> jsx-renderer/);
        # examples and tests are organised by topic too, but they are not where the behaviour lives
        pw = set(path_words(path)) if not (is_test_path(path) or is_example_path(path) or is_script_path(path)) else set()
        named = sum(1 for t in term_stems if t in pw)
        if named:
            s *= 1.0 + PATH_TERM_WEIGHT * min(2, named)
            reasons[sid].append("named module")
        # coverage: how many distinct task terms the symbol's own text (name/signature/doc) covers
        own = f"{nm} {words} {sig} {doc}".lower()
        covered = _covered(search_terms, own)
        s *= 1.0 + COVERAGE_WEIGHT * covered ** COVERAGE_POWER
        scores[sid] += s
        reasons[sid].append(f"{covered} term{'s' if covered != 1 else ''}" if covered else "path only")
    # symbols selected by structure (the constructor of a named class, a symbol that calls a task term, a callee
    # that names one) are not near-duplicates of their file's lexical hits: the per-file diversity penalty skips them
    structural_ids: set[int] = set()
    # exact-name matches get a strong boost; a bare constructor name ("the Hono constructor") boosts only the
    # constructors of classes the task names, not every constructor in the repository
    exact_boost = EXACT_BOOST
    named = named_identifiers(task)
    named_ids: set[int] = set()
    for t in exact - set(_CTOR_NAMES) - {n.lower() for n in named if "." not in n}:
        for r in c.execute("SELECT s.id, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE lower(s.name)=?" +
                           (" AND (f.path=? OR substr(f.path,1,?)=?)" if scope else "") + " LIMIT 20", (t, *scope_args)):
            scores[r[0]] += exact_boost * _path_factor(r[1])   # a same-named sample in docs_src/ is not the implementation
            if "exact name" not in reasons[r[0]]:
                reasons[r[0]].append("exact name")
    if exact & set(_CTOR_NAMES):
        for cls in exact - set(_CTOR_NAMES):
            for r in c.execute("SELECT s.id, f.path, p.name FROM symbols s JOIN symbols p ON p.id=s.parent_id "
                               "JOIN files f ON f.id=s.file_id WHERE lower(p.name)=? AND s.name IN (%s)" % ",".join("?" * len(_CTOR_NAMES)) +
                               (" AND (f.path=? OR substr(f.path,1,?)=?)" if scope else "") + " LIMIT 20",
                               (cls, *_CTOR_NAMES, *scope_args)):
                scores[r[0]] += exact_boost * _path_factor(r[1])
                reasons[r[0]].append(f"constructor of {r[2]}")
                structural_ids.add(r[0])
    # A task may name a schema/config rather than a symbol. Resolve explicit consumers
    # from the persisted relationship index instead of guessing from matching names.
    from .relationships import relationships
    related_paths = set()
    for term in terms[:8]:
        for row in c.execute("SELECT path FROM ca_artifact_files WHERE path LIKE ? ESCAPE '\\' LIMIT 12",
                             ("%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)):
            related_paths.add(row[0])
    # Entity names (DATABASE_URL, invoices) may not occur in their declaring path.
    for term in terms:
        for row in c.execute("SELECT DISTINCT path FROM ca_artifact_entities WHERE role='definition' "
                             "AND (lower(name)=? OR lower(bare_name)=?) LIMIT 12", (term, term)):
            related_paths.add(row[0])
    for dependency in sorted(related_paths)[:12]:
        for edge in relationships(ix, path=dependency, direction="in", limit=8)["items"]:
            if not edge["resolved"]:
                continue
            for row in c.execute("SELECT s.id,f.path FROM symbols s JOIN files f ON f.id=s.file_id "
                                 "WHERE f.path=? AND s.parent_id IS NULL LIMIT 8", (edge["source_path"],)):
                if scope and not (row[1] == scope or row[1].startswith(scope.rstrip("/") + "/")):
                    continue
                scores[row[0]] += 4.0
                reasons[row[0]].append(f"{edge['kind']} {dependency}")
    # pattern hints: "error handling", "async", "closures" ... map to stored pattern evidence
    hints = _pattern_hints(task)
    if hints:
        scope_sql, scope_args = ("AND f.path LIKE ?", (scope + "%",)) if scope else ("", ())
        for pat in hints:
            try:
                rows = c.execute(
                    "SELECT DISTINCT s.id, f.path FROM code_patterns p JOIN files f ON f.id=p.file_id "
                    "JOIN symbols s ON s.file_id=p.file_id AND p.line BETWEEN s.start_line AND s.end_line "
                    "WHERE p.pattern=? AND s.kind IN ('function','method','class') " + scope_sql + " LIMIT 400",
                    (pat, *scope_args)).fetchall()
            except Exception:
                rows = []
            for sid, path in rows:
                if is_test_path(path):
                    continue
                if sid in scores:
                    scores[sid] *= 1.5
                else:
                    scores[sid] += 2.0  # weak seed: the pattern alone, ranked by PageRank below
                reasons[sid].append(f"uses {pat.replace('_', ' ')}")
    if re.search(r"\bentry ?point|\bmain\b|\bstartup\b|\bbootstrap", task, re.I):
        from .graph import ENTRY_NAMES
        for r in c.execute("SELECT s.id, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name IN (%s) "
                           "AND s.parent_id IS NULL LIMIT 20" % ",".join("?" * len(ENTRY_NAMES)), tuple(ENTRY_NAMES)):
            if scope and not r[1].startswith(scope):
                continue
            scores[r[0]] += 6.0
            reasons[r[0]].append("entry point")
    # ---- 1c. what calls a term -----------------------------------------------------------
    # "body parsing" happens where `request.body()` is called, not where something named body is declared; the
    # reference table knows every call site and its enclosing symbol. Generic names (called from more than
    # CALLS_TERM_MAX_SITES places) and sites in tests, samples and tooling are ignored.
    best_lexical = max(scores.values(), default=0.0)
    if best_lexical > 0:
        called: dict[int, list[str]] = defaultdict(list)
        for t in search_terms:
            if len(t) < 4:
                continue
            rows = c.execute("SELECT r.enclosing_id, f.path FROM refs r JOIN files f ON f.id=r.file_id "
                             "WHERE lower(r.name)=? AND r.kind IN ('call', 'new') AND r.enclosing_id IS NOT NULL "
                             "GROUP BY r.enclosing_id", (t,)).fetchall()
            sites = [(eid, path) for eid, path in rows if _path_factor(path) == 1.0]   # tests call everything
            if not sites or len(sites) > CALLS_TERM_MAX_SITES:
                continue
            for eid, path in sites:
                if scope and not (path == scope or path.startswith(scope + "/")):
                    continue
                called[eid].append(t)
        for eid, ts in called.items():
            scores[eid] += best_lexical * CALLS_TERM_SHARE * len(ts)
            reasons[eid].append("calls " + ", ".join(f"{t}()" for t in ts[:3]))
            structural_ids.add(eid)
    if not scores:
        result["notes"].append("no symbols matched; try broader terms or a different scope")
    # rank boost (+ hot graph: recently changed / active symbols)
    for sid in list(scores):
        scores[sid] *= 1.0 + RANK_WEIGHT * (rank_rows.get(sid, 0.0) / max_rank) ** 0.5
        if boost:
            s = atlas.symbol_by_id(sid)
            if s and s["qualified"] in boost:
                scores[sid] *= 1.4
                reasons[sid].append("recently changed (hot)")
    # an identifier the task spells out is its subject: its definitions get a share of the best score, added after
    # the rank boost so that among same-named definitions the one matching the task's words stays first
    best_ranked = max(scores.values(), default=0.0)
    for ident in named:
        col = "s.qualified" if "." in ident else "s.name"
        for r in c.execute(f"SELECT s.id, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE {col}=?" +
                           (" AND (f.path=? OR substr(f.path,1,?)=?)" if scope else "") + " LIMIT 12", (ident, *scope_args)):
            scores[r[0]] += max(EXACT_BOOST, NAMED_SHARE * best_ranked) * _path_factor(r[1])
            reasons[r[0]].insert(0, "named in the task")
            structural_ids.add(r[0]); named_ids.add(r[0])

    # ---- 1b. what the agent already knows ---------------------------------------------
    # Candidates close to the known anchors in the connection graph are more likely to be what the task means
    # (the `process` reachable from `main` beats a same-named `process` in a legacy package).
    anchor_ids: set[int] = set()
    anchor_labels: dict = {}
    exploration = None
    graph = None
    if known:
        from .connect import Graph, explore, resolve_anchors, PROXIMITY_MAX_COST
        graph = Graph(ix, atlas)
        anchors, unresolved = resolve_anchors(graph, known)
        result["known"] = [{"input": inp, **graph.describe(n)} for n, inp in anchors]
        for u in unresolved:
            result["notes"].append(f"known item {u['input']!r} not found"
                                   + (f" (similar: {'; '.join(u['similar'])})" if u.get("similar") else ""))
        if anchors:
            anchor_labels = {n: graph.describe(n)["label"] for n, _ in anchors}
            exploration = explore(graph, [(n, anchor_labels[n]) for n, _ in anchors], max_cost=PROXIMITY_MAX_COST,
                                  max_nodes=2500, exempt={("s", sid) for sid in scores})
            anchor_ids = {n[1] for n, _ in anchors if n[0] == "s"}
            for sid in list(scores):
                if sid in anchor_ids:
                    continue
                d = exploration.distance(("s", sid))
                if d is None:
                    continue
                scores[sid] *= 1.0 + 1.0 * max(0.0, 1.0 - d / exploration.max_cost)
                p = exploration.path_to(("s", sid))
                if p:
                    reasons[sid].insert(0, f"{p.hops} hop{'s' if p.hops != 1 else ''} from {anchor_labels.get(p.nodes[0], 'known')}")

    # ---- 2. graph expansion --------------------------------------------------------
    lexical_ids = set(scores)
    seeds = sorted(scores, key=lambda s: -scores[s])[:10]
    # the route is followed for "how does X reach Y" questions whether or not bodies were requested
    flow_question = wants_source(task) and bool(_FLOW_Q.search(task))

    def constructor_of(sym: dict) -> Optional[dict]:
        """Constructing a class runs its constructor: the work of `Foo(...)` is in Foo.__init__ / constructor."""
        if sym["kind"] not in CLASS_KINDS:
            return None
        row = c.execute("SELECT id FROM symbols WHERE parent_id=? AND name IN (%s) ORDER BY start_line LIMIT 1"
                        % ",".join("?" * len(_CTOR_NAMES)), (sym["id"], *_CTOR_NAMES)).fetchone()
        return atlas.symbol_by_id(row[0]) if row else None

    best_score = scores[seeds[0]] if seeds else 0.0
    path_ids: set[int] = set()   # nodes reached as a flow question's route: kept out of the per-file diversity penalty

    def with_nested(sid: int) -> list[int]:
        """*sid* plus the closures defined inside it: a decorator factory's `decorator`, a handler's inner `app`.
        The indexer records them as separate symbols, so the calls they make would otherwise not count as the
        outer function's calls."""
        s = atlas.symbol_by_id(sid)
        if not s:
            return [sid]
        rows = c.execute("SELECT s2.id FROM symbols s2 JOIN files f ON f.id=s2.file_id WHERE f.path=? AND s2.id!=? "
                         "AND s2.start_line>=? AND s2.end_line<=? AND s2.kind IN ('function','method') LIMIT 6",
                         (s["path"], sid, s["start_line"], s["end_line"])).fetchall()
        return [sid] + [r[0] for r in rows]

    def expand_callees(sid: int, add_score: float, hop: int) -> list[int]:
        """Score the resolved callees of *sid* (and the constructor of a constructed class); return all of them,
        existing or new, so a further hop can continue through lexical hits as well."""
        targets_all = []
        src = atlas.symbol_by_id(sid)
        nested = with_nested(sid)
        callees = [ce for nid in nested for ce in atlas.callees(nid, limit=12, resolve_as=sid)]
        for ce in callees:
            t = ce.get("target")
            if not (t and t.get("id")):
                continue
            targets = [(t["id"], add_score)]
            ctor = constructor_of(atlas.symbol_by_id(t["id"]) or {"kind": None})
            if ctor:
                # constructing a class runs its constructor, and what the constructor calls is part of the same hop
                targets.append((ctor["id"], add_score))
                for inner in atlas.callees(ctor["id"], limit=12):
                    ti = inner.get("target")
                    if ti and ti.get("id"):
                        targets.append((ti["id"], add_score * 0.9))
            for tid, sc in targets:
                if tid in nested:
                    continue
                why = f"called by {src['qualified']}" if hop == 1 else f"on the path from {src['qualified']}"
                if tid not in scores:
                    # a callee whose own name covers task terms (constructor -> #addRoute for "how routes are
                    # registered") is the behaviour the task is about, not just a neighbour
                    ts = atlas.symbol_by_id(tid)
                    if ts and _covered(search_terms, f"{ts['name']} {ts.get('signature') or ''}".lower()):
                        sc = max(sc * (1.0 + COVERAGE_WEIGHT), best_score * 0.3)
                        structural_ids.add(tid)
                    scores[tid] += sc
                    reasons[tid].append(why)
                elif flow_question and scores[tid] < sc:
                    # a weak lexical hit that sits on the path: for a flow question the path is the evidence
                    scores[tid] = sc
                    reasons[tid].insert(0, why)
                if flow_question:
                    path_ids.add(tid)
                targets_all.append(tid)
        return targets_all

    for sid in seeds:
        base = scores[sid]
        for cl in atlas.callers(sid, limit=8):
            if cl["id"] and cl["id"] not in scores:
                scores[cl["id"]] += base * 0.45
                reasons[cl["id"]].append(f"calls {atlas.symbol_by_id(sid)['qualified']}")
        if flow_question and sid in seeds[:3]:
            # "how does X reach Y": the path matters as much as the match, so path nodes are scored against the
            # best match (not the seed) and followed one hop further, through constructors and existing hits.
            # From a symbol the task names ("from Hono.fetch ...") the path is the answer: its nodes outrank
            # lexical hits that merely share the question's words
            h1, h2 = (0.6, 0.5) if sid in named_ids else (0.3, 0.25)
            first_hop = expand_callees(sid, max(base * 0.35, best_score * h1), 1)
            for nid in first_hop[:8]:
                expand_callees(nid, best_score * h2, 2)
        else:
            expand_callees(sid, base * 0.35, 1)

    # ---- 3. tests & docs -------------------------------------------------------------
    ranked = sorted(scores, key=lambda s: -scores[s])
    # spread the answer over files: the k-th candidate from one file counts FILE_DIVERSITY**k (an outline of that
    # file shows its siblings anyway), so a hub file cannot fill the whole list with near-duplicates
    if FILE_DIVERSITY < 1.0:
        per_file: dict[str, int] = defaultdict(int)
        adjusted: dict[int, float] = {}
        for sid in ranked:
            if sid in path_ids or sid in structural_ids:
                adjusted[sid] = scores[sid]   # route nodes and structural picks are not near-duplicates of each other
                continue
            s = atlas.symbol_by_id(sid)
            path = s["path"] if s else ""
            adjusted[sid] = scores[sid] * (FILE_DIVERSITY ** per_file[path])
            per_file[path] += 1
        ranked.sort(key=lambda x: -adjusted[x])
        scores.update(adjusted)
    # a long tail of weak lexical matches costs tokens and dilutes the answer: keep the head, then only
    # candidates still within TAIL_CUTOFF of the best score
    best = scores[ranked[0]] if ranked else 0.0
    top = [sid for i, sid in enumerate(ranked[:max_symbols]) if i < MIN_ITEMS or scores[sid] >= best * TAIL_CUTOFF]
    top = top[:min(max_symbols, MAX_ITEMS_TASK if include_source else MAX_ITEMS_LOCATION)]
    bridge_ids: set[int] = set()
    if anchor_ids:
        # the agent already has these: do not spend the budget repeating them
        dropped = [s["qualified"] for s in (atlas.symbol_by_id(x) for x in top if x in anchor_ids) if s]
        top = [x for x in top if x not in anchor_ids]
        if dropped:
            result["notes"].append("already known, not repeated: " + ", ".join(dropped[:6]))
    if exploration is not None:
        from .connect import path_dict
        top_set = set(top)
        bridges: dict[int, str] = {}
        for sid in top[:8]:
            p = exploration.path_to(("s", sid))
            if p is None:
                result["unreached"].append(graph.describe(("s", sid)))
                continue
            pd = path_dict(graph, p)
            result["paths"].append(pd)
            for n in p.nodes[1:-1]:
                if n[0] == "s" and n[1] not in top_set and n[1] not in anchor_ids and n[1] not in bridges:
                    bridges[n[1]] = f"links {pd['anchor']['label']} to {pd['target']['label']}"
        for bid, why in list(bridges.items())[:6]:
            scores.setdefault(bid, 0.0)
            reasons[bid] = [why]
            top.append(bid)
            bridge_ids.add(bid)
    names = {atlas.symbol_by_id(s)["name"] for s in top[:15] if atlas.symbol_by_id(s)}
    tests: dict[str, set[int]] = defaultdict(set)
    if names:
        q = "SELECT f.path, r.line FROM refs r JOIN files f ON f.id=r.file_id WHERE r.name IN (%s)" % ",".join("?" * len(names))
        for path, line in c.execute(q, tuple(names)):
            if is_test_path(path):
                tests[path].add(line)
    result["tests"] = [{"path": p, "lines": sorted(ls)[:6], "hits": len(ls)} for p, ls in
                       sorted(tests.items(), key=lambda kv: -len(kv[1]))[:6]]
    result["docs"] = _docs_mentioning(ix, terms[:8], scope, limit=2)
    # pattern blurbs explain language constructs: kept in the result, rendered only when the task asks about them
    result["explain_patterns"] = bool(hints) or bool(_EXPLAIN_Q.search(task))
    # a change wants to know who calls what it touches; callers of the strongest items save a references() round trip
    show_callers = bool(_CALLERS_Q.search(task))
    caller_items = 0

    # Reserve dependency evidence and tests before adding optional source bodies.
    selected = {sid: atlas.symbol_by_id(sid) for sid in top}
    seen_edges = set()
    for path in dict.fromkeys([s["path"] for s in selected.values() if s][:8] + sorted(related_paths)[:8]):
        for edge in relationships(ix, path=path, limit=8)["items"]:
            key = (edge["source_path"], edge["target_path"], edge["kind"])
            if key not in seen_edges and len(result["relationships"]) < 16:
                seen_edges.add(key)
                result["relationships"].append(edge)
    plan = plan_context(ix, task=task, scope=scope, requested_budget=token_budget, signals={
        "candidate_count": len(scores), "exact_match": any("exact name" in r for r in reasons.values()),
        "fanout": len(set(scores) - lexical_ids), "relationship_count": len(seen_edges),
        "complexity": max((s.get("complexity", 0) or 0 for s in selected.values() if s), default=0),
        "test_count": len(tests), "test_relevance": 1.0 if tests else 0.0, "known_count": len(anchor_labels)})
    result["budget_plan"], result["budget"] = plan, plan["budget"]
    allocations = plan["allocations"]
    used = 0
    source_used = 0
    items = []
    included_spans: list[tuple[str, int, int]] = []
    for sid in top:
        s = selected[sid]
        if not s:
            continue
        # skip children whose parent is already included with source
        if any(p == s["path"] and a <= s["start_line"] and s["end_line"] <= b for p, a, b in included_spans):
            continue
        why = ", ".join(reasons[sid][:2])
        header = f"{s['kind']} {s['qualified']}  {s['path']}:{s['start_line']}-{s['end_line']}  [{why}]"
        sig = (s["signature"] or "")[:SIG_CHARS]
        doc = (s["doc"] or "")[:DOC_CHARS]
        cost = estimate_tokens(header + sig + doc) + 4
        if used + cost > allocations["symbols"]:
            result["truncated"] = True
            break
        item = {"id": sid, "qualified": s["qualified"], "kind": s["kind"], "path": s["path"],
                "start_line": s["start_line"], "end_line": s["end_line"], "why": why, "signature": sig, "doc": doc,
                "score": round(scores[sid], 2), "source": None, "bridge": sid in bridge_ids,
                "expand": f"symbol_source(\"{s['qualified']}\", path=\"{s['path']}\")"}
        used += cost
        # source bodies for the strongest items while budget allows (cap 120 lines each); bridges stay signatures
        if (include_source and len(items) < MAX_SOURCE_ITEMS and s["lines"] <= 160 and source_used < allocations["source"]
                and sid not in bridge_ids and s["kind"] in BODY_KINDS):
            src = atlas.source(s["qualified"], s["path"], max_lines=MAX_SOURCE_LINES)
            if src.get("found"):
                scost = estimate_tokens(src["text"])
                if source_used + scost <= allocations["source"]:
                    item["source"] = src["text"]
                    source_used += scost
                    included_spans.append((s["path"], s["start_line"], s["end_line"]))
        items.append(item)
        item["patterns"] = atlas.programming_patterns(s["path"], limit=4, start_line=s["start_line"],
                                                      end_line=s["end_line"])["items"]
        if (show_callers and caller_items < MAX_CALLER_ITEMS and sid not in bridge_ids
                and s["kind"] in ("function", "method", "class") and not is_test_path(s["path"])):
            line = _callers_line(atlas, sid, s)
            if line:
                ccost = estimate_tokens(line) + 1
                if used + ccost <= allocations["symbols"]:
                    item["callers"] = line
                    used += ccost
                    caller_items += 1
    result["items"] = items
    result["tokens_used"] = used + source_used
    result["symbols_considered"] = len(scores)
    result["seconds"] = round(time.time() - t0, 2)
    return result


_EXPLAIN_Q = re.compile(r"\b(explain|idiom|idioms|pattern|patterns|construct|constructs|concept|concepts|what is|teach|learn)\b", re.I)
_CALLERS_Q = re.compile(r"\b(who calls|what calls|callers?|call sites?|who uses|what uses|used by|what breaks|would break|"
                        r"depends? on|change|changes|changing|changed|modify|modifying|refactor|rename|remove|replace|"
                        r"signature|impact|affect|affects|affected)\b", re.I)


def _callers_line(atlas: Atlas, sid: int, s: dict) -> str:
    """``callers: A path:line, B path:line (+n more: references("name"); k in tests)`` for one item."""
    rows = atlas.callers(sid, limit=60)
    code = [r for r in rows if not is_test_path(r["path"])]
    in_tests = len(rows) - len(code)
    if not rows:
        return ""
    code.sort(key=lambda r: is_example_path(r["path"]))   # real callers before documentation samples
    shown = []
    last_path = None
    for r in code[:MAX_CALLERS_SHOWN]:
        loc = f"{r['path']}:{r['line']}" if r["path"] != last_path else f":{r['line']}"
        shown.append(f"{r['qualified']} {loc}")
        last_path = r["path"]
    tail = []
    if len(code) > MAX_CALLERS_SHOWN:
        tail.append(f"+{len(code) - MAX_CALLERS_SHOWN} more: references(\"{s['name']}\")")
    if in_tests:
        tail.append(f"{in_tests} in tests")
    text = "callers: " + (", ".join(shown) if shown else "only tests")
    return text + (f" ({'; '.join(tail)})" if tail else "")


_HISTORY_DOCS = frozenset({"changelog", "changes", "history", "migration", "migrating", "upgrading", "license", "licence",
                           "contributing", "code_of_conduct", "authors", "notice", "release_notes", "releases"})


def _docs_mentioning(ix: Index, terms: list[str], scope: str, limit: int = 6) -> list[dict]:
    """Config/doc files (not in the code index) whose text mentions the task terms, ranked by how many distinct
    terms they mention. The repository's own name matches everything and counts for nothing; change logs,
    migration guides and policy files are never what a task is about."""
    repo_name = ix.repo.name.lower()
    terms = [t for t in terms if t != repo_name and len(t) >= 3]
    if not terms:
        return []
    need = min(2, len(terms))
    from .indexer import DEFAULT_IGNORE_DIRS
    rx = re.compile("|".join(f"({re.escape(t)})" for t in terms), re.I)
    hits: list[tuple[int, int, str, list[str]]] = []
    root = ix.repo
    walked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        if scope and not (rel_dir.startswith(scope) or scope.startswith(rel_dir)):
            continue
        for fn in filenames:
            if fn == ".env" or fn.startswith(".env."):
                continue  # env definitions are retrieved as keys via relationships, never as value snippets
            if not fn.lower().endswith(DOC_EXT) or fn in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
                continue
            if re.sub(r"[-.].*$", "", fn.lower()).replace("-", "_") in _HISTORY_DOCS:
                continue
            p = os.path.join(dirpath, fn)
            walked += 1
            if walked > 1500:
                break
            try:
                if os.path.getsize(p) > 200_000:
                    continue
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            snippets = []
            n = 0
            distinct: set[int] = set()
            for i, ln in enumerate(lines, 1):
                found = [m.lastindex for m in rx.finditer(ln)]
                if found:
                    n += 1
                    distinct.update(found)
                    if len(snippets) < 1:
                        snippets.append(f"{i}: {ln.strip()[:100]}")
            if n and len(distinct) >= need:
                hits.append((len(distinct), n, os.path.relpath(p, root).replace("\\", "/"), snippets))
    hits.sort(key=lambda h: (-h[0], -h[1]))
    return [{"path": p, "hits": n, "terms": d, "snippets": s} for d, n, p, s in hits[:limit]]


def render(r: dict) -> str:
    """Pack evidence first, with bounded optional explanations and source bodies."""
    budget = r["budget"]
    limit = max(1, int(budget * 3.6))
    plan = r.get("budget_plan", {})
    allocations = plan.get("allocations", {})
    policy = plan.get("policy", "explicit")
    footer = (f"\nindex {r.get('index_version')}; relationships {r.get('relationships_version')}; "
              f"{policy} budget ~{budget} tokens")
    omitted = "\n(truncated: narrow scope or increase token_budget)"
    # Reserve the limit notice so optional bodies cannot erase expansion handles.
    room = max(0, limit - len(footer) - len(omitted))
    lines = []
    used = 0
    truncated = r["truncated"]

    def add(block: str, remaining: int | None = None) -> int:
        nonlocal used, truncated
        cost = len(block) + 1
        if used + cost > room or (remaining is not None and cost > remaining):
            truncated = True
            return 0
        lines.append(block)
        used += cost
        return cost

    add(f"# Context for: {r['task']}")
    add(f"terms: {', '.join(r['terms'])}" + (f"   scope: {r['scope']}" if r.get("scope") else ""))
    if r.get("known"):
        from .connect import _node_text
        add(f"known ({r.get('known_source') or 'given'}): " + ", ".join(_node_text(k, None) for k in r["known"][:8]))
    if plan:
        stats, inputs = plan["scoped"], plan["inputs"]
        add(f"budget: {policy} ~{budget} tokens; scope {stats['files']} files; "
            f"{inputs['candidate_count']} candidates; fanout {inputs['fanout']}"
            + ("" if r.get("include_source", True) else "; signatures only (a location question): symbol_source(name) for a body"))
    for note in r.get("notes", []):
        add(f"note: {note}")
    # Signatures and expansion handles remain visible even when bodies do not fit.
    if r["items"]:
        add(f"\n## Relevant symbols ({len(r['items'])} of {r.get('symbols_considered', 0)} considered; "
            f"expand: symbol_source(\"<qualified>\", path=\"<path>\"))")
        for it in r["items"]:
            block = f"{it['kind']} {it['qualified']}  {it['path']}:{it['start_line']}-{it['end_line']}  [{it['why']}]"
            if it["signature"] and it["signature"].strip() != it["qualified"]:
                block += f"\n  {it['signature']}"
            if it["doc"]:
                block += f"\n  doc: {it['doc']}"
            if it.get("callers"):
                block += f"\n  {it['callers']}"
            add(block)
    if r.get("paths") or r.get("unreached"):
        from .connect import format_route_tree, _node_text, PROXIMITY_MAX_HOPS
        remaining = int(allocations.get("paths", budget * 0.12) * 3.6)
        add("\n## How what you know connects to these")
        for line in format_route_tree(r["paths"]):
            remaining -= add(f"  {line}", remaining)
        if r.get("unreached"):
            remaining -= add(f"  no route within ~{PROXIMITY_MAX_HOPS} hops: "
                             + ", ".join(_node_text(u, None) for u in r["unreached"][:8]), remaining)
    if r.get("relationships"):
        remaining = int(allocations.get("relationships", budget) * 3.6)
        add("\n## Code / config / schema dependencies")
        for e in r["relationships"]:
            remaining -= add(f"  {e['source_path']}:{e['line']} --{e['kind']}--> {e['target_path']}"
                             + (" (unresolved)" if not e['resolved'] else ""), remaining)
    if r["tests"]:
        remaining = int(allocations.get("tests", budget) * 3.6)
        add("\n## Tests touching these symbols")
        for t in r["tests"]:
            remaining -= add(f"  {t['path']}  ({t['hits']} refs, lines {', '.join(map(str, t['lines']))})", remaining)
    remaining = int(allocations.get("patterns", budget) * 3.6)
    seen_patterns = set()
    for it in (r["items"] if r.get("explain_patterns", True) else []):
        for p in it.get("patterns", []):
            key = (p["pattern"], it["path"])
            if key not in seen_patterns:
                remaining -= add(f"  {p['title']} {it['path']}:{p['line']} — {p['explanation']}", remaining)
                seen_patterns.add(key)
    remaining = int(allocations.get("source", budget) * 3.6)
    for it in r["items"]:
        if it["source"]:
            remaining -= add(f"\n## Source: {it['path']}:{it['start_line']}\n{it['source']}", remaining)
    if r["docs"]:
        add("\n## Config / docs mentioning the terms")
        for d in r["docs"]:
            add(f"  {d['path']}  ({d['hits']} hits)\n" + "\n".join(f"    {s}" for s in d["snippets"]))
    answer = ("\n".join(lines) + (omitted if truncated else "") + footer)[:limit]
    r["truncated"] = truncated
    r["tokens_used"] = estimate_tokens(answer)
    return answer
