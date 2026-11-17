#!/usr/bin/env python3
"""Small Wiz-shaped GraphQL API for local runs and tests.

Container gx-graphql publishes this on 127.0.0.1:8791. The test client id is
gx-graphql and the test secret is tinkerlab-graphql. The access token is
tinkerlab-access. Same idea as the local Neo4j password: fixture only.

    python3 tools/graphql_fixture.py
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"
PORT = 8791
CLIENT_ID = "gx-graphql"
CLIENT_SECRET = "tinkerlab-graphql"
ACCESS = "tinkerlab-access"
MAX_BODY = 1_000_000

ENTITIES = [
    {"id": "alpha-1", "name": "alpha", "type": "Widget",
     "properties": {"name": "alpha", "region": "example-region", "note": "full"}},
    {"id": "beta-2", "name": "beta", "type": "Gadget",
     "properties": {"name": "beta", "region": "eu"}},
    {"id": "gamma-3", "name": "gamma", "type": "Account",
     "properties": {"name": "gamma"}},
    {"id": "delta-4", "name": "delta", "type": "Widget",
     "properties": {"name": "delta", "region": "us1"}},
]
BY_ID = {item["id"]: item for item in ENTITIES}
NEIGHBORS = {
    "alpha-1": ["beta-2", "gamma-3", "delta-4"],
    "beta-2": ["alpha-1"],
    "gamma-3": ["alpha-1"],
    "delta-4": ["alpha-1"],
}

# Enough of an introspection result for the data-model window. The fixture
# returns this whenever the document asks for __schema.
INTROSPECTION = {"data": {"__schema": {
    "queryType": {"name": "Query"},
    "mutationType": None,
    "types": [
        {"kind": "SCALAR", "name": "String", "fields": None, "enumValues": None,
         "inputFields": None, "possibleTypes": None},
        {"kind": "OBJECT", "name": "Widget", "fields": [
            {"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID", "ofType": None}},
            {"name": "name", "args": [], "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
            {"name": "region", "args": [], "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
            {"name": "owner", "args": [], "type": {"kind": "OBJECT", "name": "Widget", "ofType": None}},
        ], "enumValues": None, "inputFields": None, "possibleTypes": None},
        {"kind": "OBJECT", "name": "Gadget", "fields": [
            {"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID", "ofType": None}},
            {"name": "name", "args": [], "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
            {"name": "widget", "args": [], "type": {"kind": "OBJECT", "name": "Widget", "ofType": None}},
        ], "enumValues": None, "inputFields": None, "possibleTypes": None},
        {"kind": "OBJECT", "name": "Account", "fields": [
            {"name": "id", "args": [], "type": {"kind": "SCALAR", "name": "ID", "ofType": None}},
            {"name": "name", "args": [], "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
        ], "enumValues": None, "inputFields": None, "possibleTypes": None},
        {"kind": "OBJECT", "name": "Query", "fields": [
            {"name": "graphSearch", "args": [], "type": {"kind": "OBJECT", "name": "GraphSearchResult", "ofType": None}},
        ], "enumValues": None, "inputFields": None, "possibleTypes": None},
        {"kind": "OBJECT", "name": "GraphSearchResult", "fields": [
            {"name": "totalCount", "args": [], "type": {"kind": "SCALAR", "name": "Int", "ofType": None}},
        ], "enumValues": None, "inputFields": None, "possibleTypes": None},
    ],
}}}


def _matches(entity, gquery):
    types = gquery.get("type")
    if types and entity["type"] not in types:
        return False
    where = gquery.get("where") if isinstance(gquery.get("where"), dict) else {}
    contains = (where.get("name") or {}).get("CONTAINS") if isinstance(where.get("name"), dict) else None
    if contains:
        needle = str(contains[0]).lower()
        if needle not in str(entity.get("name") or "").lower():
            return False
    props = entity.get("properties") or {}
    for key, spec in where.items():
        if key in ("name", "_vertexID") or not isinstance(spec, dict) or not spec.get("EQUALS"):
            continue
        if str(props.get(key)) != str(spec["EQUALS"][0]):
            return False
    return True


def _paginate(items, first, after):
    try:
        start = int(after) if after else 0
    except (TypeError, ValueError):
        start = 0
    start = max(0, start)
    try:
        size = int(first) if first else 20
    except (TypeError, ValueError):
        size = 20
    size = max(1, min(size, 200))
    chunk = items[start:start + size]
    end = start + len(chunk)
    has_next = end < len(items)
    return chunk, has_next, (str(end) if has_next else None), len(items)


def _id_list(where, key):
    spec = where.get(key) if isinstance(where, dict) else None
    if not isinstance(spec, dict):
        return []
    values = spec.get("EQUALS") or []
    if isinstance(values, str):
        values = [values]
    return [str(item) for item in values]


def _anchor(gquery):
    rels = gquery.get("relationships")
    if not isinstance(rels, list) or not rels or not isinstance(rels[0], dict):
        return ""
    where = ((rels[0].get("with") or {}).get("where") or {})
    found = _id_list(where, "id") or _id_list(where, "_vertexID")
    return found[0] if found else ""


def answer(document, variables):
    """One GraphQL document in, one JSON body out. No request is logged."""
    document = document or ""
    variables = variables if isinstance(variables, dict) else {}
    if "__schema" in document:
        return 200, INTROSPECTION
    if "graphSearch" not in document:
        return 200, {"data": {"__typename": "Query"}}
    # The fixture requires GraphEntityQueryInput!, so a document that drops
    # the ! on the legacy path fails here. Live Wiz accepts either form when
    # type is present.
    if "$query: GraphEntityQueryInput!" not in document:
        return 200, {"errors": [{"message":
            'Variable "$query" of type "GraphEntityQueryInput" used in position '
            'expecting type "GraphEntityQueryInput!".'}]}
    gquery = variables.get("query") if isinstance(variables.get("query"), dict) else {}
    if gquery.get("relationships"):
        ids = NEIGHBORS.get(_anchor(gquery), [])
        items = [BY_ID[item] for item in ids if item in BY_ID]
        items = [entity for entity in items if _matches(entity, gquery)]
    else:
        where = gquery.get("where") if isinstance(gquery.get("where"), dict) else {}
        wanted = _id_list(where, "id") or _id_list(where, "_vertexID")
        if wanted:
            keep = set(wanted)
            items = [entity for entity in ENTITIES if entity["id"] in keep]
        else:
            items = [entity for entity in ENTITIES if _matches(entity, gquery)]
    chunk, has_next, cursor, total = _paginate(items, variables.get("first"), variables.get("after"))
    return 200, {"data": {"graphSearch": {
        "totalCount": total,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": [{"entities": chunk}],
    }}}


def _form(raw):
    parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


def _token_response(raw):
    fields = _form(raw)
    if fields.get("grant_type") != "client_credentials" or fields.get("audience") != "wiz-api":
        return 400, {"error": "invalid_request"}
    if fields.get("client_id") != CLIENT_ID or fields.get("client_secret") != CLIENT_SECRET:
        return 401, {"error": "invalid_client"}
    return 200, {"access_token": ACCESS, "token_type": "bearer", "expires_in": 3600}


class Handler(BaseHTTPRequestHandler):
    server_version = "gx-graphql"

    def log_message(self, fmt, *args):
        # The fixture is a test double. Request lines can carry the bearer.
        return

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > MAX_BODY:
            return None
        return self.rfile.read(length)

    def do_GET(self):
        if urlsplit(self.path).path == "/health":
            self._send(200, {"ok": True})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        raw = self._read()
        if raw is None:
            self._send(413, {"error": "body too large"})
            return
        if path == "/oauth/token":
            code, payload = _token_response(raw)
            self._send(code, payload)
            return
        if path != "/graphql":
            self._send(404, {"error": "not found"})
            return
        if (self.headers.get("Authorization") or "") != f"Bearer {ACCESS}":
            self._send(401, {"errors": [{"message": "unauthorized"}]})
            return
        try:
            body = json.loads(raw.decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"errors": [{"message": "body is not JSON"}]})
            return
        if not isinstance(body, dict):
            body = {}
        code, payload = answer(body.get("query") or "", body.get("variables") or {})
        self._send(code, payload)


def main():
    parser = argparse.ArgumentParser(description="Local Wiz-shaped GraphQL fixture")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"graphql fixture on {args.host}:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
