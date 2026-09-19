import os
from pathlib import Path

import pytest

from codeatlas.indexer import Index
from codeatlas.relationships import relationships, render, sync_relationships


@pytest.fixture
def ix(tmp_path):
    index = Index(tmp_path)
    yield index
    index.close()


def put(ix, path, text):
    target = ix.repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_code_config_schema_and_evidence(ix):
    put(ix, "src/app.py", 'data = open("config/app.yaml").read()\nschema = open("schemas/user.schema.json").read()\n')
    put(ix, "config/app.yaml", 'schema: ../schemas/user.schema.json\npassword: "secret-password.json"\n')
    put(ix, "schemas/user.schema.json", '{"$ref":"common.schema.json#/$defs/user"}')
    put(ix, "schemas/common.schema.json", '{"$defs": {"user": {"type":"string"}}}')
    ix.update()
    sync_relationships(ix)
    result = relationships(ix, "schemas/user.schema.json", direction="in")
    assert {r["source_path"] for r in result["items"]} == {"src/app.py", "config/app.yaml"}
    assert all(r["resolved"] and r["line"] > 0 for r in result["items"])
    refs = relationships(ix, kind="schema_ref")["items"]
    assert refs[0]["target_path"] == "schemas/common.schema.json"
    assert "secret-password" not in str(relationships(ix))


def test_incremental_config_only_and_missing_targets(ix):
    put(ix, "settings.json", '{"schema":"schema.json"}')
    sync_relationships(ix)
    assert relationships(ix)["items"][0]["resolved"] is False
    put(ix, "schema.json", '{}')
    stats = sync_relationships(ix, paths=["schema.json"])
    assert stats["scanned"] == 1
    assert "schema.json" in stats["changed_files"]
    assert relationships(ix, "schema.json", direction="in")["items"][0]["resolved"] is True
    (ix.repo / "schema.json").unlink()
    sync_relationships(ix, paths=["schema.json"])
    assert relationships(ix, "schema.json", direction="in")["items"][0]["resolved"] is False
    (ix.repo / "settings.json").unlink()
    sync_relationships(ix, paths=["settings.json"])
    assert relationships(ix)["total"] == 0


def test_json_yaml_toml_proto_graphql(ix):
    put(ix, "tsconfig.json", '{"extends":"./base.json", "files":["./entry.ts"]}')
    put(ix, "config.toml", 'schema_path = "schema.graphql"\npassword = "fake-secret.json"\n')
    put(ix, "config.yml", 'includes:\n  - ./base.json\nsecrets:\n  config: ./private.json\n')
    put(ix, "messages.proto", 'syntax = "proto3";\nimport public "base.proto";\n')
    put(ix, "schema.graphql", '# import User from "./types.graphql"\ntype Query { user: User }\n')
    sync_relationships(ix)
    rows = relationships(ix, limit=100)["items"]
    assert {(r["source_path"], r["target_path"]) for r in rows} == {
        ("tsconfig.json", "base.json"), ("tsconfig.json", "entry.ts"),
        ("config.toml", "schema.graphql"), ("config.yml", "base.json"),
        ("messages.proto", "base.proto"), ("schema.graphql", "types.graphql")}


def test_nested_ignores_outside_paths_and_source_comments(ix):
    put(ix, "pkg/.gitignore", 'ignored.json\n')
    put(ix, "pkg/ignored.json", '{"schema":"schema.json"}')
    put(ix, "pkg/config.json", '{"files":["ignored.json","../../outside.json","https://example.com/a.json"]}')
    put(ix, "src/app.py", '# open("fake.json")\ntext = "open(\'fake.json\')"\nf = open("written.json", "w")\n')
    ix.update()
    sync_relationships(ix)
    sync_relationships(ix, paths=["pkg/ignored.json"])
    assert relationships(ix)["total"] == 0


def test_cache_noop_and_query_no_source_reads(ix, monkeypatch):
    put(ix, "settings.json", '{"schema":"schema.json"}')
    sync_relationships(ix)
    original = Path.read_bytes

    def reject(path):
        raise AssertionError(f"unexpected content read: {path}")

    monkeypatch.setattr(Path, "read_bytes", reject)
    assert sync_relationships(ix)["unchanged"] == 1
    assert relationships(ix)["total"] == 1
    monkeypatch.setattr(Path, "read_bytes", original)
    assert sync_relationships(ix, paths=["settings.json"])["unchanged"] == 1


def test_targeted_edit_with_preserved_stat_and_no_unrelated_reads(ix, monkeypatch):
    put(ix, "a.json", '{"schema":"old.json"}')
    put(ix, "b.json", '{"schema":"other.json"}')
    sync_relationships(ix)
    old = (ix.repo / "a.json").stat()
    put(ix, "a.json", '{"schema":"new.json"}')
    os.utime(ix.repo / "a.json", ns=(old.st_atime_ns, old.st_mtime_ns))
    read = Path.read_bytes
    visited = []

    def tracked(path):
        visited.append(path.name)
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", tracked)
    result = sync_relationships(ix, paths=["a.json"])
    assert visited == ["a.json"]
    assert result["changed_files"] == ["a.json"]
    assert relationships(ix, path="a.json", direction="out")["items"][0]["target_path"] == "new.json"


def test_source_imports_jsonc_and_invalid_config_coverage(ix):
    put(ix, "app.ts", 'import config from "./app.jsonc";\nconst schema = require("./user.schema.json");\n')
    put(ix, "app.jsonc", '{ // comment\n "schema": "./user.schema.json",\n}')
    put(ix, "user.schema.json", '{}')
    ix.update()
    sync_relationships(ix)
    imports = relationships(ix, "app.ts", direction="out")["items"]
    assert {r["kind"] for r in imports} == {"loads_config", "uses_schema"}
    assert relationships(ix, "app.jsonc", direction="out")["items"][0]["line"] == 2
    put(ix, "app.jsonc", '{"schema": "do-not-leak-this-secret", invalid}')
    result = sync_relationships(ix, paths=["app.jsonc"])
    assert result["errors"] and "do-not-leak" not in str(result)
    assert relationships(ix, "app.jsonc", direction="out")["total"] == 0
    assert relationships(ix)["coverage"]["error"] == 1
    assert sync_relationships(ix)["changed_files"] == []


def test_bounds_and_transaction_ownership(ix):
    put(ix, "a.json", '{"files":["b.json","c.json","d.json"]}')
    ix.conn.execute("INSERT INTO meta VALUES('pending','yes')")
    sync_relationships(ix)
    assert ix.conn.in_transaction
    result = relationships(ix, limit=1)
    assert result["total"] == 3 and result["truncated"] and result["next_offset"] == 1
    assert len(render(result, token_budget=60)) <= 180
    assert len(relationships(ix, limit=1, offset=1)["items"]) == 1
    with pytest.raises(ValueError):
        relationships(ix, path="../outside.json")
    with pytest.raises(ValueError):
        relationships(ix, direction="sideways")


def test_env_entity_definitions_and_uses_never_store_values(ix):
    put(ix, ".env.example", 'DATABASE_URL=super-secret-database-url\nexport API_TOKEN="secret-token-value"\n')
    put(ix, "worker.py", 'import os\ndef connect():\n    return os.getenv("DATABASE_URL"), os.environ["API_TOKEN"]\n')
    put(ix, "client.ts", 'export function connect() { return process.env.DATABASE_URL + process.env["API_TOKEN"]; }\n')
    ix.update()
    result = relationships(ix, path=".env.example", direction="in", kind="uses_env")
    assert {r["source_path"] for r in result["items"]} == {"worker.py", "client.ts"}
    assert all(r["resolved"] and "DATABASE_URL" in r["evidence"] for r in result["items"])
    for table in ("ca_artifact_files", "ca_artifact_entities", "ca_entity_links", "ca_relationships"):
        data = str([tuple(row) for row in ix.conn.execute(f"SELECT * FROM {table}")])
        assert "super-secret" not in data and "secret-token-value" not in data


def test_removed_env_key_keeps_incoming_evidence_without_rereading_consumers(ix, monkeypatch):
    put(ix, ".env", 'DATABASE_URL=hidden\nKEEP=true\n')
    put(ix, "worker.py", 'import os\ndef connect():\n    return os.getenv("DATABASE_URL")\n')
    ix.update()
    original = Path.read_bytes
    reads = []

    def tracked(path):
        reads.append(path.name)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", tracked)
    put(ix, ".env", 'KEEP=true\n')
    ix.update(paths=[".env"])
    assert reads == [".env"]
    missing = relationships(ix, ".env", kind="uses_env", direction="in")["items"]
    assert len(missing) == 1 and missing[0]["resolved"] is False
    reads.clear()
    put(ix, ".env", 'DATABASE_URL=new-hidden-value\n')
    ix.update(paths=[".env"])
    assert reads == [".env"]
    assert relationships(ix, ".env", kind="uses_env", direction="in")["items"][0]["resolved"]


def test_sql_tables_link_queries_and_migrations_to_schema(ix):
    put(ix, "schema.sql", 'CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);\nCREATE TABLE orders (id INTEGER);\n')
    put(ix, "worker.py", 'def fetch(db):\n    return db.execute("SELECT name FROM users WHERE name = \'secret-query-value\'")\n')
    put(ix, "migrations/002.sql", 'INSERT INTO users (id, name) VALUES (1, \'hidden-name\');\nUPDATE orders SET id=2;\n')
    ix.update()
    result = relationships(ix, "schema.sql", direction="in", kind="uses_table")
    assert {r["source_path"] for r in result["items"]} == {"worker.py", "migrations/002.sql"}
    assert {r["line"] for r in result["items"] if r["source_path"] == "migrations/002.sql"} == {1, 2}
    assert all(r["resolved"] and r["confidence"] == "medium" for r in result["items"])
    assert "secret-query-value" not in str(result) and "hidden-name" not in str(result)


def test_entity_scope_ambiguity_dynamic_reads_and_comments(ix):
    for project in ("one", "two"):
        put(ix, f"{project}/pyproject.toml", '[project]\nname="' + project + '"\n')
        put(ix, f"{project}/.env.example", "DATABASE_URL=hidden\n")
        put(ix, f"{project}/schema.sql", "CREATE TABLE users (id INTEGER);\n")
        put(ix, f"{project}/worker.py", 'import os\ndef fetch(db):\n    os.getenv("DATABASE_URL")\n    return db.execute("SELECT * FROM users")\n')
    put(ix, "one/noise.py", '''import os
# os.getenv("DATABASE_URL")
text = "process.env.DATABASE_URL"
dynamic = os.getenv("DATABASE_URL" + suffix)
description = "SELECT * FROM users"
def dynamic_query(db, table):
    return db.execute(f"SELECT * FROM {table}")
''')
    ix.update()
    rows = relationships(ix, limit=100)["items"]
    assert len([r for r in rows if r["kind"] in {"uses_env", "uses_table"}]) == 4
    assert all(r["source_path"].split("/")[0] == r["target_path"].split("/")[0] for r in rows)
    put(ix, "one/duplicate.sql", "CREATE TABLE users (id INTEGER);\n")
    ix.update(paths=["one/duplicate.sql"])
    ambiguous = relationships(ix, "one/worker.py", kind="uses_table", direction="out")["items"]
    assert len(ambiguous) == 1 and ambiguous[0]["resolved"] is False
    assert relationships(ix, "two/worker.py", kind="uses_table")["items"][0]["resolved"]


def test_sql_qualified_name_does_not_bind_other_schema_and_cte_is_not_table(ix):
    put(ix, "schema.sql", "CREATE TABLE billing.users (id INTEGER);\nCREATE TABLE auth.users (id INTEGER);\n")
    put(ix, "worker.py", '''def fetch(db):
    db.query("SELECT * FROM billing.users")
    db.query("SELECT * FROM users")
    db.query("WITH users AS (SELECT 1) SELECT * FROM users")
''')
    ix.update()
    rows = relationships(ix, "worker.py", kind="uses_table")["items"]
    assert len(rows) == 1 and rows[0]["line"] == 2 and "billing.users" in rows[0]["evidence"]
