#!/usr/bin/env python3
"""GraphQL source: queries only, introspection, paging, and the source switch.

The live section starts a mock GraphQL API and a second explorer on free ports.
It does not wipe Neo4j and it does not use ports 8744, 8755, 8766, or 8791.
If Neo4j is down, the switch still runs and the count comparison is skipped.

The last section starts the gx-graphql fixture container when Docker is running
and checks search, expand, and a minted access token against it.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
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
BUSY_PORTS = {8744, 8755, 8766, 8791}


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


def graph_paths(entities, has_next, cursor, total):
    """A one-level graphSearch page in the live shape: one single-entity path per match."""
    return {"data": {"graphSearch": {
        "totalCount": total,
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": [{"entities": [entity]} for entity in entities],
    }}}


def test_search_document():
    """The legacy fixture path requires GraphEntityQueryInput!."""
    print("search document")
    for label, document in (("search", gql.SEARCH_QUERY), ("detail", gql.DETAIL_QUERY)):
        check(f"{label} query variable is required",
              "$query: GraphEntityQueryInput!" in document,
              document.split("{", 1)[0].strip())
    spec = importlib.util.spec_from_file_location("graphql_fixture", ROOT / "tools" / "graphql_fixture.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    nullable = gql.SEARCH_QUERY.replace("GraphEntityQueryInput!", "GraphEntityQueryInput")
    code, body = fixture.answer(nullable, {"query": {"select": True}, "projectId": "*", "first": 5})
    message = json.dumps(body)
    check("fixture rejects a nullable query variable",
          code == 200 and "errors" in body and "GraphEntityQueryInput!" in message and "data" not in body,
          message[:300])
    code, body = fixture.answer(gql.SEARCH_QUERY, {
        "query": {"select": True, "where": {"name": {"CONTAINS": ["alpha"]}}},
        "projectId": "*", "first": 5,
    })
    entities = (((body.get("data") or {}).get("graphSearch") or {}).get("nodes") or [{}])[0].get("entities") or []
    check("fixture accepts the required query variable",
          code == 200 and entities and entities[0].get("id") == "alpha-1" and "errors" not in body,
          json.dumps(body)[:300])


def _input_type(name, fields):
    return {
        "kind": "INPUT_OBJECT", "name": name, "fields": None,
        "inputFields": fields, "enumValues": None, "possibleTypes": None,
    }


def tenant_payload(query_non_null, contains_list, with_select, with_project, where_names=("name", "id"), with_quick=False, with_max=False):
    """A graphSearch argument small enough to prove the value is coerced."""
    string = named("SCALAR", "String")
    value_type = listing(non_null(string)) if contains_list else string
    query_fields = [
        field("type", listing(non_null(named("ENUM", "GraphEntityType")))),
        field("where", named("INPUT_OBJECT", "GraphEntityWhereInput")),
    ]
    if with_select:
        query_fields.insert(0, field("select", named("SCALAR", "Boolean")))
    query_type = named("INPUT_OBJECT", "GraphEntityQueryInput")
    if query_non_null:
        query_type = non_null(query_type)
    args = [{"name": "query", "type": query_type}]
    if with_project:
        args.append({"name": "projectId", "type": non_null(string)})
    args.extend([
        {"name": "first", "type": named("SCALAR", "Int")},
        {"name": "after", "type": string},
    ])
    if with_quick:
        args.append({"name": "quick", "type": named("SCALAR", "Boolean")})
    result_fields = [field("totalCount", named("SCALAR", "Int"))]
    if with_max:
        result_fields.append(field("maxCountReached", named("SCALAR", "Boolean")))
    return {"data": {"__schema": {
        "queryType": {"name": "Query"},
        "mutationType": None,
        "types": [
            {"kind": "OBJECT", "name": "Query", "fields": [
                field("graphSearch", named("OBJECT", "GraphSearchResult"), args),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
            _input_type("GraphEntityQueryInput", query_fields),
            _input_type("GraphEntityWhereInput", [
                field(name, named("INPUT_OBJECT", "StringFilter")) for name in where_names
            ]),
            _input_type("StringFilter", [field("CONTAINS", value_type), field("EQUALS", value_type)]),
            {"kind": "ENUM", "name": "GraphEntityType", "fields": None, "inputFields": None,
             "enumValues": [{"name": "VIRTUAL_MACHINE"}, {"name": "BUCKET"}], "possibleTypes": None},
            {"kind": "OBJECT", "name": "GraphSearchResult", "fields": result_fields,
             "enumValues": None, "inputFields": None, "possibleTypes": None},
        ],
    }}}


def test_search_follows_the_tenant_schema():
    """The query value follows the tenant input. A schema type is not an entity type."""
    print("search follows the tenant schema")
    asked = {"select": True, "type": ["APICallComponent"], "where": {"name": {"CONTAINS": ["ENTITY"]}}}
    printed, cleaned, project = gql.conform_wiz_query(
        tenant_payload(False, False, False, False), asked)
    check("a string filter drops select, the schema type, and the list",
          printed == "GraphEntityQueryInput" and project is False
          and cleaned == {"where": {"name": {"CONTAINS": "ENTITY"}}},
          safe(cleaned))
    loose = tenant_payload(False, False, False, False)
    doc, doc_vars = gql.graph_search_request(gql.WizShape(loose), printed, cleaned, False, "*", 5, None)
    check("the document sends the query as a variable and leaves out projectId",
          "$query: GraphEntityQueryInput" in doc and "GraphEntityQueryInput!" not in doc
          and "projectId" not in doc and "quick" not in doc
          and doc_vars.get("query") == cleaned and "projectId" not in doc_vars,
          doc.strip())

    printed, cleaned, project = gql.conform_wiz_query(
        tenant_payload(True, True, True, True), asked)
    check("a required list filter keeps select and a list",
          printed == "GraphEntityQueryInput!" and project is True and cleaned.get("select") is True
          and cleaned.get("where") == {"name": {"CONTAINS": ["ENTITY"]}} and "type" not in cleaned,
          safe(cleaned))
    strict = tenant_payload(True, True, True, True)
    strict_doc, strict_vars = gql.graph_search_request(gql.WizShape(strict), printed, cleaned, True, "*", 5, None)
    check("the strict document declares $query, projectId, and properties",
          "$query: GraphEntityQueryInput!" in strict_doc and "projectId: $projectId" in strict_doc
          and "properties" in strict_doc and strict_vars.get("query") == cleaned
          and strict_vars.get("projectId") == "*" and "after" not in strict_vars,
          strict_doc.split("{", 1)[0].strip())

    entities, filters = gql.wiz_search_hints(tenant_payload(True, True, True, True))
    check("entity types and filters come from the graphSearch input",
          entities == ["BUCKET", "VIRTUAL_MACHINE"] and filters == ["id", "name"],
          safe((entities, filters)))

    fallback = {"select": True, "where": {"name": {"CONTAINS": ["alpha"]}}}
    printed, cleaned, project = gql.conform_wiz_query(introspection_payload(), fallback)
    check("missing graphSearch arguments keep the fallback query",
          printed == "GraphEntityQueryInput!" and project is True and cleaned == fallback,
          safe(cleaned))

    equals_only = tenant_payload(False, False, False, False)
    for item in equals_only["data"]["__schema"]["types"]:
        if item["name"] == "StringFilter":
            item["inputFields"] = [one for one in item["inputFields"] if one["name"] != "CONTAINS"]
    _, cleaned, _ = gql.conform_wiz_query(equals_only, {"where": {"name": {"CONTAINS": ["ENTITY"]}}})
    check("a missing CONTAINS operator uses EQUALS",
          cleaned.get("where") == {"name": {"EQUALS": "ENTITY"}}, safe(cleaned))

    _, cleaned, _ = gql.conform_wiz_query(
        tenant_payload(True, True, True, False),
        {"select": True, "where": {"id": {"EQUALS": ["a", "b"]}}})
    check("an id list stays a list",
          cleaned.get("select") is True and cleaned.get("where") == {"id": {"EQUALS": ["a", "b"]}},
          safe(cleaned))

    seen = []

    def opener(request, timeout=None):
        body = json.loads(request.data.decode())
        document = body.get("query") or ""
        if "__schema" in document:
            return Resp(200, json.dumps(tenant_payload(False, False, False, False)).encode())
        seen.append(body)
        return Resp(200, json.dumps(graph_page(
            [{"id": "e1", "name": "ENTITY", "type": "BUCKET"}], False, None, 1)).encode())

    src = GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.us/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN", "project_id": "*",
    }, client=client(opener))
    try:
        src.search("ENTITY", category="APICallComponent")
        check("a schema type is refused before the search is sent", False, "no error")
    except ValueError as exc:
        check("a schema type is refused before the search is sent",
              "APICallComponent" in str(exc) and "entity type" in str(exc) and not seen, safe(exc))
    try:
        src.search("", prop="region", value="example-region")
        check("an unknown property is refused", False, "no error")
    except ValueError as exc:
        check("an unknown property is refused",
              "region" in str(exc) and "not a filter" in str(exc) and not seen, safe(exc))
    int_where = tenant_payload(False, False, False, False, where_names=("name", "id", "count"))
    int_types = int_where["data"]["__schema"]["types"]
    int_types.append(_input_type("IntFilter", [field("EQUALS", named("SCALAR", "Int"))]))
    for item in int_types:
        if item["name"] == "GraphEntityWhereInput":
            for one in item["inputFields"]:
                if one["name"] == "count":
                    one["type"] = named("INPUT_OBJECT", "IntFilter")
    int_seen = []

    def int_opener(request, timeout=None):
        body = json.loads(request.data.decode())
        if "__schema" in (body.get("query") or ""):
            return Resp(200, json.dumps(int_where).encode())
        int_seen.append(body)
        return Resp(200, json.dumps(graph_page(
            [{"id": "e1", "name": "ENTITY", "type": "BUCKET"}], False, None, 1)).encode())

    int_src = GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.us/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN", "project_id": "*",
    }, client=client(int_opener))
    try:
        int_src.search("", category="BUCKET", prop="count", value="5")
        check("a property value the schema drops is refused, not sent unfiltered", False, "no error")
    except ValueError as exc:
        check("a property value the schema drops is refused, not sent unfiltered",
              "count" in str(exc) and "not a filter" in str(exc) and not int_seen, safe(exc))
    found = src.search("ENTITY", category="BUCKET")
    sent = seen[-1] if seen else {}
    variables = sent.get("variables") or {}
    document = sent.get("query") or ""
    check("a name search sends the value the schema accepts as $query",
          variables.get("query") == {"type": ["BUCKET"], "where": {"name": {"CONTAINS": "ENTITY"}}}
          and "$query: GraphEntityQueryInput" in document
          and "projectId" not in variables and "projectId" not in document
          and "GraphEntityQueryInput!" not in document
          and found["nodes"] and found["nodes"][0]["id"] == "e1",
          safe(sent)[:700])
    model = src.schema()
    try:
        whole = src.schema(full=True)
    except Exception as exc:  # noqa: BLE001
        whole = {"error": safe(exc)}
    check("schema publishes the entity types for the search dropdown",
          model.get("entities") == ["BUCKET", "VIRTUAL_MACHINE"] and model.get("filters") == ["id", "name"]
          and model.get("filter_mode") == "schema" and model.get("name_search") == "typed"
          and model.get("categories") == {} and "Query" in (whole.get("categories") or {}),
          safe({"entities": model.get("entities"), "categories": len(model.get("categories") or {}),
                "full": whole.get("error") or len(whole.get("categories") or {})}))

    only_contains = json.loads(json.dumps(tenant_graph_payload()))
    for item in only_contains["data"]["__schema"]["types"]:
        if item["name"] == "StringFilter":
            item["inputFields"] = [one for one in item["inputFields"] if one["name"] != "EQUALS"]
    value = gql._neighbor_value(only_contains, "alpha-1")
    printed, cleaned, _ = gql.conform_wiz_query(only_contains, value)
    _, nested_vars = gql.graph_search_request(gql.WizShape(only_contains), printed, cleaned, False, "*", 5, None)
    nested = nested_vars.get("query") or {}
    check("an anchor id filter uses the operator the schema has",
          (nested.get("where") or {}).get("id") == {"CONTAINS": ["alpha-1"]}
          and nested.get("relationships") and "EQUALS" not in json.dumps(nested),
          safe(nested))

    try:
        gql._neighbor_value(tenant_payload(True, True, True, True), "alpha-1")
        check("a schema without relationships refuses expand", False, "no error")
    except ValueError as exc:
        check("a schema without relationships refuses expand",
              "no relationship filter" in str(exc), safe(exc))


def tenant_graph_payload():
    """A tenant graphSearch: query, projectId, first, after, quick, and relationships.

    The relationship type is a list of inputs with a type enum, which is the
    shape the saved Wiz documents use. The local fixture publishes no arguments,
    so it never reaches this document.
    """
    string = named("SCALAR", "String")
    boolean = named("SCALAR", "Boolean")
    integer = named("SCALAR", "Int")
    text_list = listing(non_null(string))
    entity = named("ENUM", "GraphEntityType")
    rel_enum = named("ENUM", "GraphRelationshipType")
    query_ref = named("INPUT_OBJECT", "GraphEntityQueryInput")
    return {"data": {"__schema": {
        "queryType": {"name": "Query"},
        "mutationType": None,
        "types": [
            {"kind": "OBJECT", "name": "Query", "fields": [
                field("graphSearch", named("OBJECT", "GraphSearchResult"), [
                    {"name": "query", "type": non_null(query_ref)},
                    {"name": "projectId", "type": non_null(string)},
                    {"name": "first", "type": integer},
                    {"name": "after", "type": string},
                    {"name": "quick", "type": boolean},
                ]),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
            _input_type("GraphEntityQueryInput", [
                field("select", boolean),
                field("type", listing(non_null(entity))),
                field("where", named("INPUT_OBJECT", "GraphEntityWhereInput")),
                field("relationships", listing(non_null(named("INPUT_OBJECT", "GraphRelationshipQueryInput")))),
            ]),
            _input_type("GraphRelationshipQueryInput", [
                field("type", listing(non_null(named("INPUT_OBJECT", "GraphRelationshipTypeInput")))),
                field("with", non_null(query_ref)),
            ]),
            _input_type("GraphRelationshipTypeInput", [
                field("type", non_null(rel_enum)),
            ]),
            _input_type("GraphEntityWhereInput", [
                field("name", named("INPUT_OBJECT", "StringFilter")),
                field("id", named("INPUT_OBJECT", "StringFilter")),
            ]),
            _input_type("StringFilter", [
                field("EQUALS", text_list),
                field("CONTAINS", text_list),
            ]),
            {"kind": "ENUM", "name": "GraphEntityType", "fields": None, "inputFields": None,
             "enumValues": [{"name": "VIRTUAL_MACHINE"}, {"name": "BUCKET"}], "possibleTypes": None},
            {"kind": "ENUM", "name": "GraphRelationshipType", "fields": None, "inputFields": None,
             "enumValues": [{"name": "CONTAINS"}, {"name": "CONNECTS"}], "possibleTypes": None},
            {"kind": "OBJECT", "name": "GraphSearchResult", "fields": [
                field("totalCount", integer),
                field("maxCountReached", boolean),
            ], "enumValues": None, "inputFields": None, "possibleTypes": None},
        ],
    }}}


def ifield(name, type_ref, default=None):
    """An input field as introspection returns it, with its default value."""
    return {"name": name, "type": type_ref, "defaultValue": default}


def _object_type(name, fields):
    return {"kind": "OBJECT", "name": name, "fields": fields,
            "enumValues": None, "inputFields": None, "possibleTypes": None}


def _enum_type(name, values):
    return {"kind": "ENUM", "name": name, "fields": None, "inputFields": None,
            "enumValues": [{"name": value} for value in values], "possibleTypes": None}


def _scalar_type(name):
    return {"kind": "SCALAR", "name": name, "fields": None, "inputFields": None,
            "enumValues": None, "possibleTypes": None}


LIVE_ENTITY_TYPES = ["ANY", "VIRTUAL_MACHINE", "BUCKET", "FIREWALL", "NETWORK_INTERFACE",
                     "NETWORK_SECURITY_RULE", "IP_RANGE", "USER_ACCOUNT"]
LIVE_REL_TYPES = ["ANY", "CONTAINS", "CONTAINS_SRC_IP_RANGE", "CONNECTED_TO"]
LIVE_MUTATIONS = ["updateSecretInstance", "deleteCloudResource"]
_TYPE_ALIAS_RE = re.compile(r'([A-Za-z_]\w*)\s*:\s*__type\(\s*name:\s*"([^"]*)"\s*\)')


def is_full_introspection(document):
    """The full schema document: __schema types with possibleTypes, not a __type probe."""
    text = document or ""
    return "__schema" in text and "possibleTypes" in text and "__type(" not in text


def is_shape_probe(document):
    return "__type(" in (document or "")


_SEL_TOKEN_RE = re.compile(r"\.\.\.\s*[A-Za-z_]\w*|[A-Za-z_]\w*|[{}()]|\"[^\"]*\"|[^\s]")


def _selection_tree(text):
    """Parse a selection set into {field: subtree or None}. Arguments are skipped. A spread is a key."""
    tokens = _SEL_TOKEN_RE.findall(text)
    pos = 0

    def block():
        nonlocal pos
        tree, last = {}, None
        while pos < len(tokens):
            token = tokens[pos]
            pos += 1
            if token == "}":
                return tree
            if token == "{":
                if last is not None:
                    tree[last] = block()
                continue
            if token == "(":
                depth = 1
                while pos < len(tokens) and depth:
                    depth += {"(": 1, ")": -1}.get(tokens[pos], 0)
                    pos += 1
                continue
            if token.startswith("..."):
                tree[token.replace(" ", "")] = None
                last = None
                continue
            if re.fullmatch(r"[A-Za-z_]\w*", token):
                if pos < len(tokens) and tokens[pos] == ":":
                    pos += 1
                    continue
                tree[token] = None
                last = token
        return tree

    return block()


def _fragments(document):
    """Each fragment in the document as {name: selection tree}."""
    out = {}
    for match in re.finditer(r"fragment\s+([A-Za-z_]\w*)\s+on\s+[A-Za-z_]\w*\s*\{", document):
        depth, end = 1, match.end()
        while end < len(document) and depth:
            depth += {"{": 1, "}": -1}.get(document[end], 0)
            end += 1
        out[match.group(1)] = _selection_tree(document[match.end():end - 1])
    return out


def _project(value, tree, fragments):
    """Keep only what the selection names, as a GraphQL server answers it."""
    if tree is None or value is None:
        return value
    if isinstance(value, list):
        return [_project(item, tree, fragments) for item in value]
    if not isinstance(value, dict):
        return value
    if "...TypeRef" in tree:
        return value
    merged = {}
    for key, sub in tree.items():
        if key.startswith("..."):
            merged.update(fragments.get(key[3:]) or {})
        else:
            merged[key] = sub
    return {key: _project(value[key], sub, fragments) for key, sub in merged.items() if key in value}


def live_tenant_payload(with_inventory=True, inventory_project=False):
    """Introspection in the shape the live Wiz tenant publishes.

    where is the opaque scalar GraphPropertyPredicate. type is required at
    every level. negate is NON_NULL with a default, so it must never block a
    request. cloudResourcesV2 is the inventory search. inventory_project adds
    projectId to its filter input.
    """
    string = named("SCALAR", "String")
    boolean = named("SCALAR", "Boolean")
    integer = named("SCALAR", "Int")
    ident = named("SCALAR", "ID")
    entity_enum = named("ENUM", "GraphEntityType")
    rel_enum = named("ENUM", "GraphRelationshipType")
    query_ref = named("INPUT_OBJECT", "GraphEntityQueryInput")
    rel_ref = named("INPUT_OBJECT", "GraphRelationshipQueryInput")
    directed = named("INPUT_OBJECT", "GraphDirectedRelationshipTypeInput")
    query_fields = [
        field("graphSearch", non_null(named("OBJECT", "GraphSearchResultConnection")), [
            {"name": "query", "type": query_ref},
            {"name": "controlId", "type": ident},
            {"name": "issueId", "type": ident},
            {"name": "first", "type": integer},
            {"name": "after", "type": string},
            {"name": "projectId", "type": string},
            {"name": "quick", "type": boolean},
        ]),
        field("graphEntity", named("OBJECT", "GraphEntity"), [
            {"name": "id", "type": non_null(ident)},
            {"name": "type", "type": entity_enum},
        ]),
    ]
    types = [
        _scalar_type("String"), _scalar_type("Boolean"), _scalar_type("Int"), _scalar_type("ID"),
        _scalar_type("JSON"), _scalar_type("GraphPropertyPredicate"),
        _scalar_type("GraphAggregateConstraint"),
        _object_type("GraphSearchResultConnection", [
            field("nodes", listing(non_null(named("OBJECT", "GraphSearchResult")))),
            field("pageInfo", non_null(named("OBJECT", "PageInfo"))),
            field("totalCount", non_null(integer)),
            field("maxCountReached", non_null(boolean)),
        ]),
        _object_type("GraphSearchResult", [
            field("entities", listing(named("OBJECT", "GraphEntity"))),
            field("aggregateCount", integer),
        ]),
        _object_type("GraphEntity", [
            field("id", non_null(ident)), field("name", string),
            field("type", non_null(entity_enum)), field("properties", named("SCALAR", "JSON")),
        ]),
        _object_type("PageInfo", [field("hasNextPage", non_null(boolean)), field("endCursor", string)]),
        _input_type("GraphEntityQueryInput", [
            ifield("type", non_null(listing(non_null(entity_enum)))),
            ifield("as", string),
            ifield("relationships", listing(non_null(rel_ref))),
            ifield("where", named("SCALAR", "GraphPropertyPredicate")),
            ifield("select", boolean),
            ifield("aggregate", boolean),
            ifield("aggregateConstraint", named("SCALAR", "GraphAggregateConstraint")),
            ifield("blockName", string),
            ifield("blockExpanded", boolean),
        ]),
        _input_type("GraphRelationshipQueryInput", [
            ifield("type", non_null(listing(non_null(directed)))),
            ifield("with", non_null(query_ref)),
            ifield("negate", non_null(boolean), "false"),
            ifield("optional", boolean),
            ifield("optionalSubtree", boolean),
        ]),
        _input_type("GraphDirectedRelationshipTypeInput", [
            ifield("type", non_null(rel_enum)),
            ifield("reverse", boolean),
        ]),
        _enum_type("GraphEntityType", LIVE_ENTITY_TYPES),
        _enum_type("GraphRelationshipType", LIVE_REL_TYPES),
        _object_type("Mutation", [field(name, named("OBJECT", "GraphEntity")) for name in LIVE_MUTATIONS]),
    ]
    if with_inventory:
        query_fields.append(field("cloudResourcesV2", non_null(named("OBJECT", "CloudResourceV2Connection")), [
            {"name": "first", "type": integer},
            {"name": "after", "type": string},
            {"name": "filterBy", "type": named("INPUT_OBJECT", "CloudResourceV2Filters")},
        ]))
        types.extend([
            _input_type("CloudResourceV2Filters", [
                ifield("search", string),
                ifield("type", listing(non_null(entity_enum))),
                ifield("name", string),
                ifield("id", listing(non_null(string))),
            ] + ([ifield("projectId", listing(non_null(string)))] if inventory_project else [])),
            _object_type("CloudResourceV2Connection", [
                field("nodes", listing(named("OBJECT", "CloudResourceV2"))),
                field("totalCount", non_null(integer)),
                field("pageInfo", non_null(named("OBJECT", "PageInfo"))),
            ]),
            _object_type("CloudResourceV2", [
                field("id", non_null(ident)), field("name", string),
                field("type", string), field("nativeType", string),
            ]),
        ])
    types.insert(0, _object_type("Query", query_fields))
    return {"data": {"__schema": {
        "queryType": {"name": "Query"}, "mutationType": {"name": "Mutation"}, "types": types}}}


LIVE_ENTITIES = [
    {"id": "alpha-1", "name": "alpha-prod", "type": "VIRTUAL_MACHINE", "properties": {
        "nativeType": "virtualMachine", "cloudPlatform": "AWS", "accessibleFrom.internet": True, "region": "example-region",
        # The selection panel formats these. Wiz sends tags as an object, and some values as None.
        "tags": {"env": "prod", "team": "core", "owner": {"name": "sec", "deep": {"level": {"three": 3}}}},
        "isEphemeral": False, "creationDate": "2024-03-01T12:34:56.789Z", "launchtime": 1732327995,
        "updatedAt": 1732327995123, "diskSizeBytes": 1234567890, "memoryMB": 16384, "cpuLoad": 0.20101234,
        "externalUrl": "https://console.aws.amazon.com/ec2/home?region=us-east-1#Instances:instanceId=i-0abc",
        "scriptLink": "javascript:alert(1)", "description": None, "comment": "", "ports": [22, 443]}},
    {"id": "delta-5", "name": "delta-prod", "type": "VIRTUAL_MACHINE", "properties": {
        "nativeType": "virtualMachine", "cloudPlatform": "AWS", "accessibleFrom.internet": True}},
    {"id": "beta-2", "name": "beta", "type": "BUCKET", "properties": {"nativeType": "bucket", "cloudPlatform": "AWS"}},
    {"id": "boom-1", "name": "boom", "type": "BUCKET", "properties": {}},
    {"id": "gamma-3", "name": "gamma", "type": "FIREWALL", "properties": {
        "nativeType": "securityGroup", "cloudPlatform": "AWS"}},
    {"id": "fw-1", "name": "open-sg", "type": "FIREWALL", "properties": {
        "nativeType": "securityGroup", "cloudPlatform": "AWS"}},
    {"id": "nic-4", "name": "eni-0a1", "type": "NETWORK_INTERFACE", "properties": {"nativeType": "networkInterface"}},
    # Security groups and rules for the saved query. fw-1 has no rule, so it does not match.
    {"id": "sg-web", "name": "web-sg", "type": "FIREWALL", "properties": {
        "nativeType": "securityGroup", "cloudPlatform": "AWS"}},
    {"id": "sg-db", "name": "db-sg", "type": "FIREWALL", "properties": {
        "nativeType": "securityGroup", "cloudPlatform": "AWS"}},
    {"id": "sg-int", "name": "internal-sg", "type": "FIREWALL", "properties": {
        "nativeType": "securityGroup", "cloudPlatform": "AWS"}},
    {"id": "rule-ssh", "name": "ssh-world", "type": "NETWORK_SECURITY_RULE", "properties": {
        "inbound": True, "actionAllow": True}},
    {"id": "rule-https", "name": "https-world", "type": "NETWORK_SECURITY_RULE", "properties": {
        "inbound": True, "actionAllow": True}},
    {"id": "rule-lan", "name": "lan-only", "type": "NETWORK_SECURITY_RULE", "properties": {
        "inbound": True, "actionAllow": True}},
    {"id": "rule-out", "name": "egress-all", "type": "NETWORK_SECURITY_RULE", "properties": {
        "inbound": False, "actionAllow": True}},
    {"id": "ip-world", "name": "world", "type": "IP_RANGE", "properties": {"cidr": "0.0.0.0/0"}},
    {"id": "ip-lan", "name": "lan", "type": "IP_RANGE", "properties": {"cidr": "198.51.100.0/24"}},
    # A machine for the branch, chain, optional, and negate shapes. It has no
    # nativeType, so the EC2 query and the property filters do not match it.
    {"id": "vm-9", "name": "omega-vm", "type": "VIRTUAL_MACHINE", "properties": {}},
    {"id": "nic-9", "name": "eni-9", "type": "NETWORK_INTERFACE", "properties": {}},
    {"id": "user-9", "name": "svc-9", "type": "USER_ACCOUNT", "properties": {}},
    {"id": "user-10", "name": "svc-10", "type": "USER_ACCOUNT", "properties": {}},
    {"id": "ip-9", "name": "subnet-9", "type": "IP_RANGE", "properties": {"cidr": "203.0.113.0/24"}},
]
LIVE_BY_ID = {entity["id"]: entity for entity in LIVE_ENTITIES}
LIVE_NEIGHBORS = {
    "alpha-1": [("beta-2", "CONNECTED_TO"), ("gamma-3", "CONTAINS"), ("nic-4", "CONTAINS")],
    "beta-2": [("alpha-1", "CONNECTED_TO")],
    "gamma-3": [("alpha-1", "CONTAINS")],
    "nic-4": [("alpha-1", "CONTAINS")],
    "sg-web": [("rule-ssh", "CONTAINS"), ("rule-https", "CONTAINS"), ("rule-lan", "CONTAINS")],
    "sg-db": [("rule-ssh", "CONTAINS"), ("rule-out", "CONTAINS")],
    "sg-int": [("rule-lan", "CONTAINS")],
    "rule-ssh": [("sg-web", "CONTAINS"), ("sg-db", "CONTAINS"), ("ip-world", "CONTAINS_SRC_IP_RANGE")],
    "rule-https": [("sg-web", "CONTAINS"), ("ip-world", "CONTAINS_SRC_IP_RANGE")],
    "rule-lan": [("sg-web", "CONTAINS"), ("sg-int", "CONTAINS"), ("ip-lan", "CONTAINS_SRC_IP_RANGE")],
    "rule-out": [("sg-db", "CONTAINS"), ("ip-world", "CONTAINS_SRC_IP_RANGE")],
    "ip-world": [("rule-ssh", "CONTAINS_SRC_IP_RANGE"), ("rule-https", "CONTAINS_SRC_IP_RANGE"),
                 ("rule-out", "CONTAINS_SRC_IP_RANGE")],
    "ip-lan": [("rule-lan", "CONTAINS_SRC_IP_RANGE")],
    "vm-9": [("nic-9", "CONTAINS"), ("user-9", "CONTAINS"), ("user-10", "CONTAINS")],
    "nic-9": [("vm-9", "CONTAINS"), ("ip-9", "CONNECTED_TO")],
    "user-9": [("vm-9", "CONTAINS")],
    "user-10": [("vm-9", "CONTAINS")],
    "ip-9": [("nic-9", "CONNECTED_TO")],
}
LIVE_VARIABLE_ERROR = {"errors": [{"message": "invalid type for variable: 'query'"}], "data": None}
LIVE_QUICK_PAGE_ERROR = {"errors": [{"message": "pagination is not supported in quick mode"}], "data": None}
LIVE_TYPE_REQUIRED = {"errors": [{"message":
    'Field "GraphEntityQueryInput.type" of required type "[GraphEntityType!]!" was not provided.'}],
    "data": None}
LIVE_QUERY_DECLS = ("GraphEntityQueryInput", "GraphEntityQueryInput!")
LIVE_FILTER_DECLS = ("CloudResourceV2Filters", "CloudResourceV2Filters!")

# The EC2 saved query as it was before where moved into a $where variable.
OLD_EC2_DOCUMENT = (
    "query InternetExposedEC2($projectId: String!, $first: Int, $after: String, $quick: Boolean) {\n"
    "  graphSearch(\n"
    "    projectId: $projectId, first: $first, after: $after, quick: $quick,\n"
    "    query: {\n"
    "      type: [VIRTUAL_MACHINE]\n"
    "      select: true\n"
    "      where: {\n"
    "        cloudPlatform: { EQUALS: [\"AWS\"] }\n"
    "        nativeType: { EQUALS: [\"virtualMachine\"] }\n"
    "        accessibleFrom.internet: { EQUALS: true }\n"
    "      }\n"
    "    }\n"
    "  ) {\n"
    "    totalCount\n"
    "    pageInfo { hasNextPage endCursor }\n"
    "    nodes { entities { id name type properties } }\n"
    "  }\n"
    "}\n"
)


OLD_EC2_ONE_LINE = (
    "query X($first: Int) { graphSearch(first: $first, query: { type: [VIRTUAL_MACHINE] select: true "
    "where: { accessibleFrom.internet: { EQUALS: true } } }) { totalCount nodes { entities { id } } } }"
)


def _live_typed(query):
    """True when every level and every relationship carries a non-empty type."""
    if not isinstance(query, dict) or not query.get("type"):
        return False
    for rel in query.get("relationships") or []:
        if not isinstance(rel, dict) or not rel.get("type"):
            return False
        if not _live_typed(rel.get("with")):
            return False
    return True


def _live_anchored(query):
    where = query.get("where") if isinstance(query, dict) else None
    return isinstance(where, dict) and "_vertexID" in where


def _live_slow(query):
    """The shapes that time out on the tenant (behaviours 4 and 9).

    Type ANY with a text operator and no _vertexID times out. Type ANY with
    EQUALS answers (probed on name, nativeType, and cloudPlatform).
    """
    if not isinstance(query, dict):
        return False
    where = query.get("where") if isinstance(query.get("where"), dict) else {}
    text_ops = ("CONTAINS", "CONTAINS_ANY", "STARTS_WITH", "ENDS_WITH")
    if query.get("type") == ["ANY"] and not _live_anchored(query) and any(
            isinstance(spec, dict) and any(op in spec for op in text_ops) for spec in where.values()):
        return True
    if not _live_anchored(query):
        for rel in query.get("relationships") or []:
            if isinstance(rel, dict) and _live_anchored(rel.get("with")):
                return True
    return False


def _live_match(entity, query):
    """The tenant match rules. id and an unknown key match nothing."""
    if not isinstance(query, dict):
        return False
    wanted = query.get("type") or []
    if "ANY" not in wanted and entity["type"] not in wanted:
        return False
    where = query.get("where")
    if where is None:
        return True
    if not isinstance(where, dict):
        return False
    for key, spec in where.items():
        if not isinstance(spec, dict):
            return False
        if key == "_vertexID":
            values = spec.get("EQUALS")
            values = values if isinstance(values, list) else [values]
            if entity["id"] not in values:
                return False
        elif key == "name":
            if "CONTAINS" in spec:
                values = spec["CONTAINS"] if isinstance(spec["CONTAINS"], list) else [spec["CONTAINS"]]
                if not any(str(value).lower() in entity["name"].lower() for value in values):
                    return False
            elif "EQUALS" in spec:
                values = spec["EQUALS"] if isinstance(spec["EQUALS"], list) else [spec["EQUALS"]]
                if entity["name"] not in values:
                    return False
            else:
                return False
        elif key == "id":
            return False
        else:
            props = entity.get("properties") or {}
            if key not in props or "EQUALS" not in spec:
                return False
            expected = spec["EQUALS"]
            if isinstance(expected, list):
                if props[key] not in expected:
                    return False
            elif props[key] != expected:
                return False
    return True


def _live_enum_error(query):
    if not isinstance(query, dict):
        return None
    for value in query.get("type") or []:
        if value not in LIVE_ENTITY_TYPES:
            return f'Value "{value}" does not exist in "GraphEntityType" enum.'
    for rel in query.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        for item in rel.get("type") or []:
            name = item.get("type") if isinstance(item, dict) else item
            if name not in LIVE_REL_TYPES:
                return f'Value "{name}" does not exist in "GraphRelationshipType" enum.'
        found = _live_enum_error(rel.get("with"))
        if found:
            return found
    return None


def _live_decl_error(document, name, allowed):
    """A validation error when $name is declared with a type the argument does not take."""
    found = re.search(r"\$" + name + r"\s*:\s*(\[?[A-Za-z_]\w*!?\]?!?)", document)
    declared = found.group(1) if found else ""
    if declared in allowed:
        return None
    return {"errors": [{"message":
        f'Variable "${name}" of type "{declared}" used in position expecting type "{allowed[0]}".'}],
        "data": None}


def _live_page(rows, variables):
    try:
        first = max(1, int(variables.get("first") or 50))
    except (TypeError, ValueError):
        first = 50
    try:
        start = int(variables.get("after") or 0)
    except (TypeError, ValueError):
        start = 0
    end = start + first
    has_next = end < len(rows)
    return rows[start:end], {"hasNextPage": has_next, "endCursor": str(end) if has_next else None}


def _live_slots(level):
    """How many entities one level adds to a path. A negated clause adds none."""
    if not isinstance(level, dict):
        return 0
    count = 1 if level.get("select") else 0
    for rel in level.get("relationships") or []:
        if isinstance(rel, dict) and not rel.get("negate"):
            count += _live_slots(rel.get("with"))
    return count


def _live_walk(level, entity, on_path):
    """The path rows that one matched entity gives, in the live order.

    A row lists the selected levels depth-first in clause order, the outer
    entity first. A missing optional entity is None in its slot. A negated
    clause keeps the entity only when nothing matches it, and adds no slot.
    A path does not visit the same entity twice.
    """
    rows = [[entity] if level.get("select") else []]
    on_path = on_path + [entity["id"]]
    for rel in level.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        names = [item.get("type") for item in rel.get("type") or [] if isinstance(item, dict)]
        inner = rel.get("with") or {}
        found = []
        for other, kind in LIVE_NEIGHBORS.get(entity["id"], []):
            if other in on_path or ("ANY" not in names and kind not in names):
                continue
            neighbor = LIVE_BY_ID[other]
            if _live_match(neighbor, inner):
                found.extend(_live_walk(inner, neighbor, on_path))
        if rel.get("negate"):
            if found:
                return []
            continue
        if not found:
            if not (rel.get("optional") or rel.get("optionalSubtree")):
                return []
            found = [[None] * _live_slots(inner)]
        rows = [row + extra for row in rows for extra in found]
    return rows


_MOCK_TOKEN_RE = re.compile(
    r'(?P<skip>[\s,]+|#[^\n]*)|(?P<str>"(?:[^"\\\n]|\\.)*")|(?P<var>\$[A-Za-z_]\w*)'
    r'|(?P<name>[A-Za-z_]\w*)|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|(?P<punct>[{}\[\]:()])')


def _mock_inline_query(body, variables):
    """The mock's own reading of the inline query literal. It is not the adapter's parser."""
    start = re.search(r"\bquery:\s*", body)
    if not start:
        raise ValueError("no query argument")
    tokens, pos = [], start.end()
    while pos < len(body):
        match = _MOCK_TOKEN_RE.match(body, pos)
        if not match:
            raise ValueError(f"bad token at {pos}")
        pos = match.end()
        if match.lastgroup != "skip":
            tokens.append((match.lastgroup, match.group()))
    at = 0

    def value():
        nonlocal at
        kind, text = tokens[at]
        at += 1
        if text == "{":
            out = {}
            while tokens[at][1] != "}":
                key = tokens[at][1]
                if tokens[at + 1][1] != ":":
                    raise ValueError("expected :")
                at += 2
                out[key] = value()
            at += 1
            return out
        if text == "[":
            out = []
            while tokens[at][1] != "]":
                out.append(value())
            at += 1
            return out
        if kind == "str":
            return json.loads(text)
        if kind == "num":
            return json.loads(text)
        if kind == "var":
            return variables.get(text[1:])
        if kind == "name":
            return {"true": True, "false": False, "null": None}.get(text, text)
        raise ValueError(f"unexpected {text}")

    return value()


class LiveTenant:
    """A mock of the live Wiz contract that FINDINGS.md records.

    answer(document, variables) returns (HTTP code, body). requests keeps
    every body. A slow shape raises TimeoutError in "raise" mode and returns
    HTTP 504 in "http" mode.
    """

    def __init__(self, with_inventory=True, count=10000, floor=True, timeouts="raise", inventory_project=False,
                 type_probe="answer", rename=None):
        self.with_inventory = with_inventory
        self.count = count
        self.floor = floor
        self.timeouts = timeouts
        self.payload = live_tenant_payload(with_inventory, inventory_project)
        if rename:
            text = json.dumps(self.payload)
            for old, new in rename.items():
                text = text.replace(f'"{old}"', f'"{new}"')
            self.payload = json.loads(text)
        # type_probe "reject" answers any __type document with a validation error.
        self.type_probe = type_probe
        # A name in hidden is null on every __type answer, so the probe cannot finish.
        self.hidden = set()
        self.type_names = []
        self.requests = []

    def full_introspections(self):
        return [body for body in self.requests if is_full_introspection(body["query"])]

    def shape_probes(self):
        return [body for body in self.requests if is_shape_probe(body["query"])]

    def _types(self, document):
        """Resolve each aliased __type(name: "X") from this tenant's own types.

        An unknown name is null, as a GraphQL server answers it. possibleTypes
        is left out because the probe does not select it.
        """
        if self.type_probe == "reject":
            return 200, {"errors": [{"message": 'Cannot query field "__type" on type "Query".'}], "data": None}
        # A spec-compliant server refuses a fragment that no selection spreads.
        fragments = _fragments(document)
        body = re.sub(r"fragment\s+[A-Za-z_]\w*\s+on[\s\S]*", "", document)
        for name in fragments:
            if not re.search(r"\.\.\.\s*" + name + r"\b", re.sub(r"fragment\s+" + name + r"\b", "", document)):
                return 200, {"errors": [{"message": f'Fragment "{name}" is never used.'}], "data": None}
        _, by_name = gql._schema_types(self.payload)
        schema = self.payload["data"]["__schema"]
        start = body.find("{")
        root = _selection_tree(body[start + 1:body.rfind("}")]) if start >= 0 else {}
        data = {}
        if "__schema" in root:
            data["__schema"] = _project(
                {"queryType": schema["queryType"], "mutationType": schema["mutationType"]},
                root["__schema"], fragments)
        for alias, name in _TYPE_ALIAS_RE.findall(document):
            self.type_names.append(name)
            found = by_name.get(name)
            if found is None or name in self.hidden:
                data[alias] = None
                continue
            # Each answer holds only what the document selects for this alias.
            match = re.search(re.escape(alias) + r'\s*:\s*__type\([^)]*\)\s*\{', body)
            depth, end = 1, match.end()
            while end < len(body) and depth:
                depth += {"{": 1, "}": -1}.get(body[end], 0)
                end += 1
            data[alias] = _project(found, _selection_tree(body[match.end():end - 1]), fragments)
        return 200, {"data": data}

    def searches(self):
        """The bodies that searched the tenant. Introspection is left out."""
        return [body for body in self.requests
                if "graphSearch" in body["query"] or "cloudResourcesV2" in body["query"]]

    def answer(self, document, variables):
        document = document or ""
        variables = variables if isinstance(variables, dict) else {}
        self.requests.append({"query": document, "variables": json.loads(json.dumps(variables))})
        if "__type(" in document:
            return self._types(document)
        if "__schema" in document:
            return 200, self.payload
        if "cloudResourcesV2" in document:
            return self._inventory(document, variables)
        if "graphSearch" not in document:
            return 200, {"data": {"__typename": "Query"}}
        # The tenant refuses a cursor in quick mode.
        if variables.get("after") is not None and variables.get("quick") is True:
            return 200, LIVE_QUICK_PAGE_ERROR
        if re.search(r"query:\s*\$query\b", document):
            invalid = _live_decl_error(document, "query", LIVE_QUERY_DECLS)
            if invalid:
                return 200, invalid
            query = variables.get("query")
            if not _live_typed(query):
                return 200, LIVE_VARIABLE_ERROR
            return self._evaluate(document, query, variables)
        if re.search(r"query:\s*\{", document):
            return self._inline(document, variables)
        return 200, LIVE_TYPE_REQUIRED

    def _inventory(self, document, variables):
        if not self.with_inventory:
            return 200, {"errors": [{"message": 'Cannot query field "cloudResourcesV2" on type "Query".'}],
                         "data": None}
        invalid = _live_decl_error(document, "filterBy", LIVE_FILTER_DECLS)
        if invalid:
            return 200, invalid
        flt = variables.get("filterBy") if isinstance(variables.get("filterBy"), dict) else {}
        text = flt.get("search")
        if not isinstance(text, str) or not text:
            return 200, {"errors": [{"message": "filterBy.search is required"}], "data": None}
        if text == "explode":
            return 200, {"errors": [{"message": "upstream request failed"}], "data": None}
        rows = [{"id": entity["id"], "name": entity["name"], "type": entity["type"],
                 "nativeType": entity["properties"].get("nativeType")}
                for entity in LIVE_ENTITIES if text.lower() in entity["name"].lower()]
        shown, page = _live_page(rows, variables)
        # The tenant reports totalCount only on the first page. A later page says 0.
        total = 0 if variables.get("after") is not None else len(rows)
        return 200, {"data": {"cloudResourcesV2": {"nodes": shown, "totalCount": total, "pageInfo": page}}}

    def _inline(self, document, variables):
        # The answer is data.graphSearch, so an aliased graphSearch is not the one read.
        roots = [m for m in re.finditer(r"\bgraphSearch\s*\(", document)
                 if not document[:m.start()].rstrip().endswith(":")]
        body = document[roots[0].end():] if roots else document.split("graphSearch(", 1)[-1]
        if re.search(r"[{\s,][A-Za-z_]\w*\.[A-Za-z_]\w*\s*:", body):
            return 200, {"errors": [{"message": "Syntax Error: Unterminated spread operator."}], "data": None}
        if "type:" not in document:
            return 200, LIVE_TYPE_REQUIRED
        try:
            query = _mock_inline_query(body, variables)
        except (ValueError, IndexError, KeyError):
            return 200, {"errors": [{"message": "Syntax Error: Unexpected token."}], "data": None}
        if not _live_typed(query):
            return 200, LIVE_TYPE_REQUIRED
        return self._evaluate(document, query, variables)

    def _evaluate(self, document, query, variables):
        error = _live_enum_error(query)
        if error:
            return 200, {"errors": [{"message": error}], "data": None}
        if _live_slow(query):
            if self.timeouts == "raise":
                raise TimeoutError("timed out")
            return 504, {"errors": [{"message": "upstream timeout"}]}
        if "entities" not in document:
            return 200, {"data": {"graphSearch": {"totalCount": self.count, "maxCountReached": self.floor}}}
        outer = [entity for entity in LIVE_ENTITIES if _live_match(entity, query)]
        rels = [rel for rel in query.get("relationships") or [] if isinstance(rel, dict)]
        if rels and any(entity["id"] == "boom-1" for entity in outer):
            return 502, {"errors": [{"message": "upstream error"}]}
        paths = [row for entity in outer for row in _live_walk(query, entity, [])]
        return self._paths(document, paths, variables)

    def _paths(self, document, paths, variables):
        full = "properties" in document
        shown, page = _live_page(paths, variables)
        nodes = []
        for path in shown:
            entities = []
            for entity in path:
                if entity is None:
                    entities.append(None)
                    continue
                row = {"id": entity["id"], "name": entity["name"], "type": entity["type"]}
                if full:
                    row["properties"] = dict(entity["properties"])
                entities.append(row)
            nodes.append({"entities": entities})
        return 200, {"data": {"graphSearch": {
            "totalCount": len(paths), "maxCountReached": False, "pageInfo": page, "nodes": nodes}}}


def _live_source(tenant, project_id="*", **kwargs):
    def opener(request, timeout=None):
        body = json.loads(request.data.decode())
        code, payload = tenant.answer(body.get("query") or "", body.get("variables") or {})
        return Resp(code, json.dumps(payload).encode())

    kwargs.setdefault("max_retries", 3)
    return GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.us/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN", "project_id": project_id,
    }, client=client(opener, **kwargs))


def _assert_live_invariants(requests):
    """Every adapter-built graphSearch carries type everywhere and avoids the slow shapes."""
    problems = []
    for body in requests:
        document = body.get("query") or ""
        if not re.match(r"\s*query (GraphSearch|EntityTotal)\b", document):
            continue
        query = (body.get("variables") or {}).get("query")
        if "query: {" in document or "query: $query" not in document:
            problems.append("inline query object")
        if not _live_typed(query):
            problems.append("a level or a relationship has no type")
        if _live_slow(query):
            problems.append("a slow shape was sent")
    return problems


def _searched(tenant, before):
    return tenant.searches()[before:]


def test_live_tenant_contract():
    """The adapter follows the live Wiz contract. Each check failed before this change."""
    print("live tenant contract")
    tenant = LiveTenant()
    src = _live_source(tenant)

    def call(name, fn):
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001
            return None, exc

    # a. A name with no entity type uses the inventory search.
    before = len(tenant.searches())
    found, err = call("a", lambda: src.search("alpha"))
    sent = _searched(tenant, before)
    check("a: a name with no entity type uses cloudResourcesV2",
          err is None and len(sent) == 1 and "cloudResourcesV2" in sent[0]["query"]
          and "graphSearch" not in sent[0]["query"]
          and sent[0]["variables"].get("filterBy") == {"search": "alpha"}
          and [node["id"] for node in found["nodes"]] == ["alpha-1"],
          safe(err or sent))

    # b. A typed name search sends $query with type.
    before = len(tenant.searches())
    found, err = call("b", lambda: src.search("alpha", category="VIRTUAL_MACHINE"))
    sent = _searched(tenant, before)
    body = sent[-1] if sent else {}
    check("b: a typed name search sends a $query variable with type",
          err is None and "$query: GraphEntityQueryInput" in body.get("query", "")
          and body.get("variables", {}).get("query") == {
              "select": True, "type": ["VIRTUAL_MACHINE"], "where": {"name": {"CONTAINS": ["alpha"]}}}
          and [node["id"] for node in found["nodes"]] == ["alpha-1"],
          safe(err or body))

    # c. A property with no entity type is EQUALS on type ANY. The tenant answers that in about a second.
    before = len(tenant.searches())
    found, err = call("c", lambda: src.search("", prop="nativeType", value="virtualMachine"))
    sent = _searched(tenant, before)
    query = (sent[-1]["variables"].get("query") if sent else None) or {}
    check("c: a property filter with no entity type sends type ANY and EQUALS",
          err is None and query.get("type") == ["ANY"]
          and query.get("where") == {"nativeType": {"EQUALS": ["virtualMachine"]}}
          and [node["id"] for node in found["nodes"]] == ["alpha-1", "delta-5"],
          safe(err or query))
    before = len(tenant.searches())
    found, err = call("c2", lambda: src.search(
        "", category="VIRTUAL_MACHINE", prop="nativeType", value="virtualMachine"))
    sent = _searched(tenant, before)
    query = (sent[-1]["variables"].get("query") if sent else None) or {}
    check("c: a typed property filter sends the type and the key",
          err is None and query.get("type") == ["VIRTUAL_MACHINE"]
          and query.get("where") == {"nativeType": {"EQUALS": ["virtualMachine"]}}
          and [node["id"] for node in found["nodes"]] == ["alpha-1", "delta-5"],
          safe(err or query))

    # d. id is sent as _vertexID.
    before = len(tenant.searches())
    found, err = call("d", lambda: src.search("", prop="id", value="alpha-1"))
    sent = _searched(tenant, before)
    query = (sent[-1]["variables"].get("query") if sent else None) or {}
    check("d: an id filter is sent as _vertexID",
          err is None and query.get("where") == {"_vertexID": {"EQUALS": ["alpha-1"]}}
          and [node["id"] for node in found["nodes"]] == ["alpha-1"],
          safe(err or query))

    # e. A flag value becomes a boolean and a dotted key stays as it is.
    before = len(tenant.searches())
    found, err = call("e", lambda: src.search(
        "", category="VIRTUAL_MACHINE", prop="accessibleFrom.internet", value="TRUE"))
    sent = _searched(tenant, before)
    query = (sent[-1]["variables"].get("query") if sent else None) or {}
    check("e: a dotted flag key sends EQUALS true",
          err is None and query.get("where") == {"accessibleFrom.internet": {"EQUALS": True}}
          and [node["id"] for node in found["nodes"]] == ["alpha-1", "delta-5"],
          safe(err or query))

    # f. A misspelt key returns nothing. A malformed key is refused.
    found, err = call("f", lambda: src.search("", category="VIRTUAL_MACHINE", prop="regoin", value="example-region"))
    check("f: a misspelt key returns no result and no error",
          err is None and found.get("nodes") == [], safe(err or found))
    before = len(tenant.searches())
    found, err = call("f2", lambda: src.search("", category="VIRTUAL_MACHINE", prop="bad key!", value="x"))
    check("f: a malformed key is refused before it is sent",
          isinstance(err, ValueError) and "not a filter" in str(err) and not _searched(tenant, before),
          safe(err or found))

    # g. A name and a property need an entity type.
    before = len(tenant.searches())
    found, err = call("g", lambda: src.search("alpha", prop="nativeType", value="x"))
    check("g: a name and a property with no entity type are refused",
          isinstance(err, ValueError) and "combine" in str(err) and not _searched(tenant, before),
          safe(err or found))

    # h. A schema type is refused. ANY is the same as no entity type.
    before = len(tenant.searches())
    found, err = call("h", lambda: src.search("alpha", category="APICallComponent"))
    check("h: a schema type is refused before it is sent",
          isinstance(err, ValueError) and "APICallComponent" in str(err) and not _searched(tenant, before),
          safe(err or found))
    before = len(tenant.searches())
    found, err = call("h2", lambda: src.search("delta", category="ANY"))
    sent = _searched(tenant, before)
    check("h: ANY acts as no entity type",
          err is None and sent and "cloudResourcesV2" in sent[-1]["query"]
          and [node["id"] for node in found["nodes"]] == ["delta-5"],
          safe(err or sent))

    # i. Opening nodes uses ANY and _vertexID.
    before = len(tenant.searches())
    found, err = call("i", lambda: src.nodes(["alpha-1", "beta-2"]))
    sent = _searched(tenant, before)
    query = (sent[-1]["variables"].get("query") if sent else None) or {}
    nodes = (found or {}).get("nodes") or []
    check("i: opening nodes sends ANY and _vertexID and returns properties",
          err is None and query.get("type") == ["ANY"]
          and query.get("where") == {"_vertexID": {"EQUALS": ["alpha-1", "beta-2"]}}
          and [node["id"] for node in nodes] == ["alpha-1", "beta-2"]
          and nodes[0]["props"].get("region") == "example-region" and nodes[0]["partial"] is False,
          safe(err or query))

    # j. Expand anchors the outer entity.
    before = len(tenant.searches())
    found, err = call("j", lambda: src.expand(["alpha-1"], []))
    sent = _searched(tenant, before)
    query = (sent[0]["variables"].get("query") if sent else None) or {}
    rel = (query.get("relationships") or [{}])[0]
    ids = [node["id"] for node in (found or {}).get("nodes") or []]
    edges = [edge["id"] for edge in (found or {}).get("edges") or []]
    check("j: expand anchors the outer entity by _vertexID with ANY types",
          err is None and query.get("type") == ["ANY"]
          and query.get("where") == {"_vertexID": {"EQUALS": ["alpha-1"]}}
          and rel.get("type") == [{"type": "ANY"}]
          and (rel.get("with") or {}).get("type") == ["ANY"]
          and ids == ["beta-2", "gamma-3", "nic-4"]
          and edges == ["alpha-1:related:beta-2", "alpha-1:related:gamma-3", "alpha-1:related:nic-4"],
          safe(err or {"query": query, "ids": ids}))

    # k. Category, name, and relationship filters apply to the neighbor.
    only_nic, err1 = call("k1", lambda: src.expand(["alpha-1"], [], categories=["NETWORK_INTERFACE"]))
    only_gam, err2 = call("k2", lambda: src.expand(["alpha-1"], [], q="gam"))
    contains, err3 = call("k3", lambda: src.expand(["alpha-1"], [], types=["CONTAINS"]))
    check("k: category, name, and relationship type filter the neighbors",
          not (err1 or err2 or err3)
          and [node["id"] for node in only_nic["nodes"]] == ["nic-4"]
          and [node["id"] for node in only_gam["nodes"]] == ["gamma-3"]
          and [node["id"] for node in contains["nodes"]] == ["gamma-3", "nic-4"],
          safe(err1 or err2 or err3))
    before = len(tenant.searches())
    _, bad_type = call("k4", lambda: src.expand(["alpha-1"], [], types=["NOPE"]))
    _, bad_cat = call("k5", lambda: src.expand(["alpha-1"], [], categories=["NOPE"]))
    check("k: an unknown relationship type or category is refused before it is sent",
          isinstance(bad_type, ValueError) and isinstance(bad_cat, ValueError)
          and not _searched(tenant, before),
          safe((bad_type, bad_cat)))

    # l. Expand pages.
    one, err1 = call("l1", lambda: src.expand(["alpha-1"], [], limit=1))
    nxt, err2 = call("l2", lambda: src.expand(["alpha-1"], ["alpha-1", "beta-2"], limit=1))
    check("l: expand returns one neighbor page and then the next",
          not (err1 or err2)
          and [node["id"] for node in one["nodes"]] == ["beta-2"] and one.get("more") is True
          and [node["id"] for node in nxt["nodes"]] == ["gamma-3"],
          safe(err1 or err2 or (one, nxt)))

    # l. A later page sends quick false. The tenant refuses a cursor in quick mode.
    paged = [body["variables"] for body in tenant.searches()
             if "graphSearch" in body["query"] and body["variables"].get("after") is not None]
    check("l: an expand page with a cursor sends quick false",
          paged and all(item.get("quick") is False for item in paged),
          safe(paged))

    # l. A name search that walks pages keeps the first total. cloudResourcesV2 reports 0 after page one.
    walked, err = call("l4", lambda: src.search("prod", limit=1, offset=1))
    check("l: a paged name search keeps the total from the first page",
          err is None and [node["id"] for node in walked["nodes"]] == ["delta-5"] and walked.get("total") == 2,
          safe(err or walked))

    # m. Neighbor summary.
    summary, err1 = call("m1", lambda: src.neighbor_summary(["alpha-1"], []))
    hidden, err2 = call("m2", lambda: src.neighbor_summary(["alpha-1"], ["beta-2"]))
    check("m: neighbor summary counts each neighbor type",
          not (err1 or err2) and summary.get("degree") == 3
          and summary.get("categories") == {"BUCKET": 1, "FIREWALL": 1, "NETWORK_INTERFACE": 1}
          and hidden.get("total") == 2,
          safe(err1 or err2 or (summary, hidden)))

    # n. Edges between visible nodes.
    between, err = call("n", lambda: src.edges_between(["alpha-1", "beta-2", "gamma-3"]))
    check("n: edges between visible nodes come from the anchored expand",
          err is None and [edge["id"] for edge in between["edges"]] == [
              "alpha-1:related:beta-2", "alpha-1:related:gamma-3"],
          safe(err or between))

    # o. The node total.
    before = len(tenant.searches())
    status, err = call("o", lambda: src.status())
    sent = _searched(tenant, before)
    count_vars = sent[-1]["variables"] if sent else {}
    check("o: the node total sends type ANY and reports a floor",
          err is None and count_vars.get("query") == {"type": ["ANY"], "select": True}
          and status.get("nodes") == 10000 and status.get("nodes_floor") is True,
          safe(err or count_vars))

    # p. Saved queries.
    wiz = json.loads((ROOT / "data" / "wiz-queries.json").read_text())
    saved, err1 = call("p1", lambda: src.run_document(wiz[0]["graphql"], dict(wiz[0]["variables"]), limit=500))
    fws, err2 = call("p2", lambda: src.run_document(wiz[1]["graphql"], dict(wiz[1]["variables"]), limit=50))
    before = len(tenant.searches())
    paged_vars = dict(wiz[0]["variables"])
    paged_vars["first"] = 1
    paged, err3 = call("p3", lambda: src.run_document(wiz[0]["graphql"], paged_vars, limit=500))
    page_calls = len(_searched(tenant, before))
    _, old = call("p4", lambda: src.run_document(OLD_EC2_DOCUMENT, {"projectId": "*", "first": 5}, limit=5))
    _, one_line = call("p5", lambda: src.run_document(OLD_EC2_ONE_LINE, {"first": 5}, limit=5))
    check("p: the saved Wiz searches run on the live contract",
          not (err1 or err2 or err3)
          and [node["id"] for node in saved["nodes"]] == ["alpha-1", "delta-5"]
          and [node["id"] for node in fws["nodes"]] == ["sg-web", "rule-ssh", "rule-https", "sg-db"]
          and [node["id"] for node in paged["nodes"]] == ["alpha-1", "delta-5"] and page_calls == 2,
          safe(err1 or err2 or err3 or page_calls))
    fw_edges = sorted((fws or {}).get("edges") or [], key=lambda edge: edge["id"])
    kinds = {node["id"]: node["category"] for node in (fws or {}).get("nodes") or []}
    check("p: the security-group query draws FIREWALL -> NETWORK_SECURITY_RULE edges",
          [edge["id"] for edge in fw_edges] == [
              "sg-db:related:rule-ssh", "sg-web:related:rule-https", "sg-web:related:rule-ssh"]
          and all(edge["type"] == "related" for edge in fw_edges)
          and all((kinds.get(edge["source"]), kinds.get(edge["target"])) == ("FIREWALL", "NETWORK_SECURITY_RULE")
                  for edge in fw_edges)
          and all(edge["props"] == {"relationship": ["CONTAINS"], "indirect": False} for edge in fw_edges)
          and (fws or {}).get("total") == 3 and "edges_unknown" not in (fws or {}).get("truncation", {}).get("reasons", []),
          safe(fws))
    check("p: a shared rule gets an edge from each group",
          sorted(edge["source"] for edge in fw_edges if edge["target"] == "rule-ssh") == ["sg-db", "sg-web"],
          safe(fw_edges))
    check("p: a one-level saved query has no edges and no unknown edges",
          (saved or {}).get("edges") == []
          and "edges_unknown" not in (saved or {}).get("truncation", {}).get("reasons", []),
          safe(saved))
    check("p: an inline dotted key explains the $where fix",
          isinstance(old, GraphQLCallError) and "$where" in str(old), safe(old))
    check("p: a one-line inline dotted key is the same parse error",
          isinstance(one_line, GraphQLCallError) and "$where" in str(one_line)
          and "Unterminated spread operator" in str(one_line), safe(one_line))

    # q. No inventory search means a name needs an entity type.
    bare = LiveTenant(with_inventory=False)
    bare_src = _live_source(bare)
    found, err = call("q", lambda: bare_src.search("alpha"))
    check("q: with no inventory search a name needs an entity type",
          isinstance(err, ValueError) and "Choose an entity type" in str(err) and not bare.searches(),
          safe(err or found))

    # r. The schema tells the page how to filter.
    model, err = call("r", lambda: src.schema())
    check("r: schema publishes entity types, open filters, and the inventory search",
          err is None and "ANY" not in model.get("entities", ["ANY"])
          and model.get("entities") == sorted(LIVE_ENTITY_TYPES[1:])
          and model.get("filter_mode") == "open" and model.get("name_search") == "inventory"
          and model.get("filters") == sorted(getattr(gql, "WIZ_COMMON_KEYS", ("missing",))),
          safe(err or {key: model.get(key) for key in ("entities", "filters", "filter_mode", "name_search")}))

    # s. The slow-shape guard.
    before = len(tenant.searches())
    _, slow = call("s1", lambda: src.adapter._run(
        {"type": ["ANY"], "select": True, "where": {"name": {"CONTAINS": ["x"]}}}, 5, None, False))
    _, inner = call("s2", lambda: src.adapter._run(
        {"type": ["ANY"], "select": True, "relationships": [
            {"type": [{"type": "ANY"}], "with": {"type": ["ANY"], "where": {"_vertexID": {"EQUALS": ["alpha-1"]}}}}]},
        5, None, False))
    _, starts = call("s3", lambda: src.adapter._run(
        {"type": ["ANY"], "select": True, "where": {"name": {"STARTS_WITH": ["x"]}}}, 5, None, False))
    check("s: the slow shapes are refused before they are sent",
          isinstance(slow, ValueError) and isinstance(inner, ValueError) and isinstance(starts, ValueError)
          and not _searched(tenant, before),
          safe((slow, inner, starts)))
    equals, err = call("s4", lambda: src.adapter._run(
        {"type": ["ANY"], "select": True, "where": {"name": {"EQUALS": ["beta"]}}}, 5, None, False))
    check("s: type ANY with EQUALS is sent",
          err is None and len(_searched(tenant, before)) == 1
          and [e["id"] for e in (gql._graph_search(equals)[0] or [[]])[0]] == ["beta-2"],
          safe(err or equals))

    # t. Required fields are checked before a request exists.
    live = tenant.payload
    _, missing_rel = call("t1", lambda: gql.conform_wiz_query(
        live, {"type": ["ANY"], "select": True, "relationships": [{"with": {"type": ["ANY"]}}]}))
    _, missing_type = call("t2", lambda: gql.conform_wiz_query(live, {"type": ["NOPE"]}))
    fine, negate = call("t3", lambda: gql.conform_wiz_query(
        live, {"type": ["ANY"], "relationships": [{"type": [{"type": "ANY"}], "with": {"type": ["ANY"]}}]}))
    check("t: a missing required type is refused and a default is not required",
          isinstance(missing_rel, ValueError) and "GraphRelationshipQueryInput.type is required" in str(missing_rel)
          and isinstance(missing_type, ValueError) and "GraphEntityQueryInput.type is required" in str(missing_type)
          and negate is None and fine is not None,
          safe((missing_rel, missing_type, negate)))

    # v. The mock validates declared variable types, as a GraphQL server does.
    code, wrong = tenant.answer(
        "query B($query: String) { graphSearch(query: $query) { totalCount nodes { entities { id } } } }",
        {"query": {"type": ["BUCKET"]}})
    code_ok, right = tenant.answer(
        "query B($query: GraphEntityQueryInput!) { graphSearch(query: $query) { totalCount nodes { entities { id } } } }",
        {"query": {"type": ["BUCKET"]}})
    code_inv, wrong_inv = tenant.answer(
        "query C($filterBy: JSON) { cloudResourcesV2(filterBy: $filterBy) { totalCount nodes { id } } }",
        {"filterBy": {"search": "alpha"}})
    check("v: a $query or $filterBy declared with the wrong type is a validation error",
          "String" in json.dumps(wrong.get("errors")) and not wrong.get("data")
          and (right.get("data") or {}).get("graphSearch", {}).get("totalCount") == 2
          and "JSON" in json.dumps(wrong_inv.get("errors")) and not wrong_inv.get("data"),
          safe((wrong, right, wrong_inv)))

    # w. A scoped source keeps its scope on a name search with no entity type.
    scoped = LiveTenant()
    scoped_src = _live_source(scoped, project_id="proj-1")
    found, err = call("w1", lambda: scoped_src.search("alpha"))
    model, err_model = call("w2", lambda: scoped_src.schema())
    check("w: a scoped source refuses the inventory search when its filter has no projectId",
          isinstance(err, ValueError) and "Choose an entity type" in str(err) and "project" in str(err)
          and not scoped.searches() and err_model is None and model.get("name_search") == "typed",
          safe(err or found))
    with_scope = LiveTenant(inventory_project=True)
    with_scope_src = _live_source(with_scope, project_id="proj-1")
    found, err = call("w3", lambda: with_scope_src.search("alpha"))
    sent = with_scope.searches()
    check("w: a scoped source sends projectId when the filter takes it",
          err is None and len(sent) == 1
          and sent[0]["variables"].get("filterBy") == {"search": "alpha", "projectId": ["proj-1"]}
          and [node["id"] for node in found["nodes"]] == ["alpha-1"],
          safe(err or sent))

    # u. Every adapter-built request kept the contract.
    problems = _assert_live_invariants(tenant.requests)
    check("u: every generated graphSearch keeps the live invariants", not problems, safe(problems))


QUERY_DOCUMENT = (
    "query Paths($query: GraphEntityQueryInput, $first: Int, $after: String) {\n"
    "  graphSearch(query: $query, first: $first, after: $after) {\n"
    "    totalCount\n"
    "    pageInfo { hasNextPage endCursor }\n"
    "    nodes { entities { id name type properties } }\n"
    "  }\n"
    "}\n"
)


def _vm9(*relationships):
    """vm-9 as the selected outer entity, with the given relationship clauses."""
    return {"type": ["VIRTUAL_MACHINE"], "select": True,
            "where": {"_vertexID": {"EQUALS": ["vm-9"]}}, "relationships": list(relationships)}


def _rel(kind, inner, **extra):
    clause = {"type": [{"type": kind}], "with": inner}
    clause.update(extra)
    return clause


def _edge_ids(result):
    return sorted(edge["id"] for edge in (result or {}).get("edges") or [])


def _reasons(result):
    return ((result or {}).get("truncation") or {}).get("reasons") or []


def _two_entity_source(documents, pages=None):
    """A source whose tenant answers every graphSearch with the paths in pages, keyed by cursor."""
    pages = pages or {None: ([[{"id": "a", "name": "a", "type": "VIRTUAL_MACHINE"},
                                {"id": "b", "name": "b", "type": "BUCKET"}]], False, None)}

    def opener(request, timeout=None):
        body = json.loads(request.data.decode())
        text = body.get("query") or ""
        if is_shape_probe(text) or "__schema" in text:
            return Resp(200, b'{"data":{}}')
        documents.append(body)
        paths, has_next, cursor = pages.get((body.get("variables") or {}).get("after"), ([], False, None))
        return Resp(200, json.dumps({"data": {"graphSearch": {
            "totalCount": 9, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": [{"entities": path} for path in paths]}}}).encode())

    return GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.us/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN",
    }, client=client(opener))


def test_saved_query_edges():
    """A saved graph search draws edges from its query tree. Each check failed before this change."""
    print("saved query edges")
    tenant = LiveTenant()
    src = _live_source(tenant)

    def run(query, **variables):
        try:
            return src.run_document(QUERY_DOCUMENT, {"query": query, **variables}, limit=200), None
        except Exception as exc:  # noqa: BLE001
            return None, exc

    plan_of = getattr(gql, "path_edges_plan", None)

    def parents(query):
        if plan_of is None:
            return "missing"
        plan = plan_of(query)
        return None if plan is None else [slot["parent"] for slot in plan]

    nic = {"type": ["NETWORK_INTERFACE"], "select": True}
    users = {"type": ["USER_ACCOUNT"], "select": True}
    branch = _vm9(_rel("CONTAINS", nic), _rel("CONTAINS", users))
    swapped = _vm9(_rel("CONTAINS", users), _rel("CONTAINS", nic))
    chain = _vm9(_rel("CONTAINS", dict(nic, relationships=[_rel("ANY", {"type": ["ANY"], "select": True})])))
    middle = _vm9(_rel("CONTAINS", {"type": ["NETWORK_INTERFACE"],
                                    "relationships": [_rel("ANY", {"type": ["ANY"], "select": True})]}))
    outer = {"type": ["VIRTUAL_MACHINE"], "where": {"_vertexID": {"EQUALS": ["vm-9"]}},
             "relationships": [_rel("CONTAINS", nic)]}
    optional = _vm9(_rel("CONTAINS", nic), _rel("CONTAINS", {"type": ["BUCKET"], "select": True}, optional=True))
    negated = _vm9(_rel("CONTAINS", nic), _rel("CONTAINS", {"type": ["BUCKET"], "select": True}, negate=True))

    # The slot plan follows the live path order.
    check("the slot plan places branch, chain, unselected middle and outer, optional, and negate",
          parents(branch) == [None, 0, 0] and parents(chain) == [None, 0, 1]
          and parents(middle) == [None, 0] and parents(outer) == [None]
          and parents(optional) == [None, 0, 0] and parents(negated) == [None, 0]
          and plan_of is not None
          and [slot["indirect"] for slot in plan_of(middle)] == [False, True]
          and plan_of(middle)[1]["relationship"] == ["CONTAINS", "ANY"]
          and plan_of(branch)[1]["relationship"] == ["CONTAINS"]
          and plan_of("not a query") is None
          and plan_of({"type": ["ANY"], "select": True, "relationships": [{"with": "x"}]}) is None,
          safe({"branch": parents(branch), "chain": parents(chain), "middle": parents(middle),
                "outer": parents(outer), "optional": parents(optional), "negated": parents(negated)}))

    # Branch: two sibling edges to the root and none between the siblings.
    found, err = run(branch)
    flipped, err2 = run(swapped)
    want = ["vm-9:related:nic-9", "vm-9:related:user-10", "vm-9:related:user-9"]
    check("a branch query gives sibling edges to the root and no sibling-to-sibling edge",
          not (err or err2) and _edge_ids(found) == want and _edge_ids(flipped) == want
          and [node["id"] for node in found["nodes"]] == ["vm-9", "nic-9", "user-9", "user-10"]
          and [node["id"] for node in flipped["nodes"]] == ["vm-9", "user-9", "nic-9", "user-10"]
          and not _reasons(found) and not _reasons(flipped),
          safe(err or err2 or {"found": _edge_ids(found), "flipped": _edge_ids(flipped)}))

    # The same branch written inline in the document.
    inline = (
        "query Branch($first: Int, $after: String) {\n"
        "  graphSearch(first: $first, after: $after, query: {\n"
        "    type: [VIRTUAL_MACHINE], select: true, where: { _vertexID: { EQUALS: [\"vm-9\"] } }\n"
        "    relationships: [\n"
        "      { type: [{ type: CONTAINS }], with: { type: [USER_ACCOUNT], select: true } }\n"
        "      { type: [{ type: CONTAINS, reverse: false }], with: { type: [NETWORK_INTERFACE], select: true } }\n"
        "    ]\n"
        "  }) {\n"
        "    totalCount\n"
        "    pageInfo { hasNextPage endCursor }\n"
        "    nodes { entities { id name type properties } }\n"
        "  }\n"
        "}\n")
    try:
        written, err = src.run_document(inline, {}, limit=200), None
    except Exception as exc:  # noqa: BLE001
        written, err = None, exc
    check("an inline branch query reads its tree from the document",
          err is None and _edge_ids(written) == want and not _reasons(written),
          safe(err or written))

    # Chain: two edges.
    found, err = run(chain)
    edges = {edge["id"]: edge for edge in (found or {}).get("edges") or []}
    check("a chain gives two edges",
          err is None and sorted(edges) == ["nic-9:related:ip-9", "vm-9:related:nic-9"]
          and edges["nic-9:related:ip-9"]["props"] == {"relationship": ["ANY"], "indirect": False}
          and edges["vm-9:related:nic-9"]["props"] == {"relationship": ["CONTAINS"], "indirect": False},
          safe(err or found))

    # Unselected middle: one indirect edge with the indirect label.
    found, err = run(middle)
    edges = (found or {}).get("edges") or []
    check("an unselected middle gives one indirect edge",
          err is None and [node["id"] for node in found["nodes"]] == ["vm-9", "ip-9"]
          and [edge["id"] for edge in edges] == ["vm-9:related (via unselected):ip-9"]
          and edges[0]["type"] == "related (via unselected)"
          and edges[0]["source"] == "vm-9" and edges[0]["target"] == "ip-9"
          and edges[0]["props"] == {"relationship": ["CONTAINS", "ANY"], "indirect": True},
          safe(err or found))

    # Optional: a null slot gives no edge and no error.
    found, err = run(optional)
    check("an optional null slot gives no edge and no error",
          err is None and [node["id"] for node in found["nodes"]] == ["vm-9", "nic-9"]
          and _edge_ids(found) == ["vm-9:related:nic-9"] and not _reasons(found),
          safe(err or found))

    # Negate: the negated clause adds no slot.
    found, err = run(negated)
    check("a negated clause adds no slot",
          err is None and [node["id"] for node in found["nodes"]] == ["vm-9", "nic-9"]
          and _edge_ids(found) == ["vm-9:related:nic-9"] and not _reasons(found),
          safe(err or found))

    # Pages: an edge that two cursor pages repeat is kept once.
    before = len(tenant.searches())
    found, err = run(branch, first=1)
    page_calls = len(_searched(tenant, before))
    check("edges deduplicate across cursor pages",
          err is None and page_calls == 2 and _edge_ids(found) == want
          and len(found["edges"]) == 3,
          safe(err or {"calls": page_calls, "edges": _edge_ids(found)}))

    # The canvas limit drops edges to nodes that did not fit.
    wiz = json.loads((ROOT / "data" / "wiz-queries.json").read_text())
    try:
        cut, err = src.run_document(wiz[1]["graphql"], dict(wiz[1]["variables"]), limit=2), None
    except Exception as exc:  # noqa: BLE001
        cut, err = None, exc
    check("the canvas limit drops edges to nodes that did not fit",
          err is None and [node["id"] for node in cut["nodes"]] == ["sg-web", "rule-ssh"]
          and _edge_ids(cut) == ["sg-web:related:rule-ssh"]
          and "row_limit" in _reasons(cut) and cut["truncation"].get("edges_returned") == 1,
          safe(err or cut))

    # An unknown tree falls back to no edges and says so.
    unknown = {
        "unparseable": "query U { graphSearch(query: { type: [ANY] select: true where: @oops }) "
                       "{ totalCount nodes { entities { id name type } } } }",
        "no query argument": "query U { graphSearch(issueId: \"i-1\") { totalCount nodes { entities { id name type } } } }",
        "unset variable": "query U($query: GraphEntityQueryInput) { graphSearch(query: $query) "
                          "{ totalCount nodes { entities { id name type } } } }",
        "slot count": "query U { graphSearch(query: { type: [ANY] select: true }) "
                      "{ totalCount nodes { entities { id name type } } } }",
    }
    for label, document in unknown.items():
        sent = []
        other = _two_entity_source(sent)
        try:
            found, err = other.run_document(document, {}, limit=50), None
        except Exception as exc:  # noqa: BLE001
            found, err = None, exc
        check(f"an unknown tree returns nodes, no edges, and edges_unknown ({label})",
              err is None and [node["id"] for node in found["nodes"]] == ["a", "b"]
              and found["edges"] == [] and "edges_unknown" in _reasons(found)
              and found["truncation"].get("message") and found["truncated"] is True,
              safe(err or found))

    # The same pair in both directions is one edge. The first direction wins.
    sent = []
    a = {"id": "a", "name": "a", "type": "VIRTUAL_MACHINE"}
    b = {"id": "b", "name": "b", "type": "VIRTUAL_MACHINE"}
    both = _two_entity_source(sent, {None: ([[a, b], [b, a]], False, None)})
    either = ("query E { graphSearch(query: { type: [ANY] select: true relationships: [{ type: [{ type: ANY }] "
              "with: { type: [ANY] select: true } }] }) { totalCount nodes { entities { id name type } } } }")
    try:
        found, err = both.run_document(either, {}, limit=50), None
    except Exception as exc:  # noqa: BLE001
        found, err = None, exc
    check("one pair in both directions is one edge, in the first direction",
          err is None and _edge_ids(found) == ["a:related:b"] and not _reasons(found),
          safe(err or found))

    # A branch inside a chain: the walk is depth first, so the slots are [VM, NIC, IP, USER].
    nested = _vm9(_rel("CONTAINS", dict(nic, relationships=[_rel("ANY", {"type": ["IP_RANGE"], "select": True})])),
                  _rel("CONTAINS", users))
    found, err = run(nested)
    check("a branch nested in a chain follows the depth-first slot order",
          parents(nested) == [None, 0, 1, 0] and err is None
          and _edge_ids(found) == ["nic-9:related:ip-9", "vm-9:related:nic-9",
                                   "vm-9:related:user-10", "vm-9:related:user-9"]
          and not any(edge["source"].startswith("user-") or edge["target"] == "ip-9" and edge["source"] != "nic-9"
                      for edge in found["edges"])
          and not _reasons(found),
          safe(err or {"parents": parents(nested), "edges": _edge_ids(found)}))

    # The plan reads only the root graphSearch field that the paths come from.
    two_fields = (
        "query Two($first: Int, $after: String) {\n"
        "  branch: graphSearch(query: { type: [VIRTUAL_MACHINE] select: true relationships: [\n"
        "    { type: [{ type: ANY }] with: { type: [NETWORK_INTERFACE] select: true } }\n"
        "    { type: [{ type: ANY }] with: { type: [IP_RANGE] select: true } } ] })"
        " { totalCount }\n"
        "  graphSearch(first: $first, after: $after, query: { type: [VIRTUAL_MACHINE] select: true\n"
        "    where: { _vertexID: { EQUALS: [\"vm-9\"] } }\n"
        "    relationships: [{ type: [{ type: CONTAINS }] with: { type: [NETWORK_INTERFACE] select: true\n"
        "      relationships: [{ type: [{ type: ANY }] with: { type: [ANY] select: true } }] } }] }) {\n"
        "    totalCount pageInfo { hasNextPage endCursor } nodes { entities { id name type properties } }\n"
        "  }\n"
        "}\n")
    try:
        found, err = src.run_document(two_fields, {}, limit=200), None
    except Exception as exc:  # noqa: BLE001
        found, err = None, exc
    check("an aliased graphSearch does not give the plan for data.graphSearch",
          err is None and _edge_ids(found) == ["nic-9:related:ip-9", "vm-9:related:nic-9"]
          and not _reasons(found),
          safe(err or found))
    parse = getattr(gql, "graph_search_query", None)
    refused = {}
    for label, document in {
        "two root fields": "query T { graphSearch(query: { type: [ANY] select: true }) { totalCount } "
                           "graphSearch(query: { type: [ANY] select: true }) { totalCount } }",
        "another field under the name": "query T { graphSearch: cloudResourcesV2(filterBy: {}) { totalCount } }",
        "only an alias": "query T { other: graphSearch(query: { type: [ANY] select: true }) { totalCount } }",
    }.items():
        try:
            parse(document, {})
            refused[label] = "parsed"
        except ValueError:
            refused[label] = "refused"
    check("two root graphSearch fields, or none under that name, leave the plan unknown",
          set(refused.values()) == {"refused"}, safe(refused))

    # An operation named graphSearch is not the field.
    named_op = QUERY_DOCUMENT.replace("query Paths(", "query graphSearch(")
    try:
        found, err = src.run_document(named_op, {"query": chain}, limit=200), None
    except Exception as exc:  # noqa: BLE001
        found, err = None, exc
    check("an operation named graphSearch still gives the field's plan",
          err is None and _edge_ids(found) == ["nic-9:related:ip-9", "vm-9:related:nic-9"]
          and not _reasons(found),
          safe(err or found))

    # GraphQL list coercion: one relationship type where a list goes.
    coerced = {}
    for label, kind in (("bare enum", "CONTAINS"), ("one object", "{ type: CONTAINS, reverse: true }")):
        document = ("query C { graphSearch(query: { type: [ANY] select: true relationships: [{ type: " + kind
                    + " with: { type: [ANY] select: true } }] }) { totalCount nodes { entities { id name type } } } }")
        try:
            got = _two_entity_source([]).run_document(document, {}, limit=50)
            coerced[label] = [(edge["id"], edge["props"]) for edge in got["edges"]], _reasons(got)
        except Exception as exc:  # noqa: BLE001
            coerced[label] = repr(exc)
    want_coerced = ([("a:related:b", {"relationship": ["CONTAINS"], "indirect": False})], [])
    check("a single relationship type coerces to a list of one",
          coerced == {"bare enum": want_coerced, "one object": want_coerced}, safe(coerced))

    # A malformed type value falls back to no edges and does not fail the query.
    malformed = {}
    for label, variables in (
            ("number", {"query": {"type": ["ANY"], "select": True,
                                  "relationships": [{"type": 7, "with": {"type": ["ANY"], "select": True}}]}}),
            ("list of numbers", {"query": {"type": ["ANY"], "select": True,
                                           "relationships": [{"type": [7], "with": {"type": ["ANY"], "select": True}}]}}),
    ):
        try:
            got = _two_entity_source([]).run_document(QUERY_DOCUMENT, variables, limit=50)
            malformed[label] = ([node["id"] for node in got["nodes"]], got["edges"], "edges_unknown" in _reasons(got))
        except Exception as exc:  # noqa: BLE001
            malformed[label] = repr(exc)
    check("a malformed relationship type gives nodes, no edges, and edges_unknown",
          malformed == {"number": (["a", "b"], [], True), "list of numbers": (["a", "b"], [], True)},
          safe(malformed))


def test_query_value_parser():
    """The inline query parser reads GraphQL value syntax only and evaluates nothing."""
    print("query value parser")
    parse = getattr(gql, "graph_search_query", None)
    if parse is None:
        check("graph_search_query exists", False, "missing")
        return
    wiz = json.loads((ROOT / "data" / "wiz-queries.json").read_text())
    groups = parse(wiz[1]["graphql"], dict(wiz[1]["variables"]))
    rule = ((groups.get("relationships") or [{}])[0]).get("with") or {}
    ip = ((rule.get("relationships") or [{}])[0]).get("with") or {}
    check("the security-group document parses to its query object",
          groups.get("type") == ["FIREWALL"] and groups.get("select") is True
          and groups["relationships"][0]["type"] == [{"type": "CONTAINS"}]
          and rule.get("type") == ["NETWORK_SECURITY_RULE"] and rule.get("select") is True
          and rule.get("where") == {"inbound": {"EQUALS": True}, "actionAllow": {"EQUALS": True}}
          and ip == {"type": ["IP_RANGE"], "where": {"cidr": {"EQUALS": ["0.0.0.0/0"]}}},
          safe(groups))
    ec2 = parse(wiz[0]["graphql"], dict(wiz[0]["variables"]))
    check("a $where variable inside an inline query is substituted",
          ec2 == {"type": ["VIRTUAL_MACHINE"], "select": True, "where": wiz[0]["variables"]["where"]},
          safe(ec2))
    whole = {"type": ["ANY"], "select": True}
    check("a $query variable is the query object",
          parse(QUERY_DOCUMENT, {"query": whole}) == whole, "")
    scalars = parse(
        'query S { graphSearch(query: { a: "q\\"uo\\\\te\\u0041\\n", b: -12, c: 2.5e3, d: true, e: false, '
        'f: null, g: ENUM_NAME, h: [], i: {}, j: """block "quoted" text""" }) { totalCount } }', {})
    check("strings with escapes, numbers, booleans, null, enums, and empty lists parse",
          scalars == {"a": 'q"uo\\teA\n', "b": -12, "c": 2500.0, "d": True, "e": False, "f": None,
                      "g": "ENUM_NAME", "h": [], "i": {}, "j": 'block "quoted" text'},
          safe(scalars))
    # An escaped triple quote does not end a block string.
    try:
        escaped = parse('query S { graphSearch(query: { a: """say \\"""hi\\""" """, b: 1 }) { totalCount } }', {})
    except ValueError as exc:
        escaped = repr(exc)
    try:
        # The relationships text sits inside one block string, so the tree has no relationships key.
        hidden = parse('query S { graphSearch(query: { type: [ANY] select: true where: { n: { EQUALS: ["""\\""" ] } } '
                       'relationships: [{ with: { type: [ANY] select: true } }] m: { EQUALS: [ "\\""" """] } } }) '
                       '{ totalCount } }', {})
    except ValueError as exc:
        hidden = repr(exc)
    check("an escaped triple quote stays inside its block string",
          escaped == {"a": 'say """hi""" ', "b": 1}
          and isinstance(hidden, dict) and "relationships" not in hidden
          and gql.path_edges_plan(hidden) == [{"parent": None, "relationship": [], "indirect": False}],
          safe({"escaped": escaped, "hidden": hidden}))
    bad = {
        "call": 'query X { graphSearch(query: { select: __import__("os") }) { totalCount } }',
        "operator": "query X { graphSearch(query: { first: 1+1 }) { totalCount } }",
        "single quotes": "query X { graphSearch(query: { where: {'a': 1} }) { totalCount } }",
        "dotted key": "query X { graphSearch(query: { where: { accessibleFrom.internet: { EQUALS: true } } }) { totalCount } }",
        "parens": "query X { graphSearch(query: { a: (1) }) { totalCount } }",
        "leading dot": "query X { graphSearch(query: { a: .5 }) { totalCount } }",
        "hex": "query X { graphSearch(query: { a: 0x10 }) { totalCount } }",
        "leading zero": "query X { graphSearch(query: { a: 012 }) { totalCount } }",
        "directive": "query X { graphSearch(query: { a: @skip }) { totalCount } }",
        "duplicate key": "query X { graphSearch(query: { a: 1, a: 2 }) { totalCount } }",
        "unterminated": 'query X { graphSearch(query: { a: "open }) { totalCount } }',
        "raw newline": 'query X { graphSearch(query: { a: "line\nbreak" }) { totalCount } }',
        "bad escape": 'query X { graphSearch(query: { a: "\\x41" }) { totalCount } }',
        "unclosed": "query X { graphSearch(query: { a: [1, 2 }) { totalCount } }",
        "deep": "query X { graphSearch(query: " + "[" * 500 + "]" * 500 + ") { totalCount } }",
        "no argument": "query X { graphSearch(first: 5) { totalCount } }",
        "no graphSearch": "query X { cloudResourcesV2(filterBy: { search: \"a\" }) { totalCount } }",
    }
    import builtins
    saved = {name: getattr(builtins, name) for name in ("eval", "exec", "compile", "__import__")}
    calls = []

    def trap(name):
        def inner(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"{name} was called")
        return inner

    outcomes = {}
    try:
        builtins.eval = trap("eval")
        builtins.exec = trap("exec")
        builtins.compile = trap("compile")
        for label, document in bad.items():
            try:
                outcomes[label] = repr(parse(document, {}))
            except ValueError as exc:
                outcomes[label] = "refused" if str(exc) else "refused without a message"
            except Exception as exc:  # noqa: BLE001
                outcomes[label] = f"{type(exc).__name__}: {exc}"
        again = parse(wiz[1]["graphql"], dict(wiz[1]["variables"]))
    finally:
        for name, value in saved.items():
            setattr(builtins, name, value)
    check("the parser refuses anything outside GraphQL value syntax",
          all(value == "refused" for value in outcomes.values()), safe(outcomes))
    check("the parser never evaluates input",
          not calls and again == groups and set(outcomes.values()) == {"refused"}, safe(calls))


def _live_spec(project_id="*"):
    return {
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.us/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN", "project_id": project_id,
    }


def _timed_source(tenant, max_inflight=4, delay=0.0, **kwargs):
    """A LiveTenant source that records each timeout and counts concurrent graphSearch calls."""
    state = {"now": 0, "max": 0, "timeouts": []}
    lock = threading.Lock()

    def opener(request, timeout=None):
        body = json.loads(request.data.decode())
        document = body.get("query") or ""
        searching = "graphSearch" in document
        with lock:
            state["timeouts"].append((is_full_introspection(document), timeout))
            if searching:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
        try:
            if searching and delay:
                time.sleep(delay)
            code, payload = tenant.answer(document, body.get("variables") or {})
        finally:
            if searching:
                with lock:
                    state["now"] -= 1
        return Resp(code, json.dumps(payload).encode())

    kwargs.setdefault("max_retries", 0)
    return GraphQLSource(_live_spec(), client=client(opener, max_inflight=max_inflight, **kwargs)), state


def test_shape_probe():
    """A Wiz tenant reads its graphSearch shape from __type. Full introspection waits for the data model."""
    print("shape probe")
    errors = []

    def run(label, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            return None

    def counts(tenant):
        return {"full": len(tenant.full_introspections()), "probes": len(tenant.shape_probes())}

    # 1. Every adapter path uses the probe, and the probe is sent once.
    tenant = LiveTenant()
    src = _live_source(tenant)
    run("status", src.status)
    run("count", src.adapter.entity_total)
    run("search", lambda: src.search("alpha"))
    run("typed search", lambda: src.search("alpha", category="VIRTUAL_MACHINE"))
    run("expand", lambda: src.expand(["alpha-1"], []))
    run("nodes", lambda: src.nodes(["alpha-1"]))
    run("neighbors", lambda: src.neighbor_summary(["alpha-1", "beta-2"], []))
    run("between", lambda: src.edges_between(["alpha-1", "beta-2"]))
    check("search, expand, nodes, neighbors, count, and status send only one shape probe",
          not errors and counts(tenant) == {"full": 0, "probes": 1},
          safe({"errors": errors, **counts(tenant)}))
    errors.clear()
    shape_of = getattr(src.adapter, "_shape", None)
    first_shape = run("shape", shape_of) if shape_of else None
    check("the built shape is cached for its payload",
          first_shape is not None and run("shape again", shape_of) is first_shape, safe(errors))

    # 2. The light schema has no categories. full=1 is the whole data model.
    errors.clear()
    model = run("light schema", src.schema) or {}
    check("the light Wiz schema has no categories and keeps the search fields",
          not errors and model.get("categories") == {} and model.get("labels") == {}
          and model.get("types") == {} and model.get("links") == []
          and model.get("entities") == sorted(LIVE_ENTITY_TYPES[1:])
          and model.get("filters") == sorted(gql.WIZ_COMMON_KEYS)
          and model.get("filter_mode") == "open" and model.get("name_search") == "inventory"
          and counts(tenant) == {"full": 0, "probes": 1},
          safe({"errors": errors, "categories": len(model.get("categories") or {}), **counts(tenant)}))
    full = run("full schema", lambda: src.schema(full=True)) or {}
    again = run("full schema again", lambda: src.schema(full=True)) or {}
    check("schema full=True sends full introspection once and returns the whole model",
          not errors and "Query" in (full.get("categories") or {})
          and "GraphEntityQueryInput" in (full.get("categories") or {})
          and full.get("entities") == model.get("entities") and full.get("filter_mode") == "open"
          and again.get("categories") == full.get("categories")
          and counts(tenant) == {"full": 1, "probes": 1},
          safe({"errors": errors, **counts(tenant)}))
    run("light refresh", lambda: src.schema(refresh=True))
    light_refresh = counts(tenant)
    run("full refresh", lambda: src.schema(refresh=True, full=True))
    check("refresh drops the probe for the light model and the introspection for the full one",
          not errors and light_refresh == {"full": 1, "probes": 2} and counts(tenant) == {"full": 2, "probes": 2},
          safe({"errors": errors, "light": light_refresh, "full": counts(tenant)}))

    # 3. A server that refuses __type falls back to full introspection, once.
    errors.clear()
    refusing = LiveTenant(type_probe="reject")
    refusing_src = _live_source(refusing)
    found = run("refused search", lambda: refusing_src.search("alpha", category="VIRTUAL_MACHINE")) or {}
    opened = run("refused expand", lambda: refusing_src.expand(["alpha-1"], [])) or {}
    light = run("refused schema", refusing_src.schema) or {}
    check("a tenant that refuses __type falls back to full introspection and search still works",
          not errors and [node["id"] for node in found.get("nodes") or []] == ["alpha-1"]
          and [node["id"] for node in opened.get("nodes") or []] == ["beta-2", "gamma-3", "nic-4"]
          and light.get("categories") == {} and light.get("entities") == sorted(LIVE_ENTITY_TYPES[1:])
          and counts(refusing) == {"full": 1, "probes": 1},
          safe({"errors": errors, **counts(refusing)}))

    # 4. A type the first probe does not name is fetched in a second round.
    errors.clear()
    renamed = LiveTenant(rename={"GraphEntityType": "TenantEntityKind"})
    renamed_src = _live_source(renamed)
    found = run("renamed search", lambda: renamed_src.search("alpha", category="VIRTUAL_MACHINE")) or {}
    hints = run("renamed schema", renamed_src.schema) or {}
    check("a probe that misses a type fetches it in a second round",
          not errors and [node["id"] for node in found.get("nodes") or []] == ["alpha-1"]
          and hints.get("entities") == sorted(LIVE_ENTITY_TYPES[1:])
          and "TenantEntityKind" in renamed.type_names
          and counts(renamed) == {"full": 0, "probes": 2},
          safe({"errors": errors, "asked": renamed.type_names, **counts(renamed)}))
    hidden = LiveTenant(rename={"GraphEntityType": "TenantEntityKind"})
    hidden.hidden = {"TenantEntityKind"}
    hidden_src = _live_source(hidden)
    found = run("hidden search", lambda: hidden_src.search("alpha", category="VIRTUAL_MACHINE")) or {}
    check("a type the probe never finds falls back to full introspection",
          not errors and [node["id"] for node in found.get("nodes") or []] == ["alpha-1"]
          and counts(hidden) == {"full": 1, "probes": 2},
          safe({"errors": errors, **counts(hidden)}))

    # A filterBy field the name search never sends does not cost another round.
    errors.clear()
    wide = LiveTenant()
    for item in wide.payload["data"]["__schema"]["types"]:
        if item["name"] == "CloudResourceV2Filters":
            item["inputFields"].append(ifield("vulnerability", named("INPUT_OBJECT", "CloudResourceVulnerabilityFilters")))
    wide_src = _live_source(wide)
    found = run("wide search", lambda: wide_src.search("alpha")) or {}
    check("an unused filterBy field type is not fetched",
          not errors and [node["id"] for node in found.get("nodes") or []] == ["alpha-1"]
          and "CloudResourceVulnerabilityFilters" not in wide.type_names
          and counts(wide) == {"full": 0, "probes": 1},
          safe({"errors": errors, "asked": wide.type_names, **counts(wide)}))

    # A rate limit on the probe is raised. The 11 MB fallback would be limited too.
    limited = LiveTenant()
    limited_answer = limited.answer

    def limit_probe(document, variables):
        if is_shape_probe(document):
            limited.requests.append({"query": document, "variables": {}})
            return 429, {"errors": [{"message": "slow down"}]}
        return limited_answer(document, variables)

    limited.answer = limit_probe
    limited_src = _live_source(limited, max_retries=0)
    try:
        limited_src.search("alpha", category="VIRTUAL_MACHINE")
        check("a rate-limited probe is raised and sends no full introspection", False, "no error")
    except RateLimited:
        check("a rate-limited probe is raised and sends no full introspection",
              counts(limited) == {"full": 0, "probes": 1}, safe(counts(limited)))

    # 5. The mutation guard is as strong after the probe as after introspection.
    errors.clear()
    guarded = LiveTenant()
    guarded_src = _live_source(guarded)
    run("guard search", lambda: guarded_src.search("alpha", category="VIRTUAL_MACHINE"))
    before = len(guarded.requests)
    refused = None
    try:
        guarded_src.client.execute("query { updateSecretInstance { id } }")
    except QueryRejected as exc:
        refused = exc
    except Exception as exc:  # noqa: BLE001
        errors.append(f"guard: {type(exc).__name__}: {exc}")
    check("the mutation guard refuses a root mutation field after only the shape probe",
          not errors and refused is not None and "updateSecretInstance" in str(refused)
          and len(guarded.requests) == before
          and guarded_src.client.mutation_fields == set(LIVE_MUTATIONS)
          and counts(guarded) == {"full": 0, "probes": 1},
          safe({"errors": errors, "refused": refused, **counts(guarded)}))

    # 6. Full introspection uses the longer timeout. Every other call keeps the default.
    errors.clear()
    timed, state = _timed_source(LiveTenant(), timeout=30)
    run("timed search", lambda: timed.search("alpha", category="VIRTUAL_MACHINE"))
    run("timed full", lambda: timed.schema(full=True))
    full_waits = [value for is_full, value in state["timeouts"] if is_full]
    other_waits = {value for is_full, value in state["timeouts"] if not is_full}
    check("full introspection uses the schema timeout and every other call the default",
          not errors and full_waits == [90.0] and other_waits == {30.0} and timed.client.timeout == 30.0,
          safe({"errors": errors, "full": full_waits, "other": sorted(other_waits)}))
    fallback, fallback_state = _timed_source(LiveTenant(type_probe="reject"), timeout=30)
    run("fallback search", lambda: fallback.search("alpha", category="VIRTUAL_MACHINE"))
    check("the fallback introspection uses the schema timeout too",
          not errors and [value for is_full, value in fallback_state["timeouts"] if is_full] == [90.0],
          safe({"errors": errors, "timeouts": fallback_state["timeouts"]}))

    def slow_schema(request, timeout=None):
        if is_full_introspection(json.loads(request.data.decode()).get("query")):
            raise TimeoutError("timed out")
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    try:
        GraphQLSource(_live_spec(), client=client(slow_schema, timeout=30)).schema(full=True)
        check("a slow full introspection names the schema timeout", False, "no error")
    except Exception as exc:  # noqa: BLE001
        check("a slow full introspection names the schema timeout",
              isinstance(exc, gql.QueryTimeout) and "90 s" in str(exc), safe(f"{type(exc).__name__}: {exc}"))
    previous = os.environ.get("GRAPHQL_SCHEMA_TIMEOUT")
    os.environ["GRAPHQL_SCHEMA_TIMEOUT"] = "75"
    try:
        built = GraphQLSource(_live_spec())
        check("GRAPHQL_SCHEMA_TIMEOUT sets the schema timeout and leaves the default",
              getattr(built.client, "schema_timeout", None) == 75.0 and built.client.timeout == 30.0,
              safe(getattr(built.client, "schema_timeout", None)))
    finally:
        if previous is None:
            os.environ.pop("GRAPHQL_SCHEMA_TIMEOUT", None)
        else:
            os.environ["GRAPHQL_SCHEMA_TIMEOUT"] = previous

    # 7. The neighbor summary runs its anchors at once and merges them in anchor order.
    errors.clear()
    anchors = ["alpha-1", "beta-2", "gamma-3", "nic-4", "delta-5", "fw-1"]
    one_src, one_state = _timed_source(LiveTenant(), max_inflight=1, delay=0.05)
    many_src, many_state = _timed_source(LiveTenant(), max_inflight=4, delay=0.05)
    in_order = run("sequential summary", lambda: one_src.neighbor_summary(anchors, ["beta-2"])) or {}
    at_once = run("parallel summary", lambda: many_src.neighbor_summary(anchors, ["beta-2"])) or {}
    expected = {
        "total": 5, "categories": {"FIREWALL": 1, "NETWORK_INTERFACE": 1, "VIRTUAL_MACHINE": 3},
        "types": {"related": 5},
        "combos": {"FIREWALL": {"related": 1}, "NETWORK_INTERFACE": {"related": 1},
                   "VIRTUAL_MACHINE": {"related": 3}},
        "approximate": False, "degree": 6,
    }
    check("the parallel neighbor summary equals the sequential result in the same order",
          not errors and json.dumps(at_once) == json.dumps(in_order) == json.dumps(expected),
          safe({"errors": errors, "parallel": at_once, "sequential": in_order}))
    check("the neighbor summary runs anchors concurrently within max_inflight",
          one_state["max"] == 1 and 1 < many_state["max"] <= 4,
          safe({"one": one_state["max"], "many": many_state["max"]}))
    failing, _ = _timed_source(LiveTenant(), max_inflight=4)
    try:
        failing.neighbor_summary(["alpha-1", "boom-1", "beta-2"], [])
        check("a failed anchor fails the parallel neighbor summary", False, "no error")
    except GraphQLCallError as exc:
        check("a failed anchor fails the parallel neighbor summary", "upstream error" in str(exc), safe(exc))


def _oauth_live_source(tenant, reject_first, max_inflight=4, delay=0.0):
    """A LiveTenant source that mints tokens. A graphSearch with a token in reject_first gets HTTP 401."""
    state = {"mints": 0}
    lock = threading.Lock()

    def opener(request, timeout=None):
        if request.full_url.endswith("/oauth/token"):
            with lock:
                state["mints"] += 1
                token = f"minted-{state['mints']}"
            time.sleep(0.02)
            return Resp(200, json.dumps({"access_token": token}).encode())
        headers = {key.lower(): value for key, value in request.header_items()}
        bearer = headers.get("authorization", "").split(" ", 1)[-1]
        body = json.loads(request.data.decode())
        document = body.get("query") or ""
        if "graphSearch" in document and bearer in reject_first:
            if delay:
                time.sleep(delay)
            raise http_error(401, b'{"errors":[{"message":"unauthorized"}]}')
        code, payload = tenant.answer(document, body.get("variables") or {})
        return Resp(code, json.dumps(payload).encode())

    built = client(opener, token=None, client_id="client-id-zz", client_secret="client-secret-zz",
                   auth_url="https://auth.example.test/oauth/token", max_inflight=max_inflight, max_retries=0)
    return GraphQLSource(_live_spec(), client=built), state


def test_probe_review():
    """Review findings on the shape probe, the lazy schema, and the parallel neighbor summary."""
    print("shape probe review")

    # A full introspection with no __schema is an error, never a stored None shape.
    class NoSchema:
        schema_ttl = 0
        schema_timeout = 5

        def execute(self, document, variables, **_kwargs):
            if document == gql.SHAPE_QUERY:
                raise ValueError("the probe answer is not usable")
            return {"data": {"__schema": None}, "errors": [{"message": "introspection disabled"}]}

        def _scrub(self, exc):
            return str(exc)

    raised = None
    try:
        gql._load_wiz_shape(NoSchema(), [])
    except gql.GraphQLCallError as exc:
        raised = exc
    except Exception as exc:  # noqa: BLE001
        raised = f"wrong type: {type(exc).__name__}: {exc}"
    check("a full introspection with no __schema raises instead of keeping a None shape",
          isinstance(raised, gql.GraphQLCallError), safe(raised))

    # The probe selects every field WizShape reads. The mock answers only what it selects.
    fragment = gql._SHAPE_FRAGMENT
    check("the probe fragment selects enum values, input fields, args, defaults, and TypeRef",
          all(part in fragment for part in (
              "enumValues(includeDeprecated: true) { name }", "inputFields { name type { ...TypeRef } defaultValue }",
              "args { name type { ...TypeRef } defaultValue }", "type { ...TypeRef }", "kind", "name")),
          safe(fragment))
    weak = LiveTenant()
    weak_doc = gql.SHAPE_QUERY.replace("  enumValues(includeDeprecated: true) { name }\n", "")
    _, weak_body = weak._types(weak_doc)
    entity_enum = [item for item in (weak_body.get("data") or {}).values()
                   if isinstance(item, dict) and item.get("name") == "GraphEntityType"]
    check("the tenant mock leaves out a field the probe does not select",
          entity_enum and "enumValues" not in entity_enum[0], safe(entity_enum))

    # A mutation root that the first round misses is fetched with no unused fragment.
    tenant = LiveTenant(rename={"Mutation": "RootMutation"})
    src = _live_source(tenant)
    errors = []
    try:
        src.search("alpha", category="VIRTUAL_MACHINE")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")
    check("a renamed mutation root is fetched in a second probe with no unused fragment",
          not errors and len(tenant.shape_probes()) == 2 and not tenant.full_introspections()
          and "fragment ShapeType" not in tenant.shape_probes()[1]["query"]
          and src.client.mutation_fields == set(LIVE_MUTATIONS),
          safe({"errors": errors, "probes": len(tenant.shape_probes()), "full": len(tenant.full_introspections())}))

    # A probe timeout or HTTP 5xx is raised, and the failure is kept for a short time.
    for label, fail in (("a timeout", lambda: (_ for _ in ()).throw(TimeoutError("timed out"))),
                        ("HTTP 503", lambda: (_ for _ in ()).throw(http_error(503, b'{"errors":[{"message":"down"}]}')))):
        sent = []

        def opener(request, timeout=None, fail=fail):
            document = json.loads(request.data.decode()).get("query") or ""
            sent.append("full" if is_full_introspection(document) else "probe" if is_shape_probe(document) else "other")
            if "__type(" in document or "__schema" in document:
                fail()
            return Resp(200, b'{"data":{"__typename":"Query"}}')

        slow_src = GraphQLSource(_live_spec(), client=client(opener, max_retries=0))
        raised = []
        for call in (slow_src.status, lambda: slow_src.search("alpha", category="VIRTUAL_MACHINE"),
                     lambda: slow_src.expand(["alpha-1"], [])):
            try:
                call()
            except Exception as exc:  # noqa: BLE001
                raised.append(type(exc).__name__)
        check(f"a probe that fails with {label} sends no full introspection and is not sent again at once",
              sent.count("full") == 0 and sent.count("probe") == 1 and len(raised) == 2,
              safe({"sent": sent, "raised": raised}))
        try:
            slow_src.schema(refresh=True)
        except Exception:  # noqa: BLE001
            pass
        check(f"a refresh after {label} sends the probe again", sent.count("probe") == 2, safe(sent))

    # Full introspection lives outside the result cache, so result pages cannot evict it.
    tenant = LiveTenant()
    src = _live_source(tenant)
    src.schema(full=True)
    for index in range(src.client.cache.size + 10):
        src.client.execute("query Page($n: Int) { a }", {"n": index})
    src.schema(full=True)
    check("the full schema survives more result pages than the result cache holds",
          len(tenant.full_introspections()) == 1, safe(len(tenant.full_introspections())))

    # One 401 on parallel workers mints one new token, not one per worker.
    tenant = LiveTenant()
    oauth_src, oauth_state = _oauth_live_source(tenant, {"minted-1"}, delay=0.05)
    anchors = ["alpha-1", "beta-2", "gamma-3", "nic-4", "delta-5"]
    errors = []
    try:
        oauth_src.neighbor_summary(anchors, [])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")
    check("a 401 during the parallel neighbor summary mints one new token",
          not errors and oauth_state["mints"] == 2, safe({"errors": errors, "mints": oauth_state["mints"]}))

    # A rate limit on one anchor stops the other anchors from starting.
    sent = {"search": 0}
    lock = threading.Lock()
    limited = LiveTenant()

    def limiting(request, timeout=None):
        body = json.loads(request.data.decode())
        document = body.get("query") or ""
        if "graphSearch" in document:
            with lock:
                sent["search"] += 1
            time.sleep(0.02)
            raise http_error(429, b'{"errors":[{"message":"slow"}]}', {"Retry-After": "0.05"})
        code, payload = limited.answer(document, body.get("variables") or {})
        return Resp(code, json.dumps(payload).encode())

    burst = GraphQLSource(_live_spec(), client=client(limiting, max_retries=3, max_inflight=4, sleep=time.sleep))
    try:
        burst.neighbor_summary([f"x-{index}" for index in range(20)], [])
        check("a rate-limited neighbor summary stops early", False, "no error")
    except RateLimited:
        # At most the first max_inflight anchors send, each with its retries. No later anchor starts.
        check("a rate-limited neighbor summary stops early",
              sent["search"] <= burst.client.max_inflight * (burst.client.max_retries + 1), safe(sent))

    # GRAPHQL_SCHEMA_TIMEOUT refuses a value that is not finite and caps a large one.
    check("the schema timeout refuses inf and nan and caps a large value",
          gql._seconds("1e400", 90) == 90.0 and gql._seconds("nan", 90) == 90.0
          and gql._seconds("-5", 90) == 90.0 and gql._seconds("100000", 90) == gql.MAX_SCHEMA_TIMEOUT
          and gql._seconds("75", 90) == 75.0,
          safe([gql._seconds(raw, 90) for raw in ("1e400", "nan", "100000", "75")]))

    # A saved query on a cold source loads the shape first, so a mutation field is refused.
    tenant = LiveTenant()
    src = _live_source(tenant)
    try:
        src.run_document("query X { deleteCloudResource { id } }", {})
        check("a cold saved query that names a mutation field is refused", False, "sent")
    except QueryRejected as exc:
        check("a cold saved query that names a mutation field is refused",
              "deleteCloudResource" in str(exc)
              and not any("deleteCloudResource" in body["query"] for body in tenant.requests),
              safe(exc))


def test_query_timeout():
    """A read timeout fails at once with QueryTimeout and is not retried."""
    print("query timeout")
    timeout_class = getattr(gql, "QueryTimeout", None)
    calls = []

    def slow(request, timeout=None):
        calls.append(1)
        raise TimeoutError("timed out")

    try:
        client(slow, max_retries=3).execute("{ __typename }")
        check("a read timeout is a QueryTimeout and is not retried", False, "no error")
    except Exception as exc:  # noqa: BLE001
        check("a read timeout is a QueryTimeout and is not retried",
              timeout_class is not None and isinstance(exc, timeout_class) and isinstance(exc, GraphQLCallError)
              and len(calls) == 1 and "did not answer within" in str(exc),
              safe(f"{type(exc).__name__}: {exc} after {len(calls)} calls"))

    wrapped = []

    def url_timeout(request, timeout=None):
        wrapped.append(1)
        raise urllib.error.URLError(socket.timeout("timed out"))

    try:
        client(url_timeout, max_retries=3).execute("{ __typename }")
        check("a URLError timeout is a QueryTimeout too", False, "no error")
    except Exception as exc:  # noqa: BLE001
        check("a URLError timeout is a QueryTimeout too",
              timeout_class is not None and isinstance(exc, timeout_class)
              and len(wrapped) == 1 and "did not answer within" in str(exc),
              safe(f"{type(exc).__name__}: {exc} after {len(wrapped)} calls"))

    tenant = LiveTenant(timeouts="raise")
    src = _live_source(tenant)
    document = ("query Slow($query: GraphEntityQueryInput) {\n"
                "  graphSearch(query: $query, first: 5) { totalCount nodes { entities { id } } }\n}\n")
    try:
        src.client.execute(document, {"query": {"type": ["ANY"], "where": {"name": {"CONTAINS": ["x"]}}}})
        check("a slow tenant call raises QueryTimeout", False, "no error")
    except Exception as exc:  # noqa: BLE001
        check("a slow tenant call raises QueryTimeout",
              timeout_class is not None and isinstance(exc, timeout_class), safe(f"{type(exc).__name__}: {exc}"))

    answered = []

    class _Fake:
        def _json(self, obj, code=200):
            answered.append((code, obj))

    if timeout_class is not None:
        server.Handler._api_error(_Fake(), timeout_class("Wiz did not answer within 30 s."))
    check("the API answers a timeout with HTTP 504",
          answered and answered[0][0] == 504 and "did not answer" in answered[0][1].get("error", ""),
          safe(answered))


class _RecordingGraphQL(BaseHTTPRequestHandler):
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
        self.server.documents.append(document)
        code, payload = self.server.answer(document, body.get("variables") or {})
        self._send(code, payload)


def _start_tenant_mock():
    """A local HTTP mock of the live Wiz contract. A slow shape answers HTTP 504."""
    port = free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _RecordingGraphQL)
    httpd.tenant = LiveTenant(count=3, floor=False, timeouts="http")
    httpd.answer = httpd.tenant.answer
    httpd.documents = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


def _chrome_executable(playwright):
    """The browser for the headed page. CHROME wins, then the Chromium Playwright installed.

    None means neither is on this machine, so the headed page is skipped.
    """
    override = os.environ.get("CHROME")
    if override:
        return override if Path(override).is_file() else None
    try:
        bundled = playwright.chromium.executable_path
    except Exception:  # noqa: BLE001
        return None
    return bundled if bundled and Path(bundled).is_file() else None


def test_tenant_page_flow():
    """HTTP search, neighbors, and expand stay 200, and the page can run that flow."""
    print("tenant page flow")
    found = neo4j_up()
    if found:
        uri, user, password = found
    else:
        uri, user, password = "bolt://127.0.0.1:7687", "neo4j", "tinkerlab-test"
    mock, mock_port = _start_tenant_mock()
    cfg_dir = tempfile.TemporaryDirectory()
    env_file = tempfile.NamedTemporaryFile(prefix="gx-env-", delete=False)
    env_file.close()
    err_file = tempfile.NamedTemporaryFile(prefix="gx-tenant-err-", delete=False)
    port = free_port()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GRAPHQL_") or key in ("GX_FRESH", "GX_QUERIES"):
            env.pop(key, None)
    env.update({
        "NEO4J_URI": uri, "NEO4J_USER": user, "NEO4J_PASSWORD": password,
        "NEO4J_DATABASE": os.environ.get("NEO4J_DATABASE") or "neo4j",
        "HOST": "127.0.0.1", "PORT": str(port), "QUIET": "1",
        "GX_CONFIG": str(Path(cfg_dir.name) / "gx.local.json"),
        "GX_CACHE_DIR": str(Path(cfg_dir.name) / "cache"),
        "GX_ENV": env_file.name,
        "GRAPHQL_ENDPOINT": f"http://127.0.0.1:{mock_port}/graphql",
        "GRAPHQL_TOKEN": TOKEN,
        "GRAPHQL_ADAPTER": "wiz",
        "GRAPHQL_LABEL": "Wiz",
        "GRAPHQL_ID": "wiz",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")], env=env,
        stdout=subprocess.DEVNULL, stderr=err_file)
    base = f"http://127.0.0.1:{port}"
    try:
        status = None
        for _ in range(80):
            if proc.poll() is not None:
                err_file.flush()
                check("explorer starts beside the tenant mock", False, safe(Path(err_file.name).read_text()[-500:]))
                return
            try:
                code, _, status = http_json("GET", base + "/api/status")
                if code == 200:
                    break
            except Exception:  # noqa: BLE001
                status = None
            time.sleep(0.25)
        if not status:
            check("explorer starts beside the tenant mock", False, "status never answered")
            return
        check("explorer starts beside the tenant mock", True)
        code, raw, wiz = http_json("POST", base + "/api/source", {"id": "wiz"})
        check("the tenant source connects and counts nodes",
              code == 200 and wiz.get("source", {}).get("id") == "wiz"
              and wiz.get("connected") is True and wiz.get("nodes") == 3
              and wiz.get("edges") is None and TOKEN not in raw,
              safe(raw[:400]))
        code, raw, found_nodes = http_json("GET", base + "/api/search?type=node&limit=5&q=alpha")
        nodes = found_nodes.get("nodes") or []
        check("search returns the tenant entity",
              code == 200 and nodes and nodes[0]["id"] == "alpha-1" and "invalid type" not in raw,
              safe(raw[:400]))
        code, raw, skipped = http_json("GET", base + "/api/search?type=node&limit=5&q=alpha&offset=20")
        check("an offset past the page returns no entities",
              code == 200 and skipped.get("nodes") == [] and skipped.get("total") == 1
              and TOKEN not in raw,
              safe(skipped))
        code, raw, buckets = http_json("GET", base + "/api/search?type=node&limit=5&category=BUCKET")
        bucket_nodes = buckets.get("nodes") or []
        check("an entity type search returns the buckets",
              code == 200 and [node["id"] for node in bucket_nodes] == ["beta-2", "boom-1"]
              and "invalid type" not in raw,
              safe(raw[:400]))
        check("status and search send the shape probe and no full introspection",
              not mock.tenant.full_introspections() and len(mock.tenant.shape_probes()) == 1,
              safe({"full": len(mock.tenant.full_introspections()), "probes": len(mock.tenant.shape_probes())}))
        code, raw, schema = http_json("GET", base + "/api/schema")
        check("the light schema publishes entity types and no GraphQL type names",
              code == 200 and schema.get("entities") == sorted(LIVE_ENTITY_TYPES[1:])
              and schema.get("filters") == sorted(gql.WIZ_COMMON_KEYS)
              and schema.get("filter_mode") == "open" and schema.get("name_search") == "inventory"
              and schema.get("categories") == {} and schema.get("links") == []
              and not mock.tenant.full_introspections() and TOKEN not in raw,
              safe(sorted((schema.get("categories") or {}))[:20]))
        code, raw, whole = http_json("GET", base + "/api/schema?full=1")
        check("schema full=1 keeps every schema type",
              code == 200 and "Query" in (whole.get("categories") or {})
              and "GraphEntityQueryInput" in (whole.get("categories") or {})
              and whole.get("entities") == sorted(LIVE_ENTITY_TYPES[1:])
              and len(mock.tenant.full_introspections()) == 1 and TOKEN not in raw,
              safe(sorted((whole.get("categories") or {}))[:20]))
        code, raw, library = http_json("GET", base + "/api/queries")
        lib_names = [item.get("name") for item in library] if isinstance(library, list) else []
        check("the query library is the two Wiz searches",
              code == 200 and lib_names == [
                  "Internet-exposed EC2 instances", "Security groups open to 0.0.0.0/0"]
              and all("graphql" in item and "cypher" not in item for item in library)
              and "MATCH" not in raw and TOKEN not in raw,
              safe(lib_names))
        first = library[0] if isinstance(library, list) and library else {}
        second = library[1] if isinstance(library, list) and len(library) > 1 else {}
        code, raw, placed = http_json("POST", base + "/api/graphql", {
            "document": first.get("graphql") or "", "variables": first.get("variables") or {},
        })
        check("a saved query pages entities through the API",
              code == 200 and [node.get("id") for node in placed.get("nodes") or []] == ["alpha-1", "delta-5"]
              and placed.get("edges") == [] and TOKEN not in raw,
              safe(placed.get("nodes")))
        code, raw, opened = http_json("POST", base + "/api/graphql", {
            "document": second.get("graphql") or "", "variables": second.get("variables") or {},
        })
        check("the security-group query returns its groups, rules, and edges",
              code == 200 and [node.get("id") for node in opened.get("nodes") or []] == [
                  "sg-web", "rule-ssh", "rule-https", "sg-db"]
              and sorted(edge.get("id") for edge in opened.get("edges") or []) == [
                  "sg-db:related:rule-ssh", "sg-web:related:rule-https", "sg-web:related:rule-ssh"]
              and TOKEN not in raw,
              safe(opened))
        code, raw, named = http_json(
            "GET", base + "/api/search?type=node&limit=5&category=BUCKET&prop=name&value=beta")
        check("a property filter returns the matching entity",
              code == 200 and [node.get("id") for node in named.get("nodes") or []] == ["beta-2"]
              and "invalid type" not in raw,
              safe(raw[:300]))
        code, raw, untyped = http_json("GET", base + "/api/search?type=node&limit=5&prop=name&value=beta")
        check("a property filter with no entity type returns the matching entity",
              code == 200 and [node.get("id") for node in untyped.get("nodes") or []] == ["beta-2"]
              and TOKEN not in raw,
              safe(untyped))
        code, raw, missing_type = http_json("GET", base + "/api/search?type=node&limit=5&category=APICallComponent")
        check("an unknown entity type is rejected",
              code == 400 and "APICallComponent" in (missing_type.get("error") or "") and TOKEN not in raw,
              safe(missing_type))
        code, raw, missing_prop = http_json(
            "GET", base + "/api/search?type=node&limit=5&category=VIRTUAL_MACHINE&prop=regoin&value=example-region")
        check("a misspelt property returns no result and no error",
              code == 200 and missing_prop.get("nodes") == [] and TOKEN not in raw,
              safe(missing_prop))
        code, raw, edges = http_json("GET", base + "/api/search?type=edge&limit=5&q=alpha")
        check("an edge search returns no relationships",
              code == 200 and edges.get("edges") == [] and TOKEN not in raw, safe(edges))
        code, raw, denied = http_json("POST", base + "/api/cypher", {"query": "MATCH (n) RETURN n"})
        check("Cypher is refused on the GraphQL source",
              code == 400 and "no Cypher" in (denied.get("error") or "") and TOKEN not in raw, safe(denied))
        code, raw, mutated = http_json("POST", base + "/api/graphql", {"document": "mutation { updateSecretInstance { id } }", "variables": {}})
        check("a mutation is refused before it is sent",
              code == 400 and "not sent" in (mutated.get("error") or "") and TOKEN not in raw, safe(mutated))
        code, raw, summary = http_json("POST", base + "/api/neighbors", {"ids": ["alpha-1"], "visible": []})
        code2, raw2, hidden = http_json("POST", base + "/api/neighbors", {"ids": ["alpha-1"], "visible": ["beta-2"]})
        check("neighbors answers instead of failing the request",
              code == 200 and summary.get("degree") == 3
              and (summary.get("categories") or {}).get("BUCKET") == 1
              and code2 == 200 and hidden.get("total") == 2 and hidden.get("degree") == 3
              and "invalid type" not in raw and TOKEN not in raw,
              safe(raw[:400]))
        code, raw, expanded = http_json("POST", base + "/api/expand", {"ids": ["alpha-1"], "visible": [], "limit": 1})
        code2, raw2, nxt = http_json("POST", base + "/api/expand", {
            "ids": ["alpha-1"], "visible": ["alpha-1", "beta-2"], "limit": 1,
        })
        check("expand returns one page and then the next",
              code == 200 and [node.get("id") for node in expanded.get("nodes") or []] == ["beta-2"]
              and expanded.get("more") is True
              and expanded.get("edges") and expanded["edges"][0]["id"] == "alpha-1:related:beta-2"
              and code2 == 200 and [node.get("id") for node in nxt.get("nodes") or []] == ["gamma-3"]
              and "invalid type" not in raw and TOKEN not in raw,
              safe(raw[:400]))
        code, raw, details = http_json("POST", base + "/api/nodes", {"ids": ["alpha-1", "beta-2"]})
        detail_ids = [node.get("id") for node in details.get("nodes") or []]
        props = ((details.get("nodes") or [{}])[0]).get("props") or {}
        check("opening nodes returns each id and its properties",
              code == 200 and detail_ids == ["alpha-1", "beta-2"] and props.get("region") == "example-region"
              and details["nodes"][0].get("partial") is False and TOKEN not in raw,
              safe(raw[:300]))
        code, raw, linked = http_json("POST", base + "/api/edges-between", {"ids": ["alpha-1", "beta-2", "gamma-3"]})
        check("edges between nodes return the related links",
              code == 200 and [edge.get("id") for edge in linked.get("edges") or []] == [
                  "alpha-1:related:beta-2", "alpha-1:related:gamma-3"]
              and TOKEN not in raw,
              safe(linked.get("edges")))
        code, raw, diag = http_json("GET", base + "/api/diagnostics?source=query")
        logged_queries = "\n".join(
            item.get("message") or "" for item in (diag.get("entries") or []) if isinstance(item, dict))
        check("diagnostics keeps the sent document and not the token",
              code == 200 and "graphSearch" in logged_queries and "quick" in logged_queries
              and "alpha" in logged_queries and "fragment TypeRef" not in logged_queries
              and TOKEN not in raw,
              safe(logged_queries[-500:]))
        code, raw, exploded = http_json("GET", base + "/api/search?type=node&limit=5&q=explode")
        check("a failed inventory search is the tenant error",
              code == 502 and exploded.get("error") == "upstream request failed"
              and TOKEN not in raw,
              safe(exploded))
        code, raw, rejected = http_json("POST", base + "/api/graphql", {
            "document": "query Bad($query: GraphEntityQueryInput) { graphSearch(query: $query) { totalCount } }",
            "variables": {"query": {"select": True}},
        })
        generated = [body for body in mock.tenant.requests
                     if re.match(r"\s*query (GraphSearch|EntityTotal)\b", body["query"])]
        problems = _assert_live_invariants(mock.tenant.requests)
        check("every app graphSearch sends a typed $query and an untyped one is the tenant error",
              code == 502 and "invalid type for variable: 'query'" in (rejected.get("error") or "")
              and "has no type" in (rejected.get("error") or "")
              and generated and all("$query" in body["query"] for body in generated)
              and not problems
              and any((body["variables"].get("query") or {}).get("relationships") for body in generated)
              and TOKEN not in raw,
              safe({"error": rejected.get("error"), "problems": problems, "generated": len(generated)}))
        _drive_tenant_page(base)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        err_file.close()
        try:
            log = Path(err_file.name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            log = ""
        check("the server log omits the token", TOKEN not in log, safe(log[-400:]))
        mock.shutdown()
        mock.server_close()
        cfg_dir.cleanup()
        for path in (env_file.name, err_file.name):
            try:
                os.remove(path)
            except OSError:
                pass


def _select_node(page, node_id):
    page.evaluate(
        """(id) => {
            const node = window.cy.getElementById(id);
            window.cy.$(':selected').unselect();
            node.select();
        }""",
        node_id)
    page.wait_for_function(
        """(id) => {
            const node = window.cy.getElementById(id);
            return node.nonempty() && node.selected();
        }""",
        arg=node_id, timeout=5000)


def _open_panel(page):
    """Wait for the panel or its > tab after a selection, and open the panel when it is collapsed."""
    page.wait_for_function(
        """() => !document.querySelector('#leftpanel').classList.contains('hidden')
            || !document.querySelector('#lp-tab').classList.contains('hidden')""",
        timeout=5000)
    if page.evaluate("() => document.querySelector('#leftpanel').classList.contains('hidden')"):
        page.click("#lp-tab")
    page.wait_for_selector("#leftpanel [data-lp=expand]", timeout=5000)


def _expand_selected(page, node_id):
    """Open the selection panel and choose All neighbors."""
    _select_node(page, node_id)
    _open_panel(page)
    page.click("#leftpanel [data-lp=expand]")
    page.hover("#menus .mi")
    page.wait_for_function(
        """() => [...document.querySelectorAll('#menus .mi')]
            .some(el => el.textContent.includes('All neighbors'))""",
        timeout=10000)
    page.locator("#menus .mi", has_text="All neighbors").click()


PANEL_SHOTS = Path(os.environ.get("GX_PANEL_SHOTS") or "")


def _panel_shot(page, name):
    """Save a panel screenshot when GX_PANEL_SHOTS names a folder."""
    if not os.environ.get("GX_PANEL_SHOTS"):
        return
    PANEL_SHOTS.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(250)
    page.screenshot(path=str(PANEL_SHOTS / f"{name}.png"))


def _panel_state(page):
    """What the selection panel shows now, read from the page."""
    return page.evaluate(
        """() => {
            const lp = document.querySelector('#leftpanel');
            const tab = document.querySelector('#lp-tab');
            const card = document.querySelector('#open-selpanel');
            const shown = el => !!el && !el.classList.contains('hidden') && el.getClientRects().length > 0;
            return { open: shown(lp), tab: shown(tab), tabDir: tab ? tab.dataset.dir || '' : '',
                     card: shown(card), pref: (S.prefs || {}).selPanel || null,
                     stored: (() => { try { return JSON.parse(localStorage.getItem('gx:prefs') || '{}').selPanel || null; }
                                      catch (e) { return 'error'; } })() };
        }""")


def _check_selection_panel(page, traffic):
    """The selection panel opens on select, collapses, and formats Wiz values (PANEL_SPEC gaps 1 to 6)."""
    def guarded(name, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(name, False, safe(f"{type(exc).__name__}: {exc}")[:400])

    def opens_on_select():
        page.evaluate("() => { window.cy.$(':selected').unselect(); }")
        page.wait_for_timeout(100)
        _select_node(page, "alpha-1")
        page.wait_for_timeout(200)
        first = _panel_state(page)
        check("1: selecting a node opens the selection panel and hides the card",
              first["open"] and not first["card"] and first["pref"] in (None, "auto"), safe(first))

    def collapse_tab():
        right = page.evaluate(
            """() => {
                const tab = document.querySelector('#leftpanel .lp-collapse');
                const lp = document.querySelector('#leftpanel').getBoundingClientRect();
                if (!tab) return null;
                const r = tab.getBoundingClientRect();
                return { edge: Math.round(r.right - lp.right), mid: Math.round((r.top + r.bottom) / 2 - (lp.top + lp.bottom) / 2) };
            }""")
        check("2: the panel has a collapse tab on its right edge, near the vertical middle",
              right is not None and -40 <= right["edge"] <= 30 and abs(right["mid"]) <= 40, safe(right))
        page.click("#leftpanel .lp-collapse")
        page.wait_for_timeout(150)
        closed = _panel_state(page)
        check("2: the collapse tab closes the panel, leaves a > tab, hides the card, and saves the choice",
              not closed["open"] and closed["tab"] and closed["tabDir"] == "open" and not closed["card"]
              and closed["pref"] == "closed" and closed["stored"] == "closed", safe(closed))
        _panel_shot(page, "03-collapsed-tab")
        _select_node(page, "delta-5")
        page.wait_for_timeout(150)
        later = _panel_state(page)
        check("1: a closed panel stays closed for the next selection",
              not later["open"] and later["tab"], safe(later))
        page.click("#lp-tab")
        page.wait_for_timeout(150)
        reopened = _panel_state(page)
        check("2: the > tab opens the panel again and sets the choice back to auto",
              reopened["open"] and not reopened["tab"] and reopened["pref"] == "auto"
              and reopened["stored"] == "auto", safe(reopened))
        page.evaluate("() => { window.cy.$(':selected').unselect(); }")
        page.wait_for_timeout(150)
        none = _panel_state(page)
        check("2: with no selection, neither the panel nor the tab shows",
              not none["open"] and not none["tab"] and not none["card"], safe(none))

    def values():
        _select_node(page, "alpha-1")
        page.wait_for_function(
            """() => {
                const lp = document.querySelector('#leftpanel');
                return lp && !lp.classList.contains('hidden') && !window.cy.getElementById('alpha-1').data('partial')
                    && lp.querySelector('.prop[data-k="tags"]');
            }""", timeout=10000)
        page.wait_for_timeout(150)
        got = page.evaluate(
            """() => {
                const lp = document.querySelector('#leftpanel');
                const row = k => lp.querySelector(`.prop[data-k="${k}"]`);
                const val = k => { const r = row(k); return r ? r.querySelector('.v') : null; };
                const text = k => { const v = val(k); return v ? v.textContent.trim() : null; };
                const vis = k => { const r = row(k); return !!r && !r.classList.contains('hidden'); };
                const bool = k => { const v = val(k); const c = v && v.querySelector('.pv-bool'); return c ? [c.textContent.trim(), c.className] : null; };
                const link = k => { const v = val(k); const a = v && v.querySelector('a.pv-link'); return a ? [a.getAttribute('href'), a.target, a.rel] : null; };
                const tags = val('tags');
                const empties = lp.querySelector('[data-lp-empty]');
                const keys = [...lp.querySelectorAll('.prop[data-k]')].filter(r => !r.classList.contains('hidden')).map(r => r.dataset.k);
                return {
                    keys,
                    internet: bool('accessibleFrom.internet'), ephemeral: bool('isEphemeral'),
                    disk: text('diskSizeBytes'), mem: text('memoryMB'),
                    cpu: text('cpuLoad'), cpuTitle: val('cpuLoad') ? (val('cpuLoad').querySelector('[title]') || {}).title || '' : '',
                    created: text('creationDate'), createdTitle: val('creationDate') ? (val('creationDate').querySelector('[title]') || {}).title || '' : '',
                    launch: text('launchtime'), launchTitle: val('launchtime') ? (val('launchtime').querySelector('[title]') || {}).title || '' : '',
                    updated: text('updatedAt'),
                    url: link('externalUrl'), script: link('scriptLink'), scriptText: text('scriptLink'),
                    nested: tags ? [...tags.querySelectorAll('.pv-obj .pv-k')].map(k => k.textContent.trim()) : [],
                    deep: tags ? tags.textContent.includes('{"three":3}') : false,
                    ports: text('ports'),
                    emptyRows: ['description', 'comment'].map(vis),
                    emptyLabel: empties ? empties.textContent.trim() : null,
                    copy: !!(val('region') && row('region').querySelector('[data-copy]')),
                };
            }""")
        check("3: booleans render as true and false chips",
              got["internet"] and got["internet"][0] == "true" and "on" in got["internet"][1]
              and got["ephemeral"] and got["ephemeral"][0] == "false" and "off" in got["ephemeral"][1], safe(got))
        check("3: integers show thousands separators and floats round with the full value in the tooltip",
              got["disk"] == f"{1234567890:,}" and got["mem"] == f"{16384:,}"
              and got["cpu"] and got["cpu"] != "0.20101234" and got["cpu"].startswith("0.201")
              and "0.20101234" in got["cpuTitle"], safe(got))
        check("3: ISO dates and time-named epoch keys show a local date with the raw value in the tooltip",
              got["created"] and "T12:34" not in got["created"] and "2024" in got["created"]
              and "2024-03-01T12:34:56.789Z" in got["createdTitle"]
              and got["launch"] and "2024" in got["launch"] and "1732327995" in got["launchTitle"]
              and got["updated"] and "2024" in got["updated"] and got["mem"] == f"{16384:,}", safe(got))
        check("3: http links open in a new tab and javascript: stays text",
              got["url"] and got["url"][0].startswith("https://console.aws.amazon.com/")
              and got["url"][1] == "_blank" and "noopener" in got["url"][2] and "noreferrer" in got["url"][2]
              and got["script"] is None and got["scriptText"] == "javascript:alert(1)", safe(got))
        check("3: an object renders as a nested list two levels deep, then compact JSON",
              {"env", "team", "owner", "name", "deep"} <= set(got["nested"]) and got["deep"]
              and got["ports"] == "22, 443", safe(got))
        check("3: empty values hide behind a Show empty properties toggle",
              got["emptyRows"] == [False, False] and got["emptyLabel"] == "Show empty properties (2)"
              and "description" not in got["keys"], safe(got))
        check("3: each value has a copy button", got["copy"], safe(got))
        _panel_shot(page, "01-wiz-node")
        page.click("#leftpanel [data-lp-empty]")
        page.wait_for_timeout(100)
        shown = page.evaluate(
            """() => ['description', 'comment'].map(k => {
                const r = document.querySelector(`#leftpanel .prop[data-k="${k}"]`);
                return !!r && !r.classList.contains('hidden'); })""")
        check("3: the toggle shows the empty properties", shown == [True, True], safe(shown))
        page.click("#leftpanel [data-lp-empty]")
        page.fill("#leftpanel .searchbox input", "core")
        page.wait_for_timeout(150)
        hits = page.evaluate(
            """() => [...document.querySelectorAll('#leftpanel .prop[data-k]')]
                .filter(r => !r.classList.contains('hidden')).map(r => r.dataset.k)""")
        page.fill("#leftpanel .searchbox input", "")
        check("3: the property search matches nested values", hits == ["tags"], safe(hits))
        page.evaluate("""() => document.querySelector('#leftpanel .prop[data-k="tags"]').scrollIntoView({ block: 'center' })""")
        page.hover('#leftpanel .prop[data-k="tags"]')
        _panel_shot(page, "02-wiz-node-tags")
        page.evaluate("""() => { window.__copied = null;
            try { Object.defineProperty(navigator, 'clipboard', { configurable: true,
                value: { writeText: t => { window.__copied = t; return Promise.resolve(); } } }); } catch (e) {} }""")
        page.hover('#leftpanel .prop[data-k="memoryMB"]')
        page.click('#leftpanel .prop[data-k="memoryMB"] [data-copy]')
        page.wait_for_timeout(150)
        copied = page.evaluate("() => [window.__copied, document.querySelector('#toast').textContent]")
        check("3: copy puts the raw value on the clipboard and says so",
              copied[0] == "16384" and "Copied" in (copied[1] or ""), safe(copied))

    def strict():
        got = page.evaluate(
            """() => {
                const html = (k, v) => { const d = document.createElement('div'); d.innerHTML = propValue(k, v); return d; };
                const isDate = (k, v) => !!html(k, v).querySelector('.pv-date');
                const isLink = v => !!html('u', v).querySelector('a.pv-link');
                return {
                    dates: [isDate('firstseen', 1794438072532), isDate('lastupdated', 1794441231),
                            isDate('launchtime', '2018-10-14T16:30:26.000000000'), isDate('creationDate', '2024-03-01T12:34:56Z')],
                    notDates: [isDate('uptime', 3600), isDate('count', 1732327995), isDate('memoryMB', 1732327995),
                               isDate('format', 1732327995), isDate('name', '2024-03-01'), isDate('name', '20240301'),
                               isDate('launchtime', 17323279950), isDate('launchtime', 1732327995.5)],
                    // A duration in seconds or milliseconds can have 10 or 13 digits. It is not a date.
                    durations: [isDate('uptime', 1209600000), isDate('retentionTime', 2592000000),
                                isDate('runtime', 1500000000), isDate('maxConnectionIdleTime', 3600000000),
                                isDate('cpuTime', 1500000000000), isDate('lifetime', 1500000000),
                                isDate('responseTime', 1500000000), isDate('downtime', 1500000000)],
                    links: [isLink('http://a.example/x'), isLink('https://a.example/x')],
                    notLinks: [isLink('javascript:alert(1)'), isLink('data:text/html,hi'), isLink('ftp://a.example'),
                               isLink(' javascript:alert(1)'),
                               // A user name or password in a URL can hide the real host after a trusted-looking prefix.
                               isLink('https://app.wiz.io.' + 'console.'.repeat(22) + '@evil.example/login'),
                               isLink('https://user:pw@a.example/x')],
                    linkTitle: (() => { const a = html('u', 'https://a.example/' + 'p'.repeat(200)).querySelector('a');
                                        return a ? a.title : null; })(),
                    emptyArray: html('subnets', []).innerHTML, nullArray: html('subnets', [null, 'a', null]).textContent,
                    emptyCount: [isEmptyValue([]), isEmptyValue({}), isEmptyValue([0]), isEmptyValue(0), isEmptyValue(false)],
                    quoted: (() => { const a = html('u', 'https://a.example/"onmouseover="x').querySelector('a');
                                     return a ? [...a.attributes].map(x => x.name).sort().join(',') : null; })(),
                    escaped: html('k', '<img src=x onerror=alert(1)>').querySelector('img') === null,
                    deep: html('tags', { a: { b: { c: 1 } } }).textContent,
                };
            }""")
        check("3: dates are strict: time-named keys with 10 or 13 digits, and ISO strings only",
              got["dates"] == [True] * 4 and got["notDates"] == [False] * 8, safe(got))
        check("3: a duration with 10 or 13 digits is not a date",
              got["durations"] == [False] * 8, safe(got))
        check("3: only http and https become links, and values are escaped",
              got["links"] == [True, True] and got["notLinks"] == [False] * 6 and got["escaped"]
              and got["quoted"] == "class,href,rel,target,title", safe(got))
        check("3: a long link keeps its full address in the tooltip",
              got["linkTitle"] == "https://a.example/" + "p" * 200, safe(got))
        check("3: an empty array or object counts as empty, and an array of nulls shows as empty",
              "[]" in got["emptyArray"] and "pv-null" in got["emptyArray"] and got["nullArray"] == "null, a, null"
              and got["emptyCount"] == [True, True, False, False, False], safe(got))

    def chip_icon():
        got = page.evaluate(
            """() => {
                S.cat.VIRTUAL_MACHINE = { ...catStyle('VIRTUAL_MACHINE'), icon: 'Server' };
                renderLeftPanel();
                const chip = document.querySelector('#leftpanel .lp-cats .cat');
                const r = { icon: !!(chip && chip.querySelector('i[data-icon="Server"] svg')),
                            text: chip ? chip.textContent.trim() : null };
                S.cat.VIRTUAL_MACHINE.icon = null;
                renderLeftPanel();
                const bare = document.querySelector('#leftpanel .lp-cats .cat');
                r.bare = bare ? [bare.textContent.trim(), !!bare.querySelector('i')] : null;
                return r;
            }""")
        check("4: the category chip shows its icon before the label, and stays plain without one",
              got["icon"] and got["text"] == "VIRTUAL_MACHINE" and got["bare"] == ["VIRTUAL_MACHINE", False], safe(got))

    def edges():
        # An expand edge carries no properties. Wiz graph search does not report its type.
        page.evaluate(
            """() => { addGraph({ nodes: [], edges: [{ id: 'alpha-1:related:sg-web', type: 'related',
                source: 'alpha-1', target: 'sg-web', props: {} }] }, null, { toastTruncation: false });
                selectOnly(window.cy.getElementById('alpha-1:related:sg-web')); }""")
        page.wait_for_timeout(200)
        plain = page.evaluate(
            """() => {
                const lp = document.querySelector('#leftpanel');
                return { text: lp.innerText, chips: [...lp.querySelectorAll('.lp-ends .cat')].map(c => c.textContent.trim()),
                         links: [...lp.querySelectorAll('.lp-ends [data-goto]')].map(a => a.dataset.goto),
                         none: !!lp.querySelector('.empty') && lp.querySelector('.empty').textContent.includes('No properties') };
            }""")
        check("5: a Wiz related edge says Wiz does not report its type, and shows both ends with chips",
              "Wiz graph search does not report" in plain["text"] and not plain["none"]
              and plain["chips"] == ["VIRTUAL_MACHINE", "FIREWALL"]
              and plain["links"] == ["alpha-1", "sg-web"], safe(plain))
        _panel_shot(page, "04-wiz-edge")
        page.evaluate("() => window.cy.getElementById('alpha-1:related:sg-web').remove()")
        page.evaluate(
            """() => addGraph({ nodes: [], edges: [{ id: 'delta-5:related (via unselected):sg-db',
                type: 'related (via unselected)', source: 'delta-5', target: 'sg-db',
                props: { indirect: true, relationship: ['CONTAINS', 'ANY'] } }] }, null, { toastTruncation: false })""")
        has_ends = page.evaluate(
            "() => window.cy.getElementById('delta-5:related (via unselected):sg-db').nonempty()")
        if has_ends:
            page.evaluate("() => selectOnly(window.cy.getElementById('delta-5:related (via unselected):sg-db'))")
            page.wait_for_timeout(200)
            rich = page.evaluate(
                """() => {
                    const lp = document.querySelector('#leftpanel');
                    const v = k => { const r = lp.querySelector(`.prop[data-k="${k}"] .v`); return r ? r.textContent.trim() : null; };
                    const chip = lp.querySelector('.prop[data-k="indirect"] .pv-bool');
                    return { rel: v('relationship'), indirect: chip ? chip.textContent.trim() : null,
                             note: lp.innerText.includes("Wiz graph search does not report") };
                }""")
            page.evaluate("() => window.cy.getElementById('delta-5:related (via unselected):sg-db').remove()")
        else:
            rich = {"missing": "delta-5 or sg-db is not on the canvas"}
        check("5: a saved-query edge lists relationship and indirect through the property list",
              rich.get("rel") == "CONTAINS, ANY" and rich.get("indirect") == "true" and not rich.get("note"), safe(rich))

    def partial():
        page.fill("#search-input", "gamma")
        page.wait_for_selector("#search-results .sr")
        page.route("**/api/nodes", lambda route: route.fulfill(
            status=502, content_type="application/json", body=json.dumps({"error": "tenant said no"})))
        reads = []
        page.on("request", lambda req: reads.append(req.url) if "/api/nodes" in req.url else None)
        page.click("#search-results .sr")
        page.wait_for_function("() => window.cy.getElementById('gamma-3').nonempty()", timeout=10000)
        page.wait_for_function(
            "() => (document.querySelector('#leftpanel') || {}).textContent.includes('tenant said no')", timeout=10000)
        failed = page.evaluate(
            """() => { const lp = document.querySelector('#leftpanel');
                return { retry: !!lp.querySelector('[data-lp=retry]'), shortProps: !!lp.querySelector('.prop[data-k="name"]') }; }""")
        check("6: a failed property read shows the error in the panel with a Retry button, and keeps the short props",
              failed["retry"] and failed["shortProps"], safe(failed))
        _panel_shot(page, "06-partial-error")
        page.unroute("**/api/nodes")
        page.click("#leftpanel [data-lp=retry]")
        page.wait_for_function(
            "() => !window.cy.getElementById('gamma-3').data('partial')", timeout=10000)
        page.wait_for_timeout(150)
        loaded = page.evaluate(
            """() => { const lp = document.querySelector('#leftpanel');
                return { native: !!lp.querySelector('.prop[data-k="nativeType"]'),
                         reading: lp.textContent.includes('Reading properties'), err: lp.textContent.includes('tenant said no') }; }""")
        check("6: Retry loads the full properties and clears the error",
              loaded["native"] and not loaded["reading"] and not loaded["err"], safe(loaded))
        _select_node(page, "alpha-1")
        _select_node(page, "gamma-3")
        page.wait_for_timeout(200)
        check("6: a node whose full properties are loaded is not read again", len(reads) == 2, safe(reads))

    def header():
        # A Wiz id is a UUID. It stays on one line and leaves the caption most of the header.
        page.evaluate(
            """() => addGraph({ nodes: [
                { id: 'e2b5a1c4-3f1e-5b7a-9c2d-1e2f3a4b5c6d', category: 'VIRTUAL_MACHINE', labels: ['VIRTUAL_MACHINE'],
                  props: { name: 'worker-notify-3 - i-2ccd015c1ad5900dd' }, partial: false },
                { id: 'long-1', category: 'VIRTUAL_MACHINE', labels: ['VIRTUAL_MACHINE'],
                  props: { name: 'worker-notify-3 - i-2ccd015c1ad5900dd' }, partial: false }], edges: [] },
                null, { toastTruncation: false })""")
        sizes = {}
        for nid in ("e2b5a1c4-3f1e-5b7a-9c2d-1e2f3a4b5c6d", "long-1"):
            _select_node(page, nid)
            page.wait_for_timeout(150)
            if nid == "long-1":
                _panel_shot(page, "10b-short-id-header")
            else:
                _panel_shot(page, "10a-uuid-header")
            sizes[nid] = page.evaluate(
                """() => { const lp = document.querySelector('#leftpanel');
                    const t = lp.querySelector('.lp-title').getBoundingClientRect(), i = lp.querySelector('.lp-id').getBoundingClientRect();
                    const line = parseFloat(getComputedStyle(lp.querySelector('.lp-id')).lineHeight) || 18;
                    return { titleW: Math.round(t.width), idH: Math.round(i.height), line: Math.round(line), idTop: Math.round(i.top),
                             titleBottom: Math.round(t.bottom), cats: Math.round(lp.querySelector('.lp-cats').getBoundingClientRect().top - t.top),
                             id: lp.querySelector('.lp-id').textContent, title: lp.querySelector('.lp-id').title }; }""")
        uuid, short = sizes["e2b5a1c4-3f1e-5b7a-9c2d-1e2f3a4b5c6d"], sizes["long-1"]
        check("header: the short id stays on one line beside the caption",
              uuid["idH"] <= uuid["line"] + 2 and short["idH"] <= short["line"] + 2 and short["id"] == "#long-1"
              and uuid["title"] == "e2b5a1c4-3f1e-5b7a-9c2d-1e2f3a4b5c6d" and uuid["titleW"] >= 240
              and uuid["cats"] == short["cats"], safe(sizes))
        page.evaluate("""() => removeEles(window.cy.$('#e2b5a1c4-3f1e-5b7a-9c2d-1e2f3a4b5c6d, #long-1'))""")
        page.evaluate("() => { S.undo.pop(); updateUndoBtns(); }")

    def edge_id():
        # A Wiz edge id joins its two ends. Its last part is the target's id, so the header must not show it.
        page.evaluate(
            """() => { addGraph({ nodes: [], edges: [{ id: 'alpha-1:related:sg-web', type: 'related',
                source: 'alpha-1', target: 'sg-web', props: {} }] }, null, { toastTruncation: false });
                selectOnly(window.cy.getElementById('alpha-1:related:sg-web')); }""")
        page.wait_for_timeout(150)
        _panel_shot(page, "11-wiz-edge-header")
        got = page.evaluate(
            """() => { const lp = document.querySelector('#leftpanel'); const i = lp.querySelector('.lp-id');
                const t = lp.querySelector('.lp-title');
                return { id: i ? i.textContent.trim() : '', idTitle: i ? i.title : '', titleTitle: t ? t.title : '' }; }""")
        page.evaluate("() => window.cy.getElementById('alpha-1:related:sg-web').remove()")
        check("edge header: a Wiz edge does not show its target's id as its own",
              "sg-web" not in got["id"] and "alpha-1:related:sg-web" in (got["idTitle"] + got["titleTitle"]), safe(got))

    def stale_load():
        # A failed read belongs to the node that asked. A node that leaves and comes back gets a fresh read.
        page.route("**/api/nodes", lambda route: route.fulfill(
            status=502, content_type="application/json", body=json.dumps({"error": "tenant said no"})))
        reads = []
        page.on("request", lambda req: reads.append(req.url) if "/api/nodes" in req.url else None)

        def add_from_search():
            page.fill("#search-input", "open-sg")
            page.wait_for_function(
                "() => (searchRes || []).some(it => it.kind === 'node' && it.node.id === 'fw-1')", timeout=10000)
            idx = page.evaluate("() => searchRes.findIndex(it => it.kind === 'node' && it.node.id === 'fw-1')")
            page.click(f'#search-results .sr[data-i="{idx}"]')
            page.wait_for_function("() => window.cy.getElementById('fw-1').nonempty()", timeout=10000)

        page.evaluate("() => window.cy.getElementById('fw-1').remove()")
        try:
            add_from_search()
            page.wait_for_function(
                "() => (document.querySelector('#leftpanel') || {}).textContent.includes('tenant said no')", timeout=10000)
            page.evaluate("() => removeEles(window.cy.getElementById('fw-1'))")
        finally:
            page.unroute("**/api/nodes")
        before = len(reads)
        add_from_search()
        page.wait_for_function("() => !window.cy.getElementById('fw-1').data('partial')", timeout=10000)
        page.wait_for_timeout(150)
        got = page.evaluate(
            """() => { const lp = document.querySelector('#leftpanel');
                return { err: lp.textContent.includes('tenant said no'), native: !!lp.querySelector('.prop[data-k="nativeType"]') }; }""")
        got["reads"] = len(reads) - before
        check("6: a node that leaves the canvas after a failed read gets a fresh read when it comes back",
              got["reads"] == 1 and not got["err"] and got["native"], safe(got))
        page.evaluate("() => { window.cy.getElementById('fw-1').remove(); closeSearch(); }")

    def menu_chip():
        got = page.evaluate(
            """() => { S.cat.VIRTUAL_MACHINE = { ...catStyle('VIRTUAL_MACHINE'), icon: 'Server' };
                showMenu([{ html: catChip('VIRTUAL_MACHINE'), right: '1', act: () => {} }], 200, 200);
                const i = document.querySelector('#menus .cat i[data-icon]');
                const r = i ? [Math.round(i.getBoundingClientRect().width), Math.round(i.getBoundingClientRect().height)] : null;
                closeMenus(); S.cat.VIRTUAL_MACHINE.icon = null; return r; }""")
        check("4: a category chip icon keeps its 13px size inside a menu", got == [13, 13], safe(got))

    def dark():
        _select_node(page, "alpha-1")
        page.wait_for_timeout(150)
        light = page.evaluate(
            "() => { const c = document.querySelector('#leftpanel .pv-bool.on'); return c ? getComputedStyle(c).backgroundColor : null; }")
        page.evaluate("() => { document.body.classList.add('dark'); }")
        page.wait_for_timeout(200)
        colours = page.evaluate(
            """() => { const c = document.querySelector('#leftpanel .pv-bool.on');
                const lp = document.querySelector('#leftpanel');
                return c ? [getComputedStyle(c).backgroundColor, getComputedStyle(c).color, getComputedStyle(lp).backgroundColor] : null; }""")
        _panel_shot(page, "05-dark-node")
        page.evaluate(
            """() => { addGraph({ nodes: [], edges: [{ id: 'delta-5:related (via unselected):sg-db',
                type: 'related (via unselected)', source: 'delta-5', target: 'sg-db',
                props: { indirect: true, relationship: ['CONTAINS', 'ANY'] } }] }, null, { toastTruncation: false });
                selectOnly(window.cy.getElementById('delta-5:related (via unselected):sg-db')); }""")
        _panel_shot(page, "07-dark-edge")
        page.evaluate("() => window.cy.getElementById('delta-5:related (via unselected):sg-db').remove()")
        page.evaluate("() => { document.body.classList.remove('dark'); }")
        # The dark tokens must apply. The light chip on the dark card would pass a plain contrast check.
        check("3: the boolean chip uses theme tokens in dark mode",
              colours is not None and light is not None and colours[0] != light
              and colours[0] == "rgb(31, 66, 41)" and colours[1] == "rgb(159, 220, 170)"
              and colours[0] != colours[2], safe({"light": light, "dark": colours}))

    guarded("1: the panel opens on select", opens_on_select)
    guarded("2: the collapse tab works", collapse_tab)
    guarded("3: the panel formats Wiz values", values)
    guarded("3: the value rules are strict", strict)
    guarded("4: the category chip shows the icon", chip_icon)
    guarded("4: the menu chip icon size", menu_chip)
    guarded("header: one-line short id", header)
    guarded("5: the edge panel is useful", edges)
    guarded("edge header: the short id", edge_id)
    try:
        guarded("6: partial nodes load clearly", partial)
    finally:
        page.unroute("**/api/nodes")
    guarded("6: a stale read does not outlive its node", stale_load)
    guarded("3: dark mode chips", dark)
    page.evaluate("() => { window.cy.$(':selected').unselect(); closeSearch(); }")


def _check_wiz_query_editor(page):
    """New query on Wiz: a mutation is refused inline, a saved document runs, and delete has Undo."""
    page.evaluate("() => { newViz(); if (S.rightPanel !== 'queries') openPanel('queries'); }")
    page.wait_for_selector("#rightpanel [data-act=new-query]")
    builtin_tags = page.locator("#rightpanel .q-item .q-builtin").count()
    document = page.evaluate("() => S.queries.find(q => q.id === 'builtin:wiz-queries:open-security-groups').graphql")
    page.click("#rightpanel [data-act=new-query]")
    page.wait_for_selector(".modal [data-qe-body]")
    page.fill(".modal [data-qe-name]", "<b>Open groups</b>")
    page.fill(".modal [data-qe-tags]", "aws, mine")
    page.fill(".modal [data-qe-body]", "mutation { deleteEverything { id } }")
    page.click(".modal [data-qe-validate]")
    page.wait_for_selector(".modal [data-qe-out] .err")
    refused = page.locator(".modal [data-qe-out] .err").inner_text()
    page.fill(".modal [data-qe-body]", document)
    page.fill(".modal [data-qe-vars]", "{not json")
    page.click(".modal [data-qe-save]")
    page.wait_for_function("() => (document.querySelector('.modal [data-qe-out] .err') || {}).textContent.includes('JSON')")
    page.fill(".modal [data-qe-vars]", json.dumps({"projectId": "*", "first": 50, "quick": True}))
    page.click(".modal [data-qe-preview]")
    page.wait_for_function("() => (document.querySelector('.modal [data-qe-out]') || {}).textContent.includes('4 nodes')", timeout=15000)
    preview = page.locator(".modal [data-qe-out]").inner_text()
    still_empty = page.evaluate("() => window.cy.nodes().length")
    page.click(".modal [data-qe-saverun]")
    page.wait_for_function("() => window.cy.getElementById('sg-web').nonempty()", timeout=15000)
    drawn = page.evaluate(
        """() => ({ edges: window.cy.edges().map(edge => edge.id()).sort(),
                    rows: [...document.querySelectorAll('#rightpanel .q-item')].map(el => el.querySelector('.q-name').textContent),
                    bold: !!document.querySelector('#rightpanel .q-name b'),
                    mine: S.queries.filter(q => !q.builtin).map(q => q.id) })""")
    check("Wiz New query refuses a mutation, checks variables, previews, and saves and runs",
          builtin_tags == 2 and "not sent" in refused and "4 nodes" in preview and still_empty == 0
          and drawn["edges"] == ["sg-db:related:rule-ssh", "sg-web:related:rule-https", "sg-web:related:rule-ssh"]
          and "<b>Open groups</b>" in drawn["rows"] and not drawn["bold"] and len(drawn["mine"]) == 1,
          safe({"refused": refused, "preview": preview, "drawn": drawn, "tags": builtin_tags}))
    qid = drawn["mine"][0] if drawn["mine"] else ""
    page.evaluate("() => { window.confirm = () => true; newViz(); }")
    row = page.locator(f"#rightpanel .q-item[data-qrun='{qid}']")
    row.hover()
    row.locator("[data-qact=dup]").click()
    page.wait_for_function("() => S.queries.filter(q => !q.builtin).length === 2")
    row.hover()
    row.locator("[data-qact=del]").click()
    page.wait_for_function(f"() => !document.querySelector(\"#rightpanel [data-qrun='{qid}']\")")
    page.click("#toast [data-toast-act]")
    page.wait_for_selector(f"#rightpanel [data-qrun='{qid}']")
    page.click(f"#rightpanel [data-qrun='{qid}'] .q-name")
    page.wait_for_function("() => window.cy.getElementById('rule-ssh').nonempty() && window.cy.edges().length === 3", timeout=15000)
    check("Wiz user query: duplicate, delete with Undo, and a row click runs it", True)


def _check_blocked_storage(browser, base):
    """A browser that blocks localStorage still starts the page. It only loses what the page would save."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
    ctx.add_init_script(
        """Object.defineProperty(window, 'localStorage', { configurable: true,
            get() { throw new DOMException('storage blocked', 'SecurityError'); } });""")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    try:
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function("() => typeof S !== 'undefined' && S.ready", timeout=40000)
        got = page.evaluate(
            """() => { addGraph({ nodes: [{ id: 'blk-1', category: 'BUCKET', labels: ['BUCKET'], props: { name: 'b' } }], edges: [] },
                    null, { toastTruncation: false });
                selectOnly(window.cy.getElementById('blk-1'));
                try { persistStyles(); saveRules(); toggleTheme(); toggleTheme(); } catch (e) { return 'threw: ' + e.message; }
                return true; }""")
        if got is True:
            page.wait_for_timeout(200)
            got = page.evaluate(
                """() => ({ panel: !document.querySelector('#leftpanel').classList.contains('hidden'),
                           title: (document.querySelector('#leftpanel .lp-title') || {}).textContent || null })""")
        check("blocked localStorage does not stop the page", isinstance(got, dict) and got.get("panel") and not errors,
              safe({"got": got, "errors": errors}))
    except Exception as exc:  # noqa: BLE001
        check("blocked localStorage does not stop the page", False, safe({"exc": str(exc), "errors": errors})[:400])
    finally:
        ctx.close()


def _check_wiz_connection_dialog(page):
    """The connection is Wiz in the menu and the dialog. The dialog names each offering."""
    menu = page.evaluate(
        """async () => { await refreshConnectionLinks(); return {
            items: mainMenuItems().filter(it => it && it.label).map(it => it.label),
            button: (document.querySelector('[data-gql-label]') || {}).textContent || '',
            name: savedGraphqlName() }; }""")
    check("the menu and the connection button call the connection Wiz",
          menu["name"] == "Wiz" and menu["button"] == "Wiz connection"
          and "Wiz connection…" in menu["items"] and not any("GraphQL" in it for it in menu["items"]),
          safe(menu))
    page.evaluate("() => openConnection('graphql')")
    page.wait_for_selector("[data-ge]", timeout=10000)
    title = page.evaluate("() => [...document.querySelectorAll('.modal-bg .mh')].map(e => e.textContent.trim()).join('|')")
    note = page.evaluate("() => (document.querySelector('.modal-bg .section-d') || {}).textContent || ''")
    placeholder = page.get_attribute("[data-ge]", "placeholder") or ""
    check("the dialog is titled Wiz connection and names both offerings",
          "Wiz connection" in title and "Wiz (wiz.io)" in note and "Wiz for Government (wiz.us)" in note
          and "<region>" in placeholder and not re.search(r"api\.[a-z]{2}[0-9]\.app", placeholder), safe({"title": title, "note": note[:160]}))
    seen = {}
    for host, want in (("https://api.example-region.app.wiz.us/graphql", _offering("wiz.us")),
                       ("https://api.example-region.app.wiz.io/graphql", _offering("wiz.io"))):
        page.evaluate("() => { const a = document.querySelector('[data-gauth]'); a.value = ''; delete a.dataset.touched; }")
        page.fill("[data-ge]", host)
        seen[want["domain"]] = page.evaluate(
            """() => ({ line: document.querySelector('[data-goffer]').textContent,
                        auth: document.querySelector('[data-gauth]').value })""")
        check(f"typing a {want['domain']} host shows {want['label']} and its token URL",
              want["label"] in seen[want["domain"]]["line"] and seen[want["domain"]]["auth"] == want["auth_url"],
              safe(seen[want["domain"]]))
    page.fill("[data-ge]", "https://graphql.example.test/graphql")
    other = page.evaluate("() => document.querySelector('[data-goffer]').textContent")
    check("a host that is not Wiz says so", "Not a Wiz host" in other, safe(other))
    generic = page.evaluate(
        """() => ({ g: connectionName('generic', 'https://graphql.example.test/graphql'),
                    w: connectionName('', 'https://api.example-region.app.wiz.us/graphql'),
                    unknown: connectionName('', 'https://graphql.example.test/graphql') })""")
    check("a generic source keeps the name GraphQL and a Wiz host is Wiz",
          generic == {"g": "GraphQL", "w": "Wiz", "unknown": "GraphQL"}, safe(generic))
    page.evaluate("() => closeAllModals()")
    page.wait_for_function("() => !document.querySelector('[data-ge]')", timeout=5000)


def _drive_tenant_page(base):
    """Headed page: search, saved queries, expand, filters, diagnostics, and a failed expand."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  skip headed page (playwright is not installed)")
        return
    traffic = []
    errors = []
    where = "boot"
    try:
        with sync_playwright() as p:
            chrome = _chrome_executable(p)
            if not chrome:
                print("  skip headed page (no browser: set CHROME or run playwright install chromium)")
                return
            # A Linux host with no display (CI) cannot open a headed browser. GX_HEADLESS=1 also forces it.
            headless = os.environ.get("GX_HEADLESS") == "1" or (
                sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))
            browser = p.chromium.launch(headless=headless, executable_path=chrome)
            # en-US fixes the number and date format that the panel checks read.
            page = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US").new_page()
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.on("response", lambda resp: traffic.append((resp.url, resp.status)))
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_function(
                """() => window.cy && typeof window.cy.nodes === 'function'
                    && document.querySelector('#source-pick')
                    && !document.querySelector('#source-pick').disabled""",
                timeout=40000)
            page.wait_for_function(
                "() => (document.querySelector('#land-kicker') || {}).textContent.includes('Wiz')",
                timeout=20000)
            landing = page.evaluate(
                """() => ({
                    nodes: document.querySelector('#land-nodes').textContent,
                    edges: document.querySelector('#land-edges').textContent,
                    cypherHidden: [...document.querySelectorAll('[data-act=code-query]')]
                        .every(el => el.classList.contains('hidden')),
                    queries: !!document.querySelector('[data-act=explore-queries]'),
                    sources: [...document.querySelectorAll('#source-pick option')].map(o => o.value),
                    picked: document.querySelector('#source-pick').value,
                })""")
            check("the Wiz landing counts nodes and hides Cypher",
                  landing["nodes"] == "3" and landing["edges"] == "—"
                  and landing["cypherHidden"] and landing["queries"]
                  and landing["sources"] == ["neo4j", "wiz"] and landing["picked"] == "wiz",
                  safe(landing))
            page.click("[data-act=data-model]")
            page.wait_for_function(
                """() => {
                    const foot = document.querySelector('[data-dm-foot]');
                    const info = document.querySelector('[data-dminfo]');
                    return foot && info && foot.textContent.includes('node categories')
                        && info.innerText.includes('graphSearch');
                }""",
                timeout=20000)
            model_text = page.locator("[data-dm-foot]").inner_text()
            # Cytoscape indexes four caches by a 32-bit string hash. As arrays they get a
            # length near two billion, V8 sometimes gives one a dense store of gigabytes,
            # and Chrome kills the tab. The vendored file starts each cache as an object.
            caches = page.evaluate(
                """() => {
                    const kind = (c) => c == null ? 'none' : Array.isArray(c) ? 'array' : 'object';
                    const dm = (document.querySelector('.modal-bg .modal') || {})._dm;
                    const out = {};
                    for (const [name, inst] of [['main', window.cy], ['model', dm]]) {
                        if (!inst) { out[name] = 'missing'; continue; }
                        inst.elements(':visible');
                        const kinds = new Set(inst.elements().map(e => kind(e._private.styleCache)));
                        out[name] = { prop: kind(inst.style().propCache), ele: [...kinds].sort().join(',') };
                        const r = inst.renderer && inst.renderer();
                        if (r && r.nodePathCache !== undefined) out[name].nodePath = kind(r.nodePathCache);
                        if (r && r.arrowPathCache !== undefined) out[name].arrowPath = kind(r.arrowPathCache);
                    }
                    return out;
                }""")
            flat = [v for inst in caches.values() if isinstance(inst, dict) for v in inst.values()]
            check("every hash-indexed Cytoscape cache is an object, not a sparse array",
                  all(isinstance(inst, dict) for inst in caches.values())
                  and caches["main"]["ele"] in ("object", "") and caches["model"]["ele"] == "object"
                  and not any("array" in v for v in flat), safe(caches))
            page.click(".modal [data-close]")
            light_schema = page.evaluate(
                """() => ({ cats: Object.keys(S.schema.categories || {}).length,
                            entities: (S.schema.entities || []).length,
                            mode: S.schema.filter_mode })""")
            schema_calls = [url.split("/api/schema", 1)[1] for url, _status in traffic if "/api/schema" in url]
            check("the data model opens from the live schema",
                  "node categories" in model_text and "relationship totals stay on the source" in model_text
                  and "?full=1" in schema_calls and "" in schema_calls,
                  safe({"text": model_text[:300], "calls": schema_calls}))
            check("the data model leaves the light schema that Advanced search reads",
                  light_schema == {"cats": 0, "entities": len(LIVE_ENTITY_TYPES) - 1, "mode": "open"},
                  safe(light_schema))
            page.focus("#search-input")
            page.click("[data-act=advanced-search]")
            page.wait_for_selector("[data-ares]")
            page.wait_for_timeout(400)
            empty = page.locator("[data-ares]").inner_text()
            searches_before = [item for item in traffic if "/api/search" in item[0]]
            options = page.evaluate(
                """() => [...document.querySelectorAll('[data-acat] option')].map(o => o.value)""")
            page.evaluate(
                """() => {
                    const sel = document.querySelector('[data-acat]');
                    sel.value = 'BUCKET';
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                }""")
            page.wait_for_function(
                "() => (document.querySelector('[data-ares]') || {}).innerText.includes('beta')",
                timeout=10000)
            props = page.evaluate(
                """() => [...document.querySelectorAll('#aprop-list option')].map(o => o.value)""")
            advanced_text = page.locator(".modal").inner_text()
            page.evaluate(
                """() => {
                    const prop = document.querySelector('[data-aprop]');
                    prop.value = 'name';
                    prop.dispatchEvent(new Event('input', { bubbles: true }));
                    const val = document.querySelector('[data-aval]');
                    val.value = 'beta';
                    val.dispatchEvent(new Event('input', { bubbles: true }));
                }""")
            page.wait_for_function(
                """() => [...performance.getEntriesByType('resource')]
                    .some(entry => entry.name.includes('prop=name') && entry.name.includes('value=beta'))""",
                timeout=10000)
            property_hits = [
                (url, status) for url, status in traffic
                if "/api/search" in url and "prop=name" in url and "value=beta" in url]
            page.click(".modal [data-close]")
            check("advanced search waits for a name or an entity type",
                  "Type a name, or choose an entity type." in empty
                  and "invalid type" not in empty
                  and not searches_before
                  and options == ["", "BUCKET", "FIREWALL", "IP_RANGE", "NETWORK_INTERFACE",
                                  "NETWORK_SECURITY_RULE", "USER_ACCOUNT", "VIRTUAL_MACHINE"],
                  safe({"empty": empty, "options": options, "searches": len(searches_before)}))
            check("choosing an entity type lists the filters and the bucket",
                  "beta" in advanced_text and "invalid type" not in advanced_text
                  and "name" in props and "_vertexID" in props
                  and property_hits and all(status == 200 for _url, status in property_hits),
                  safe({"props": props, "property": property_hits, "text": advanced_text[:300]}))
            page.click("[data-act=explore-queries]")
            page.wait_for_function(
                """() => {
                    const panel = document.querySelector('#rightpanel');
                    const text = panel ? panel.innerText : '';
                    return text.includes('Wiz graph search')
                        && text.includes('Internet-exposed EC2 instances')
                        && text.includes('Security groups open to 0.0.0.0/0')
                        && !text.includes('MATCH')
                        && !panel.querySelector('[data-act=code-query]');
                }""")
            where = "security-group edges"
            nodes_before = sum(1 for url, _ in traffic if "/api/nodes" in url)
            page.click('#rightpanel [data-qrun="builtin:wiz-queries:open-security-groups"]')
            page.wait_for_function("() => window.cy.getElementById('sg-web').nonempty()", timeout=15000)
            drawn = page.evaluate(
                """() => ({ count: window.cy.edges().length,
                            ids: window.cy.edges().map(edge => edge.id()).sort(),
                            toast: (document.querySelector('#toast') || {}).textContent || '' })""")
            check("the security-group saved query draws edges on the canvas",
                  drawn["count"] > 0 and drawn["ids"] == [
                      "sg-db:related:rule-ssh", "sg-web:related:rule-https", "sg-web:related:rule-ssh"]
                  and "edges" in drawn["toast"],
                  safe(drawn))
            twins = page.evaluate(
                """() => {
                    addGraph({ nodes: [], edges: [
                        { id: 'rule-ssh:related:sg-web', type: 'related', source: 'rule-ssh', target: 'sg-web', props: {} },
                        { id: 'sg-web:related (via unselected):rule-ssh', type: 'related (via unselected)',
                          source: 'sg-web', target: 'rule-ssh', props: { indirect: true, relationship: ['ANY'] } },
                    ] }, null, { toastTruncation: false });
                    const pair = window.cy.edges().filter(edge =>
                        [edge.source().id(), edge.target().id()].sort().join('|') === 'rule-ssh|sg-web');
                    return pair.map(edge => edge.id());
                }""")
            check("an expand edge and a saved-query edge for one pair stay one edge",
                  twins == ["sg-web:related:rule-ssh"], safe(twins))
            upgraded = page.evaluate(
                """() => {
                    addGraph({ nodes: [], edges: [{ id: 'sg-db:related (via unselected):rule-https',
                        type: 'related (via unselected)', source: 'sg-db', target: 'rule-https',
                        props: { indirect: true, relationship: ['ANY'] } }] }, null, { toastTruncation: false });
                    const before = window.cy.getElementById('sg-db:related (via unselected):rule-https').nonempty();
                    addGraph({ nodes: [], edges: [{ id: 'rule-https:related:sg-db', type: 'related',
                        source: 'rule-https', target: 'sg-db', props: {} }] }, null, { toastTruncation: false });
                    const pair = window.cy.edges().filter(edge =>
                        [edge.source().id(), edge.target().id()].sort().join('|') === 'rule-https|sg-db');
                    const ids = pair.map(edge => edge.id());
                    pair.remove();
                    return { before, ids };
                }""")
            check("a direct edge replaces an indirect edge for the same pair",
                  upgraded == {"before": True, "ids": ["rule-https:related:sg-db"]}, safe(upgraded))
            full = page.evaluate(
                """() => {
                    addGraph({ nodes: [], edges: [{ id: 'sg-db:related (via unselected):rule-https',
                        type: 'related (via unselected)', source: 'sg-db', target: 'rule-https',
                        props: { indirect: true, relationship: ['ANY'] } }] }, null, { toastTruncation: false });
                    const saved = S.budget.edges;
                    S.budget.edges = canvasEdges();
                    try {
                        addGraph({ nodes: [], edges: [
                            { id: 'rule-https:related:sg-db', type: 'related', source: 'rule-https', target: 'sg-db', props: {} },
                            { id: 'sg-web:related:sg-db', type: 'related', source: 'sg-web', target: 'sg-db', props: {} },
                        ] }, null, { toastTruncation: false });
                    } finally { S.budget.edges = saved; }
                    const pair = window.cy.edges().filter(edge =>
                        [edge.source().id(), edge.target().id()].sort().join('|') === 'rule-https|sg-db');
                    const ids = pair.map(edge => edge.id());
                    const extra = window.cy.getElementById('sg-web:related:sg-db').nonempty();
                    pair.remove();
                    return { ids, extra };
                }""")
            check("a full edge budget still swaps an indirect edge for its direct edge",
                  full == {"ids": ["rule-https:related:sg-db"], "extra": False}, safe(full))
            page.click('#rightpanel [data-qrun="builtin:wiz-queries:internet-exposed-ec2"]')
            page.wait_for_function(
                """() => window.cy.getElementById('alpha-1').nonempty()
                    && window.cy.getElementById('delta-5').nonempty()""",
                timeout=15000)
            check("saved queries place their entities on the canvas",
                  page.evaluate(
                      """() => ['sg-web', 'rule-ssh', 'alpha-1', 'delta-5']
                          .every(id => window.cy.getElementById(id).nonempty())"""),
                  "")
            # A Wiz node has no degree, so the page must not ask /api/nodes for one after a saved query.
            page.wait_for_timeout(500)
            degree_lookups = sum(1 for url, _ in traffic if "/api/nodes" in url) - nodes_before
            check("a saved query on Wiz sends no degree lookup",
                  degree_lookups == 0, f"{degree_lookups} /api/nodes requests")
            # A full (not partial) Wiz node from a saved query must keep an unknown degree.
            # A degree of 0 makes double-click and E say "All neighbors are already displayed".
            degrees = page.evaluate(
                """() => ['alpha-1', 'delta-5'].map(id => {
                    const n = window.cy.getElementById(id);
                    return [n.data('partial') === true, n.data('degree') === undefined ? 'undefined' : n.data('degree')];
                })""")
            check("a saved-query node on Wiz keeps an unknown degree, so expand asks for neighbors",
                  all(degree is None for _, degree in degrees), safe(degrees))
            where = "selection panel"
            _check_selection_panel(page, traffic)
            _expand_selected(page, "alpha-1")
            page.wait_for_function(
                "() => window.cy.getElementById('beta-2').nonempty()",
                timeout=15000)
            toast = page.locator("#toast").inner_text()
            neighbor_codes = [status for url, status in traffic if "/api/neighbors" in url]
            expand_codes = [status for url, status in traffic if "/api/expand" in url]
            between = [url for url, _status in traffic if "/api/edges-between" in url]
            where = "filters"
            check("Expand adds the neighbor and does not fan out edge lookups",
                  neighbor_codes and expand_codes
                  and all(code == 200 for code in neighbor_codes)
                  and all(code == 200 for code in expand_codes)
                  and not between
                  and "invalid type" not in toast and "Expand failed" not in toast,
                  safe({"neighbors": neighbor_codes, "expand": expand_codes, "between": between, "toast": toast}))
            page.click("[data-panel=filters]")
            page.wait_for_function(
                "() => document.querySelector('#rightpanel').textContent.includes('VIRTUAL_MACHINE')")
            where = "filter toggle"
            page.click('#rightpanel [data-ftog="cat|VIRTUAL_MACHINE"]')
            page.wait_for_function("() => window.cy.getElementById('alpha-1').hasClass('filtered')")
            page.click('#rightpanel [data-ftog="cat|VIRTUAL_MACHINE"]')
            page.wait_for_function("() => !window.cy.getElementById('alpha-1').hasClass('filtered')")
            where = "edge filters"
            page.click("#rightpanel [data-ftab=edges]")
            page.wait_for_function(
                """() => {
                    const text = document.querySelector('#rightpanel').textContent || '';
                    return text.includes('related')
                        && window.cy.edges().some(edge => edge.data('type') === 'related');
                }""")
            check("filters hide a category and list the related edge", True)
            where = "indirect edge"
            page.evaluate(
                """() => addGraph({ nodes: [], edges: [{
                    id: 'sg-db:related (via unselected):rule-https', type: 'related (via unselected)',
                    source: 'sg-db', target: 'rule-https', props: { indirect: true, relationship: ['CONTAINS'] } }] },
                    null, { toastTruncation: false })""")
            page.wait_for_function(
                """() => (document.querySelector('#rightpanel').textContent || '')
                    .includes('related (via unselected)')""")
            page.click('#rightpanel [data-ftog="type|related (via unselected)"]')
            page.wait_for_function(
                """() => window.cy.getElementById('sg-db:related (via unselected):rule-https').hasClass('filtered')""")
            indirect = page.evaluate(
                """() => {
                    const edge = window.cy.getElementById('sg-db:related (via unselected):rule-https');
                    const d = edge.data();
                    return { caption: VISUALS.edgeCaption(d), dash: !!(VISUALS.edgeDash && VISUALS.edgeDash(d)),
                             direct: !!(VISUALS.edgeDash && VISUALS.edgeDash(
                                 window.cy.getElementById('sg-web:related:rule-ssh').data())) };
                }""")
            page.click('#rightpanel [data-ftog="type|related (via unselected)"]')
            page.wait_for_function(
                """() => !window.cy.getElementById('sg-db:related (via unselected):rule-https').hasClass('filtered')""")
            check("the indirect edge type renders dashed, lists in the filters, and filters",
                  indirect == {"caption": "related (via unselected)", "dash": True, "direct": False},
                  safe(indirect))
            where = "diagnostics"
            page.click("[data-act=main-menu]")
            page.locator("#menus .mi", has_text="Diagnostics").click()
            page.wait_for_function(
                """() => {
                    const box = document.querySelector('[data-dlog]');
                    return box && box.value.includes('graphSearch') && box.value.includes('alpha');
                }""",
                timeout=10000)
            log_text = page.locator("[data-dlog]").input_value()
            page.click("[data-dexpand]")
            page.wait_for_function("() => document.querySelector('.modal.diag').classList.contains('is-full')")
            page.click("[data-dexpand]")
            page.wait_for_function("() => !document.querySelector('.modal.diag').classList.contains('is-full')")
            page.click(".modal.diag [data-close]")
            check("Diagnostics shows the sent query and the window expands",
                  "graphSearch" in log_text and "alpha" in log_text
                  and "fragment TypeRef" not in log_text and TOKEN not in log_text,
                  safe(log_text[-400:]))
            where = "explode"
            page.fill("#search-input", "explode")
            page.wait_for_function(
                """() => (document.querySelector('#search-results') || {}).innerText
                    .includes('upstream request failed')""",
                timeout=10000)
            check("a search error stays in the result list", True)
            where = "boom"
            page.fill("#search-input", "boom")
            page.wait_for_selector("#search-results .sr")
            page.click("#search-results .sr")
            page.wait_for_function("() => window.cy.getElementById('boom-1').nonempty()", timeout=10000)
            where = "boom expand"
            expand_before = page.evaluate(
                """() => performance.getEntriesByType('resource')
                    .filter(entry => entry.name.includes('/api/expand')).length""")
            _expand_selected(page, "boom-1")
            page.wait_for_function(
                """(n) => {
                    const more = performance.getEntriesByType('resource')
                        .filter(entry => entry.name.includes('/api/expand')).length > n;
                    const toast = (document.querySelector('#toast') || {}).textContent || '';
                    return more && toast.includes('Expand failed');
                }""",
                arg=expand_before, timeout=10000)
            failed_neighbors = [status for url, status in traffic if "/api/neighbors" in url]
            failed_expand = [status for url, status in traffic if "/api/expand" in url]
            still_there = page.evaluate("() => window.cy.getElementById('alpha-1').nonempty()")
            check("a failed Expand reports the error and leaves the canvas",
                  502 in failed_neighbors and 502 in failed_expand and still_there and not errors,
                  safe({"neighbors": failed_neighbors, "expand": failed_expand, "errors": errors}))
            where = "source switch"
            page.evaluate("() => { window.confirm = () => false; }")
            page.evaluate(
                """() => {
                    const sel = document.querySelector('#source-pick');
                    sel.value = 'neo4j';
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                }""")
            page.wait_for_function(
                """() => document.querySelector('#source-pick').value === 'wiz'
                    && window.cy.getElementById('alpha-1').nonempty()""")
            check("cancelling a source switch keeps the Wiz canvas", True)
            where = "wiz connection"
            _check_wiz_connection_dialog(page)
            where = "remove"
            _select_node(page, "beta-2")
            _open_panel(page)
            page.click("#leftpanel [data-lp=remove]")
            page.wait_for_function("() => window.cy.getElementById('beta-2').empty()")
            page.click("#btn-undo")
            page.wait_for_function("() => window.cy.getElementById('beta-2').nonempty()")
            check("removing a node can be undone", True)
            where = "wiz saved query editor"
            _check_wiz_query_editor(page)
            where = "blocked storage"
            _check_blocked_storage(browser, base)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        check("the headed page finishes the GraphQL flow", False, where + ": " + safe(exc))
        return
    print("  HEADED ok")


def test_wiz_library_and_node_total():
    """Each connection has its own query library. Wiz node total is one totalCount."""
    print("wiz library and node total")
    wiz = json.loads((ROOT / "data" / "wiz-queries.json").read_text())
    cypher = json.loads((ROOT / "data" / "cartography-queries.json").read_text())
    names = [item["name"] for item in wiz]
    check("the Wiz library is the two shared graph searches",
          names == ["Internet-exposed EC2 instances", "Security groups open to 0.0.0.0/0"]
          and all({key: item.get("variables", {}).get(key) for key in ("projectId", "first", "quick")}
                  == {"projectId": "*", "first": 500, "quick": True} for item in wiz)
          and all("$query" not in item["graphql"] and "quick: $quick" in item["graphql"] for item in wiz)
          and "$where: GraphPropertyPredicate" in wiz[0]["graphql"]
          and "accessibleFrom.internet" in wiz[0]["variables"].get("where", {})
          and ".internet:" not in wiz[0]["graphql"]
          and "0.0.0.0/0" in wiz[1]["graphql"]
          and "CONTAINS_SRC_IP_RANGE" in wiz[1]["graphql"]
          and all("cypher" in item for item in cypher),
          safe(names))
    class _Kind:
        def __init__(self, kind, adapter=""):
            self.kind = kind
            self.adapter_name = adapter

    neo = server.query_library(_Kind("neo4j"))
    check("Neo4j still loads the Cypher library",
          any(item.get("name") == "Publicly readable S3 buckets" for item in neo)
          and all("cypher" in item for item in neo),
          safe([item.get("name") for item in neo]))
    empty = server.query_library(server.UnconfiguredNeo4j())
    check("an unconfigured Neo4j has no query library", empty == [])
    generic = GraphQLSource({
        "id": "g", "label": "Generic", "endpoint": "https://example.test/graphql",
        "adapter": "generic", "token_env": "GRAPHQL_TOKEN",
    }, client=client(lambda request, timeout=None: Resp(200, b'{"data":{"__typename":"Query"}}')))
    check("a generic source has no query library", server.query_library(generic) == [])
    check("a Wiz source loads the graph-search library",
          [item["name"] for item in server.query_library(GraphQLSource({
              "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.io/graphql",
              "adapter": "wiz", "token_env": "GRAPHQL_TOKEN",
          }, client=client(lambda request, timeout=None: Resp(200, b'{"data":{}}'))))] == names)

    built = gql.entity_count_request(tenant_payload(True, True, True, True, with_quick=True, with_max=True), "*")
    document, variables = built
    check("the count sends the query as a variable and sends quick",
          "$query: GraphEntityQueryInput!" in document
          and "$quick: Boolean" in document
          and "$after" not in document
          and "maxCountReached" in document
          and variables == {"query": {"select": True}, "projectId": "*", "first": 1, "quick": True},
          document)
    check("missing graphSearch arguments skip the count",
          gql.entity_count_request(introspection_payload(), "*") is None)

    seen = []

    def opener(request, timeout=None):
        body = json.loads(request.data.decode())
        text = body.get("query") or ""
        if "__schema" in text:
            return Resp(200, json.dumps(tenant_payload(True, True, True, True, with_quick=True, with_max=True)).encode())
        if "graphSearch" in text:
            seen.append(body)
            return Resp(200, b'{"data":{"graphSearch":{"totalCount":42,"maxCountReached":true}}}')
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    src = GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.io/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN", "project_id": "*",
    }, client=client(opener))
    status = src.status()
    sent = seen[-1] if seen else {}
    check("status reports the node total as a floor and leaves edges unknown",
          status.get("connected") is True and status.get("nodes") == 42
          and status.get("edges") is None and status.get("nodes_floor") is True
          and (sent.get("variables") or {}) == {
              "query": {"select": True}, "projectId": "*", "first": 1, "quick": True}
          and "$query: GraphEntityQueryInput!" in (sent.get("query") or ""),
          safe(sent)[:500])

    def fail_count(request, timeout=None):
        text = json.loads(request.data.decode()).get("query") or ""
        if "__schema" in text:
            return Resp(200, json.dumps(tenant_payload(True, True, True, True, with_quick=True)).encode())
        if "graphSearch" in text:
            return Resp(500, b'{"errors":[{"message":"count failed"}]}')
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    failed = GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.io/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN",
    }, client=client(fail_count, max_retries=0))
    quiet = failed.status()
    check("a count failure leaves the node total unknown and stays connected",
          quiet.get("connected") is True and quiet.get("nodes") is None and quiet.get("edges") is None
          and "error" not in quiet,
          safe(quiet))

    bare = []

    def bare_opener(request, timeout=None):
        text = json.loads(request.data.decode()).get("query") or ""
        bare.append(text)
        if "__schema" in text:
            return Resp(200, json.dumps(introspection_payload()).encode())
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    bare_status = GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example.test/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN",
    }, client=client(bare_opener)).status()
    check("a schema with no graphSearch arguments does not send a count",
          bare_status.get("nodes") is None and bare_status.get("connected") is True
          and not any("EntityTotal" in text or "graphSearch(" in text for text in bare),
          safe(bare_status))

    pages = []

    def page_opener(request, timeout=None):
        body = json.loads(request.data.decode())
        # The shape load that runs before the first saved query is not a page.
        if is_shape_probe(body.get("query")) or "__schema" in (body.get("query") or ""):
            return Resp(200, b'{"data":{}}')
        pages.append(body.get("variables") or {})
        after = pages[-1].get("after")
        if after == "c2":
            return Resp(200, json.dumps(graph_page(
                [{"id": "b", "name": "b", "type": "FIREWALL", "properties": {"region": "eu"}}],
                False, None, 2)).encode())
        return Resp(200, json.dumps(graph_page(
            [{"id": "a", "name": "a", "type": "VIRTUAL_MACHINE", "properties": {"region": "us"}}],
            True, "c2", 2)).encode())

    runner = GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.io/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN",
    }, client=client(page_opener))
    placed = runner.run_document(wiz[0]["graphql"], dict(wiz[0]["variables"]), limit=500)
    check("a saved query pages entities and caps first at 200",
          [node["id"] for node in placed["nodes"]] == ["a", "b"]
          and placed["edges"] == []
          and placed["nodes"][0]["props"].get("region") == "us"
          and placed["nodes"][0]["partial"] is False
          and pages[0].get("first") == 200 and pages[0].get("after") is None
          and pages[1].get("first") == 200 and pages[1].get("after") == "c2"
          and "query" not in pages[0],
          safe(pages))
    one = runner.run_document(wiz[0]["graphql"], dict(wiz[0]["variables"]), limit=1)
    check("the canvas limit stops the walk",
          [node["id"] for node in one["nodes"]] == ["a"] and one["truncated"] is True)

    sent_mutation = []

    def mutation_opener(request, timeout=None):
        sent_mutation.append(1)
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    try:
        runner.client = client(mutation_opener)
        runner.run_document("mutation { updateSecretInstance { id } }", {})
        check("a mutation is not sent", False, "no error")
    except QueryRejected as exc:
        check("a mutation is not sent", "not sent" in str(exc) and not sent_mutation, safe(exc))
    try:
        generic.run_document("query { __typename }", {})
        check("a generic source refuses a saved query", False, "no error")
    except ValueError as exc:
        check("a generic source refuses a saved query", "no instance adapter" in str(exc), safe(exc))


def _offering(domain):
    """The offering for a domain, from the one table the server and the page read."""
    return next(item for item in gql.WIZ_OFFERINGS if item["domain"] == domain)


def _check_wiz_offerings():
    """Both Wiz offerings match on the whole host. A look-alike is not Wiz."""
    io, us = _offering("wiz.io"), _offering("wiz.us")
    check("the offerings table names Wiz and Wiz for Government",
          [item["domain"] for item in gql.WIZ_OFFERINGS] == ["wiz.io", "wiz.us"]
          and io["label"] == "Wiz" and us["label"] == "Wiz for Government"
          and io["auth_url"].startswith("https://") and us["auth_url"].startswith("https://")
          and io["auth_url"] != us["auth_url"])
    for endpoint, want in (
            ("https://api.example-region.app.wiz.io/graphql", io),
            ("https://API.EXAMPLE-REGION.APP.WIZ.US/graphql", us),
            ("https://wiz.io/graphql", io),
            ("https://app.wiz.us./graphql", us),
    ):
        got = gql.wiz_offering(endpoint)
        check(f"{endpoint} is the {want['label']} offering",
              got is not None and got["domain"] == want["domain"]
              and gql.wiz_auth_url(endpoint) == want["auth_url"] and gql._adapter_name("", endpoint) == "wiz",
              safe(got))
    for endpoint in ("https://notwiz.io/graphql", "https://wiz.io.example.com/graphql",
                     "https://api.wiz.us.example/graphql", "https://example.com/wiz.io", "not a url", ""):
        check(f"{endpoint or 'an empty endpoint'} is not Wiz",
              gql.wiz_offering(endpoint) is None and gql.wiz_auth_url(endpoint) == ""
              and gql._adapter_name("", endpoint) == "generic")
    check("the page gets the offerings table and no tenant data",
          gql.wiz_offerings_public() == [dict(item) for item in gql.WIZ_OFFERINGS]
          and all(set(item) == {"id", "label", "domain", "auth_url"} for item in gql.wiz_offerings_public()))


def test_wiz_auth():
    print("wiz auth")
    seen = []

    def opener(request, timeout=None):
        headers = {key.lower(): value for key, value in request.header_items()}
        seen.append({"url": request.full_url, "headers": headers, "body": request.data.decode()})
        if request.full_url.endswith("/oauth/token"):
            return Resp(200, b'{"access_token":"minted-access-token","token_type":"bearer"}')
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    client(
        opener, token=None, client_id="client-id-zz", client_secret="client-secret-zz",
        auth_url="https://auth.example.test/oauth/token",
    ).execute("{ __typename }")
    auth, query = seen
    auth_body = auth["body"]
    check("the token request is a form and the query sends only the access token",
          auth["url"].endswith("/oauth/token")
          and "grant_type=client_credentials" in auth_body
          and "audience=wiz-api" in auth_body
          and "client_id=client-id-zz" in auth_body
          and "client-secret-zz" in auth_body
          and "clientid" not in query["headers"]
          and "client-secret-zz" not in query["body"]
          and query["headers"].get("authorization") == "Bearer minted-access-token")
    check("a wiz.us host uses the wiz.us auth URL",
          gql.wiz_auth_url("https://api.example-region.app.wiz.us/graphql") == _offering("wiz.us")["auth_url"])
    check("a wiz.io host uses the wiz.io auth URL",
          gql.wiz_auth_url("https://api.example-region.app.wiz.io/graphql") == _offering("wiz.io")["auth_url"])
    check("a wiz.us host selects the wiz adapter",
          gql._adapter_name("", "https://api.example-region.app.wiz.us/graphql") == "wiz")
    _check_wiz_offerings()

    calls = {"auth": 0, "query": 0}

    def refresh(request, timeout=None):
        if request.full_url.endswith("/oauth/token"):
            calls["auth"] += 1
            token = f"minted-{calls['auth']}"
            return Resp(200, json.dumps({"access_token": token}).encode())
        calls["query"] += 1
        if calls["query"] == 1:
            raise http_error(401, b'{"errors":[{"message":"unauthorized"}]}')
        return Resp(200, b'{"data":{"__typename":"Query"}}')

    again = client(
        refresh, token=None, client_id="client-id-zz", client_secret="client-secret-zz",
        auth_url="https://auth.example.test/oauth/token",
    ).execute("{ __typename }")
    check("HTTP 401 requests a new access token once and retries",
          calls["auth"] == 2 and calls["query"] == 2 and again["data"]["__typename"] == "Query",
          f"auth {calls['auth']} query {calls['query']}")

    def still(request, timeout=None):
        if request.full_url.endswith("/oauth/token"):
            return Resp(200, b'{"access_token":"minted-stays"}')
        raise http_error(401, b'{"errors":[{"message":"token expired"}]}')

    try:
        client(
            still, token=None, client_id="client-id-zz", client_secret="client-secret-zz",
            auth_url="https://auth.example.test/oauth/token",
        ).execute("{ __typename }")
        check("a repeated expiry is token expired", False, "no error")
    except TokenExpired as exc:
        check("a repeated expiry is token expired", str(exc) == "token expired", safe(exc))


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
        document = body.get("query") or ""
        if "__schema" in document:
            return Resp(200, json.dumps(introspection_payload()).encode())
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
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.io/graphql",
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
    sent = calls[0]["query"]["where"]["id"]["EQUALS"]
    check("id lookup sends id EQUALS as a list and asks for properties",
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
        check("HTTP 401 keeps the API reason", False, "no error")
    except GraphQLCallError as exc:
        check("HTTP 401 keeps the API reason",
              "HTTP 401" in str(exc) and "token expired" not in str(exc).lower()
              and TOKEN not in str(exc) and "[redacted]" in str(exc), safe(exc))
    except TokenExpired as exc:
        check("HTTP 401 keeps the API reason", False, safe(exc))
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
            "id": "wiz", "endpoint": "https://api.example-region.app.wiz.io/graphql",
            "adapter": "wiz", "token": TOKEN, "client_secret": "client-secret-zz",
            "token_env": "GRAPHQL_TOKEN",
        },
    })
    blob = json.dumps(norm)
    check("a token in the config is dropped and the endpoint stays",
          TOKEN not in blob and "token" not in norm["graphql"][0]
          and "client_secret" not in norm["graphql"][0] and "client-secret-zz" not in blob
          and norm["graphql"][0]["endpoint"] == "https://api.example-region.app.wiz.io/graphql"
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
        {"id": "wiz", "endpoint": "https://api.example-region.app.wiz.io/graphql"},
        {"id": "wiz", "endpoint": "https://api.example-region-2.app.wiz.io/graphql"},
    ]})
    check("neo4j and a duplicate id are skipped",
          [item["id"] for item in skipped] == ["wiz"]
          and skipped[0]["endpoint"].endswith("api.example-region.app.wiz.io/graphql"),
          safe(skipped))
    os.environ["GRAPHQL_ENDPOINT"] = "https://api.example-region.app.wiz.io/graphql"
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
                "id": "wiz", "label": "Wiz", "endpoint": "https://api.example-region.app.wiz.io/graphql",
                "adapter": "wiz", "token": TOKEN, "token_env": "GRAPHQL_TOKEN", "project_id": "*",
            },
        }))
        loaded = server.ensure_config()
        disk = path.read_text()
        check("startup removes a token from the config file",
              TOKEN not in disk and TOKEN not in json.dumps(loaded)
              and loaded["graphql"][0]["endpoint"].startswith("https://api.example-region.app.wiz.io/"),
              safe(disk))
        saved = server.save_config({
            "nodes": 80, "edges": 90, "expand_limit": 10, "cypher_limit": 20, "reason": "edited",
        })
        disk = path.read_text()
        check("saving the budget keeps the GraphQL source and omits the token",
              saved["nodes"] == 80 and TOKEN not in disk and "api.example-region.app.wiz.io" in disk
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
         "properties": {"name": "alpha", "region": "example-region", "note": "full"}},
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
        if text == "expiry-check":
            return 401, {"errors": [{"message": "token expired"}]}
        if text == "slow-check":
            return 429, {"errors": [{"message": TOKEN}]}
        if "relationships" in query:
            if variables.get("after") == "p2":
                return 200, graph_page([entities[2]], False, None, 2)
            return 200, graph_page([entities[1]], True, "p2", 2)
        if "id" in where or "_vertexID" in where:
            spec = where.get("id") or where.get("_vertexID") or {}
            wanted = set(spec.get("EQUALS") or [])
            found = [item for item in entities if item["id"] in wanted]
            return 200, graph_paths(found, False, None, len(found))
        found = [item for item in entities if not text or text.lower() in item["name"].lower()]
        return 200, graph_paths(found, False, None, len(found))

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
        if key.startswith("GRAPHQL_") or key == "GX_QUERIES":
            env.pop(key, None)
    env.update({
        "NEO4J_URI": uri, "NEO4J_USER": user, "NEO4J_PASSWORD": password,
        "NEO4J_DATABASE": os.environ.get("NEO4J_DATABASE") or "neo4j",
        "HOST": "127.0.0.1", "PORT": str(port), "QUIET": "1",
        "GX_CONFIG": str(Path(cfg_dir.name) / "gx.local.json"),
        "GX_CACHE_DIR": str(Path(cfg_dir.name) / "cache"),
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
        code, raw, neo_queries = http_json("GET", base + "/api/queries")
        neo_names = [item.get("name") for item in neo_queries] if isinstance(neo_queries, list) else []
        check("Neo4j queries stay Cypher",
              code == 200 and "Publicly readable S3 buckets" in neo_names
              and all(isinstance(item, dict) and "cypher" in item for item in neo_queries)
              and "graphSearch" not in raw and TOKEN not in raw,
              safe(neo_names))
        code, raw, denied_gql = http_json("POST", base + "/api/graphql", {"document": "query { __typename }", "variables": {}})
        check("GraphQL queries are refused on Neo4j",
              code == 400 and "no GraphQL" in (denied_gql.get("error") or "") and TOKEN not in raw,
              safe(denied_gql))
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
        code, raw, library = http_json("GET", base + "/api/queries")
        lib_names = [item.get("name") for item in library] if isinstance(library, list) else []
        check("Wiz queries are the two graph searches",
              code == 200 and lib_names == [
                  "Internet-exposed EC2 instances", "Security groups open to 0.0.0.0/0"]
              and all("graphSearch" in (item.get("graphql") or "") and "cypher" not in item for item in library)
              and "MATCH" not in raw and TOKEN not in raw,
              safe(lib_names))
        first = library[0] if isinstance(library, list) and library else {}
        code, raw, placed = http_json("POST", base + "/api/graphql", {
            "document": first.get("graphql") or "",
            "variables": first.get("variables") or {},
        })
        placed_ids = [node.get("id") for node in placed.get("nodes") or []]
        check("a Wiz query places the mock entities",
              code == 200 and placed_ids == ["alpha-1", "beta-2", "gamma-3"]
              and placed.get("edges") == [] and TOKEN not in raw
              and "edges_unknown" not in (placed.get("truncation") or {}).get("reasons", []),
              safe({"ids": placed_ids, "error": placed.get("error"), "truncation": placed.get("truncation")}))
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
        code, raw, diag = http_json("GET", base + "/api/diagnostics?source=query")
        logged = "\n".join(item.get("message") or "" for item in (diag.get("entries") or []) if isinstance(item, dict))
        check("diagnostics keeps the search document and not the token",
              code == 200 and "alpha" in logged and "graphSearch" in logged
              and "fragment TypeRef" not in logged and TOKEN not in raw,
              safe(logged[-500:]))
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
              code == 200 and props.get("region") == "example-region" and props.get("note") == "full"
              and details["nodes"][0]["partial"] is False and TOKEN not in raw,
              safe(props))
        code, raw, denied = http_json("POST", base + "/api/cypher", {"query": "MATCH (n) RETURN n"})
        check("Cypher on the GraphQL source is refused",
              code == 400 and "no Cypher" in (denied.get("error") or "") and TOKEN not in raw,
              safe(denied))
        code, raw, expired = http_json("GET", base + "/api/search?type=node&limit=5&q=expired-check")
        check("HTTP 401 from the API keeps the reason",
              code == 502 and "HTTP 401" in (expired.get("error") or "")
              and expired.get("error") != "token expired" and TOKEN not in raw, safe(raw))
        code, raw, expiry = http_json("GET", base + "/api/search?type=node&limit=5&q=expiry-check")
        check("a token-expired body stays token expired",
              code == 401 and expiry.get("error") == "token expired" and TOKEN not in raw, safe(raw))
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


def _start_explorer(env_extra):
    """Start server.py on a free port with a temporary config folder. Returns proc, base, cleanup."""
    cfg_dir = tempfile.TemporaryDirectory()
    env_file = tempfile.NamedTemporaryFile(prefix="gx-env-", delete=False)
    env_file.close()
    port = free_port()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GRAPHQL_") or key in ("GX_FRESH", "GX_QUERIES"):
            env.pop(key, None)
    env.update({
        "HOST": "127.0.0.1", "PORT": str(port), "QUIET": "1",
        "GX_CONFIG": str(Path(cfg_dir.name) / "gx.local.json"),
        "GX_CACHE_DIR": str(Path(cfg_dir.name) / "cache"),
        "GX_ENV": env_file.name,
    })
    env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"

    def cleanup():
        if proc.poll() is None:
            proc.terminate()
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate(timeout=5)
        cfg_dir.cleanup()
        try:
            os.remove(env_file.name)
        except OSError:
            pass
        return (err or b"").decode(errors="replace")

    for _ in range(80):
        if proc.poll() is not None:
            break
        try:
            code, _, _ = http_json("GET", base + "/api/status")
            if code == 200:
                return proc, base, cleanup, Path(cfg_dir.name)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    return None, base, cleanup, Path(cfg_dir.name)


def test_query_routes():
    """Saved-query routes: create, edit, delete, built-ins, import, export, and refusals."""
    print("saved-query routes")
    mock, mock_port = start_mock()
    proc, base, cleanup, folder = _start_explorer({
        "NEO4J_URI": "bolt://127.0.0.1:7999", "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": "routes-test",
        "NEO4J_DATABASE": "neo4j",
        "GRAPHQL_ENDPOINT": f"http://127.0.0.1:{mock_port}/graphql",
        "GRAPHQL_TOKEN": TOKEN, "GRAPHQL_ADAPTER": "wiz", "GRAPHQL_LABEL": "Wiz", "GRAPHQL_ID": "wiz",
    })
    store = folder / "gx.queries.json"
    try:
        if proc is None:
            check("explorer starts for the saved-query routes", False)
            return
        code, raw, neo = http_json("GET", base + "/api/queries")
        check("Neo4j lists its built-ins with an id and the flags",
              code == 200 and neo and all(item.get("builtin") is True and item.get("editable") is False
                                          and str(item.get("id")).startswith("builtin:cartography-queries:")
                                          and item.get("kind") == "cypher" for item in neo),
              safe(raw[:300]))
        cypher = "MATCH (a:AWSAccount)-[r:RESOURCE]->(b:S3Bucket) RETURN a, r, b LIMIT 5"
        code, raw, made = http_json("POST", base + "/api/queries", {
            "name": "My buckets", "description": "Buckets per account", "tags": ["aws", "s3"], "cypher": cypher,
        })
        mine = made.get("query") or {}
        check("POST creates a Cypher query on Neo4j",
              code == 200 and mine.get("name") == "My buckets" and mine.get("editable") is True
              and mine.get("id", "").startswith("q_")
              and (made.get("queries") or [{}])[-1].get("id") == mine.get("id"),
              safe(raw[:300]))
        check("the store file is next to gx.local.json with mode 0600",
              store.is_file() and (store.stat().st_mode & 0o777) == 0o600,
              oct(store.stat().st_mode & 0o777) if store.is_file() else "missing")
        code, raw, refused_gql = http_json("POST", base + "/api/queries", {"name": "g", "graphql": "query { __typename }"})
        check("a GraphQL query is refused on Neo4j", code == 400 and "GraphQL" in (refused_gql.get("error") or ""), safe(raw))
        code, raw, write = http_json("POST", base + "/api/queries", {"name": "w", "cypher": "MATCH (n) DETACH DELETE n"})
        code_run, _raw, run = http_json("POST", base + "/api/cypher", {"query": "MATCH (n) DETACH DELETE n"})
        check("a Cypher write is refused at save time with the run-time message",
              code == 403 and code_run == 403 and write.get("error") == run.get("error")
              and "not allowed" in (write.get("error") or ""),
              safe({"save": write, "run": run}))
        code, raw, checked = http_json("POST", base + "/api/queries/validate", {"cypher": "MATCH (n) SET n.x = 1"})
        code2, raw2, fine = http_json("POST", base + "/api/queries/validate", {"cypher": cypher})
        check("validate checks a body without saving it",
              code == 403 and "not allowed" in (checked.get("error") or "") and code2 == 200 and fine.get("ok") is True,
              safe({"bad": checked, "good": fine}))
        code, raw, big = http_json("POST", base + "/api/queries", {"name": "b", "cypher": "RETURN 1 //" + "x" * (101 * 1024)})
        check("an oversized body is refused", code == 400 and "100 KB" in (big.get("error") or ""), safe(raw[:200]))
        if not (isinstance(neo, list) and len(neo) > 1 and "id" in neo[0]):
            check("the saved-query routes can continue", False, safe(raw[:200]))
            return
        builtin_id = neo[0]["id"]
        code, raw, edited = http_json("PUT", base + "/api/queries/" + urllib.request.quote(builtin_id, safe=""),
                                      {"name": "My EC2 copy"})
        copy = edited.get("query") or {}
        listed_ids = [item["id"] for item in edited.get("queries") or []]
        check("PUT on a built-in saves a copy and hides the built-in",
              code == 200 and edited.get("hid") == builtin_id and copy.get("id") != builtin_id
              and copy.get("cypher") == neo[0]["cypher"] and builtin_id not in listed_ids and copy.get("id") in listed_ids,
              safe(raw[:400]))
        code, raw, changed = http_json("PUT", base + "/api/queries/" + mine["id"], {"tags": ["s3"]})
        check("PUT on a user query updates it in place",
              code == 200 and changed["query"]["id"] == mine["id"] and changed["query"]["tags"] == ["s3"]
              and changed["query"]["name"] == "My buckets" and changed.get("hid") is None, safe(raw[:300]))
        code, raw, missing = http_json("PUT", base + "/api/queries/q_0000000000000000", {"name": "x"})
        check("PUT on an unknown id is 404", code == 404, safe(raw))
        second_id = neo[1]["id"]
        code, raw, hidden = http_json("DELETE", base + "/api/queries/" + urllib.request.quote(second_id, safe=""))
        check("DELETE on a built-in hides it",
              code == 200 and hidden.get("builtin") is True
              and second_id not in [item["id"] for item in hidden.get("queries") or []], safe(raw[:300]))
        code, raw, restored = http_json("POST", base + "/api/queries/restore-builtins", {})
        ids = [item["id"] for item in restored.get("queries") or []]
        check("restore-builtins brings the hidden built-ins back",
              code == 200 and restored.get("restored") == 2 and builtin_id in ids and second_id in ids, safe(raw[:300]))
        code, raw, exported = http_json("GET", base + "/api/queries/export")
        check("export is the array of user queries",
              code == 200 and isinstance(exported, list)
              and sorted(item["name"] for item in exported) == ["My EC2 copy", "My buckets"]
              and all("builtin" not in item for item in exported), safe(raw[:300]))
        code, raw, switched = http_json("POST", base + "/api/source", {"id": "wiz"})
        code, raw, wiz = http_json("GET", base + "/api/queries")
        check("the Wiz connection does not see the Neo4j queries",
              code == 200 and [item["id"] for item in wiz] == ["builtin:wiz-queries:internet-exposed-ec2", "builtin:wiz-queries:open-security-groups"]
              and all(item["kind"] == "graphql" for item in wiz), safe(raw[:300]))
        code, raw, wrong = http_json("POST", base + "/api/queries", {"name": "c", "kind": "cypher", "cypher": cypher})
        check("a Cypher query is refused on the Wiz connection", code == 400 and "Cypher" in (wrong.get("error") or ""), safe(raw))
        code, raw, mutation = http_json("POST", base + "/api/queries", {"name": "m", "graphql": "mutation { deleteAll { id } }"})
        check("a mutation is refused at save time",
              code == 400 and "not sent" in (mutation.get("error") or "") and TOKEN not in raw, safe(raw))
        document = wiz[1]["graphql"]
        code, raw, bad_vars = http_json("POST", base + "/api/queries", {"name": "v", "graphql": document, "variables": "{nope"})
        check("bad JSON variables are refused", code == 400 and "JSON object" in (bad_vars.get("error") or ""), safe(raw))
        code, raw, imported = http_json("POST", base + "/api/queries/import", [
            {"name": "Open groups", "tags": ["aws"], "graphql": document, "variables": {"projectId": "*", "first": 50}},
            {"name": "bad", "graphql": "mutation { x { id } }"},
        ])
        check("import adds the valid entries and reports the skipped ones",
              code == 200 and imported.get("added") == 1 and len(imported.get("skipped") or []) == 1
              and imported["skipped"][0]["index"] == 1 and "not sent" in imported["skipped"][0]["reason"],
              safe(raw[:400]))
        code, raw, wiz_export = http_json("GET", base + "/api/queries/export")
        check("the Wiz export has only the Wiz query",
              code == 200 and [item["name"] for item in wiz_export] == ["Open groups"]
              and wiz_export[0]["variables"] == {"projectId": "*", "first": 50}, safe(raw[:300]))
        code, raw, back = http_json("POST", base + "/api/source", {"id": "neo4j"})
        code, raw, neo_again = http_json("GET", base + "/api/queries")
        check("switching back shows the Neo4j user queries again",
              "My buckets" in [item["name"] for item in neo_again] and "Open groups" not in raw, safe(raw[:300]))
        code, raw, deleted = http_json("DELETE", base + "/api/queries/" + mine["id"])
        check("DELETE removes a user query and returns it",
              code == 200 and deleted.get("builtin") is False and deleted["query"]["id"] == mine["id"]
              and mine["id"] not in [item["id"] for item in deleted.get("queries") or []], safe(raw[:300]))
        on_disk = store.read_text()
        check("the store holds no credential", TOKEN not in on_disk and "routes-test" not in on_disk)
        _check_route_guards(base, store)
    finally:
        log = cleanup()
        check("the saved-query server log omits the token", TOKEN not in log, safe(log[-300:]))
        mock.shutdown()
        mock.server_close()


def _raw_http(port, request_bytes, timeout=10):
    """Send raw bytes to the explorer and return the status code and the body text."""
    import socket as _socket
    sock = _socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        sock.sendall(request_bytes)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (TimeoutError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    raw = b"".join(chunks).decode("utf-8", errors="replace")
    head, _, body = raw.partition("\r\n\r\n")
    try:
        code = int(head.split(" ", 2)[1])
    except (IndexError, ValueError):
        code = 0
    return code, body


def _check_route_guards(base, store):
    """Cross-site writes, a negative Content-Length, NaN variables, and syntax checks."""
    port = int(base.rsplit(":", 1)[1])
    planted = json.dumps({"name": "planted", "cypher": "MATCH (n) RETURN n LIMIT 1"}).encode()
    before = store.read_text() if store.is_file() else ""

    def post(path, body, headers):
        req = urllib.request.Request(base + path, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code

    text_plain = post("/api/queries", planted, {"Content-Type": "text/plain"})
    other_site = post("/api/queries", planted, {"Content-Type": "application/json", "Origin": "https://evil.example"})
    restore = post("/api/queries/restore-builtins", b"{}", {"Content-Type": "text/plain"})
    refresh = post("/api/cache/refresh", b'{"what":"all"}', {"Content-Type": "text/plain;charset=UTF-8"})
    after = store.read_text() if store.is_file() else ""
    check("a cross-site text/plain or foreign-Origin POST is refused and writes nothing",
          text_plain == 415 and other_site == 403 and restore == 415 and refresh == 415
          and before == after and "planted" not in after,
          {"text_plain": text_plain, "origin": other_site, "restore": restore, "refresh": refresh})
    own = post("/api/queries/validate", json.dumps({"cypher": "MATCH (n) RETURN n"}).encode(),
               {"Content-Type": "application/json", "Origin": base})
    check("a POST from the page's own origin still works", own == 200, own)
    rebound = urllib.request.Request(base + "/api/queries/export", headers={"Host": "attacker.example:" + str(port)})
    try:
        with urllib.request.urlopen(rebound, timeout=20) as response:
            host_code = response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        host_code = exc.code
    check("a GET with a rebound Host name is refused", host_code == 403, host_code)
    big = json.dumps([{"name": f"q{i}", "cypher": "RETURN 1 // " + "x" * 300} for i in range(200)]).encode()
    code, body = _raw_http(port, b"POST /api/queries/import HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                 b"Content-Type: application/json\r\nContent-Length: -1\r\nConnection: close\r\n\r\n" + big)
    check("a negative Content-Length is refused before the body is read",
          code == 400 and "Content-Length" in body, (code, body[:200]))
    code, raw, nan = http_json("POST", base + "/api/source", {"id": "wiz"})
    code, raw, bad = http_json("POST", base + "/api/queries", {"name": "nan", "graphql": "query { a }",
                                                              "variables": '{"first": NaN}'})
    code2, raw2, listed = http_json("GET", base + "/api/queries")
    check("NaN in the variables text is refused, and the list stays valid JSON",
          code == 400 and "JSON" in (bad.get("error") or "") and code2 == 200 and "NaN" not in raw2,
          safe({"save": raw[:200], "list": raw2[:200]}))
    code, raw, garbage = http_json("POST", base + "/api/queries/validate", {"graphql": "not graphql at all {{{"})
    code_ok, raw_ok, fine = http_json("POST", base + "/api/queries/validate", {"graphql": "query { a { b } }"})
    check("Validate refuses a GraphQL document that is not well formed, and its note does not overclaim",
          code == 400 and "not valid" in (garbage.get("error") or "")
          and code_ok == 200 and "Preview" in (fine.get("note") or ""), safe({"bad": raw, "good": raw_ok}))
    code, raw, ran = http_json("POST", base + "/api/graphql", {"document": "query { a ", "variables": {}})
    check("running a document that is not well formed is an error, not an empty graph",
          code == 400 and "not valid" in (ran.get("error") or ""), safe(raw[:200]))
    http_json("POST", base + "/api/source", {"id": "neo4j"})
    code, raw, neo_note = http_json("POST", base + "/api/queries/validate", {"cypher": "MATCH (n RETURN n"})
    check("Cypher Validate says the syntax was not checked when Neo4j is not reachable",
          code == 200 and "not checked" in (neo_note.get("note") or "") and "passes" not in raw, safe(raw[:200]))


def _hide_fixture(text):
    shown = safe(text)
    for secret in ("tinkerlab-graphql", "tinkerlab-access"):
        shown = shown.replace(secret, "[redacted]")
    return shown


def test_query_log():
    """Diagnostics keeps the GraphQL document and the outcome, not the token or entity properties."""
    print("query log")
    import diagnostics
    def opener(request, timeout=None):
        document = (json.loads(request.data.decode()).get("query") or "")
        if "__schema" in document:
            return Resp(200, json.dumps(introspection_payload()).encode())
        if "Bad" in document:
            return Resp(200, b'{"errors":[{"message":"invalid type for variable: \'query\'"}]}')
        return Resp(200, json.dumps(graph_page(
            [{"id": "a", "name": "alpha", "type": "Widget", "properties": {"secret": "do-not-log-props"}}],
            False, None, 4)).encode())

    src = GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": "https://api.example.test/graphql",
        "adapter": "wiz", "token_env": "GRAPHQL_TOKEN",
    }, client=client(opener, result_ttl=0, schema_ttl=0))
    src.client.execute(
        "query Example($projectId: String!, $quick: Boolean) {\n"
        "  graphSearch(projectId: $projectId, quick: $quick, query: { select: true }) {\n"
        "    totalCount\n"
        "    nodes { entities { id name type properties } }\n"
        "  }\n"
        "}\n",
        {"projectId": "*", "quick": True, "token": TOKEN},
        cache_ttl=0,
    )
    rows = [row["message"] for row in diagnostics.snapshot("query") if "Example" in row["message"]]
    message = rows[-1] if rows else ""
    check("a sent query keeps the document, the variables, and the counts",
          "GraphQL Example" in message and "\n" in message
          and '"projectId": "*"' in message and '"quick": true' in message
          and "select: true" in message and "totalCount 4" in message and "1 entities" in message
          and "do-not-log-props" not in message and TOKEN not in message and "Bearer" not in message,
          safe(message)[:800])
    src.client.execute(INTROSPECTION, {}, cache_ttl=0)
    intro = [row["message"] for row in diagnostics.snapshot("query") if "introspection" in row["message"]]
    check("introspection stays a short line",
          bool(intro) and all("fragment TypeRef" not in item and "\n" not in item for item in intro),
          safe(intro[-1] if intro else ""))
    try:
        src.client.execute(
            "query Bad($query: String) {\n  graphSearch(query: $query) { totalCount }\n}\n",
            {"query": {"select": True}},
            cache_ttl=0,
        )
        check("a rejected query keeps the document and the error", False, "no error")
    except GraphQLCallError:
        failed = [row["message"] for row in diagnostics.snapshot("query") if "Bad" in row["message"]]
        text = failed[-1] if failed else ""
        check("a rejected query keeps the document and the error",
              "invalid type for variable" in text and "$query" in text and "\n" in text and TOKEN not in text,
              safe(text)[:500])


def test_log_filter():
    """Passwords, client secrets, and bearer tokens stay out of the log."""
    print("log filter")
    import diagnostics
    secret = "log-secret-zz-9f3a"
    token = "log-token-zz-9f3a"
    previous = os.environ.get("GRAPHQL_TOKEN")
    os.environ["GRAPHQL_TOKEN"] = token
    try:
        diagnostics.note_secret(secret)
        diagnostics.record("explorer", "token expired")
        diagnostics.record("explorer", "Data connection saved.")
        diagnostics.record("server", f"client id stays visible: gx-client-zz password={secret}")
        diagnostics.record("explorer", f'Authorization: Bearer {token} GRAPHQL_TOKEN={token}')
        diagnostics.record("explorer", '{"client_secret": "%s", "access_token": "%s"}' % (secret, token))
        diagnostics.record("server", "bolt://reader:%s@127.0.0.1:7687" % secret)
        rows = diagnostics.snapshot()
        blob = "\n".join(row["message"] for row in rows)
        leaked = [item for item in (secret, token) if item in blob]
        check("the log keeps ordinary words and removes credential values",
              not leaked
              and "token expired" in blob
              and "Data connection saved." in blob
              and "gx-client-zz" in blob
              and "password=[redacted]" in blob
              and "Bearer [redacted]" in blob
              and "bolt://reader:[redacted]@127.0.0.1:7687" in blob,
              safe(blob[-500:]).replace(secret, "[redacted]").replace(token, "[redacted]"))
    finally:
        if previous is None:
            os.environ.pop("GRAPHQL_TOKEN", None)
        else:
            os.environ["GRAPHQL_TOKEN"] = previous


def test_docker_fixture():
    """Search and expand against the gx-graphql container. Skip when Docker is down."""
    print("docker fixture")
    import run_local
    if not run_local.docker_daemon():
        print("  skip docker GraphQL fixture (Docker is not running)")
        return
    try:
        endpoint = run_local.start_graphql_fixture()
    except RuntimeError as exc:
        check("gx-graphql fixture starts", False, _hide_fixture(exc)[:300])
        return
    check("gx-graphql fixture starts", endpoint.endswith("/graphql"))
    client = GraphQLClient(
        endpoint, token=None, client_id=run_local.GQL_CLIENT_ID,
        client_secret=run_local.GQL_CLIENT_SECRET, auth_url=run_local.graphql_auth_url(),
        timeout=15, max_retries=1, backoff=0, result_ttl=0, schema_ttl=0)
    source = GraphQLSource({
        "id": "wiz", "label": "Wiz fixture", "endpoint": endpoint, "adapter": "wiz",
        "auth_url": run_local.graphql_auth_url(), "client_id": run_local.GQL_CLIENT_ID,
    }, client=client)
    status = source.status()
    blob = json.dumps(status)
    check("fixture status is connected with a minted token",
          status.get("connected") is True and status.get("adapter") == "wiz"
          and "tinkerlab-graphql" not in blob and "tinkerlab-access" not in blob,
          _hide_fixture(blob)[:300])
    schema = source.schema()
    categories = schema.get("categories") or {}
    owner = ((categories.get("Widget") or {}).get("props") or {}).get("owner")
    check("fixture data model comes from introspection",
          "Widget" in categories and owner == "-> Widget",
          _hide_fixture(sorted(categories)))
    found = source.search(q="alpha", limit=5)
    nodes = found.get("nodes") or []
    check("fixture search returns alpha",
          bool(nodes) and nodes[0]["id"] == "alpha-1" and nodes[0]["partial"] is True,
          _hide_fixture(nodes[:1]))
    first = source.expand(["alpha-1"], [], limit=1)
    second = source.expand(["alpha-1"], ["alpha-1", "beta-2"], limit=1)
    check("fixture expand pages neighbors",
          [node["id"] for node in first.get("nodes") or []] == ["beta-2"]
          and first.get("more") is True
          and first["edges"][0]["type"] == "related"
          and first["edges"][0]["id"] == "alpha-1:related:beta-2"
          and [node["id"] for node in second.get("nodes") or []] == ["gamma-3"],
          _hide_fixture({"first": first.get("nodes"), "second": second.get("nodes"), "more": first.get("more")}))
    details = source.nodes(["alpha-1"])
    props = ((details.get("nodes") or [{}])[0]).get("props") or {}
    check("fixture node load returns properties",
          props.get("region") == "example-region" and props.get("note") == "full"
          and details["nodes"][0]["partial"] is False,
          _hide_fixture(props))
    refused = False
    try:
        source.cypher("MATCH (n) RETURN n")
    except ValueError as exc:
        refused = "no Cypher" in str(exc)
    check("fixture source has no Cypher", refused)


def main():
    saved = {key: os.environ.get(key) for key in list(os.environ) if key.startswith("GRAPHQL_") or key in ("GX_ENV", "GX_CONFIG", "GX_QUERIES")}
    for key in list(saved):
        os.environ.pop(key, None)
    empty = tempfile.NamedTemporaryFile(prefix="gx-env-", delete=False)
    empty.close()
    os.environ["GX_ENV"] = empty.name
    # Library reads in this process use an empty temporary store, never the project's
    # gx.queries.json. A child server that drops GX_QUERIES keeps its store next to its GX_CONFIG.
    store_dir = tempfile.TemporaryDirectory()
    os.environ["GX_QUERIES"] = str(Path(store_dir.name) / "gx.queries.json")
    try:
        test_search_document()
        test_search_follows_the_tenant_schema()
        test_live_tenant_contract()
        test_saved_query_edges()
        test_query_value_parser()
        test_shape_probe()
        test_probe_review()
        test_query_timeout()
        test_tenant_page_flow()
        test_wiz_library_and_node_total()
        test_wiz_auth()
        test_mutations()
        test_introspection_model()
        test_paging_and_wiz_shape()
        test_auth_and_rate_limit()
        test_cache_and_inflight()
        test_config_and_generic()
        test_source_switch()
        test_query_routes()
        test_query_log()
        test_log_filter()
        test_docker_fixture()
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
        store_dir.cleanup()
    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print("\nall graphql checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
