#!/usr/bin/env python3
"""GraphQL source: queries only, introspection, paging, and the source switch.

The live section starts a mock GraphQL API and a second explorer on free ports.
It does not wipe Neo4j and it does not use ports 8744 or 8755. If Neo4j is down,
the switch still runs and the count comparison is skipped.
"""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import graphql_source as gql  # noqa: E402
import server  # noqa: E402
from graphql_source import (  # noqa: E402
    INTROSPECTION,
    GraphQLCallError,
    GraphQLClient,
    GraphQLConfigError,
    GraphQLSource,
    QueryRejected,
    RateLimited,
    TokenExpired,
    assert_query_only,
    mutation_field_names,
    operation_types,
    resolve_graphql_sources,
    schema_from_introspection,
    sources_from_config,
)

TOKEN = "graphql-test-token-do-not-log"
FAILS = []
BUSY_PORTS = {8744, 8755}


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        shown = str(detail).replace(TOKEN, "[redacted]")
        print(f"  FAIL {name}{(': ' + shown) if shown else ''}")
        FAILS.append(name)


def safe(text):
    return str(text).replace(TOKEN, "[redacted]")


class Resp:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body if isinstance(body, bytes) else body.encode()

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code, body, headers=None):
    payload = body if isinstance(body, bytes) else str(body).encode()
    return urllib.error.HTTPError(
        "https://api.example.test/graphql", code, "error", headers or {}, io.BytesIO(payload))


def client(opener, **kwargs):
    kwargs.setdefault("token", TOKEN)
    kwargs.setdefault("backoff", 0)
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("result_ttl", 60)
    kwargs.setdefault("schema_ttl", 600)
    return GraphQLClient("https://api.example.test/graphql", opener=opener, **kwargs)


def named(kind, name):
    return {"kind": kind, "name": name, "ofType": None}


def non_null(inner):
    return {"kind": "NON_NULL", "name": None, "ofType": inner}


def listing(inner):
    return {"kind": "LIST", "name": None, "ofType": inner}


def field(name, type_ref, args=None):
    return {"name": name, "args": args or [], "type": type_ref}


def introspection_payload():
    string = named("SCALAR", "String")
    gadget = named("OBJECT", "Gadget")
    widget = named("OBJECT", "Widget")
    return {"data": {"__schema": {
        "queryType": {"name": "Query"},
        "mutationType": {"name": "Mutation"},
        "types": [
            {"kind": "OBJECT", "name": "__Schema", "fields": [field("types", string)],
             "enumValues": None, "inputFields": None, "possibleTypes": None},
            {"kind": "SCALAR", "name": "String", "fields": None},
            {"kind": "OBJECT", "name": "Widget", "fields": [
                field("id", non_null(named("SCALAR", "ID"))),
                field("name", string),
                field("tags", non_null(listing(non_null(string)))),
                field("owner", widget),
                field("gadget", non_null(listing(non_null(gadget))), [
                    {"name": "first", "type": named("SCALAR", "Int")},
                    {"name": "after", "type": string},
                ]),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
            {"kind": "OBJECT", "name": "Gadget", "fields": [
                field("id", non_null(named("SCALAR", "ID"))),
                field("widget", widget),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
            {"kind": "OBJECT", "name": "Account", "fields": [
                field("id", non_null(named("SCALAR", "ID"))),
                field("name", string),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
            {"kind": "OBJECT", "name": "Query", "fields": [
                field("graphSearch", named("OBJECT", "GraphSearchResult")),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
            {"kind": "OBJECT", "name": "GraphSearchResult", "fields": [
                field("totalCount", named("SCALAR", "Int")),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
            {"kind": "ENUM", "name": "Cloud", "fields": None,
             "enumValues": [{"name": "AWS"}, {"name": "GCP"}], "inputFields": None, "possibleTypes": None},
            {"kind": "OBJECT", "name": "Mutation", "fields": [
                field("updateSecretInstance", widget),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
        ],
    }}}


def graph_page(entities, has_next, cursor, total):
    return {"data": {"graphSearch": {
        "totalCount": total,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": [{"entities": entities}],
    }}}


def test_mutations():
    print("mutations")
    calls = []

    def opener(request, timeout=None):
        calls.append(1)
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    bare = client(opener, token=None, token_env="GRAPHQL_TOKEN_MISSING_FOR_TEST")
    for document in ("mutation { updateSecretInstance(id: \"1\") { id } }", "subscription { watch }"):
        try:
            bare.execute(document)
            check(f"refuses {document.split()[0]} before any request", False, "no error")
        except QueryRejected as exc:
            check(f"refuses {document.split()[0]} before any request",
                  "not sent" in str(exc) and not calls, safe(exc))
        except GraphQLConfigError as exc:
            check(f"refuses {document.split()[0]} before any request", False, safe(exc))
    allowed = [
        'query { field(name: "mutation") { id } }',
        "query { field }\n# mutation { dropAll }\n",
        'query { field(note: """mutation { dropAll }""") { id } }',
        'query Q($f: Filter = {name: "mutation"}) { field }',
        "query { mutation }",
        "{ field }",
    ]
    for document in allowed:
        ops = operation_types(document)
        try:
            assert_query_only(document)
            ok = True
        except QueryRejected as exc:
            ok = False
            ops = f"{ops} {exc}"
        check("a query that only mentions mutation in a string, comment, or field name is kept",
              ok and ops == ["query"], safe(ops))
    check("the introspection document is one query", operation_types(INTROSPECTION) == ["query"],
          repr(operation_types(INTROSPECTION)))
    def schema_opener(request, timeout=None):
        calls.append(json.loads(request.data.decode())["query"][:40])
        return Resp(200, json.dumps(introspection_payload()).encode())

    learned = client(schema_opener)
    learned.execute(INTROSPECTION)
    before = len(calls)
    try:
        learned.execute("query { updateSecretInstance { id } }")
        check("a root mutation field is refused after introspection", False, "sent")
    except QueryRejected as exc:
        check("a root mutation field is refused after introspection",
              "updateSecretInstance" in str(exc) and len(calls) == before, safe(exc))
    learned.execute("query { widget { updateSecretInstance } }")
    check("a nested field with the same name is a property, so it is sent", len(calls) == before + 1)
    learned.cache.drop(learned._key(INTROSPECTION, {}))
    learned.execute(INTROSPECTION)
    check("introspection still runs after mutation fields are known", len(calls) == before + 2)


def test_introspection_model():
    print("introspection model")
    payload = introspection_payload()
    model = schema_from_introspection(payload)
    names = set(model["categories"])
    check("every user type is present and introspection types are left out",
          {"Widget", "Gadget", "Account", "Query", "Mutation", "GraphSearchResult", "Cloud"} <= names
          and "__Schema" not in names and "String" not in names,
          sorted(names))
    widget = model["categories"]["Widget"]["props"]
    check("scalars print their wrappers and object fields become links",
          widget["tags"] == "[String!]!" and widget["owner"] == "-> Widget"
          and widget["gadget"].startswith("-> Gadget")
          and any(link == {"source": "Widget", "type": "owner", "target": "Widget"} for link in model["links"])
          and any(link["type"] == "gadget" and link["target"] == "Gadget" for link in model["links"]),
          widget)
    check("mutation names come from the mutation type",
          mutation_field_names(payload) == {"updateSecretInstance"})
    try:
        schema_from_introspection({"data": {}})
        check("a body without a schema raises", False)
    except GraphQLCallError:
        check("a body without a schema raises", True)


def test_paging_and_wiz_shape():
    print("paging and Wiz query shape")
    calls = []

    def opener(request, timeout=None):
        body = json.loads(request.data.decode())
        variables = body.get("variables") or {}
        after = variables.get("after")
        calls.append({"after": after, "query": variables.get("query"), "document": body.get("query") or ""})
        query = variables.get("query") or {}
        if "relationships" in query:
            idx = 0 if after is None else int(after)
            nxt = idx + 1
            entity = {"id": f"n{idx}", "name": f"node-{idx}", "type": "Gadget"}
            return Resp(200, json.dumps(graph_page([entity], nxt < 20, str(nxt), 50)).encode())
        if after == "s1":
            entity = {"id": "alpha-2", "name": "alpha-2", "type": "Widget"}
            return Resp(200, json.dumps(graph_page([entity], False, None, 2)).encode())
        entity = {"id": "alpha-1", "name": "alpha", "type": "Widget"}
        return Resp(200, json.dumps(graph_page([entity], True, "s1", 2)).encode())

    src = GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.us2.app.wiz.io/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN", "project_id": "*",
    }, client=client(opener))
    first = src.search("alpha", limit=1)
    second = src.search("alpha", limit=1, offset=1)
    check("search pages with the cursor and skips the offset",
          [node["id"] for node in first["nodes"]] == ["alpha-1"]
          and [node["id"] for node in second["nodes"]] == ["alpha-2"]
          and calls[1]["after"] == "s1"
          and first["nodes"][0]["partial"] is True and first["nodes"][0]["degree"] is None,
          safe(calls))
    calls.clear()
    page = src.expand(["hub"], ["hub"], limit=100)
    check("expand walks at most eight pages and reports more",
          len(page["nodes"]) == gql.MAX_PAGES and page["more"] is True and len(calls) == gql.MAX_PAGES
          and calls[1]["after"] == "1" and all("related" not in json.dumps(item["query"]) for item in calls),
          f"{len(calls)} calls, {len(page['nodes'])} nodes, more={page['more']}")
    calls.clear()
    looked = src.nodes(["alpha-1", "beta-9"])
    sent = calls[0]["query"]["where"]["_vertexID"]["EQUALS"]
    check("id lookup sends _vertexID EQUALS as a list and asks for properties",
          sent == ["alpha-1", "beta-9"] and "properties" in calls[0]["document"]
          and looked["nodes"][0]["partial"] is False,
          safe(sent))
    edges = src.search("", kind="edge")
    check("an edge search has no Wiz relationship index", edges == {"edges": []})
    summary = src.neighbor_summary(["hub"], [])
    check("neighbor summary keeps a degree for hub grouping",
          summary["degree"] == 50 and summary["approximate"] is True and summary["types"].get("related", 0) > 0,
          safe(summary))
    full = src.expand(["hub"], [], body={"budget": {"nodes": 1, "expand_limit": 10}, "on_screen": {"nodes": 1}})
    check("a full canvas returns the same empty truncation as Neo4j",
          full["nodes"] == [] and full["truncation"]["reasons"] == ["node_budget"]
          and full["truncation"]["message"] == "canvas is full",
          safe(full))


def test_auth_and_rate_limit():
    print("auth and rate limit")
    calls = {"n": 0}

    def expired(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise http_error(401, json.dumps({"errors": [{"message": f"unauthorized {TOKEN}"}]}))
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    same = client(expired)
    try:
        same.execute("{ __typename }")
        check("HTTP 401 is token expired", False, "no error")
    except TokenExpired as exc:
        check("HTTP 401 is token expired",
              str(exc) == "token expired" and TOKEN not in str(exc), safe(exc))
    again = same.execute("{ __typename }")
    check("a 401 is not cached", calls["n"] == 2 and again["data"]["__typename"] == "Query")

    leaked = {"n": 0}

    def boom(request, timeout=None):
        leaked["n"] += 1
        return Resp(200, json.dumps({"errors": [{"message": f"boom {TOKEN}"}], "data": None}).encode())

    try:
        client(boom).execute("{ __typename }")
        check("an error body is scrubbed", False, "no error")
    except GraphQLCallError as exc:
        check("an error body is scrubbed", TOKEN not in str(exc) and "[redacted]" in str(exc), safe(exc))

    slow = {"n": 0}
    slept = []

    def limited(request, timeout=None):
        slow["n"] += 1
        if slow["n"] == 1:
            raise http_error(429, b'{"errors":[{"message":"slow"}]}', {"Retry-After": "0"})
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    got = client(limited, sleep=slept.append, max_retries=3).execute("{ __typename }")
    check("one HTTP 429 is retried", slow["n"] == 2 and got["data"]["__typename"] == "Query" and slept == [0],
          f"calls={slow['n']} slept={slept}")

    always = {"n": 0}

    def always_limited(request, timeout=None):
        always["n"] += 1
        raise http_error(429, json.dumps({"errors": [{"message": TOKEN}]}).encode(), {"Retry-After": "0"})

    try:
        client(always_limited, sleep=lambda _s: None, max_retries=2).execute("{ __typename }")
        check("HTTP 429 gives up with the slow-down error", False, "no error")
    except RateLimited as exc:
        check("HTTP 429 gives up with the slow-down error",
              str(exc) == "The GraphQL API asked us to slow down (HTTP 429)."
              and always["n"] == 3 and TOKEN not in str(exc), safe(exc))


def test_cache_and_inflight():
    print("cache and in-flight")
    calls = []

    def opener(request, timeout=None):
        calls.append(json.loads(request.data.decode())["query"])
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    cached = client(opener)
    cached.execute("query Q($n: Int) { a }", {"n": 1})
    cached.execute("query Q($n: Int) { a }", {"n": 1})
    cached.execute("query Q($n: Int) { a }", {"n": 2})
    check("the same query and variables are cached", calls == [
        "query Q($n: Int) { a }", "query Q($n: Int) { a }"], safe(calls))

    started = threading.Event()
    release = threading.Event()
    shared = []
    errors = []

    def slow(request, timeout=None):
        shared.append(1)
        started.set()
        if not release.wait(3):
            raise TimeoutError("release")
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    shared_client = client(slow)

    def run():
        try:
            shared_client.execute("{ __typename }")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    check("the leader reaches the API", started.wait(2))
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(3)
    second.join(3)
    check("two callers share one in-flight query", shared == [1] and not errors, safe(errors or shared))

    overlap = {"n": 0, "max": 0}
    lock = threading.Lock()
    entered = []
    gate = threading.Event()

    def gated(request, timeout=None):
        with lock:
            overlap["n"] += 1
            overlap["max"] = max(overlap["max"], overlap["n"])
            entered.append(json.loads(request.data.decode())["query"])
        gate.wait(3)
        with lock:
            overlap["n"] -= 1
        return Resp(200, b'{"data":{"ok":true}}')

    capped = client(gated, max_inflight=1)
    threads = [
        threading.Thread(target=lambda: capped.execute("{ a }")),
        threading.Thread(target=lambda: capped.execute("{ b }")),
    ]
    threads[0].start()
    deadline = time.time() + 2
    while len(entered) < 1 and time.time() < deadline:
        time.sleep(0.01)
    threads[1].start()
    time.sleep(0.1)
    held = overlap["max"] == 1 and len(entered) == 1
    gate.set()
    for thread in threads:
        thread.join(3)
    check("one request runs at a time", held and overlap["max"] == 1 and len(entered) == 2, safe(entered))


def test_config_and_generic():
    print("config and generic adapter")
    gql._TOKEN_WARNED = False
    norm = server._normalize_config({
        "nodes": 10, "edges": 20, "source": "user",
        "graphql": {
            "id": "wiz", "endpoint": "https://api.us2.app.wiz.io/graphql",
            "adapter": "wiz", "token": TOKEN, "token_env": "GRAPHQL_TOKEN",
        },
    })
    blob = json.dumps(norm)
    check("a token in the config is dropped and the endpoint stays",
          TOKEN not in blob and "token" not in norm["graphql"][0]
          and norm["graphql"][0]["endpoint"] == "https://api.us2.app.wiz.io/graphql"
          and norm["graphql"][0]["adapter"] == "wiz"
          and norm["graphql"][0]["token_env"] == "GRAPHQL_TOKEN",
          safe(blob))
    plain = server._normalize_config({"nodes": 10, "edges": 10, "source": "user"})
    check("a budget file gains no graphql key", "graphql" not in plain)
    check("an endpoint with a user or password is ignored",
          sources_from_config({"graphql": {"endpoint": "https://user:secret@api.wiz.io/graphql"}}) == [])
    check("a token in the query string is ignored",
          sources_from_config({"graphql": {"endpoint": "https://api.wiz.io/graphql?token=abc"}}) == [])
    os.environ.pop("GRAPHQL_ENDPOINT", None)
    skipped = resolve_graphql_sources({"graphql": [
        {"id": "neo4j", "endpoint": "https://example.test/graphql"},
        {"id": "wiz", "endpoint": "https://api.us2.app.wiz.io/graphql"},
        {"id": "wiz", "endpoint": "https://api.eu1.app.wiz.io/graphql"},
    ]})
    check("neo4j and a duplicate id are skipped",
          [item["id"] for item in skipped] == ["wiz"]
          and skipped[0]["endpoint"].endswith("api.us2.app.wiz.io/graphql"),
          safe(skipped))
    os.environ["GRAPHQL_ENDPOINT"] = "https://api.us2.app.wiz.io/graphql"
    os.environ["GRAPHQL_TOKEN"] = TOKEN
    os.environ["GRAPHQL_ADAPTER"] = "wiz"
    try:
        specs = resolve_graphql_sources({})
        spec_blob = json.dumps(specs)
        check("GRAPHQL_ENDPOINT builds a source without the token value",
              len(specs) == 1 and specs[0]["id"] == "wiz" and specs[0]["adapter"] == "wiz"
              and TOKEN not in spec_blob and "token" not in specs[0],
              safe(spec_blob))
    finally:
        os.environ.pop("GRAPHQL_ENDPOINT", None)
        os.environ.pop("GRAPHQL_TOKEN", None)
        os.environ.pop("GRAPHQL_ADAPTER", None)

    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gx.local.json"
    old = os.environ.get("GX_CONFIG")
    os.environ["GX_CONFIG"] = str(path)
    server._WORKING = None
    server._CONFIG_CREATED = False
    try:
        path.write_text(json.dumps({
            "nodes": 100, "edges": 200, "expand_limit": 10, "cypher_limit": 20,
            "source": "user", "gpu": "unknown", "lru": False, "reason": "test",
            "graphql": {
                "id": "wiz", "label": "Wiz", "endpoint": "https://api.us2.app.wiz.io/graphql",
                "adapter": "wiz", "token": TOKEN, "token_env": "GRAPHQL_TOKEN", "project_id": "*",
            },
        }))
        loaded = server.ensure_config()
        disk = path.read_text()
        check("startup removes a token from the config file",
              TOKEN not in disk and TOKEN not in json.dumps(loaded)
              and loaded["graphql"][0]["endpoint"].startswith("https://api.us2.app.wiz.io/"),
              safe(disk))
        saved = server.save_config({
            "nodes": 80, "edges": 90, "expand_limit": 10, "cypher_limit": 20, "reason": "edited",
        })
        disk = path.read_text()
        check("saving the budget keeps the GraphQL source and omits the token",
              saved["nodes"] == 80 and TOKEN not in disk and "api.us2.app.wiz.io" in disk
              and json.loads(disk)["graphql"][0]["token_env"] == "GRAPHQL_TOKEN",
              safe(disk))
    finally:
        server._WORKING = None
        server._CONFIG_CREATED = False
        if old is None:
            os.environ.pop("GX_CONFIG", None)
        else:
            os.environ["GX_CONFIG"] = old
        tmp.cleanup()

    calls = []

    def opener(request, timeout=None):
        calls.append(1)
        return Resp(200, b"{}")

    generic = GraphQLSource({
        "id": "other", "label": "Other", "endpoint": "https://example.test/graphql",
        "adapter": "generic", "token_env": "GRAPHQL_TOKEN", "project_id": "*",
    }, client=client(opener))
    try:
        generic.search("alpha")
        check("a generic source does not invent a search", False, "returned")
    except ValueError as exc:
        check("a generic source does not invent a search",
              "introspection" in str(exc) and not calls, safe(exc))
    between = generic.edges_between(["a", "b"])
    check("edges among visible nodes are empty for a generic source", between["edges"] == [] and not between["truncated"])
    try:
        generic.cypher("MATCH (n) RETURN n")
        check("GraphQL has no Cypher", False)
    except ValueError as exc:
        check("GraphQL has no Cypher", "no Cypher" in str(exc), safe(exc))
    for name in server.SOURCE_METHODS:
        check(f"Neo4j implements {name}", callable(getattr(server.Graph, name, None)))
        check(f"GraphQL implements {name}", callable(getattr(GraphQLSource, name, None)))


class MockGraphQL(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw.decode() or "{}")
        document = body.get("query") or ""
        variables = body.get("variables") or {}
        self.server.queries.append(variables.get("query"))
        code, payload = self.server.answer(document, variables)
        self._send(code, payload)


def start_mock():
    entities = [
        {"id": "alpha-1", "name": "alpha", "type": "Widget",
         "properties": {"name": "alpha", "region": "us2", "note": "full"}},
        {"id": "beta-2", "name": "beta", "type": "Gadget", "properties": {"name": "beta", "region": "eu"}},
        {"id": "gamma-3", "name": "gamma", "type": "Account", "properties": {"name": "gamma"}},
    ]

    def answer(document, variables):
        if "__schema" in document:
            return 200, introspection_payload()
        if "graphSearch" not in document:
            return 200, {"data": {"__typename": "Query"}}
        query = variables.get("query") or {}
        where = query.get("where") or {}
        text = ""
        name_filter = where.get("name") or {}
        if name_filter.get("CONTAINS"):
            text = name_filter["CONTAINS"][0]
        if text == "expired-check":
            return 401, {"errors": [{"message": f"unauthorized {TOKEN}"}]}
        if text == "slow-check":
            return 429, {"errors": [{"message": TOKEN}]}
        if "relationships" in query:
            if variables.get("after") == "p2":
                return 200, graph_page([entities[2]], False, None, 2)
            return 200, graph_page([entities[1]], True, "p2", 2)
        if "_vertexID" in where:
            wanted = set(where["_vertexID"].get("EQUALS") or [])
            found = [item for item in entities if item["id"] in wanted]
            return 200, graph_page(found, False, None, len(found))
        found = [item for item in entities if not text or text.lower() in item["name"].lower()]
        return 200, graph_page(found, False, None, len(found))

    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), MockGraphQL)
    httpd.answer = answer
    httpd.queries = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


def free_port():
    while True:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        if port not in BUSY_PORTS:
            return port


def neo4j_up():
    from neo4j import GraphDatabase
    uri = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    passwords = []
    if os.environ.get("NEO4J_PASSWORD"):
        passwords.append(os.environ["NEO4J_PASSWORD"])
    if "tinkerlab-test" not in passwords:
        passwords.append("tinkerlab-test")
    for password in passwords:
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password))
            driver.verify_connectivity()
            driver.close()
            return uri, user, password
        except Exception:  # noqa: BLE001
            continue
    return None


def http_json(method, url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read().decode()
            return response.status, raw, json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {"error": raw}
        return exc.code, raw, body


def test_source_switch():
    print("source switch")
    found = neo4j_up()
    if found:
        uri, user, password = found
    else:
        print("  skip count comparison (Neo4j is not reachable)")
        uri, user, password = "bolt://127.0.0.1:7687", "neo4j", "tinkerlab-test"
    mock, mock_port = start_mock()
    cfg_dir = tempfile.TemporaryDirectory()
    env_dir = tempfile.NamedTemporaryFile(prefix="gx-env-", delete=False)
    env_dir.close()
    port = free_port()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GRAPHQL_"):
            env.pop(key)
    env.update({
        "NEO4J_URI": uri, "NEO4J_USER": user, "NEO4J_PASSWORD": password,
        "NEO4J_DATABASE": os.environ.get("NEO4J_DATABASE") or "neo4j",
        "HOST": "127.0.0.1", "PORT": str(port), "QUIET": "1",
        "GX_CONFIG": str(Path(cfg_dir.name) / "gx.local.json"),
        "GX_ENV": env_dir.name,
        "GRAPHQL_ENDPOINT": f"http://127.0.0.1:{mock_port}/graphql",
        "GRAPHQL_TOKEN": TOKEN,
        "GRAPHQL_ADAPTER": "wiz",
        "GRAPHQL_LABEL": "Wiz",
        "GRAPHQL_ID": "wiz",
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        status = None
        raw_status = ""
        for _ in range(80):
            if proc.poll() is not None:
                err = proc.stderr.read().decode()[-500:]
                check("explorer starts beside the mock", False, safe(err))
                return
            try:
                code, raw_status, status = http_json("GET", base + "/api/status")
                if code == 200:
                    break
            except Exception:  # noqa: BLE001
                status = None
            time.sleep(0.25)
        if not status:
            check("explorer starts beside the mock", False, "status never answered")
            return
        check("explorer starts beside the mock", True)
        ids = [item["id"] for item in status.get("sources") or []]
        check("status starts on Neo4j and lists Wiz without the token",
              status.get("source", {}).get("id") == "neo4j" and ids == ["neo4j", "wiz"]
              and TOKEN not in raw_status,
              safe(raw_status[:300]))
        code, raw, catalog = http_json("GET", base + "/api/sources")
        check("the source list has no token",
              code == 200 and TOKEN not in raw and [item["id"] for item in catalog] == ["neo4j", "wiz"],
              safe(raw))
        neo4j_counts = (status.get("nodes"), status.get("edges"))
        connected = bool(status.get("connected"))
        if connected:
            code, raw, counted = http_json("POST", base + "/api/cypher", {"query": "MATCH (n) RETURN count(n) AS n"})
            rows = counted.get("rows") or []
            cell = rows[0][0] if rows and rows[0] else None
            check("Neo4j counts still match a read-only Cypher count",
                  code == 200 and status.get("nodes") == cell and TOKEN not in raw,
                  f"status {status.get('nodes')} vs {cell}")
        code, raw, wiz = http_json("POST", base + "/api/source", {"id": "wiz"})
        check("switching to Wiz leaves totals unknown and reports the adapter",
              code == 200 and wiz.get("source", {}).get("id") == "wiz"
              and wiz.get("source", {}).get("adapter") == "wiz"
              and wiz.get("nodes") is None and wiz.get("edges") is None
              and wiz.get("connected") is True and TOKEN not in raw,
              safe(raw[:400]))
        code, raw, schema = http_json("GET", base + "/api/schema")
        check("the data model comes from introspection",
              code == 200 and "Widget" in (schema.get("categories") or {})
              and "__Schema" not in (schema.get("categories") or {})
              and schema["categories"]["Widget"]["props"]["owner"] == "-> Widget"
              and TOKEN not in raw,
              safe(sorted((schema.get("categories") or {}))))
        code, raw, found_nodes = http_json("GET", base + "/api/search?type=node&limit=5&q=alpha")
        nodes = found_nodes.get("nodes") or []
        check("search returns the mock entity",
              code == 200 and nodes and nodes[0]["id"] == "alpha-1" and nodes[0]["partial"] is True
              and TOKEN not in raw,
              safe(nodes[:1]))
        code, raw, first = http_json("POST", base + "/api/expand", {"ids": ["alpha-1"], "visible": [], "limit": 1})
        code2, raw2, second = http_json("POST", base + "/api/expand", {
            "ids": ["alpha-1"], "visible": ["alpha-1", "beta-2"], "limit": 1,
        })
        check("expand returns one page and then the next",
              code == 200 and [node["id"] for node in first.get("nodes") or []] == ["beta-2"]
              and first.get("more") is True and first["edges"][0]["type"] == "related"
              and first["edges"][0]["id"] == "alpha-1:related:beta-2"
              and code2 == 200 and [node["id"] for node in second.get("nodes") or []] == ["gamma-3"]
              and TOKEN not in raw and TOKEN not in raw2,
              safe({"first": first.get("nodes"), "second": second.get("nodes"), "more": first.get("more")}))
        code, raw, details = http_json("POST", base + "/api/nodes", {"ids": ["alpha-1"]})
        props = ((details.get("nodes") or [{}])[0]).get("props") or {}
        check("opening a node loads its properties",
              code == 200 and props.get("region") == "us2" and props.get("note") == "full"
              and details["nodes"][0]["partial"] is False and TOKEN not in raw,
              safe(props))
        code, raw, denied = http_json("POST", base + "/api/cypher", {"query": "MATCH (n) RETURN n"})
        check("Cypher on the GraphQL source is refused",
              code == 400 and "no Cypher" in (denied.get("error") or "") and TOKEN not in raw,
              safe(denied))
        code, raw, expired = http_json("GET", base + "/api/search?type=node&limit=5&q=expired-check")
        check("HTTP 401 from the API is token expired",
              code == 401 and expired.get("error") == "token expired" and TOKEN not in raw, safe(raw))
        code, raw, slow = http_json("GET", base + "/api/search?type=node&limit=5&q=slow-check")
        check("HTTP 429 from the API asks the page to slow down",
              code == 429 and slow.get("error") == "The GraphQL API asked us to slow down (HTTP 429)."
              and TOKEN not in raw,
              safe(raw))
        code, raw, back = http_json("POST", base + "/api/source", {"id": "neo4j"})
        check("switching back selects Neo4j",
              code == 200 and back.get("source", {}).get("id") == "neo4j" and TOKEN not in raw, safe(raw[:240]))
        if connected:
            check("Neo4j counts are the same after the switch",
                  (back.get("nodes"), back.get("edges")) == neo4j_counts,
                  f"{back.get('nodes')}/{back.get('edges')} vs {neo4j_counts}")
    finally:
        proc.terminate()
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate(timeout=5)
        log = err.decode()
        check("the server log omits the token", TOKEN not in log, safe(log[-400:]))
        mock.shutdown()
        mock.server_close()
        cfg_dir.cleanup()
        try:
            os.remove(env_dir.name)
        except OSError:
            pass


def main():
    saved = {key: os.environ.get(key) for key in list(os.environ) if key.startswith("GRAPHQL_") or key in ("GX_ENV", "GX_CONFIG")}
    for key in list(saved):
        os.environ.pop(key, None)
    empty = tempfile.NamedTemporaryFile(prefix="gx-env-", delete=False)
    empty.close()
    os.environ["GX_ENV"] = empty.name
    try:
        test_mutations()
        test_introspection_model()
        test_paging_and_wiz_shape()
        test_auth_and_rate_limit()
        test_cache_and_inflight()
        test_config_and_generic()
        test_source_switch()
    finally:
        os.environ.pop("GX_ENV", None)
        for key in list(os.environ):
            if key.startswith("GRAPHQL_"):
                os.environ.pop(key, None)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            os.remove(empty.name)
        except OSError:
            pass
    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print("\nall graphql checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
