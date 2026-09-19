"""Programming lessons must be anchored to code rather than prose or keywords."""
from __future__ import annotations

from pathlib import Path

import pytest

from codeatlas import patterns as lessons
from codeatlas.indexer import Index
from codeatlas.languages import get_parser
from codeatlas.rank import estimate_tokens


CASES = [
    ("generics", "typescript", "function identity<T>(value: T): T { return value; }"),
    ("closures", "python", "def multiplier(factor):\n    return lambda value: value * factor\n"),
    ("decorators", "python", "@cache\ndef read(key):\n    return load(key)\n"),
    ("async_await", "javascript", "async function load() { return await fetch('/api'); }"),
    ("generators", "python", "def rows():\n    yield from stream()\n"),
    ("context_managers", "python", "with open('input.txt') as stream:\n    data = stream.read()\n"),
    ("pattern_matching", "rust", "fn classify(x: Option<i32>) { match x { Some(v) => v, None => 0 }; }"),
    ("interfaces_traits", "go", "package io\ntype Reader interface { Read() error }\n"),
    ("inheritance", "java", "class Child extends Base implements Runnable { public void run() {} }"),
    ("error_handling", "typescript", "try { parse(input); } catch (error) { recover(error); }"),
    ("comprehensions", "python", "squares = [x*x for x in range(10) if x > 2]\n"),
    ("goroutines_channels", "go", "package main\nfunc run() { jobs := make(chan int); go worker(jobs); jobs <- 1 }\n"),
]


@pytest.mark.parametrize("pattern,lang,code", CASES)
def test_all_twelve_patterns_have_real_source_evidence(pattern, lang, code):
    src = code.encode()
    tree = get_parser(lang).parse(src)
    assert not tree.root_node.has_error, (lang, tree.root_node)
    matches = [p for p in lessons.extract_patterns(lang, src, tree) if p["pattern"] == pattern]
    assert matches, (pattern, lang)
    for match in matches:
        assert match["line"] >= 1 and match["end_line"] >= match["line"]
        assert match["evidence"] and len(match["evidence"]) <= 240
        assert match["owner"] and match["explanation"].startswith("In ")
        assert 0 < match["confidence"] <= 1


@pytest.mark.parametrize("lang,code,expected", [
    ("python", "def identity[T](value: T):\n    return value\n", "generics"),
    ("go", "package p\nfunc Identity[T any](value T) T { return value }", "generics"),
    ("rust", "fn identity<T>(value: T) -> T { value }", "generics"),
    ("java", "class Box<T> {}", "generics"),
    ("csharp", "class Box<T> {}", "generics"),
    ("cpp", "template <typename T> T identity(T x) { return x; }", "generics"),
    ("typescript", "@registered class Controller {}", "decorators"),
    ("java", "@Deprecated class Old {}", "decorators"),
    ("rust", "#[derive(Debug)] struct Point;", "decorators"),
    ("csharp", "[Serializable] class Value {}", "decorators"),
    ("python", "async def wait():\n    return 1\n", "async_await"),
    ("rust", "async fn wait() { work().await; }", "async_await"),
    ("csharp", "class C { async Task Wait() { await Work(); } }", "async_await"),
    ("typescript", "function* values() { yield 1; }", "generators"),
    ("csharp", "class C { IEnumerable<int> Values() { yield return 1; } }", "generators"),
    ("java", "class C { void read() { try (InputStream s = open()) { s.read(); } } }", "context_managers"),
    ("csharp", "class C { void Read() { using (var s = Open()) { s.Read(); } } }", "context_managers"),
    ("python", "match item:\n    case [x, y]: pass\n", "pattern_matching"),
    ("csharp", "class C { int F(int x) => x switch { 0 => 1, _ => 2 }; }", "pattern_matching"),
    ("typescript", "interface Reader { read(): string; }", "interfaces_traits"),
    ("rust", "trait Reader { fn read(&self); }", "interfaces_traits"),
    ("java", "interface Reader { void read(); }", "interfaces_traits"),
    ("csharp", "interface Reader { void Read(); }", "interfaces_traits"),
    ("python", "class Child(Base):\n    pass\n", "inheritance"),
    ("javascript", "class Child extends Base {}", "inheritance"),
    ("csharp", "class Child : Base {}", "inheritance"),
    ("cpp", "class Child : public Base {};", "inheritance"),
    ("python", "raise ValueError('bad input')\n", "error_handling"),
    ("rust", "fn read() -> Result<(), Error> { task()?; Ok(()) }", "error_handling"),
    ("java", "class C { void work() { throw new RuntimeException(); } }", "error_handling"),
    ("python", "values = (x*x for x in inputs)\n", "comprehensions"),
    ("go", "package p\nfunc receive(ch chan int) { x := <-ch; _ = x }", "goroutines_channels"),
])
def test_language_specific_grammars(lang, code, expected):
    assert expected in {p["pattern"] for p in lessons.extract_patterns(lang, code.encode())}


@pytest.mark.parametrize("lang,source", [
    ("python", '# @decorator async with yield match try\ntext = "lambda x: captured; class Child(Base); [x for x in xs]"\n'),
    ("typescript", '// class Child<T> extends Base; await work();\nconst text = "@decorator function* x() { yield 1; }";\n'),
    ("go", 'package main\n// go work(); ch <- 1\nvar text = "chan int; go worker()"\n'),
    ("rust", '// trait Reader; match x; async fn f()\nconst TEXT: &str = "|x| captured + x; value?";\n'),
    ("java", '// @Override class C<T> extends B\nclass C { String text = "try {} catch(Exception e) {}"; }'),
    ("csharp", '// [Attribute] class C<T> : B\nclass C { string text = "await Work(); using (x) {}"; }'),
    ("cpp", '// template<typename T> class X : Base\nconst char *text = "throw error; try {} catch (...) {}";'),
])
def test_no_detections_from_comments_or_strings(lang, source):
    assert lessons.extract_patterns(lang, source.encode()) == []


@pytest.mark.parametrize("lang,source", [
    ("python", "def make(factor):\n    return lambda value: value * factor\n"),
    ("python", "def outer(value):\n    def read():\n        return value\n    return read\n"),
    ("javascript", "function make(prefix) { return value => prefix + value; }"),
    ("typescript", "function make(prefix: string) { return (value: string) => prefix + value; }"),
    ("javascript", "function make() { const step = 2; return value => value + step; }"),
])
def test_closures_require_evidence_of_outer_binding(lang, source):
    closures = [p for p in lessons.extract_patterns(lang, source.encode()) if p["pattern"] == "closures"]
    assert len(closures) == 1
    assert closures[0]["confidence"] < 1
    assert "inferred" in closures[0]["explanation"]


@pytest.mark.parametrize("lang,source", [
    ("python", "value = 2\nf = lambda x: x + value\n"),  # module globals are not function captures
    ("python", "def outer(value):\n    return lambda value: value + 1\n"),
    ("python", "def outer(value):\n    return lambda other: other + 1\n"),
    ("python", "def outer(value):\n    def inner():\n        value = 1\n        return value\n    return inner\n"),
    ("python", "def outer(value):\n    def inner(obj):\n        return obj.value\n    return inner\n"),
    ("python", "def outer(value):\n    def inner():\n        global value\n        return value\n    return inner\n"),
    ("javascript", "function outer(value) { return value => value + 1; }"),
    ("javascript", "function outer(value) { return x => x.value; }"),
    ("typescript", "function outer(value: number) { return (value: number) => value + 1; }"),
])
def test_noncapturing_functions_are_not_closures(lang, source):
    assert not [p for p in lessons.extract_patterns(lang, source.encode()) if p["pattern"] == "closures"]


def test_existing_tree_is_reused_and_evidence_is_bounded(monkeypatch):
    src = ("@decorate(" + ", ".join("value" for _ in range(400)) + ")\ndef work():\n    pass\n").encode()
    tree = get_parser("python").parse(src)
    monkeypatch.setattr(lessons, "get_parser", lambda *_: pytest.fail("unexpected reparse"))
    found = lessons.extract_patterns("python", src, tree)
    assert found and len(found[0]["evidence"]) <= 240
    assert found == lessons.extract_patterns("python", src, tree)


def test_catalogue_exactly_twelve_and_unknown_languages():
    ids = {p["id"] for p in lessons.catalogue()}
    assert len(ids) == 12 and ids == {p[0] for p in CASES}
    assert lessons.extract_patterns("markdown", b"# async yield closures") == []


def test_store_replaces_rows_pages_and_cascades(tmp_path: Path):
    source = "@cache\ndef read():\n    yield 1\n\ndef more():\n    yield 2\n"
    path = tmp_path / "app.py"
    path.write_text(source, encoding="utf-8")
    ix = Index(tmp_path)
    try:
        ix.update()
        fid = ix.conn.execute("SELECT id FROM files WHERE path='app.py'").fetchone()[0]
        tree = get_parser("python").parse(source.encode())
        lessons.store_patterns(ix, fid, "python", source.encode(), tree)
        lessons.store_patterns(ix, fid, "python", source.encode(), tree)
        first = lessons.patterns(ix, path="app.py", limit=1)
        assert first["total"] == 3 and first["truncated"] and first["next_offset"] == 1
        second = lessons.patterns(ix, path="app.py", offset=1, limit=20)
        assert len(second["patterns"]) == 2 and second["next_offset"] is None
        only = lessons.patterns(ix, path="app.py", start_line=4, end_line=6)
        assert [p["line"] for p in only["patterns"]] == [6]
        assert lessons.patterns(ix, pattern="generators")["total"] == 2
        assert lessons.patterns(ix, path="app")["total"] == 0  # no substring path collisions
        assert lessons.patterns(ix, pattern="made_up")["error"]
        assert lessons.patterns(ix, path="app.py")["patterns"][0]["path"] == "app.py"
        lessons.store_patterns(ix, fid, "python", b"answer = 42\n")
        assert lessons.patterns(ix)["total"] == 0
        lessons.store_patterns(ix, fid, "python", source.encode(), tree)
        ix.conn.execute("DELETE FROM files WHERE id=?", (fid,))
        assert ix.conn.execute("SELECT COUNT(*) FROM code_patterns").fetchone()[0] == 0
    finally:
        ix.close()


@pytest.mark.parametrize("budget", [0, 1, 20, 100, 500])
def test_render_obeys_token_estimate_and_marks_truncation(budget):
    found = lessons.extract_patterns("python", b"@cache\ndef rows():\n    yield 1\n")
    for item in found:
        item["path"] = "app.py"
    result = {"patterns": found, "total": len(found), "truncated": False}
    output = lessons.render(result, token_budget=budget)
    assert output == "" if budget == 0 else estimate_tokens(output) <= budget
    if budget == 100:
        assert "More results" in output
    if budget == 500:
        assert "app.py:1" in output and "Generators" in output
