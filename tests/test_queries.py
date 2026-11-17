#!/usr/bin/env python3
"""Saved queries: the gx.queries.json store, its source keys, and its validation.

These checks call query_store and the server helpers directly. They start no
server and need no Neo4j or GraphQL endpoint. Every file lives in a temporary
folder. tests/test_graphql.py covers the HTTP routes and the headed page.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import query_store as qs  # noqa: E402
import server  # noqa: E402
from graphql_source import QueryRejected  # noqa: E402

FAILS = []
PASSWORD = "store-test-password-do-not-save"


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        shown = str(detail).replace(PASSWORD, "[redacted]")
        print(f"  FAIL {name}{(': ' + shown) if shown else ''}")
        FAILS.append(name)


def refused(fn, *exc_types):
    """Run fn and return the message of the expected error, or None when it passes."""
    try:
        fn()
    except exc_types as exc:  # noqa: PERF203
        return str(exc)
    return None


class FakeNeo4j:
    kind = "neo4j"

    def __init__(self, uri, db="neo4j"):
        self.uri = uri
        self.db = db
        self.id = "neo4j"
        self.label = "Neo4j"


class FakeGraphQL:
    kind = "graphql"

    def __init__(self, endpoint, adapter="wiz", project_id="*", label="Wiz"):
        self.endpoint = endpoint
        self.adapter_name = adapter
        self.project_id = project_id
        self.label = label
        self.id = "wiz"


def validate(kind, text):
    server.validate_query_body(kind, text)


CYPHER = "MATCH (a:AWSAccount)-[r:RESOURCE]->(b:S3Bucket) RETURN a, r, b LIMIT 5"
GRAPHQL = "query Q($projectId: String!) { graphSearch(projectId: $projectId, query: { type: [BUCKET] select: true }) { totalCount } }"


def test_source_keys():
    print("source keys")
    a = server.query_source(FakeNeo4j("bolt://db-a:7687"))
    a2 = server.query_source(FakeNeo4j("bolt://db-a:7687"))
    b = server.query_source(FakeNeo4j("bolt://db-b:7687"))
    other_db = server.query_source(FakeNeo4j("bolt://db-a:7687", "other"))
    check("one Neo4j connection gives one stable key", a["key"] == a2["key"] and a["kind"] == "cypher")
    check("the key is the first 16 hex of a sha256",
          len(a["key"]) == 16 and all(ch in "0123456789abcdef" for ch in a["key"]), a["key"])
    check("another URI or database is another connection",
          len({a["key"], b["key"], other_db["key"]}) == 3, (a, b, other_db))
    secret = server.query_source(FakeNeo4j(f"neo4j+s://neo4j:{PASSWORD}@db-a:7687"))
    check("the readable label has no user, password, or host",
          PASSWORD not in json.dumps(secret) and "db-a" not in secret["label"]
          and secret["label"] == "Neo4j / neo4j", secret)
    w1 = server.query_source(FakeGraphQL("https://api.example.test/graphql"))
    w2 = server.query_source(FakeGraphQL("https://api.example.test/graphql", project_id="p-2"))
    w3 = server.query_source(FakeGraphQL("https://api.other.test/graphql"))
    check("a GraphQL key covers the endpoint and the project",
          w1["kind"] == "graphql" and len({w1["key"], w2["key"], w3["key"]}) == 3, (w1, w2, w3))
    check("a Wiz label names no endpoint host",
          all("example.test" not in w["label"] and "other.test" not in w["label"] for w in (w1, w2, w3)),
          [w["label"] for w in (w1, w2, w3)])
    check("a generic GraphQL source cannot save queries",
          server.query_source(FakeGraphQL("https://api.example.test/graphql", adapter="generic")) is None)
    check("no connection cannot save queries", server.query_source(server.UnconfiguredNeo4j()) is None)


def test_paths():
    print("store path")
    saved = {key: os.environ.get(key) for key in ("GX_CONFIG", "GX_QUERIES")}
    tmp = tempfile.TemporaryDirectory()
    try:
        os.environ.pop("GX_QUERIES", None)
        os.environ["GX_CONFIG"] = str(Path(tmp.name) / "sub" / "gx.local.json")
        check("the store sits next to GX_CONFIG",
              server.queries_path() == Path(tmp.name) / "sub" / "gx.queries.json", server.queries_path())
        os.environ["GX_QUERIES"] = str(Path(tmp.name) / "elsewhere.json")
        check("GX_QUERIES overrides the store path",
              server.queries_path() == Path(tmp.name) / "elsewhere.json", server.queries_path())
        os.environ.pop("GX_CONFIG", None)
        os.environ.pop("GX_QUERIES", None)
        check("the default store is in the project folder", server.queries_path() == ROOT / "gx.queries.json")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tmp.cleanup()
    names = ["gx.queries.json", "gx.queries.json.tmp", "gx.queries.json.k2x9.tmp", "gx.queries.json.lock"]
    ignored = subprocess.run(["git", "check-ignore", *names], cwd=ROOT, capture_output=True, text=True)
    check("the store, its temp files, and its lock file are gitignored",
          ignored.returncode == 0 and ignored.stdout.split() == names, ignored.stdout or ignored.stderr)


def _builtins():
    return server.builtin_queries(FakeNeo4j("bolt://db-a:7687"))


def test_crud_and_isolation():
    print("create, update, delete, and isolation")
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gx.queries.json"
    neo_a = server.query_source(FakeNeo4j("bolt://db-a:7687"))
    neo_b = server.query_source(FakeNeo4j("bolt://db-b:7687"))
    builtins = _builtins()
    try:
        listed = qs.list_queries(path, neo_a, builtins)
        check("a missing store lists the built-ins and creates no file",
              [q["id"] for q in listed] == [f"builtin:cartography-queries:{q['id']}" for q in json.loads((ROOT / "data" / "cartography-queries.json").read_text())]
              and all(q["builtin"] is True and q["editable"] is False and q["kind"] == "cypher" for q in listed)
              and not path.exists(), [q["id"] for q in listed])
        made = qs.create(path, neo_a, {
            "name": "  Buckets  ", "description": "All buckets", "tags": [" aws ", "storage", "aws", ""],
            "kind": "cypher", "cypher": CYPHER,
        }, validate)
        check("create trims the name, cleans the tags, and sets an id and times",
              made["name"] == "Buckets" and made["tags"] == ["aws", "storage"] and made["kind"] == "cypher"
              and made["cypher"] == CYPHER and made["id"].startswith("q_") and len(made["id"]) == 18
              and made["created"] == made["updated"] and made["builtin"] is False and made["editable"] is True,
              made)
        listed = qs.list_queries(path, neo_a, builtins)
        check("the user query lists after the built-ins",
              listed[-1]["id"] == made["id"] and listed[0]["builtin"] is True, [q["id"] for q in listed])
        check("another Neo4j connection does not see it",
              made["id"] not in [q["id"] for q in qs.list_queries(path, neo_b, builtins)])
        mode = stat.S_IMODE(path.stat().st_mode)
        check("the store file is owner read and write only", mode == 0o600, oct(mode))
        on_disk = json.loads(path.read_text())
        bucket = on_disk["sources"].get(neo_a["key"]) or {}
        check("the file has the version 1 shape and a readable label",
              on_disk["version"] == 1 and set(bucket) >= {"label", "queries", "hidden_builtins"}
              and bucket["label"] == "Neo4j / neo4j" and "db-a" not in bucket["label"]
              and bucket["queries"][0]["id"] == made["id"], on_disk)
        import time as _time
        _time.sleep(1.05)
        changed = qs.update(path, neo_a, made["id"], {"name": "Buckets v2", "tags": ["s3"]}, validate, builtins)
        check("update keeps the id and the created time and moves the updated time",
              changed["query"]["id"] == made["id"] and changed["query"]["name"] == "Buckets v2"
              and changed["query"]["tags"] == ["s3"] and changed["query"]["cypher"] == CYPHER
              and changed["query"]["created"] == made["created"] and changed["query"]["updated"] > made["updated"]
              and changed["hid"] is None, changed)
        missing = refused(lambda: qs.update(path, neo_b, made["id"], {"name": "x"}, validate, builtins), qs.QueryNotFound)
        check("another connection cannot update the query", missing is not None)
        gone = qs.delete(path, neo_a, made["id"], builtins)
        check("delete returns the removed query for Undo",
              gone["builtin"] is False and gone["query"]["name"] == "Buckets v2"
              and made["id"] not in [q["id"] for q in qs.list_queries(path, neo_a, builtins)], gone)
        back = qs.create(path, neo_a, gone["query"], validate)
        check("Undo creates it again with the same id and created time",
              back["id"] == made["id"] and back["created"] == made["created"], back)
        twice = qs.create(path, neo_a, gone["query"], validate)
        check("a second create with a used id gets a new id", twice["id"] != made["id"], twice)
        check("delete of an unknown id is not found",
              refused(lambda: qs.delete(path, neo_a, "q_0000000000000000", builtins), qs.QueryNotFound) is not None)
    finally:
        tmp.cleanup()


def test_builtins_flow():
    print("built-in hide, edit copy, and restore")
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gx.queries.json"
    neo = server.query_source(FakeNeo4j("bolt://db-a:7687"))
    other = server.query_source(FakeNeo4j("bolt://db-b:7687"))
    builtins = _builtins()
    first = builtins[0]["id"]
    try:
        edited = qs.update(path, neo, first, {"name": "My EC2 view"}, validate, builtins)
        listed = qs.list_queries(path, neo, builtins)
        ids = [q["id"] for q in listed]
        check("editing a built-in saves a copy with a new id and hides the built-in",
              edited["hid"] == first and edited["query"]["id"] != first and first not in ids
              and edited["query"]["id"] in ids and edited["query"]["cypher"] == builtins[0]["cypher"]
              and edited["query"]["builtin"] is False, edited)
        check("the other connection still sees the built-in", first in [q["id"] for q in qs.list_queries(path, other, builtins)])
        second = builtins[1]["id"]
        hidden = qs.delete(path, neo, second, builtins)
        check("deleting a built-in hides it",
              hidden["builtin"] is True and second not in [q["id"] for q in qs.list_queries(path, neo, builtins)], hidden)
        one = qs.restore_builtins(path, neo, builtins, ids=[second])
        ids = [q["id"] for q in qs.list_queries(path, neo, builtins)]
        check("restore with one id brings that built-in back", one["restored"] == 1 and second in ids and first not in ids, one)
        every = qs.restore_builtins(path, neo, builtins)
        ids = [q["id"] for q in qs.list_queries(path, neo, builtins)]
        check("restore brings every hidden built-in back and keeps the user copy",
              every["restored"] == 1 and first in ids and edited["query"]["id"] in ids
              and ids.index(first) < ids.index(edited["query"]["id"]), ids)
        again = qs.restore_builtins(path, neo, builtins)
        check("a second restore has nothing to do", again["restored"] == 0, again)
        bucket = json.loads(path.read_text())["sources"][neo["key"]]
        check("the file keeps built-ins out of the user list", all(not q["id"].startswith("builtin:") for q in bucket["queries"]))
    finally:
        tmp.cleanup()


def test_validation():
    print("validation")
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gx.queries.json"
    neo = server.query_source(FakeNeo4j("bolt://db-a:7687"))
    wiz = server.query_source(FakeGraphQL("https://api.example.test/graphql"))
    try:
        try:
            server.check_read_only("MATCH (n) DETACH DELETE n")
            runtime = ""
        except PermissionError as exc:
            runtime = str(exc)
        write = refused(lambda: qs.create(path, neo, {"name": "w", "cypher": "MATCH (n) DETACH DELETE n"}, validate), PermissionError)
        check("a Cypher write is refused with the run-time message", write and write == runtime, (write, runtime))
        try:
            server.validate_query_body("graphql", "mutation { deleteAll { id } }")
            runtime_gql = ""
        except QueryRejected as exc:
            runtime_gql = str(exc)
        mutation = refused(lambda: qs.create(path, wiz, {"name": "m", "graphql": "mutation { deleteAll { id } }"}, validate), QueryRejected)
        check("a GraphQL mutation is refused with the run-time message",
              mutation and mutation == runtime_gql and "not sent" in mutation, (mutation, runtime_gql))
        wrong = refused(lambda: qs.create(path, wiz, {"name": "c", "kind": "cypher", "cypher": CYPHER}, validate), qs.QueryStoreError)
        check("a Cypher query is refused on a GraphQL connection", wrong and "Cypher" in wrong, wrong)
        wrong2 = refused(lambda: qs.create(path, neo, {"name": "g", "graphql": GRAPHQL}, validate), qs.QueryStoreError)
        check("a GraphQL query is refused on Neo4j", wrong2 and "GraphQL" in wrong2, wrong2)
        big = refused(lambda: qs.create(path, neo, {"name": "b", "cypher": "RETURN 1 //" + "x" * (100 * 1024)}, validate), qs.QueryStoreError)
        check("a body over 100 KB is refused", big and "100 KB" in big, big)
        bad_vars = refused(lambda: qs.create(path, wiz, {"name": "v", "graphql": GRAPHQL, "variables": "{not json"}, validate), qs.QueryStoreError)
        check("variables that are not JSON are refused", bad_vars and "JSON object" in bad_vars, bad_vars)
        list_vars = refused(lambda: qs.create(path, wiz, {"name": "v", "graphql": GRAPHQL, "variables": [1, 2]}, validate), qs.QueryStoreError)
        check("variables that are not an object are refused", list_vars and "JSON object" in list_vars, list_vars)
        text_vars = qs.create(path, wiz, {"name": "v", "graphql": GRAPHQL, "variables": '{"projectId": "*"}'}, validate)
        check("variables sent as JSON text are stored as an object", text_vars["variables"] == {"projectId": "*"}, text_vars)
        no_name = refused(lambda: qs.create(path, neo, {"name": "   ", "cypher": CYPHER}, validate), qs.QueryStoreError)
        long_name = refused(lambda: qs.create(path, neo, {"name": "n" * 121, "cypher": CYPHER}, validate), qs.QueryStoreError)
        long_desc = refused(lambda: qs.create(path, neo, {"name": "d", "description": "d" * 2001, "cypher": CYPHER}, validate), qs.QueryStoreError)
        bad_tags = refused(lambda: qs.create(path, neo, {"name": "t", "tags": "aws", "cypher": CYPHER}, validate), qs.QueryStoreError)
        empty = refused(lambda: qs.create(path, neo, {"name": "e", "cypher": "   "}, validate), qs.QueryStoreError)
        check("name, description, tags, and an empty body are checked",
              all([no_name, long_name, long_desc, bad_tags, empty]) and "120" in long_name and "2000" in long_desc,
              (no_name, long_name, long_desc, bad_tags, empty))
        on_disk = json.loads(path.read_text())
        saved = [q["name"] for bucket in on_disk["sources"].values() for q in bucket["queries"]]
        check("refused queries are not written", saved == ["v"], saved)
    finally:
        tmp.cleanup()


def test_file_safety():
    print("atomic write, file limit, and a broken file")
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gx.queries.json"
    neo = server.query_source(FakeNeo4j("bolt://db-a:7687"))
    builtins = _builtins()
    real_replace = os.replace
    try:
        qs.create(path, neo, {"name": "kept", "cypher": CYPHER}, validate)
        before = path.read_text()

        def broken_replace(src, dst):
            raise OSError("disk full")

        os.replace = broken_replace
        try:
            failed = refused(lambda: qs.create(path, neo, {"name": "lost", "cypher": CYPHER}, validate), qs.QueryStoreError)
        finally:
            os.replace = real_replace
        leftovers = sorted(p.name for p in Path(tmp.name).iterdir())
        check("a failed replace leaves the old file and no temp file",
              failed and "could not write" in failed and path.read_text() == before
              and [name for name in leftovers if name != "gx.queries.json.lock"] == ["gx.queries.json"], (failed, leftovers))
        old_max = qs.FILE_MAX
        qs.FILE_MAX = len(before.encode()) + 40
        try:
            full = refused(lambda: qs.create(path, neo, {"name": "too much", "cypher": CYPHER}, validate), qs.QueryStoreError)
        finally:
            qs.FILE_MAX = old_max
        check("a write past the file limit is refused", full and "5 MB" in full and path.read_text() == before, full)
        path.write_text("{not json")
        broken = refused(lambda: qs.create(path, neo, {"name": "x", "cypher": CYPHER}, validate), qs.QueryStoreError)
        listed = qs.list_queries(path, neo, builtins)
        check("a broken file is left unchanged and still lists the built-ins",
              broken and "left unchanged" in broken and path.read_text() == "{not json"
              and [q["id"] for q in listed] == [q["id"] for q in builtins], broken)
        path.write_text(json.dumps({"version": 9, "sources": {}}))
        future = refused(lambda: qs.create(path, neo, {"name": "x", "cypher": CYPHER}, validate), qs.QueryStoreError)
        check("a file with another version is left unchanged", future and "version" in future, future)
        os.chmod(path, 0o644)
        path.write_text(json.dumps({"version": 1, "sources": {}}))
        qs.create(path, neo, {"name": "x", "cypher": CYPHER}, validate)
        check("a rewrite sets the file mode back to 0600", stat.S_IMODE(path.stat().st_mode) == 0o600,
              oct(stat.S_IMODE(path.stat().st_mode)))
    finally:
        os.replace = real_replace
        tmp.cleanup()


def test_import_export():
    print("import and export")
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gx.queries.json"
    wiz_a = server.query_source(FakeGraphQL("https://api.a.test/graphql"))
    wiz_b = server.query_source(FakeGraphQL("https://api.b.test/graphql"))
    try:
        qs.create(path, wiz_a, {"name": "Buckets", "tags": ["s3"], "description": "d",
                                "graphql": GRAPHQL, "variables": {"projectId": "*"}}, validate)
        qs.create(path, wiz_a, {"name": "Second", "graphql": GRAPHQL}, validate)
        exported = qs.export_queries(path, wiz_a)
        check("export lists the user queries with their bodies",
              [q["name"] for q in exported] == ["Buckets", "Second"]
              and exported[0]["graphql"] == GRAPHQL and exported[0]["variables"] == {"projectId": "*"}
              and exported[0]["kind"] == "graphql" and "builtin" not in exported[0], exported)
        moved = qs.import_queries(path, wiz_b, json.loads(json.dumps(exported)) + [
            {"name": "bad", "graphql": "mutation { x { id } }"},
            {"name": "cypher", "kind": "cypher", "cypher": CYPHER},
            "not an object",
        ], validate)
        reasons = {item["index"]: item for item in moved["skipped"]}
        check("import adds the valid entries and says why it skipped the rest",
              moved["added"] == 2 and sorted(reasons) == [2, 3, 4]
              and "not sent" in reasons[2]["reason"] and "Cypher" in reasons[3]["reason"]
              and reasons[2]["code"] == "invalid" and reasons[2]["name"] == "bad", moved)
        round_trip = qs.export_queries(path, wiz_b)
        strip = lambda q: {k: q[k] for k in ("name", "description", "tags", "kind", "graphql", "variables")}  # noqa: E731
        check("the round trip keeps every field", [strip(q) for q in round_trip] == [strip(q) for q in exported], round_trip)
        again = qs.import_queries(path, wiz_b, exported, validate)
        check("importing the same entries again adds nothing",
              again["added"] == 0 and [item["code"] for item in again["skipped"]] == ["duplicate", "duplicate"], again)
        not_list = refused(lambda: qs.import_queries(path, wiz_b, {"name": "x"}, validate), qs.QueryStoreError)
        check("an import that is not a list is refused", not_list and "array" in not_list, not_list)
    finally:
        tmp.cleanup()


def test_review_fixes():
    print("non-finite variables, stable built-in ids, copy delete, import size, two writers")
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gx.queries.json"
    wiz = server.query_source(FakeGraphQL("https://api.example.test/graphql"))
    neo = server.query_source(FakeNeo4j("bolt://db-a:7687"))
    builtins = _builtins()
    try:
        for text in ('{"first": NaN}', '{"first": Infinity}', '{"first": -Infinity}', '{"first": 1e400}'):
            bad = refused(lambda: qs.create(path, wiz, {"name": "n", "graphql": GRAPHQL, "variables": text}, validate),
                          qs.QueryStoreError)
            check(f"variables text {text} is refused", bad and "JSON object" in bad, bad)
        nested = refused(lambda: qs.create(path, wiz, {"name": "n", "graphql": GRAPHQL,
                                                       "variables": {"a": [1, {"b": float("nan")}]}}, validate),
                         qs.QueryStoreError)
        check("a NaN inside a variables object is refused", nested is not None, nested)
        check("refused variables are not written", not path.exists())
        listed = qs.list_queries(path, wiz, [])
        check("the list stays valid JSON for the page", json.dumps(listed, allow_nan=False) is not None)

        ids = [q["id"] for q in builtins]
        check("built-in ids come from the entry id, not its place in the file",
              ids[0] == "builtin:cartography-queries:internet-exposed-ec2" and len(set(ids)) == len(ids), ids)
        real = (server.ROOT / "data" / "cartography-queries.json").read_text()
        entries = json.loads(real)
        moved = Path(tmp.name) / "data"
        moved.mkdir()
        (moved / "cartography-queries.json").write_text(json.dumps([{"name": "A new first query", "cypher": CYPHER}] + entries))
        old_root = server.ROOT
        server.ROOT = Path(tmp.name)
        try:
            shifted = server.builtin_queries(FakeNeo4j("bolt://db-a:7687"))
        finally:
            server.ROOT = old_root
        check("a new entry at the top of the file does not move the other ids",
              [q["id"] for q in shifted[1:]] == ids and shifted[0]["id"] == "builtin:cartography-queries:a-new-first-query",
              [q["id"] for q in shifted[:3]])
        twins = [{"name": "Same name", "cypher": CYPHER}, {"name": "Same name", "cypher": CYPHER}]
        (moved / "cartography-queries.json").write_text(json.dumps(twins))
        server.ROOT = Path(tmp.name)
        try:
            pair = [q["id"] for q in server.builtin_queries(FakeNeo4j("bolt://db-a:7687"))]
        finally:
            server.ROOT = old_root
        check("two built-ins with one name get two ids", pair == ["builtin:cartography-queries:same-name", "builtin:cartography-queries:same-name-2"], pair)

        first = builtins[0]["id"]
        edited = qs.update(path, neo, first, {"name": "My EC2 copy"}, validate, builtins)
        copy = edited["query"]
        check("the copy names the built-in it replaces", copy.get("replaces") == first, copy)
        gone = qs.delete(path, neo, copy["id"], builtins)
        ids_after = [q["id"] for q in qs.list_queries(path, neo, builtins)]
        check("deleting the copy of a built-in shows the built-in again",
              first in ids_after and (gone.get("restored_builtin") or {}).get("id") == first
              and gone["restored_builtin"]["name"] == builtins[0]["name"], gone)
        back = qs.create(path, neo, gone["query"], validate, builtins)
        ids_undo = [q["id"] for q in qs.list_queries(path, neo, builtins)]
        check("Undo of that delete brings the copy back and hides the built-in again",
              back["id"] == copy["id"] and first not in ids_undo and back.get("replaces") == first, ids_undo)
        check("export leaves out the replaces link",
              all("replaces" not in q for q in qs.export_queries(path, neo)))
        hidden = qs.delete(path, neo, builtins[1]["id"], builtins)
        plain = qs.create(path, neo, {"name": "plain", "cypher": CYPHER}, validate, builtins)
        gone_plain = qs.delete(path, neo, plain["id"], builtins)
        check("deleting a plain user query shows no built-in",
              gone_plain.get("restored_builtin") is None
              and builtins[1]["id"] not in [q["id"] for q in qs.list_queries(path, neo, builtins)], (hidden, gone_plain))

        big = [{"name": f"q{i}", "cypher": "RETURN 1"} for i in range(qs.IMPORT_MAX + 1)]
        over = refused(lambda: qs.import_queries(path, neo, big, validate), qs.QueryStoreError)
        check("an import over the entry cap is refused before it runs", over and str(qs.IMPORT_MAX) in over, over)
        import time as _time
        many = [{"name": f"q{i}", "cypher": f"RETURN {i}"} for i in range(qs.IMPORT_MAX)]
        started = _time.perf_counter()
        done = qs.import_queries(path, neo, many, validate)
        again = qs.import_queries(path, neo, many, validate)
        took = _time.perf_counter() - started
        check("an import of 1,000 entries and its duplicate pass stay fast",
              done["added"] == qs.IMPORT_MAX and again["added"] == 0 and took < 3.0, {"took": took})

        shared = Path(tmp.name) / "shared" / "gx.queries.json"
        script = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import query_store as qs\n"
            "src = {'key': 'k', 'kind': 'cypher', 'label': 'L'}\n"
            "for i in range(40):\n"
            "    qs.create(sys.argv[1], src, {'name': sys.argv[2] + str(i), 'cypher': 'RETURN ' + str(i) + ' // ' + 'x' * (i * 50 + int(sys.argv[3]))}, lambda k, t: None)\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", script, str(shared), tag, pad])
                 for tag, pad in (("a", "0"), ("b", "3000"), ("c", "7"))]
        codes = [proc.wait(timeout=120) for proc in procs]
        names = []
        try:
            names = [q["name"] for q in json.loads(shared.read_text())["sources"]["k"]["queries"]]
        except Exception as exc:  # noqa: BLE001
            names = [f"unreadable: {exc}"]
        leftovers = sorted(p.name for p in shared.parent.iterdir() if p.name.endswith(".tmp"))
        check("three processes that save at once keep one valid file and every query",
              codes == [0, 0, 0] and len(names) == 120 and len(set(names)) == 120 and not leftovers,
              {"codes": codes, "count": len(names), "leftovers": leftovers})
    finally:
        tmp.cleanup()


def main():
    test_source_keys()
    test_paths()
    test_crud_and_isolation()
    test_builtins_flow()
    test_validation()
    test_file_safety()
    test_import_export()
    test_review_fixes()
    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print("\nall saved-query checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
