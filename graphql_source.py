"""A GraphQL data source beside Neo4j.

The explorer shows one source at a time. This module never writes the bearer
token, the client secret, or the auth request to disk, to a log line, or to a
response. Data connection in the page may store them in the gitignored .env
file. Wiz exchanges the client id and client secret for an access token.
The GraphQL call sends that token as Authorization: Bearer and does not send
the client id. Mutations are refused before that request is built.

Wiz is the first adapter: graphSearch, entity id lookup, and cursor pages.
A source with no adapter still exposes the data model from
introspection and does not invent an instance query.
"""
from __future__ import annotations

import json
import math
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

import contextlib

import diagnostics
from schema_cache import DiskCache, Slot, cache_key, principal_hash

TOKEN_KEYS = {"token", "bearer", "authorization", "secret", "password", "access_token"}
ENV_KEYS = (
    "GRAPHQL_ENDPOINT", "GRAPHQL_TOKEN", "GRAPHQL_CLIENT_ID", "GRAPHQL_CLIENT_SECRET",
    "GRAPHQL_AUTH_URL", "GRAPHQL_ADAPTER", "GRAPHQL_LABEL", "GRAPHQL_ID",
    "GRAPHQL_TOKEN_ENV", "GRAPHQL_PROJECT_ID", "GRAPHQL_SCHEMA_TIMEOUT",
)
_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SKIP_NAMES = {"query", "mutation", "subscription", "fragment", "on", "true", "false", "null"}
MAX_PAGES = 8
_TOKEN_WARNED = False

_TYPE_REF = """
fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
              ofType { kind name }
            }
          }
        }
      }
    }
  }
}
"""

INTROSPECTION = """
query Introspection {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind
      name
      fields(includeDeprecated: true) {
        name
        args { name type { ...TypeRef } }
        type { ...TypeRef }
      }
      inputFields { name type { ...TypeRef } defaultValue }
      enumValues(includeDeprecated: true) { name }
      possibleTypes { name }
    }
  }
}
""" + _TYPE_REF

# The types a Wiz graphSearch walks, named as the tenant names them. The probe
# asks for these with __type instead of the 11 MB __schema. Any name the tenant
# answers with null, or a type the walk reaches that is not here, is fetched in
# a later round. Mutation names only feed the read-only guard.
SHAPE_TYPES = (
    "Query", "GraphEntityQueryInput", "GraphRelationshipQueryInput",
    "GraphDirectedRelationshipTypeInput", "GraphEntityType", "GraphRelationshipType",
    "GraphPropertyPredicate", "GraphSearchResultConnection", "CloudResourceV2Filters",
)
SHAPE_ROUNDS = 2
_SHAPE_FRAGMENT = """
fragment ShapeType on __Type {
  kind
  name
  fields(includeDeprecated: true) {
    name
    args { name type { ...TypeRef } defaultValue }
    type { ...TypeRef }
  }
  inputFields { name type { ...TypeRef } defaultValue }
  enumValues(includeDeprecated: true) { name }
}
"""
_NAME_RE = re.compile(r"[_A-Za-z][_0-9A-Za-z]{0,127}")


def shape_document(names, mutation=None, roots=True):
    """One __type query for these names. The mutation type selects field names only."""
    lines = ["query WizShape {"]
    if roots:
        lines.append("  __schema { queryType { name } mutationType { name } }")
    for index, name in enumerate(names):
        lines.append(f'  t{index}: __type(name: "{name}") {{ ...ShapeType }}')
    if mutation:
        lines.append(f'  m0: __type(name: "{mutation}") {{ kind name fields(includeDeprecated: true) {{ name }} }}')
    lines.append("}")
    document = "\n".join(lines) + "\n"
    # A server refuses a fragment that no selection spreads. The m0 selection is inline.
    if names:
        document += _SHAPE_FRAGMENT + _TYPE_REF
    return document


SHAPE_QUERY = shape_document(SHAPE_TYPES, "Mutation")

# Legacy documents for an introspection result with no graphSearch arguments
# (the gx-graphql fixture). The fixture requires GraphEntityQueryInput!. A
# tenant schema uses graph_search_request. $after stays nullable because the
# first page omits it.
SEARCH_QUERY = """
query GraphSearch($query: GraphEntityQueryInput!, $projectId: String!, $first: Int, $after: String) {
  graphSearch(query: $query, projectId: $projectId, first: $first, after: $after) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes { entities { id name type } }
  }
}
"""

DETAIL_QUERY = """
query GraphSearch($query: GraphEntityQueryInput!, $projectId: String!, $first: Int, $after: String) {
  graphSearch(query: $query, projectId: $projectId, first: $first, after: $after) {
    totalCount
    pageInfo { hasNextPage endCursor }
    nodes { entities { id name type properties } }
  }
}
"""

_TEXT_OPS = ("CONTAINS", "contains", "CONTAINS_ANY", "STARTS_WITH", "EQUALS", "equals")
ANY_TYPE = "ANY"
# Property keys the live tenant answers on. where is an opaque scalar there, so
# introspection cannot list its keys. The page offers these and accepts others.
WIZ_COMMON_KEYS = ("_vertexID", "accessibleFrom.internet", "cloudPlatform", "externalId",
                   "name", "nativeType", "updatedAt")
_PROP_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}")
# A text operator on type ANY with no anchor times out on the tenant.
_SLOW_OPS = ("CONTAINS", "CONTAINS_ANY", "STARTS_WITH", "ENDS_WITH")


def _schema_types(payload):
    schema = ((payload or {}).get("data") or {}).get("__schema")
    if not isinstance(schema, dict):
        return {}, {}
    types = {}
    for item in schema.get("types") or []:
        if isinstance(item, dict) and item.get("name"):
            types[item["name"]] = item
    return schema, types


def _print_type(type_ref):
    if not isinstance(type_ref, dict):
        return ""
    kind = type_ref.get("kind")
    if kind == "NON_NULL":
        inner = _print_type(type_ref.get("ofType"))
        return f"{inner}!" if inner else ""
    if kind == "LIST":
        inner = _print_type(type_ref.get("ofType"))
        return f"[{inner}]" if inner else ""
    return str(type_ref.get("name") or "")


def _graph_search_field(payload):
    schema, types = _schema_types(payload)
    query_name = (schema.get("queryType") or {}).get("name")
    for field in (types.get(query_name) or {}).get("fields") or []:
        if isinstance(field, dict) and field.get("name") == "graphSearch" and field.get("args"):
            return field
    return None


def _field_arg(field, name):
    for arg in (field or {}).get("args") or []:
        if isinstance(arg, dict) and arg.get("name") == name:
            return arg
    return None


def _input_fields(types, type_ref):
    node = type_ref if isinstance(type_ref, dict) else {}
    while node.get("kind") in ("NON_NULL", "LIST"):
        node = node.get("ofType") or {}
    named = types.get(node.get("name") or "") or {}
    if named.get("kind") != "INPUT_OBJECT":
        return {}
    return {
        item["name"]: item for item in named.get("inputFields") or []
        if isinstance(item, dict) and item.get("name")
    }


def _enum_names(types, type_ref):
    node = type_ref if isinstance(type_ref, dict) else {}
    while node.get("kind") in ("NON_NULL", "LIST"):
        node = node.get("ofType") or {}
    named = types.get(node.get("name") or "") or {}
    if named.get("kind") != "ENUM":
        return []
    return [item["name"] for item in named.get("enumValues") or [] if isinstance(item, dict) and item.get("name")]


def _is_list_type(type_ref):
    node = type_ref if isinstance(type_ref, dict) else {}
    while node.get("kind") == "NON_NULL":
        node = node.get("ofType") or {}
    return node.get("kind") == "LIST"


def _named(types, type_ref):
    """The named type behind NON_NULL and LIST wrappers, or {}."""
    node = type_ref if isinstance(type_ref, dict) else {}
    while node.get("kind") in ("NON_NULL", "LIST"):
        node = node.get("ofType") or {}
    name = node.get("name") or ""
    if not name:
        return {}
    return types.get(name) or {"kind": node.get("kind"), "name": name}


def _is_required(spec):
    """A field is required when it is NON_NULL and has no default value."""
    type_ref = (spec or {}).get("type") if isinstance(spec, dict) else None
    return isinstance(type_ref, dict) and type_ref.get("kind") == "NON_NULL" and spec.get("defaultValue") is None


def _query_field(payload, name):
    """A root Query field by name. Its arguments are not checked."""
    schema, types = _schema_types(payload)
    query_name = (schema.get("queryType") or {}).get("name") if isinstance(schema, dict) else None
    for item in (types.get(query_name) or {}).get("fields") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _object_field_names(types, type_ref):
    node = type_ref if isinstance(type_ref, dict) else {}
    while isinstance(node, dict) and node.get("kind") in ("NON_NULL", "LIST"):
        node = node.get("ofType") or {}
    named = types.get((node or {}).get("name") or "") or {}
    return {
        item.get("name") for item in named.get("fields") or []
        if isinstance(item, dict) and item.get("name")
    }


class WizShape:
    """What one introspection result lets a Wiz graphSearch send.

    legacy means introspection lists no graphSearch query argument (the
    gx-graphql fixture). open means where is a scalar, so Wiz does not check
    its keys. schema means where is an input object with named fields. Every
    request builder asks this object, so the branching is written once.
    """

    def __init__(self, payload):
        _, types = _schema_types(payload)
        self.types = types
        self.field = _graph_search_field(payload)
        self.query_arg = _field_arg(self.field, "query") if self.field else None
        query_ref = (self.query_arg or {}).get("type")
        self.query_type = _print_type(query_ref) if query_ref else ""
        self.legacy = not self.query_type
        self.query_fields = {} if self.legacy else _input_fields(types, query_ref)
        type_spec = self.query_fields.get("type") or {}
        self.entity_types = set(_enum_names(types, type_spec.get("type")))
        self.type_required = bool(type_spec) and _is_required(type_spec)
        where_ref = (self.query_fields.get("where") or {}).get("type")
        self.where_kind = _named(types, where_ref).get("kind") or ""
        self.where_fields = _input_fields(types, where_ref) if self.where_kind == "INPUT_OBJECT" else {}
        rel_spec = self.query_fields.get("relationships") or {}
        self.rel_fields = _input_fields(types, rel_spec.get("type")) if rel_spec else {}
        rel_type = self.rel_fields.get("type") or {}
        self.rel_type_ref = rel_type.get("type")
        self.rel_type_required = bool(rel_type) and _is_required(rel_type)
        directed = _input_fields(types, self.rel_type_ref) if self.rel_type_ref else {}
        self.rel_is_directed = (
            _named(types, self.rel_type_ref).get("kind") == "INPUT_OBJECT" and "type" in directed)
        if self.rel_is_directed:
            self.rel_enum = set(_enum_names(types, directed["type"].get("type")))
        else:
            self.rel_enum = set(_enum_names(types, self.rel_type_ref))
        self.text_def = None
        self.text_filter_arg = None
        self.text_field = ""
        if not self.legacy:
            inventory = _query_field(payload, "cloudResourcesV2")
            filter_arg = _field_arg(inventory, "filterBy") if inventory else None
            if filter_arg and "search" in _input_fields(types, filter_arg.get("type")):
                self.text_def = inventory
                self.text_filter_arg = filter_arg
                self.text_field = "cloudResourcesV2"
        self.result_fields = _object_field_names(types, self.field.get("type")) if self.field else set()
        self.max_count = "maxCountReached" in self.result_fields

    @property
    def open_where(self):
        return not self.legacy and "where" in self.query_fields and self.where_kind != "INPUT_OBJECT"

    def entity_type(self, category=None):
        """The type list for one level. A category outside the enum is refused."""
        if self.legacy:
            return [category] if category else None
        if category:
            if self.entity_types and category not in self.entity_types:
                raise ValueError(f"{category} is not a Wiz entity type.")
            return [category]
        if ANY_TYPE in self.entity_types:
            return [ANY_TYPE]
        if self.type_required:
            raise ValueError("This graph search needs an entity type.")
        return None

    def id_key(self):
        """The where key that matches an entity id. Empty when there is none."""
        if self.legacy:
            return "id"
        if self.where_kind == "INPUT_OBJECT":
            if "id" in self.where_fields:
                return "id"
            if "_vertexID" in self.where_fields:
                return "_vertexID"
            return ""
        return "_vertexID" if self.open_where else ""

    def rel_type(self, names=None):
        """The relationship type value. With no names it is ANY when the enum has it."""
        names = [str(name) for name in names or [] if name and name != "related"]
        if not self.rel_is_directed:
            if not names or not self.rel_type_ref:
                return None
            return _relationship_type_value(self.types, self.rel_type_ref, names)
        for name in names:
            if self.rel_enum and name not in self.rel_enum:
                raise ValueError(f"{name} is not a Wiz relationship type.")
        if names:
            return [{"type": name} for name in names]
        if ANY_TYPE in self.rel_enum:
            return [{"type": ANY_TYPE}]
        if self.rel_type_required:
            raise ValueError("This graph search needs a relationship type.")
        return None


def _as_shape(value):
    """A WizShape as it is, or one built from an introspection payload."""
    return value if isinstance(value, WizShape) else WizShape(value)


def wiz_search_hints(payload):
    """Entity types and where keys graphSearch can filter on. Both may be empty.

    An open where cannot list its keys, so the keys the tenant answers on are
    offered instead. ANY is left out because no entity type already means ANY.
    ``payload`` can also be a WizShape.
    """
    shape = _as_shape(payload)
    if shape.legacy:
        return [], []
    entities = sorted(name for name in shape.entity_types if name != ANY_TYPE)
    filters = sorted(WIZ_COMMON_KEYS) if shape.open_where else sorted(shape.where_fields)
    return entities, filters


def wiz_filter_mode(payload):
    """none for the legacy shape, open for a scalar where, schema for an input object."""
    shape = _as_shape(payload)
    if shape.legacy:
        return "none"
    return "open" if shape.open_where else "schema"


def _predicate(value):
    """EQUALS for one where key. true and false become booleans."""
    text = str(value)
    if text.lower() in ("true", "false"):
        return {"EQUALS": text.lower() == "true"}
    return {"EQUALS": [value]}


def wiz_search_value(shape, category=None, text="", prop=None, value=None):
    """The graphSearch query value for a name, an entity type, and one property."""
    query = {"select": True}
    typed = shape.entity_type(category)
    if typed:
        query["type"] = typed
    where = {}
    text = str(text or "").strip()
    if text:
        where["name"] = {"CONTAINS": [text]}
    if prop and value is not None and str(value) != "":
        key = str(prop)
        if key in ("id", "_vertexID"):
            key = shape.id_key() or key
        if shape.open_where:
            if not _PROP_RE.fullmatch(key):
                raise ValueError(f"{prop} is not a filter on this graph search.")
            where[key] = _predicate(value)
        else:
            where[key] = {"EQUALS": [value]}
    if where:
        query["where"] = where
    return query


def graph_search_request(shape, query_type, cleaned, full=False, project_id="*", first=50, after=None,
                         entities=True):
    """The one graphSearch document for a tenant schema, and its variables.

    The query object goes in a $query variable. projectId, first, after, and
    quick are declared only when introspection publishes them. The first page
    omits after. The count call (entities False) declares no $after. Wiz
    refuses a cursor in quick mode, so a page with after sends quick false.
    """
    decls = [f"$query: {query_type}"]
    args = ["query: $query"]
    variables = {"query": cleaned if isinstance(cleaned, dict) else {}}
    page = max(1, min(int(first), 200))
    for name, value, fallback in (
        ("projectId", project_id or "*", "String"),
        ("first", page, "Int"),
        ("after", after, "String"),
        ("quick", after is None, "Boolean"),
    ):
        if name == "after" and not entities:
            continue
        arg = _field_arg(shape.field, name)
        if not arg:
            continue
        decls.append(f"${name}: {_print_type(arg.get('type')) or fallback}")
        args.append(f"{name}: ${name}")
        if name == "after" and value is None:
            continue
        variables[name] = value
    if entities:
        body = "id name type properties" if full else "id name type"
        lines = ["totalCount", "pageInfo { hasNextPage endCursor }", f"nodes {{ entities {{ {body} }} }}"]
        operation = "GraphSearch"
    else:
        lines = ["totalCount"] + (["maxCountReached"] if shape.max_count else [])
        operation = "EntityTotal"
    document = (
        f"query {operation}({', '.join(decls)}) {{\n"
        f"  graphSearch({', '.join(args)}) {{\n"
        + "".join(f"    {line}\n" for line in lines)
        + "  }\n"
        "}\n"
    )
    return document, variables


def _relationship_type_value(types, type_ref, names):
    """The relationship-type filter this input declares. None when the names do not fit."""
    node = type_ref if isinstance(type_ref, dict) else {}
    while node.get("kind") == "NON_NULL":
        node = node.get("ofType") or {}
    if node.get("kind") == "LIST":
        inner = node.get("ofType") or {}
        while inner.get("kind") == "NON_NULL":
            inner = inner.get("ofType") or {}
        named = types.get(inner.get("name") or "") or {}
        if named.get("kind") == "ENUM":
            allowed = {item.get("name") for item in named.get("enumValues") or [] if isinstance(item, dict)}
            kept = [name for name in names if not allowed or name in allowed]
            return kept or None
        if named.get("kind") == "INPUT_OBJECT":
            fields = _input_fields(types, inner)
            enum_field = fields.get("type")
            if not enum_field:
                return None
            allowed = set(_enum_names(types, enum_field.get("type")))
            kept = [name for name in names if not allowed or name in allowed]
            return [{"type": name} for name in kept] or None
        return None
    named = types.get(node.get("name") or "") or {}
    if named.get("kind") == "INPUT_OBJECT":
        fields = _input_fields(types, node)
        exact = fields.get("EXACT")
        if not exact:
            return None
        value = _comparison_value(exact.get("type"), names)
        return {"EXACT": value} if value not in (None, "", []) else None
    return None


def _legacy_neighbor_value(anchor, categories=None, rel_types=None, q=None):
    """The historical neighbor shape. The local fixture reads relationships.with.where.id."""
    clause = {"with": {"where": {"id": {"EQUALS": [anchor]}}}}
    if rel_types:
        clause["type"] = {"EXACT": rel_types}
    query = {"select": True, "relationships": [clause]}
    if categories:
        query["type"] = list(categories)
    text = str(q or "").strip()
    if text:
        query["where"] = {"name": {"CONTAINS": [text]}}
    return query


def wiz_neighbor_value(shape, anchor, categories=None, rel_types=None, q=None):
    """The graphSearch value that finds entities connected to ``anchor``.

    A tenant schema anchors the outer entity by its id key and reads the
    neighbor from ``with``. Categories and the name filter apply to ``with``.
    The relationship type is ANY unless names are given. The legacy shape
    is kept for the local fixture.
    """
    if shape.legacy:
        rel_types = [item for item in (rel_types or []) if item and item != "related"]
        return _legacy_neighbor_value(anchor, categories, rel_types, q)
    if "relationships" not in shape.query_fields or "with" not in shape.rel_fields:
        raise ValueError("This graph search has no relationship filter.")
    ident = shape.id_key()
    if not ident:
        raise ValueError("This graph search has no id filter.")
    inner = {"select": True}
    if categories:
        inner_type = [shape.entity_type(category)[0] for category in categories]
    else:
        inner_type = shape.entity_type(None)
    if inner_type:
        inner["type"] = inner_type
    text = str(q or "").strip()
    if text:
        inner["where"] = {"name": {"CONTAINS": [text]}}
    clause = {"with": inner}
    typed = shape.rel_type(rel_types)
    if typed:
        clause["type"] = typed
    outer = {"select": True}
    outer_type = shape.entity_type(None)
    if outer_type:
        outer["type"] = outer_type
    outer["where"] = {ident: {"EQUALS": [anchor]}}
    outer["relationships"] = [clause]
    return outer


def _neighbor_value(payload, anchor, categories=None, rel_types=None, q=None):
    """wiz_neighbor_value for an introspection result or a WizShape."""
    return wiz_neighbor_value(_as_shape(payload), anchor, categories, rel_types, q)


def entity_count_request(payload, project_id="*"):
    """One graphSearch that reads totalCount. None when graphSearch has no arguments.

    The query object is a $query variable with type ANY when the enum has it.
    None also means the result has no totalCount or the schema needs a type
    this call cannot give. ``payload`` can also be a WizShape.
    """
    shape = _as_shape(payload)
    if shape.legacy:
        return None
    if shape.result_fields and "totalCount" not in shape.result_fields:
        return None
    try:
        typed = shape.entity_type(None)
    except ValueError:
        return None
    value = {"select": True} if "select" in shape.query_fields else {}
    if typed:
        value["type"] = typed
    return graph_search_request(shape, shape.query_type, value, False, project_id, 1, None, entities=False)


_WIZ_HINTS = (
    ("GraphEntityQueryInput.type",
     "Every level of a graph search needs an entity type. Add type: [ANY] or a named type."),
    ("invalid type for variable: 'query'",
     "Wiz refused the $query variable. A level or a relationship in it has no type."),
    ("Unterminated spread operator",
     "A property key with a dot, such as accessibleFrom.internet, cannot be written inline. "
     "Pass where as a $where: GraphPropertyPredicate variable."),
)


def explain_wiz_error(message):
    """Add a fix hint to a Wiz error that a saved query can cause."""
    text = str(message or "")
    for needle, hint in _WIZ_HINTS:
        if needle in text:
            return f"{hint} (Wiz said: {text[:200]})"
    return text


def _clean_value(types, type_ref, value):
    """Keep only values the input type accepts. A list and a single value coerce either way."""
    if not isinstance(type_ref, dict):
        return value
    kind = type_ref.get("kind")
    if kind == "NON_NULL":
        if value is None:
            return None
        return _clean_value(types, type_ref.get("ofType"), value)
    if kind == "LIST":
        items = value if isinstance(value, list) else [value]
        cleaned = []
        for item in items:
            one = _clean_value(types, type_ref.get("ofType"), item)
            if one is not None:
                cleaned.append(one)
        return cleaned
    name = type_ref.get("name")
    named = types.get(name or "") or {}
    named_kind = named.get("kind") or kind
    if named_kind == "ENUM":
        allowed = {item.get("name") for item in named.get("enumValues") or [] if isinstance(item, dict)}
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else None
        return value if value in allowed else None
    if named_kind == "INPUT_OBJECT":
        if not isinstance(value, dict):
            return None
        fields = _input_fields(types, type_ref)
        out = {}
        for key, item in value.items():
            if key not in fields:
                continue
            cleaned = _clean_value(types, fields[key].get("type"), item)
            if cleaned in (None, [], {}):
                continue
            out[key] = cleaned
        return out
    if name == "Boolean":
        return value if isinstance(value, bool) else None
    if name == "Int":
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value
    if name in ("String", "ID"):
        if isinstance(value, list):
            return str(value[0]) if value else None
        if value is None:
            return None
        return str(value)
    return value


def _comparison_value(op_type, raw):
    """Keep every value when the operator is a list. A single-value operator takes the first."""
    if isinstance(raw, list):
        items = [item for item in raw if item not in (None, "")]
    elif raw in (None, ""):
        items = []
    else:
        items = [raw]
    if not items:
        return None
    if _is_list_type(op_type):
        return items
    return items[0]


def _rewrite_comparisons(types, query_type, query):
    """Point name and property filters at an operator the where-input actually has.

    A neighbor query can filter both levels, so each relationships.with clause
    is rewritten on its own. Only an input-object where is rewritten. A scalar
    where (GraphPropertyPredicate on a Wiz tenant) goes on as JSON.
    """
    if not isinstance(query, dict):
        return query
    copied = dict(query)
    where = query.get("where")
    where_ref = (_input_fields(types, query_type).get("where") or {}).get("type")
    if isinstance(where, dict) and _named(types, where_ref).get("kind") == "INPUT_OBJECT":
        fields = _input_fields(types, where_ref)
        rewritten = {}
        for key, spec in where.items():
            field = fields.get(key)
            if not field:
                continue
            if not isinstance(spec, dict) or len(spec) != 1:
                rewritten[key] = spec
                continue
            op, raw = next(iter(spec.items()))
            comparisons = _input_fields(types, field.get("type"))
            if not comparisons:
                text = raw[0] if isinstance(raw, list) and raw else raw
                if text in (None, ""):
                    continue
                rewritten[key] = text
                continue
            chosen = op if op in comparisons else next((item for item in _TEXT_OPS if item in comparisons), None)
            if not chosen:
                continue
            value = _comparison_value(comparisons[chosen].get("type"), raw)
            if value in (None, "", []):
                continue
            rewritten[key] = {chosen: value}
        if rewritten:
            copied["where"] = rewritten
        else:
            copied.pop("where", None)
    rel_field = _input_fields(types, query_type).get("relationships")
    rels = copied.get("relationships")
    if rel_field and isinstance(rels, list):
        item_fields = _input_fields(types, rel_field.get("type"))
        with_field = item_fields.get("with")
        nested = []
        for rel in rels:
            if not isinstance(rel, dict):
                continue
            item = dict(rel)
            if with_field and isinstance(rel.get("with"), dict):
                item["with"] = _rewrite_comparisons(types, with_field.get("type"), rel["with"])
            nested.append(item)
        copied["relationships"] = nested
    return copied


def _require_fields(types, type_ref, value):
    """Refuse a value that leaves out a field the input requires.

    A field is required when it is NON_NULL and has no default value. The
    check recurses into the fields that are present.
    """
    named = _named(types, type_ref)
    if named.get("kind") != "INPUT_OBJECT":
        return
    items = value if isinstance(value, list) else [value]
    fields = _input_fields(types, type_ref)
    for item in items:
        if not isinstance(item, dict):
            continue
        for name, spec in fields.items():
            present = item.get(name)
            if present in (None, [], {}):
                if _is_required(spec):
                    raise ValueError(f"{named.get('name')}.{name} is required by this graph search.")
                continue
            _require_fields(types, spec.get("type"), present)


def conform_wiz_query(payload, query):
    """Return ``(variable type, query value, send projectId)`` for this tenant.

    Without graphSearch arguments in the schema, the historical document is
    kept. Otherwise the value is rewritten, cleaned, and then checked for the
    fields the input requires, so a missing type never leaves the process.
    ``payload`` can also be a WizShape.
    """
    shape = _as_shape(payload)
    if shape.legacy:
        return "GraphEntityQueryInput!", query, True
    types = shape.types
    arg_type = shape.query_arg.get("type")
    prepared = _rewrite_comparisons(types, arg_type, query if isinstance(query, dict) else {})
    cleaned = _clean_value(types, arg_type, prepared)
    if not isinstance(cleaned, dict):
        cleaned = {}
    _require_fields(types, arg_type, cleaned)
    return shape.query_type, cleaned, _field_arg(shape.field, "projectId") is not None


def _anchored(level):
    where = level.get("where") if isinstance(level, dict) else None
    return isinstance(where, dict) and "_vertexID" in where


def _slow_shape(query):
    """Why a graphSearch value would time out on the tenant, or None.

    Type ANY with a text operator and no _vertexID scans every entity. An
    anchor inside ``with`` with no anchor on the outer entity does the same.
    Type ANY with EQUALS answers in about a second (name, nativeType, and
    cloudPlatform were probed), so it is allowed.
    """
    if not isinstance(query, dict):
        return None
    where = query.get("where") if isinstance(query.get("where"), dict) else {}
    if query.get("type") == [ANY_TYPE] and not _anchored(query):
        for spec in where.values():
            if isinstance(spec, dict) and any(op in spec for op in _SLOW_OPS):
                return "Choose an entity type. A text filter across every Wiz type is too slow."
    if not _anchored(query):
        for rel in query.get("relationships") or []:
            if isinstance(rel, dict) and _anchored(rel.get("with")):
                return "Choose an entity type. Anchor the outer entity by _vertexID, not the inner one."
    return None


def inventory_route(shape, project_id):
    """True when a name search with no entity type can use cloudResourcesV2.

    A scoped source uses it only when the filter input publishes projectId.
    Otherwise the search would run across the whole tenant, so it fails closed.
    """
    if not shape.text_field:
        return False
    if (project_id or "*") == "*":
        return True
    return "projectId" in _input_fields(shape.types, shape.text_filter_arg.get("type"))


def _contains_type(query, category):
    found = query.get("type") if isinstance(query, dict) else None
    if isinstance(found, list):
        return category in found
    return found == category


class QueryRejected(ValueError):
    """A mutation or subscription was refused before any request was sent."""


class TokenExpired(Exception):
    """The API said the access token is expired, and a refresh did not help."""


class RateLimited(Exception):
    """The GraphQL API returned HTTP 429 after the backoff was used up."""


class GraphQLCallError(Exception):
    """The GraphQL API answered with an error that is safe to show.

    status is the HTTP status of the answer, or None when no answer came.
    """

    def __init__(self, message="", status=None):
        super().__init__(message)
        self.status = status


class QueryTimeout(GraphQLCallError):
    """The GraphQL API did not answer in time. The request is not retried."""


class GraphQLConfigError(ValueError):
    """The source is configured, but the token or endpoint cannot be used."""


def load_graphql_env():
    """Fill unset GraphQL variables from the gitignored .env. The process environment wins.

    A fresh process does not read the file. A connection saved during that
    run is already in the environment and is used as it is.
    """
    if os.environ.get("GX_FRESH") == "1":
        return
    from neo4j_env import dotenv_path, parse_dotenv
    path = dotenv_path()
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for key, value in parse_dotenv(text):
        if key in ENV_KEYS and key not in os.environ:
            os.environ[key] = value


def _secret_key(name) -> bool:
    key = str(name).lower()
    return key in TOKEN_KEYS or key.endswith("_token") or key.endswith("_secret")


def _warn_ignored_token():
    global _TOKEN_WARNED
    if _TOKEN_WARNED:
        return
    _TOKEN_WARNED = True
    print("WARNING: a GraphQL token in the config file is not kept. Set it in the environment.", file=sys.stderr)


def without_secrets(raw):
    """Copy ``raw`` with GraphQL token fields removed.

    Returns ``(cleaned, changed)``. Budget numbers are not clamped here.
    ``token_env`` is the name of an environment variable, so it stays.
    """
    if not isinstance(raw, dict) or "graphql" not in raw:
        return raw, False
    blob = raw["graphql"]
    items = blob if isinstance(blob, list) else [blob] if isinstance(blob, dict) else None
    if items is None:
        return raw, False
    changed = False
    cleaned_items = []
    for item in items:
        if not isinstance(item, dict):
            cleaned_items.append(item)
            continue
        keep = {}
        for key, value in item.items():
            if _secret_key(key):
                changed = True
                continue
            keep[key] = value
        cleaned_items.append(keep)
    if not changed:
        return raw, False
    _warn_ignored_token()
    cleaned = dict(raw)
    cleaned["graphql"] = cleaned_items[0] if isinstance(blob, dict) else cleaned_items
    return cleaned, True


def _clean_endpoint(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        print(f"WARNING: a GraphQL endpoint was ignored (host {host or 'unknown'}).", file=sys.stderr)
        return None
    if parts.username is not None or parts.password is not None:
        print("WARNING: a GraphQL endpoint containing a user or password was ignored.", file=sys.stderr)
        return None
    if {key.lower() for key in parse_qs(parts.query)} & TOKEN_KEYS:
        print("WARNING: a GraphQL endpoint containing a token was ignored.", file=sys.stderr)
        return None
    return raw


# The Wiz offerings. This table is the only place that names a Wiz domain or a Wiz
# token host. The server and the page both read it (the page gets it from
# /api/connection), so the two always agree. An endpoint belongs to an offering
# when its host is the domain or ends in "." plus the domain.
WIZ_OFFERINGS = (
    {"id": "wiz.io", "label": "Wiz", "domain": "wiz.io",
     "auth_url": "https://auth.app.wiz.io/oauth/token"},
    {"id": "wiz.us", "label": "Wiz for Government", "domain": "wiz.us",
     "auth_url": "https://auth.app.wiz.us/oauth/token"},
)


def wiz_offering(endpoint):
    """The Wiz offering an endpoint belongs to, or None.

    The match is on the whole host name, in any letter case. A look-alike such as
    notwiz.io or wiz.io.example.com is not Wiz.
    """
    host = (urlsplit(str(endpoint or "")).hostname or "").lower().rstrip(".")
    for offering in WIZ_OFFERINGS:
        domain = offering["domain"]
        if host == domain or host.endswith("." + domain):
            return offering
    return None


def _wiz_host(endpoint):
    return wiz_offering(endpoint) is not None


def wiz_auth_url(endpoint):
    """Auth URL for a Wiz endpoint. Empty when the host is not Wiz."""
    offering = wiz_offering(endpoint)
    return offering["auth_url"] if offering else ""


def wiz_offerings_public():
    """The offerings table for the page. It names domains only, never a tenant."""
    return [dict(item) for item in WIZ_OFFERINGS]


def _adapter_name(value, endpoint):
    raw = str(value or "").strip().lower()
    if raw in ("wiz", "generic"):
        return raw
    return "wiz" if _wiz_host(endpoint) else "generic"


def _client_id(value):
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text:
        return ""
    return text[:200]


def _source_id(value, adapter):
    raw = str(value or "").strip()
    if _ID_RE.fullmatch(raw):
        return raw
    return "wiz" if adapter == "wiz" else "graphql"


def _one_source(item):
    if not isinstance(item, dict):
        return None
    if any(_secret_key(key) for key in item):
        _warn_ignored_token()
    endpoint = _clean_endpoint(item.get("endpoint"))
    if not endpoint:
        return None
    adapter = _adapter_name(item.get("adapter"), endpoint)
    token_env = str(item.get("token_env") or "GRAPHQL_TOKEN").strip()
    if not _ENV_RE.fullmatch(token_env):
        token_env = "GRAPHQL_TOKEN"
    label = str(item.get("label") or ("Wiz" if adapter == "wiz" else "GraphQL")).strip()[:80]
    project = str(item.get("project_id") or "*")[:200] or "*"
    return {
        "id": _source_id(item.get("id"), adapter),
        "label": label or ("Wiz" if adapter == "wiz" else "GraphQL"),
        "endpoint": endpoint,
        "adapter": adapter,
        "token_env": token_env,
        "project_id": project,
    }


def sources_from_config(data):
    """Sanitized GraphQL sources stored next to the working set. Tokens are dropped."""
    if not isinstance(data, dict):
        return []
    blob = data.get("graphql")
    items = blob if isinstance(blob, list) else [blob] if isinstance(blob, dict) else []
    out = []
    for item in items:
        spec = _one_source(item)
        if spec:
            out.append(spec)
    return out


def _env_source():
    endpoint = _clean_endpoint(os.environ.get("GRAPHQL_ENDPOINT"))
    if not endpoint:
        return None
    adapter = _adapter_name(os.environ.get("GRAPHQL_ADAPTER"), endpoint)
    return _one_source({
        "id": os.environ.get("GRAPHQL_ID"),
        "label": os.environ.get("GRAPHQL_LABEL"),
        "endpoint": endpoint,
        "adapter": adapter,
        "token_env": os.environ.get("GRAPHQL_TOKEN_ENV") or "GRAPHQL_TOKEN",
        "project_id": os.environ.get("GRAPHQL_PROJECT_ID") or "*",
    })


def resolve_graphql_sources(config):
    """File sources, then the environment. ``GRAPHQL_ENDPOINT`` overrides the same id.

    The returned dicts name the environment variable that holds the token.
    They do not contain the token. A fresh process ignores the file and
    ``gx.local.json`` and keeps only a source set in this process.
    """
    fresh = os.environ.get("GX_FRESH") == "1"
    if fresh:
        entries = []
    else:
        load_graphql_env()
        entries = sources_from_config(config if isinstance(config, dict) else {})
    env_entry = _env_source()
    if env_entry:
        entries = [entry for entry in entries if entry["id"] != env_entry["id"]]
        entries.append(env_entry)
    out, seen = [], set()
    for entry in entries:
        ident = entry["id"]
        if ident == "neo4j":
            print("WARNING: a GraphQL source named neo4j was skipped.", file=sys.stderr)
            continue
        if ident in seen:
            print(f"WARNING: duplicate GraphQL source {ident} was skipped.", file=sys.stderr)
            continue
        seen.add(ident)
        out.append(entry)
    client_id = _client_id(os.environ.get("GRAPHQL_CLIENT_ID"))
    if client_id:
        for entry in out:
            entry["client_id"] = client_id
    return out


def _block_end(document, i):
    """The offset of the closing triple quote of a block string, or -1.

    ``i`` points just past the opening triple quote. A backslash before a
    triple quote escapes it, so the string goes on.
    """
    n = len(document)
    while i < n:
        if document.startswith('\\"""', i):
            i += 4
            continue
        if document.startswith('"""', i):
            return i
        i += 1
    return -1


def _skip_string(document, i):
    n = len(document)
    if document.startswith('"""', i):
        end = _block_end(document, i + 3)
        return n if end < 0 else end + 3
    quote = document[i]
    i += 1
    while i < n:
        if document[i] == "\\":
            i += 2
            continue
        if document[i] == quote:
            return i + 1
        # A raw newline does not end a GraphQL block string; ordinary strings
        # are already invalid there, and stopping at the quote is enough.
        i += 1
    return n


def _read_name(document, i):
    n = len(document)
    if i >= n or not (document[i].isalpha() or document[i] == "_"):
        return None, i
    j = i + 1
    while j < n and (document[j].isalnum() or document[j] == "_"):
        j += 1
    return document[i:j], j


def _skip_balanced(document, i):
    """Skip a ``{...}`` group. ``i`` points at the opening brace."""
    n = len(document)
    depth = 0
    while i < n:
        if document.startswith('"""', i) or document[i] in "\"'":
            i = _skip_string(document, i)
            continue
        if document[i] == "#":
            while i < n and document[i] != "\n":
                i += 1
            continue
        if document[i] == "{":
            depth += 1
        elif document[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def operation_types(document: str) -> list:
    """Operation kinds in a document. A shorthand selection is a query.

    A fragment is not an operation. Braces inside an argument default are not
    a shorthand query.
    """
    i, n = 0, len(document or "")
    ops, depth, paren, awaiting_body = [], 0, 0, False
    while i < n:
        ch = document[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if ch == "#":
            while i < n and document[i] != "\n":
                i += 1
            continue
        if document.startswith('"""', i) or ch in "\"'":
            i = _skip_string(document, i)
            continue
        if ch == "(":
            paren += 1
            i += 1
            continue
        if ch == ")":
            paren = max(0, paren - 1)
            i += 1
            continue
        if ch == "{":
            if paren > 0:
                i = _skip_balanced(document, i)
                continue
            if depth == 0 and not awaiting_body:
                ops.append("query")
            if depth == 0:
                awaiting_body = False
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        name, nxt = _read_name(document, i)
        if name is None:
            i += 1
            continue
        if depth == 0 and paren == 0 and name in ("query", "mutation", "subscription", "fragment"):
            if name != "fragment":
                ops.append(name)
            awaiting_body = True
        i = nxt
    return ops


_OPENERS = {"{": "}", "(": ")", "[": "]"}
_CLOSERS = {"}": "{", ")": "(", "]": "["}


def syntax_problem(document: str):
    """A short reason the document cannot be GraphQL, or None.

    This is not a full parser. It checks that brackets, braces, and
    parentheses match outside strings and comments, and that each top-level
    definition starts with query, mutation, subscription, fragment, or a
    selection set in braces. The tenant still checks fields and types.
    """
    text = document or ""
    i, n = 0, len(text)
    stack = []
    expect_definition = True
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith('"""', i):
            end = _block_end(text, i + 3)
            if end < 0:
                return "a block string has no closing quotes"
            i = end + 3
            continue
        if ch == '"':
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += 2 if text[j] == "\\" else 1
            if j >= n or text[j] != '"':
                return "a string has no closing quote"
            i = j + 1
            continue
        if ch in _OPENERS:
            if not stack and expect_definition and ch != "{":
                return f"the document cannot start with {ch!r}"
            stack.append(ch)
            expect_definition = False
            i += 1
            continue
        if ch in _CLOSERS:
            if not stack or stack[-1] != _CLOSERS[ch]:
                return f"there is an extra {ch!r}"
            stack.pop()
            if not stack and ch == "}":
                expect_definition = True
            i += 1
            continue
        if not stack and expect_definition:
            name, nxt = _read_name(text, i)
            if name not in ("query", "mutation", "subscription", "fragment"):
                shown = name if name else ch
                return f"a definition must start with query, fragment, or {{, not {shown!r}"
            expect_definition = False
            i = nxt
            continue
        i += 1
    if stack:
        return f"a {stack[-1]!r} has no closing {_OPENERS[stack[-1]]!r}"
    if not expect_definition:
        return "the last definition has no selection set in braces"
    return None


def assert_well_formed(document: str) -> None:
    """Refuse a document whose brackets or definitions cannot be GraphQL."""
    problem = syntax_problem(document)
    if problem:
        raise QueryRejected(f"The GraphQL document is not valid: {problem}.")


def assert_query_only(document: str) -> None:
    """Refuse a mutation or subscription before a request exists."""
    ops = operation_types(document)
    if any(op != "query" for op in ops):
        raise QueryRejected("Read-only explorer: a GraphQL mutation is not sent.")
    if "query" not in ops:
        raise QueryRejected("Read-only explorer: the GraphQL document has no query.")


def selection_names(document: str, *, roots_only: bool = False) -> set:
    """Field names in selection sets. Names inside arguments are left out.

    ``roots_only`` keeps the fields the operation itself selects. Nested
    names belong to the returned type, so they are not mutation calls.
    """
    i, n = 0, len(document or "")
    names, depth, paren, previous = set(), 0, 0, ""
    while i < n:
        ch = document[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if ch == "#":
            while i < n and document[i] != "\n":
                i += 1
            continue
        if document.startswith('"""', i) or ch in "\"'":
            i = _skip_string(document, i)
            previous = ""
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if ch == "(":
            paren += 1
            i += 1
            continue
        if ch == ")":
            paren = max(0, paren - 1)
            i += 1
            continue
        name, nxt = _read_name(document, i)
        if name is None:
            i += 1
            continue
        if depth >= 1 and paren == 0 and name not in _SKIP_NAMES and previous != "on":
            if not roots_only or depth == 1:
                names.add(name)
        previous = name
        i = nxt
    return names


# GraphQL value syntax for the inline query object of a saved graph search.
# The reader builds dicts, lists, strings, numbers, booleans, and None. It
# never calls eval, exec, or an import, and it refuses every other token.
_VALUE_MAX_DEPTH = 64
_VALUE_MAX_LENGTH = 200_000
_VALUE_NUMBER_RE = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_VALUE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class _ValueReader:
    """Read one GraphQL input value from ``text`` at ``pos``.

    A $name is looked up in ``variables``. An unset variable is null, as a
    GraphQL server reads it.
    """

    def __init__(self, text, pos, variables):
        self.text = text
        self.pos = pos
        self.variables = variables if isinstance(variables, dict) else {}

    def fail(self, why):
        raise ValueError(f"The query object is not GraphQL value syntax: {why} at offset {self.pos}.")

    def skip(self):
        text, n = self.text, len(self.text)
        while self.pos < n:
            ch = text[self.pos]
            if ch in " \t\r\n,﻿":
                self.pos += 1
            elif ch == "#":
                while self.pos < n and text[self.pos] not in "\r\n":
                    self.pos += 1
            else:
                return

    def peek(self):
        self.skip()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def name(self):
        found = _NAME_RE.match(self.text, self.pos)
        if not found:
            self.fail("expected a name")
        self.pos = found.end()
        return found.group()

    def value(self, depth=0):
        if depth > _VALUE_MAX_DEPTH:
            self.fail("the value nests too deep")
        ch = self.peek()
        if ch == "{":
            return self.object(depth)
        if ch == "[":
            self.pos += 1
            out = []
            while self.peek() != "]":
                if not self.peek():
                    self.fail("a list is not closed")
                out.append(self.value(depth + 1))
            self.pos += 1
            return out
        if ch == '"':
            return self.block() if self.text.startswith('"""', self.pos) else self.string()
        if ch == "$":
            self.pos += 1
            return self.variables.get(self.name())
        if ch == "-" or ch.isdigit():
            return self.number()
        if ch == "_" or ch.isalpha():
            word = self.name()
            return {"true": True, "false": False, "null": None}.get(word, word)
        self.fail(f"unexpected {ch!r}" if ch else "the value ends early")

    def object(self, depth):
        self.pos += 1
        out = {}
        while self.peek() != "}":
            if not self.peek():
                self.fail("an object is not closed")
            key = self.name()
            if key in out:
                self.fail(f"{key} is given twice")
            if self.peek() != ":":
                self.fail("expected ':'")
            self.pos += 1
            out[key] = self.value(depth + 1)
        self.pos += 1
        return out

    def number(self):
        found = _VALUE_NUMBER_RE.match(self.text, self.pos)
        if not found:
            self.fail("a number is malformed")
        end = found.end()
        if end < len(self.text) and (self.text[end] in "._" or self.text[end].isalnum()):
            self.fail("a number is malformed")
        self.pos = end
        raw = found.group()
        if found.group(2) or found.group(3):
            return float(raw)
        return int(raw)

    def string(self):
        text, n = self.text, len(self.text)
        self.pos += 1
        out = []
        while self.pos < n:
            ch = text[self.pos]
            if ch == '"':
                self.pos += 1
                return "".join(out)
            if ch in "\r\n":
                self.fail("a string has a raw line break")
            if ch == "\\":
                code = text[self.pos + 1:self.pos + 2]
                if code in _VALUE_ESCAPES:
                    out.append(_VALUE_ESCAPES[code])
                    self.pos += 2
                    continue
                if code == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", text[self.pos + 2:self.pos + 6]):
                    out.append(chr(int(text[self.pos + 2:self.pos + 6], 16)))
                    self.pos += 6
                    continue
                self.fail("a string has an unknown escape")
            out.append(ch)
            self.pos += 1
        self.fail("a string is not closed")

    def block(self):
        end = _block_end(self.text, self.pos + 3)
        if end < 0:
            self.fail("a block string is not closed")
        raw = self.text[self.pos + 3:end].replace('\\"""', '"""')
        self.pos = end + 3
        lines = raw.splitlines()
        indents = [len(line) - len(line.lstrip(" \t")) for line in lines[1:] if line.strip()]
        cut = min(indents) if indents else 0
        lines = lines[:1] + [line[cut:] for line in lines[1:]]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)


def _graph_search_args(document):
    """The offset just inside the arguments of the root ``graphSearch`` field.

    run_document reads paths from data.graphSearch, so only the root field
    with that response name counts. An aliased graphSearch has another
    response name and is skipped. The operation name and the variable
    definitions are not fields. Comments and strings are skipped. ValueError
    means no root field answers as graphSearch, more than one does, or the
    field under that name is not graphSearch.
    """
    i, n = 0, len(document)
    depth, paren, found = 0, 0, []

    def gap(j):
        while j < n and document[j] in " \t\r\n,":
            j += 1
        return j

    while i < n:
        ch = document[i]
        if ch == "#":
            while i < n and document[i] not in "\r\n":
                i += 1
            continue
        if document.startswith('"""', i) or ch in "\"'":
            i = _skip_string(document, i)
            continue
        if ch in "()":
            paren = paren + 1 if ch == "(" else max(0, paren - 1)
            i += 1
            continue
        if ch in "{}" and paren == 0:
            depth = depth + 1 if ch == "{" else max(0, depth - 1)
            i += 1
            continue
        if ch == "$" or (ch == "." and document.startswith("...", i)):
            # A variable or a spread is not a field name. Skip its name too.
            i += 3 if ch == "." else 1
            _, i = _read_name(document, gap(i))
            continue
        name, nxt = _read_name(document, i)
        if name is None:
            i += 1
            continue
        if depth != 1 or paren != 0:
            i = nxt
            continue
        key, field = name, name
        j = gap(nxt)
        if j < n and document[j] == ":":
            field, nxt = _read_name(document, gap(j + 1))
        if key == "graphSearch":
            if field != "graphSearch":
                raise ValueError("The graphSearch response name holds another field.")
            j = gap(nxt)
            found.append(j + 1 if j < n and document[j] == "(" else None)
        i = nxt
    if not found:
        raise ValueError("The document has no root graphSearch field.")
    if len(found) > 1:
        raise ValueError("The document has more than one root graphSearch field.")
    if found[0] is None:
        raise ValueError("The document has no graphSearch arguments.")
    return found[0]


def graph_search_query(document, variables=None):
    """The query object a saved graphSearch sends, read from the document.

    The object may be inline, may hold $variables, or may be one $query
    variable. Only GraphQL value syntax is read. ValueError means the
    document has no graphSearch query argument, or the argument cannot be
    read, so the shape of each result path is unknown.
    """
    document = str(document or "")
    if len(document) > _VALUE_MAX_LENGTH:
        raise ValueError("The document is too long to read its query object.")
    start = _graph_search_args(document)
    reader = _ValueReader(document, start, variables)
    while reader.peek() not in ("", ")"):
        key = reader.name()
        if reader.peek() != ":":
            reader.fail("expected ':'")
        reader.pos += 1
        if key == "query":
            value = reader.value()
            if not isinstance(value, dict):
                reader.fail("query is not an object")
            return value
        reader.value()
    raise ValueError("graphSearch has no query argument.")


def _as_list(value):
    """GraphQL list input coercion: one value where a list goes is a list of one."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _clause_names(clause):
    """The relationship type names on one clause. ANY when it names none.

    None means a type item is neither an enum name nor a {type} object.
    """
    names = []
    for item in _as_list(clause.get("type")):
        name = item.get("type") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name:
            return None
        if name not in names:
            names.append(name)
    return names or [ANY_TYPE]


def path_edges_plan(query):
    """The slot plan for each path a graphSearch query returns.

    A path lists the levels with select true, depth first, in clause order,
    the outer level first. Each slot in the plan names its nearest selected
    ancestor slot as parent (None for a root), the relationship type names
    on the clauses between them, and indirect when an unselected level sits
    between them. A negated clause and its subtree select nothing, so they
    add no slot. None means the tree has a shape the plan cannot place.
    """
    if not isinstance(query, dict):
        return None
    plan = []

    def walk(level, parent, names, indirect, depth):
        # parent is the nearest selected ancestor slot. names and indirect
        # describe the clauses from that slot down to this level.
        if not isinstance(level, dict) or depth > _VALUE_MAX_DEPTH:
            return False
        rels = _as_list(level.get("relationships"))
        selected = level.get("select") is True
        here = None
        if selected:
            here = len(plan)
            if parent is None:
                plan.append({"parent": None, "relationship": [], "indirect": False})
            else:
                plan.append({"parent": parent, "relationship": list(names), "indirect": indirect})
        for clause in rels:
            if not isinstance(clause, dict):
                return False
            if clause.get("negate") is True:
                continue
            kinds = _clause_names(clause)
            if kinds is None:
                return False
            if selected:
                ok = walk(clause.get("with"), here, kinds, False, depth + 1)
            elif parent is None:
                # An unselected outer level: its selected children are roots.
                ok = walk(clause.get("with"), None, [], False, depth + 1)
            else:
                # An unselected middle level: the link runs through it.
                ok = walk(clause.get("with"), parent, names + kinds, True, depth + 1)
            if not ok:
                return False
        return True

    return plan if walk(query, None, [], False, 0) else None


def _type_ref(type_ref):
    """Return ``(name, kind, printed)`` with Non-Null and List wrappers kept."""
    wrappers = []
    current = type_ref if isinstance(type_ref, dict) else None
    while isinstance(current, dict) and current.get("kind") in ("NON_NULL", "LIST"):
        wrappers.append(current["kind"])
        current = current.get("ofType")
    name = str((current or {}).get("name") or "")
    kind = str((current or {}).get("kind") or "")
    printed = name
    for wrapper in reversed(wrappers):
        printed = f"{printed}!" if wrapper == "NON_NULL" else f"[{printed}]"
    return name, kind, printed or "Unknown"


def _field_type(field):
    name, kind, printed = _type_ref((field or {}).get("type"))
    args = field.get("args") if isinstance(field, dict) else None
    if args:
        parts = []
        for arg in args:
            if not isinstance(arg, dict) or not arg.get("name"):
                continue
            _, _, arg_type = _type_ref(arg.get("type"))
            parts.append(f"{arg['name']}: {arg_type}")
        if parts:
            printed = f"{printed}({', '.join(parts)})"
    return name, kind, printed


def schema_from_introspection(payload):
    """Map an introspection result onto the data-model payload.

    Object types and interfaces are categories. Scalar and enum fields are
    properties. Object-valued fields are relationships. Introspection types
    (names starting with ``__``) are left out. Nothing is filtered by name.
    """
    schema = ((payload or {}).get("data") or {}).get("__schema")
    if not isinstance(schema, dict):
        raise GraphQLCallError("This API did not return an introspection schema.")
    types = [item for item in schema.get("types") or [] if isinstance(item, dict)]
    categories, links = {}, []
    objectish = {"OBJECT", "INTERFACE", "UNION"}
    for item in types:
        name = str(item.get("name") or "")
        kind = item.get("kind")
        if not name or name.startswith("__"):
            continue
        if kind not in ("OBJECT", "INTERFACE", "UNION", "INPUT_OBJECT", "ENUM"):
            continue
        props = {}
        if kind == "ENUM":
            for value in item.get("enumValues") or []:
                if isinstance(value, dict) and value.get("name"):
                    props[str(value["name"])] = "enum"
        fields = item.get("fields") if kind in ("OBJECT", "INTERFACE") else None
        if fields is None and kind == "INPUT_OBJECT":
            fields = item.get("inputFields")
        for field in fields or []:
            if not isinstance(field, dict) or not field.get("name"):
                continue
            target, target_kind, printed = _field_type(field)
            if target_kind in objectish and target and not target.startswith("__"):
                props[field["name"]] = f"-> {target}"
                links.append({"source": name, "type": field["name"], "target": target})
            else:
                props[field["name"]] = printed
        if kind == "UNION":
            for possible in item.get("possibleTypes") or []:
                possible_name = (possible or {}).get("name")
                if possible_name and not str(possible_name).startswith("__"):
                    links.append({"source": name, "type": "possibleType", "target": possible_name})
                    props.setdefault("possibleType", f"-> {possible_name}")
        categories[name] = {"count": None, "labels": [name], "props": props}
    for link in links:
        categories.setdefault(link["target"], {"count": None, "labels": [link["target"]], "props": {}})
    rel_counts = {}
    for link in links:
        rel_counts[link["type"]] = rel_counts.get(link["type"], 0) + 1
    return {
        "labels": {name: None for name in categories},
        "types": rel_counts,
        "categories": categories,
        "links": sorted(links, key=lambda item: (item["source"], item["type"], item["target"])),
        "query": (schema.get("queryType") or {}).get("name"),
        "mutation": (schema.get("mutationType") or {}).get("name"),
    }


def mutation_field_names(payload):
    schema = ((payload or {}).get("data") or {}).get("__schema") or {}
    mutation = (schema.get("mutationType") or {}).get("name")
    if not mutation:
        return set()
    for item in schema.get("types") or []:
        if isinstance(item, dict) and item.get("name") == mutation:
            return {field.get("name") for field in item.get("fields") or [] if isinstance(field, dict) and field.get("name")}
    return set()


def _named_ref(type_ref):
    """The innermost {kind, name} behind NON_NULL and LIST wrappers."""
    node = type_ref if isinstance(type_ref, dict) else {}
    while isinstance(node, dict) and node.get("kind") in ("NON_NULL", "LIST"):
        node = node.get("ofType") or {}
    return node if isinstance(node, dict) else {}


def shape_missing(payload):
    """Type names WizShape walks to that ``payload`` does not hold. None when the roots are unknown.

    The walk is the one WizShape and the request builders take: the query and
    mutation roots, the graphSearch result type, every input object and enum
    reachable from the graphSearch query argument, and the cloudResourcesV2
    filterBy input with the types of the two keys the name search sends
    (search and projectId). The other filterBy fields are never sent, so
    their 30 or so filter types are not fetched. A scalar where needs no
    entry, because its kind is on the type reference.
    """
    schema, types = _schema_types(payload)
    if not isinstance(schema, dict):
        return None
    query_name = (schema.get("queryType") or {}).get("name")
    if not query_name:
        return None
    mutation_name = (schema.get("mutationType") or {}).get("name")
    missing = []

    def need(name):
        if name and name not in types and name not in missing:
            missing.append(name)

    need(query_name)
    need(mutation_name)
    if query_name not in types:
        return missing
    stack = []
    field = _graph_search_field(payload)
    if field:
        result = _named_ref(field.get("type"))
        if result.get("kind") in ("OBJECT", "INTERFACE"):
            need(result.get("name"))
        query_arg = _field_arg(field, "query")
        if isinstance(query_arg, dict):
            stack.append(query_arg.get("type"))
    inventory = _query_field(payload, "cloudResourcesV2")
    filter_arg = _field_arg(inventory, "filterBy") if inventory else None
    if isinstance(filter_arg, dict):
        filter_ref = _named_ref(filter_arg.get("type"))
        if filter_ref.get("kind") == "INPUT_OBJECT":
            need(filter_ref.get("name"))
            for key in ("search", "projectId"):
                spec = _input_fields(types, filter_arg.get("type")).get(key)
                if spec:
                    stack.append(spec.get("type"))
    seen = set()
    while stack:
        node = _named_ref(stack.pop())
        name = node.get("name")
        if not name or name in seen or node.get("kind") not in ("INPUT_OBJECT", "ENUM"):
            continue
        seen.add(name)
        item = types.get(name)
        if item is None:
            need(name)
            continue
        for spec in item.get("inputFields") or []:
            if isinstance(spec, dict):
                stack.append(spec.get("type"))
    return missing


def _probe_entries(result, found):
    """Add each aliased __type answer to ``found``. A null answer is skipped."""
    data = (result or {}).get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return
    for alias, item in data.items():
        if alias == "__schema" or not isinstance(item, dict) or not item.get("name"):
            continue
        entry = dict(item)
        for key in ("fields", "inputFields", "enumValues", "possibleTypes"):
            entry.setdefault(key, None)
        found[entry["name"]] = entry


def _synthetic_payload(roots, found):
    return {"data": {"__schema": {
        "queryType": roots.get("queryType"),
        "mutationType": roots.get("mutationType"),
        "types": list(found.values()),
    }}}


# Errors that the full introspection would hit too. The probe does not fall
# back on these, so a rate limit is not answered with an 11 MB request.
_NO_FALLBACK = (RateLimited, TokenExpired, GraphQLConfigError)
# A GraphQL error answer comes with one of these statuses. A server that
# refuses __type answers this way, so only this answer falls back.
_REFUSED_STATUS = (200, 400, 422)
# A failed shape load is kept this long, so each call does not send it again.
SHAPE_FAIL_TTL = 30.0
# The longest read timeout that GRAPHQL_SCHEMA_TIMEOUT can set.
MAX_SCHEMA_TIMEOUT = 600.0


def _probe_refused(exc):
    """True when the server answered the probe with a GraphQL error.

    A timeout, an HTTP 401 or 5xx, and an answer that is not JSON are not
    refusals. The full schema is about 27 times larger and would fail the
    same way, so those errors are raised.
    """
    if isinstance(exc, _NO_FALLBACK):
        return False
    if isinstance(exc, GraphQLCallError):
        return getattr(exc, "status", None) in _REFUSED_STATUS
    return True


def _fresh_error(exc):
    """A new exception like ``exc``. A kept failure is raised again without a shared traceback."""
    try:
        copy = type(exc)(*exc.args)
    except Exception:  # noqa: BLE001
        return exc
    copy.__dict__.update(getattr(exc, "__dict__", {}))
    return copy


def load_wiz_shape(client, sent=None):
    """The introspection payload a WizShape needs, from __type queries when the server allows them.

    Returns ``(payload, kind)``. kind is "probe" for a payload built from
    __type answers, "answer" when the server answered the probe with a full
    __schema (the gx-graphql fixture does), and "full" for the full
    INTROSPECTION fallback. Each round is cached for schema_ttl. A type that
    the walk needs and the first round did not name is fetched in at most
    SHAPE_ROUNDS more rounds. A GraphQL error answer to the probe, or a type
    still missing after that, falls back to full introspection. Any other
    error is raised (a rate limit, an expired token, a missing credential, a
    timeout, an HTTP 401 or 5xx), because the fallback would fail the same
    way. The caller keeps the fallback payload, so the result cache does not
    hold it. ``sent`` collects each document, so a refresh can drop them.
    The mutation guard learns the mutation field names from the result.
    """
    sent = sent if sent is not None else []
    payload, kind = _load_wiz_shape(client, sent)
    fields = mutation_field_names(payload)
    if fields:
        client.mutation_fields = fields
    return payload, kind


def _load_wiz_shape(client, sent):
    ttl = client.schema_ttl
    try:
        sent.append(SHAPE_QUERY)
        first = client.execute(SHAPE_QUERY, {}, cache_ttl=ttl)
        schema = ((first or {}).get("data") or {}).get("__schema")
        if isinstance(schema, dict) and isinstance(schema.get("types"), list):
            return first, "answer"
        roots = schema if isinstance(schema, dict) else {}
        found = {}
        _probe_entries(first, found)
        payload = _synthetic_payload(roots, found)
        rounds = 0
        while True:
            missing = shape_missing(payload)
            if missing is None:
                payload = None
                break
            if not missing:
                break
            if rounds >= SHAPE_ROUNDS or not all(_NAME_RE.fullmatch(name) for name in missing):
                payload = None
                break
            rounds += 1
            mutation = (roots.get("mutationType") or {}).get("name")
            names = [name for name in missing if name != mutation]
            document = shape_document(names, mutation if mutation in missing else None, roots=False)
            sent.append(document)
            extra = client.execute(document, {}, cache_ttl=ttl)
            _probe_entries(extra, found)
            payload = _synthetic_payload(roots, found)
    except Exception as exc:  # noqa: BLE001
        if not _probe_refused(exc):
            raise
        diagnostics.record("explorer", f"The Wiz shape probe failed. Reading the full schema. ({client._scrub(exc)[:160]})")
        payload = None
    if payload is not None:
        return payload, "probe"
    sent.append(INTROSPECTION)
    full = client.execute(INTROSPECTION, {}, cache_ttl=0, timeout=client.schema_timeout)
    schema = ((full or {}).get("data") or {}).get("__schema")
    if not isinstance(schema, dict):
        raise GraphQLCallError("This API did not return an introspection schema.")
    return full, "full"


def _seconds(raw, default, ceiling=MAX_SCHEMA_TIMEOUT):
    """A positive, finite number of seconds from the environment, at most ceiling, or the default."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value <= 0:
        return float(default)
    return min(value, float(ceiling))


def _gap_seconds(raw, default):
    """Seconds between two background schema rebuilds. Zero is allowed."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value < 0:
        return float(default)
    return min(value, 3600.0)


def _truncation(message, reasons, **extra):
    reasons = [reason for reason in reasons if reason]
    out = {"truncated": bool(reasons), "message": message if reasons else None, "reasons": reasons}
    out.update(extra)
    return out


class _ExpiryCache:
    def __init__(self, size=256):
        self.size = size
        self._items = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires, value = item
            if expires < time.time():
                self._items.pop(key, None)
                return None
            return value

    def put(self, key, value, ttl):
        if ttl <= 0:
            return value
        with self._lock:
            if len(self._items) >= self.size:
                self._items.pop(next(iter(self._items)))
            self._items[key] = (time.time() + ttl, value)
        return value

    def drop(self, key):
        with self._lock:
            self._items.pop(key, None)


class _Slot:
    def __init__(self):
        self.event = threading.Event()
        self.value = None
        self.error = None

    def ok(self, value):
        self.value = value
        self.event.set()

    def fail(self, error):
        self.error = error
        self.event.set()

    def wait(self, timeout):
        if not self.event.wait(timeout):
            raise GraphQLCallError("GraphQL request timed out while waiting for an in-flight query.")
        if self.error:
            raise self.error
        return self.value


class GraphQLClient:
    """POST queries to one endpoint. Mutations never leave this process."""

    def __init__(self, endpoint, token_env="GRAPHQL_TOKEN", *, token=None, opener=None,
                 max_inflight=4, max_retries=3, backoff=0.5, result_ttl=60, schema_ttl=600,
                 timeout=30, schema_timeout=90, sleep=None, client_id=None, client_secret=None,
                 auth_url=None):
        self.endpoint = endpoint
        self.token_env = token_env or "GRAPHQL_TOKEN"
        self.client_id = _client_id(client_id)
        self.auth_url = str(auth_url or "").strip()
        self._fixed_token = token
        # None reads GRAPHQL_CLIENT_SECRET at request time. "" means there is none.
        self._fixed_secret = client_secret
        self._minted = ""
        # One thread mints at a time. The others use the token it gets.
        self._mint_lock = threading.Lock()
        self._noted = False
        self._opener = opener or urllib.request.urlopen
        self.max_retries = max(0, int(max_retries))
        self.backoff = float(backoff)
        self.result_ttl = float(result_ttl)
        self.schema_ttl = float(schema_ttl)
        self.timeout = float(timeout)
        # Full introspection is about 11 MB on a Wiz tenant and takes 18-23 s.
        # It gets this longer read timeout. Every other call keeps timeout.
        self.schema_timeout = float(schema_timeout)
        self._sleep = sleep or time.sleep
        self.max_inflight = max(1, int(max_inflight))
        self._sem = threading.BoundedSemaphore(self.max_inflight)
        self.cache = _ExpiryCache()
        self._lock = threading.Lock()
        self._inflight = {}
        self.mutation_fields = set()
        # After HTTP 429 every thread waits until this time before it sends.
        self._calm_until = 0.0

    def _token(self):
        if self._fixed_token is not None:
            value = self._fixed_token
        else:
            value = os.environ.get(self.token_env)
        if value is None or value == "":
            raise GraphQLConfigError(f"{self.token_env} is not set")
        return value

    def _secret_material(self):
        values = [self._minted, self._fixed_token, self._fixed_secret, self.client_id]
        values.append(os.environ.get("GRAPHQL_CLIENT_SECRET"))
        values.append(os.environ.get(self.token_env))
        return [value for value in values if value]

    def _scrub(self, text):
        text = str(text or "")
        secrets = []
        client = self.client_id or ""
        for secret in self._secret_material():
            if not secret or secret == client:
                continue
            diagnostics.note_secret(secret)
            secrets.append(secret)
        text = diagnostics.redact(text, secrets)
        # The client id stays on the connection form. Error text still hides it.
        if len(client) >= 4 and client in text:
            text = text.replace(client, "[redacted]")
        return text[:500]

    def _note(self, message):
        if self._noted:
            return
        self._noted = True
        diagnostics.record("explorer", self._scrub(message))

    def _oauth_parts(self):
        client_id = self.client_id or _client_id(os.environ.get("GRAPHQL_CLIENT_ID"))
        if self._fixed_secret is None:
            secret = os.environ.get("GRAPHQL_CLIENT_SECRET") or ""
        else:
            secret = self._fixed_secret or ""
        secret = str(secret)
        if "\n" in secret or "\r" in secret:
            secret = ""
        url = self.auth_url or (os.environ.get("GRAPHQL_AUTH_URL") or "").strip() or wiz_auth_url(self.endpoint)
        return client_id, secret, url

    def _use_oauth(self):
        client_id, secret, url = self._oauth_parts()
        return bool(client_id and secret and url)

    def cache_principal(self):
        """A one-way id of the credential, for a schema cache key.

        The client id is always part of it. A static bearer token is part of it
        when no client secret mints the token. Only the hash leaves this method.
        """
        client_id = self.client_id or _client_id(os.environ.get("GRAPHQL_CLIENT_ID")) or ""
        parts = ["client", client_id]
        if not self._use_oauth():
            try:
                token = self._token()
            except GraphQLConfigError:
                token = ""
            parts += ["token", token]
        return principal_hash("|".join(parts))

    def _mint(self):
        """Trade the client id and secret for an access token. The request is not logged."""
        client_id, secret, url = self._oauth_parts()
        if not (client_id and secret and url):
            raise GraphQLConfigError(
                "A Wiz client id, client secret, and auth URL are required to request an access token.")
        form = urlencode({
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "audience": "wiz-api",
        }).encode()
        request = urllib.request.Request(
            url, data=form, method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            })
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (TimeoutError, socket.timeout) as exc:
            diagnostics.record("explorer", f"Wiz auth did not answer within {self.timeout:.0f} s.")
            raise GraphQLCallError(f"Wiz auth did not answer within {self.timeout:.0f} s.") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                diagnostics.record("explorer", f"Wiz auth did not answer within {self.timeout:.0f} s.")
                raise GraphQLCallError(f"Wiz auth did not answer within {self.timeout:.0f} s.") from exc
            diagnostics.record("explorer", f"Wiz auth failed: {self._scrub(exc.reason or exc)}")
            raise GraphQLCallError(f"Wiz auth failed: {self._scrub(exc.reason or exc)}") from exc
        if status >= 400:
            diagnostics.record("explorer", f"Wiz auth failed (HTTP {status}).")
            raise GraphQLCallError(f"Wiz auth failed (HTTP {status}).")
        try:
            payload = json.loads(raw.decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            diagnostics.record("explorer", "Wiz auth returned a body that was not JSON.")
            raise GraphQLCallError("Wiz auth returned a body that was not JSON.")
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            diagnostics.record("explorer", "Wiz auth returned no access token.")
            raise GraphQLCallError("Wiz auth returned no access token.")
        self._minted = token
        diagnostics.record("explorer", "Wiz auth returned an access token.")
        return token

    def _bearer(self):
        if self._minted:
            return self._minted
        if self._use_oauth():
            self._note("GraphQL uses an access token from the Wiz client id and secret.")
            with self._mint_lock:
                if self._minted:
                    return self._minted
                return self._mint()
        self._note("GraphQL uses the saved bearer token.")
        return self._token()

    def _remint(self, stale):
        """Mint a new token after HTTP 401 on ``stale``. A token another thread minted is used as it is."""
        with self._mint_lock:
            if self._minted and self._minted != stale:
                return self._minted
            self._minted = ""
            diagnostics.record("explorer", "GraphQL HTTP 401. Requesting a new access token.")
            return self._mint()

    def _key(self, document, variables):
        return document, json.dumps(variables or {}, sort_keys=True, default=str)

    def execute(self, document, variables=None, *, cache_ttl=None, timeout=None):
        """Send one query. ``timeout`` overrides the read timeout for this call only."""
        assert_query_only(document)
        if self.mutation_fields:
            found = selection_names(document, roots_only=True) & self.mutation_fields
            if found:
                shown = ", ".join(sorted(found)[:5])
                raise QueryRejected(f"Read-only explorer: a GraphQL mutation is not sent ({shown}).")
        variables = variables or {}
        key = self._key(document, variables)
        ttl = self.result_ttl if cache_ttl is None else cache_ttl
        if ttl:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        with self._lock:
            slot = self._inflight.get(key)
            if slot is None:
                slot = _Slot()
                self._inflight[key] = slot
                leader = True
            else:
                leader = False
        wait = float(timeout) if timeout else self.timeout
        if not leader:
            return slot.wait(wait * (self.max_retries + 1) + 5)
        try:
            value = self._fetch(document, variables, timeout)
        except Exception as exc:
            slot.fail(exc)
            with self._lock:
                self._inflight.pop(key, None)
            raise
        if isinstance(value, dict):
            fields = mutation_field_names(value)
            if fields:
                self.mutation_fields = fields
        if ttl:
            self.cache.put(key, value, ttl)
        slot.ok(value)
        with self._lock:
            self._inflight.pop(key, None)
        return value

    def _log_query(self, document, variables, status, payload, error):
        """Record the document that was sent. The bearer and the client secret are not."""
        client = self.client_id or ""
        for secret in self._secret_material():
            if secret and secret != client:
                diagnostics.note_secret(secret)
        text = (document or "").strip()
        label = f"HTTP {status}" if status else "no response"
        if "__type(" in text:
            outcome = f"{label}: {error}" if error else label
            diagnostics.record("query", f"GraphQL shape probe -> {outcome}")
            return
        if "__schema" in text:
            outcome = f"{label}: {error}" if error else label
            diagnostics.record("query", f"GraphQL introspection -> {outcome}")
            return
        if text.replace(" ", "") == "{__typename}":
            outcome = f"{label}: {error}" if error else label
            diagnostics.record("query", f"GraphQL __typename -> {outcome}")
            return
        match = re.match(r"(?:query|mutation|subscription)\s+([A-Za-z_][A-Za-z0-9_]*)", text)
        if match:
            name = match.group(1)
        elif "graphSearch" in text:
            name = "graphSearch"
        else:
            name = "query"
        try:
            shown = json.dumps(variables or {}, sort_keys=True, default=str)
        except TypeError:
            shown = "{}"
        brief = _graph_search_brief(payload)
        if error:
            outcome = f"{label}: {error}"
        elif brief:
            outcome = f"{label}, {brief}"
        else:
            outcome = label
        diagnostics.record("query", f"GraphQL {name}\nvariables: {shown}\n{text}\n-> {outcome}")

    def _fetch(self, document, variables, timeout=None):
        body = json.dumps({"query": document, "variables": variables}).encode()
        last = RateLimited("The GraphQL API asked us to slow down (HTTP 429).")
        attempt = 0
        auth_tries = 0
        while True:
            token = self._bearer()
            calm = self._calm_until - time.time()
            if calm > 0:
                self._sleep(min(calm, 30))
            self._sem.acquire()
            try:
                try:
                    status, headers, raw = self._send(body, timeout, token)
                except GraphQLCallError as exc:
                    self._log_query(document, variables, None, None, exc)
                    raise
            finally:
                self._sem.release()
            if status == 401:
                detail = self._refusal(status, raw)
                diagnostics.record("explorer", detail)
                self._log_query(document, variables, status, None, detail)
                if auth_tries < 1 and self._use_oauth():
                    auth_tries += 1
                    self._remint(token)
                    continue
                if _token_is_expired(detail):
                    raise TokenExpired("token expired")
                raise GraphQLCallError(detail, status)
            if status == 429:
                delay = self._retry_delay(headers, attempt)
                # One backoff for every thread, so parallel calls do not resend inside the window.
                # The last 429 sets it too, so the rebuild gate and later calls wait it out.
                with self._lock:
                    self._calm_until = max(self._calm_until, time.time() + delay)
                if attempt >= self.max_retries:
                    self._log_query(document, variables, status, None, last)
                    raise last
                self._sleep(delay)
                attempt += 1
                continue
            payload = self._payload(raw)
            if status >= 400:
                message = self._scrub(_error_text(payload) or f"GraphQL request failed (HTTP {status}).")
                diagnostics.record("explorer", message)
                self._log_query(document, variables, status, payload, message)
                raise GraphQLCallError(message, status)
            if isinstance(payload, dict) and payload.get("errors") and payload.get("data") is None:
                message = self._scrub(_error_text(payload) or "GraphQL request failed.")
                diagnostics.record("explorer", message)
                self._log_query(document, variables, status, payload, message)
                if _token_is_expired(message):
                    raise TokenExpired("token expired")
                raise GraphQLCallError(message, status)
            self._log_query(document, variables, status, payload, None)
            return payload

    def _refusal(self, status, raw):
        message = ""
        try:
            payload = self._payload(raw)
        except GraphQLCallError:
            payload = None
        if isinstance(payload, dict):
            message = _error_text(payload) or str(payload.get("message") or "")
        if not message:
            message = f"The GraphQL API refused the request (HTTP {status})."
        else:
            message = f"The GraphQL API refused the request (HTTP {status}): {message}"
        return self._scrub(message)

    def _send(self, body, timeout=None, token=None):
        token = token or self._bearer()
        wait = float(timeout) if timeout else self.timeout
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
        try:
            with self._opener(request, timeout=wait) as response:
                status = getattr(response, "status", None) or response.getcode()
                return status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
        except (TimeoutError, socket.timeout) as exc:
            raise QueryTimeout(self._timeout_message(timeout)) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise QueryTimeout(self._timeout_message(timeout)) from exc
            raise GraphQLCallError(self._scrub(exc.reason or exc)) from exc

    def _timeout_message(self, timeout=None):
        if timeout:
            return f"The GraphQL schema did not arrive within {float(timeout):.0f} s."
        return (f"Wiz did not answer within {self.timeout:.0f} s. "
                "Choose an entity type or a category to narrow the search.")

    def _payload(self, raw):
        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GraphQLCallError("GraphQL response was not JSON.") from exc

    def _retry_delay(self, headers, attempt):
        raw = None
        if headers is not None:
            getter = getattr(headers, "get", None)
            raw = getter("Retry-After") if getter else None
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            delay = self.backoff * (2 ** attempt)
        return min(max(delay, 0), 30)


def _error_text(payload):
    if not isinstance(payload, dict):
        return ""
    parts = []
    for item in payload.get("errors") or []:
        if isinstance(item, dict) and item.get("message"):
            parts.append(str(item["message"]))
    return "; ".join(parts)


def _token_is_expired(message):
    text = str(message or "").lower()
    return (
        "token expired" in text
        or "token has expired" in text
        or "token is expired" in text
        or "jwt expired" in text
    )


def _page_size(body, limit):
    body = body or {}
    budget = body.get("budget") or {}
    on_screen = body.get("on_screen") or {}
    try:
        cap = int(budget.get("expand_limit") or limit or 50)
    except (TypeError, ValueError):
        cap = 50
    cap = max(1, min(cap, 200))
    try:
        room = int(budget.get("nodes") or cap) - int(on_screen.get("nodes") or 0)
    except (TypeError, ValueError):
        room = cap
    if room <= 0:
        return 0
    try:
        asked = int(limit) if limit is not None else cap
    except (TypeError, ValueError):
        asked = cap
    return max(1, min(asked, cap, room))


def _entity_node(entity, full):
    kind = str(entity.get("type") or "Entity")
    name = entity.get("name")
    props = {"name": name, "type": kind}
    partial = not full
    if full:
        raw = entity.get("properties")
        if isinstance(raw, dict):
            props.update({str(key): value for key, value in raw.items()})
        elif raw is not None:
            props["properties"] = raw
        if name and not props.get("name"):
            props["name"] = name
        props.setdefault("type", kind)
    return {
        "id": str(entity.get("id")),
        "labels": [kind],
        "category": kind,
        "props": props,
        "degree": None,
        "partial": partial,
    }


def _graph_search_brief(payload):
    """Counts only. Entity properties stay out of the log."""
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    result = (data or {}).get("graphSearch") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return ""
    parts = []
    total = result.get("totalCount")
    if isinstance(total, int) and not isinstance(total, bool):
        parts.append(f"totalCount {total}")
    if result.get("maxCountReached"):
        parts.append("maxCountReached")
    count = 0
    for node in result.get("nodes") or []:
        count += len((node or {}).get("entities") or [])
    parts.append(f"{count} entities")
    page = result.get("pageInfo") or {}
    if isinstance(page, dict) and page.get("hasNextPage"):
        parts.append("hasNextPage")
    return ", ".join(parts)


def _graph_search(payload, slots=False):
    """Paths, totalCount, hasNextPage, and endCursor from one graphSearch page.

    A path drops null entities by default. slots=True keeps each path as
    the tenant sent it, so a null optional entity holds its slot.
    """
    data = (payload or {}).get("data") or {}
    result = data.get("graphSearch") or {}
    paths = []
    for node in result.get("nodes") or []:
        raw = (node or {}).get("entities") or []
        entities = [entity for entity in raw if isinstance(entity, dict) and entity.get("id")]
        if entities:
            paths.append([entity if isinstance(entity, dict) and entity.get("id") else None for entity in raw]
                         if slots else entities)
    page = result.get("pageInfo") or {}
    return paths, result.get("totalCount"), bool(page.get("hasNextPage")), page.get("endCursor")


INDIRECT_EDGE = "related (via unselected)"


def _path_edges(plan, path):
    """The edges one result path gives under a slot plan, or None when the path does not fit.

    A slot with a parent gives one edge parent -> child when both entities
    are present. A null slot (a missing optional entity) gives no edge.
    """
    if plan is None or len(path) != len(plan):
        return None
    out = []
    for index, slot in enumerate(plan):
        parent = slot["parent"]
        if parent is None:
            continue
        if not isinstance(parent, int) or not 0 <= parent < index:
            return None
        source, target = path[parent], path[index]
        if source is None or target is None:
            continue
        label = INDIRECT_EDGE if slot["indirect"] else "related"
        out.append((str(source.get("id")), label, str(target.get("id")),
                    {"relationship": list(slot["relationship"]) or [ANY_TYPE], "indirect": bool(slot["indirect"])}))
    return out


class WizAdapter:
    """Wiz graphSearch. One cursor page is fetched at a time.

    On a tenant schema the outer entity is the anchor, matched by _vertexID.
    ``with`` selects the neighbor, and the relationship type is ANY unless the
    caller names one. The legacy fixture keeps the older shape, where ``with``
    holds the anchor. The canvas edge type is ``related`` because the result
    is the entity, not the relationship name.
    """

    def __init__(self, source):
        self.source = source
        self._lock = threading.Lock()
        # The documents the last shape load sent. A rebuild drops their cached answers.
        self._sent = ()
        # (payload, WizShape). The shape is built once per payload object.
        self._built = None
        # (expires, error). A failed load is raised again until it expires.
        self._failed = None
        self.kind = ""

    def entity_total(self):
        """Read totalCount with one graphSearch. None when the schema has no arguments.

        A failure is the caller's to catch. maxCountReached means the number is a
        floor, not the size of the tenant.
        """
        built = entity_count_request(self._shape(), self.source.project_id)
        if not built:
            return None
        document, variables = built
        payload = self.source.client.execute(
            document, variables, cache_ttl=self.source.client.schema_ttl)
        data = ((payload or {}).get("data") or {}).get("graphSearch") or {}
        total = data.get("totalCount")
        if isinstance(total, bool) or not isinstance(total, int):
            if isinstance(total, float) and total.is_integer():
                total = int(total)
            else:
                return None
        return {"count": int(total), "floor": bool(data.get("maxCountReached"))}

    def _schema_payload(self, refresh=False):
        """The payload WizShape reads. The source's shape slot keeps it in memory and on disk.

        A restart reads the saved shape and sends no probe. A refresh loads it
        again and rewrites the saved copy. ``self.kind`` names how the payload
        was read (see load_wiz_shape). A failed load is kept for
        SHAPE_FAIL_TTL, so status, search, and each page do not send the probe
        again on a tenant that is slow or refuses the call. A refresh drops the
        kept failure too.
        """
        slot = self.source.slots["shape"]
        if refresh:
            with self._lock:
                self._failed = None
        else:
            held = slot.peek()
            if held is not None:
                return held["payload"]
            with self._lock:
                failed = self._failed
            if failed and failed[0] > time.time():
                raise _fresh_error(failed[1])
        try:
            value = slot.get(refresh=refresh)
        except GraphQLConfigError:
            # A missing credential costs no request, so it is not kept.
            raise
        except Exception as exc:
            with self._lock:
                self._failed = (time.time() + SHAPE_FAIL_TTL, exc)
            raise
        return value["payload"]

    def build_shape(self):
        """Load the shape from the tenant. The cached answers of the last load are dropped first."""
        client = self.source.client
        for document in (self._sent or (SHAPE_QUERY, INTROSPECTION)):
            client.cache.drop(client._key(document, {}))
        sent = []
        payload, kind = load_wiz_shape(client, sent)
        self._sent = tuple(sent)
        return {"payload": payload, "kind": kind}

    def adopt_shape(self, value):
        """A shape read from the cache sets the kind and the mutation guard, as a load does."""
        if not isinstance(value, dict):
            return
        self.kind = str(value.get("kind") or "")
        fields = mutation_field_names(value.get("payload"))
        if fields:
            self.source.client.mutation_fields = fields

    def forget(self):
        """Drop the kept failure and the cached probe answers."""
        client = self.source.client
        with self._lock:
            self._failed = None
        for document in (self._sent or (SHAPE_QUERY, INTROSPECTION)):
            client.cache.drop(client._key(document, {}))

    def _shape(self, payload=None):
        """The WizShape for the current payload. It is built once per payload object."""
        payload = self._schema_payload() if payload is None else payload
        built = self._built
        if built is not None and built[0] is payload:
            return built[1]
        shape = WizShape(payload)
        self._built = (payload, shape)
        return shape

    def _run(self, query, first, after, full, shape=None):
        """Send graphSearch with the fields this schema allows.

        A tenant schema gets the query object as a $query variable of the type
        introspection prints. The value is cleaned and checked first, and a
        shape that times out on the tenant is refused before it is sent. An
        introspection result with no graphSearch arguments keeps the legacy
        document, so the local fixture still sees GraphEntityQueryInput!.
        A caller that already holds the shape passes it, so each page does not
        look it up again.
        """
        shape = shape or self._shape()
        query_type, cleaned, project = conform_wiz_query(shape, query)
        if not shape.legacy:
            reason = _slow_shape(cleaned)
            if reason:
                raise ValueError(reason)
            document, variables = graph_search_request(
                shape, query_type, cleaned, full, self.source.project_id, first, after)
        else:
            document = DETAIL_QUERY if full else SEARCH_QUERY
            variables = {
                "query": cleaned if isinstance(cleaned, dict) else {},
                "first": max(1, min(int(first), 200)),
                "after": after,
            }
            if project:
                variables["projectId"] = self.source.project_id or "*"
        return self.source.client.execute(document, variables)

    def _text_page(self, shape, text, first, after):
        """One cloudResourcesV2 page for a name search with no entity type.

        Its ids are the graph _vertexID values. projectId goes into filterBy
        only when that input publishes it and the source is scoped.
        """
        field = shape.text_def
        filter_type = shape.text_filter_arg.get("type")
        raw = {"search": text}
        project = self.source.project_id or "*"
        if project != "*" and "projectId" in _input_fields(shape.types, filter_type):
            raw["projectId"] = [project]
        decls, args = [], []
        variables = {"filterBy": _clean_value(shape.types, filter_type, raw)}
        decls.append(f"$filterBy: {_print_type(filter_type) or 'CloudResourceV2Filters'}")
        args.append("filterBy: $filterBy")
        for name, value, fallback in (("first", max(1, min(int(first), 200)), "Int"), ("after", after, "String")):
            arg = _field_arg(field, name)
            if not arg:
                continue
            decls.append(f"${name}: {_print_type(arg.get('type')) or fallback}")
            args.append(f"{name}: ${name}")
            if value is not None:
                variables[name] = value
        document = (
            f"query CloudResourceSearch({', '.join(decls)}) {{\n"
            f"  cloudResourcesV2({', '.join(args)}) {{\n"
            f"    totalCount\n"
            f"    pageInfo {{ hasNextPage endCursor }}\n"
            f"    nodes {{ id name type nativeType }}\n"
            f"  }}\n"
            f"}}\n"
        )
        payload = self.source.client.execute(document, variables)
        result = ((payload or {}).get("data") or {}).get("cloudResourcesV2") or {}
        paths = []
        for row in result.get("nodes") or []:
            if isinstance(row, dict) and row.get("id"):
                paths.append([{"id": row["id"], "name": row.get("name"), "type": row.get("type")}])
        page = result.get("pageInfo") or {}
        return paths, result.get("totalCount"), bool(page.get("hasNextPage")), page.get("endCursor")

    def _collect(self, query, limit, skip_ids, offset, full, fetch=None, shape=None):
        """Walk cursor pages until ``limit`` new entities, and then stop.

        Pages already fetched are cached by the client, so the next call that
        skips those ids continues at the following cursor instead of pulling
        the whole result. The walk stops after MAX_PAGES even when the API
        still has one. ``fetch`` reads one page from another field.
        """
        if fetch is None:
            def fetch(first, after):
                return _graph_search(self._run(query, first, after, full, shape))
        skip_ids = {str(item) for item in skip_ids or []}
        offset = max(0, int(offset or 0))
        found, seen, skipped = [], set(), 0
        cursor, pages, total, more = None, 0, None, False
        while len(found) < limit and pages < MAX_PAGES:
            paths, page_total, has_next, end = fetch(limit, cursor)
            # cloudResourcesV2 reports totalCount 0 after the first page, so the first total is kept.
            if isinstance(page_total, int) and not isinstance(page_total, bool) and (total is None or page_total > 0):
                total = page_total
            pages += 1
            page_has_more = False
            for entity in (entity for path in paths for entity in path):
                ident = str(entity.get("id"))
                if ident in seen or ident in skip_ids:
                    continue
                if skipped < offset:
                    skipped += 1
                    seen.add(ident)
                    continue
                if len(found) >= limit:
                    page_has_more = True
                    break
                seen.add(ident)
                found.append(entity)
            if len(found) >= limit:
                more = page_has_more or bool(has_next)
                break
            if not has_next or not end or end == cursor:
                more = False
                break
            cursor = end
        else:
            more = True
        return found, more, total

    def search(self, q="", kind="node", category=None, prop=None, value=None, limit=20, offset=0, fuzzy=False):
        """Search entities by name, entity type, and one property.

        On a tenant whose default type is ANY, a name with no entity type goes
        to cloudResourcesV2, because ANY with a name filter times out. A name
        and a property with no entity type are refused for the same reason.
        A scoped source uses cloudResourcesV2 only when its filter takes
        projectId. A property with no entity type is an EQUALS on type ANY,
        which the tenant answers in about a second.
        """
        if kind == "edge":
            return {"edges": []}
        limit = max(1, min(int(limit or 20), 200))
        if category in ("", ANY_TYPE):
            category = None
        text = str(q or "").strip()
        has_prop = bool(prop) and value is not None and str(value) != ""
        shape = self._shape()
        if shape.legacy:
            query = {"select": True}
            if category:
                query["type"] = [category]
            where = {}
            if text:
                where["name"] = {"CONTAINS": [text]}
            if has_prop:
                where[str(prop)] = {"EQUALS": [value]}
            if where:
                query["where"] = where
            found, _, total = self._collect(query, limit, (), offset, full=False, shape=shape)
            return {"nodes": [_entity_node(entity, full=False) for entity in found], "total": total}
        if category:
            shape.entity_type(category)
        if not shape.open_where and has_prop:
            key = shape.id_key() if str(prop) in ("id", "_vertexID") else str(prop)
            if key not in shape.where_fields:
                raise ValueError(f"{prop} is not a filter on this graph search.")
        slow = bool(text) and not category and shape.entity_type(None) == [ANY_TYPE]
        if slow and has_prop:
            raise ValueError("Choose an entity type to combine a name and a property.")
        if slow and inventory_route(shape, self.source.project_id):
            found, _, total = self._collect(
                None, limit, (), offset, full=False,
                fetch=lambda first, after: self._text_page(shape, text, first, after))
            return {"nodes": [_entity_node(entity, full=False) for entity in found], "total": total}
        if slow and shape.text_field:
            raise ValueError("Choose an entity type to search by name. "
                             "The Wiz resource search cannot keep this source's project scope.")
        if slow:
            raise ValueError("Choose an entity type to search by name. A name search across every Wiz type is too slow.")
        query = wiz_search_value(shape, category, text, prop if has_prop else None, value)
        _, cleaned, _ = conform_wiz_query(shape, query)
        if category and not _contains_type(cleaned, category):
            raise ValueError(f"{category} is not a Wiz entity type.")
        if not shape.open_where:
            kept = cleaned.get("where") if isinstance(cleaned.get("where"), dict) else {}
            if text and "name" not in kept:
                raise ValueError("This graph search has no name filter.")
            if has_prop:
                key = shape.id_key() if str(prop) in ("id", "_vertexID") else str(prop)
                if key not in kept:
                    raise ValueError(f"{prop} is not a filter on this graph search.")
        found, _, total = self._collect(cleaned, limit, (), offset, full=False, shape=shape)
        return {"nodes": [_entity_node(entity, full=False) for entity in found], "total": total}

    def nodes(self, ids):
        wanted = [str(item) for item in ids or [] if item]
        if not wanted:
            return {"nodes": []}
        found = []
        shape = self._shape()
        ident = shape.id_key()
        if not ident:
            raise ValueError("This graph search has no id filter.")
        typed = shape.entity_type(None)
        # One page per chunk. The caller asks for the nodes it is opening, not the graph.
        # The id on the canvas is the entity id graphSearch returned. A tenant
        # matches it as _vertexID, because an id filter matches nothing there.
        for start in range(0, min(len(wanted), 200), 50):
            chunk = wanted[start:start + 50]
            query = {"select": True, "where": {ident: {"EQUALS": chunk}}}
            if typed:
                query["type"] = typed
            payload = self._run(query, len(chunk), None, full=True, shape=shape)
            paths, _, _, _ = _graph_search(payload)
            found.extend(entity for path in paths for entity in path if str(entity.get("id")) in set(chunk))
        return {"nodes": [_entity_node(entity, full=True) for entity in found]}

    def _neighbor_query(self, anchor, categories=None, types=None, q=None, shape=None):
        # On a tenant the anchor is the outer entity and `with` is the neighbor.
        # A missing relationship type asks for ANY.
        return wiz_neighbor_value(shape or self._shape(), anchor, categories, types, q)

    def _neighbor_pages(self, anchors, shape):
        """One 20-entity neighbor page per anchor, in anchor order.

        The requests go out at the same time, at most max_inflight at once,
        because each one waits about 400 ms on the tenant. Every query is built
        before the first request, so a refused one sends nothing. The first
        failure in anchor order is raised, and requests not yet started are
        cancelled. After the first failure no other anchor starts, so a rate
        limit does not get one more request for each anchor left.
        """
        queries = [self._neighbor_query(anchor, shape=shape) for anchor in anchors]
        if len(queries) <= 1:
            return [self._run(query, 20, None, False, shape) for query in queries]
        workers = min(len(queries), getattr(self.source.client, "max_inflight", 4))
        stop = threading.Event()

        def page(query):
            # Anchors start in order, so a skipped one comes after the failure that is raised.
            if stop.is_set():
                raise GraphQLCallError("The neighbor summary stopped after an earlier error.")
            try:
                return self._run(query, 20, None, False, shape)
            except Exception:
                stop.set()
                raise

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wiz-neighbors")
        try:
            futures = [pool.submit(page, query) for query in queries]
            return [future.result() for future in futures]
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def neighbor_summary(self, ids, visible):
        visible = {str(item) for item in visible or []}
        categories, types, combos, total, degree = {}, {}, {}, 0, 0
        approximate = False
        anchors = [str(item) for item in ids or []][:20]
        pages = self._neighbor_pages(anchors, self._shape()) if anchors else []
        for anchor, payload in zip(anchors, pages):
            paths, page_total, has_next, _ = _graph_search(payload)
            entities = [entity for path in paths for entity in path if str(entity.get("id")) != anchor]
            page_degree = page_total if isinstance(page_total, int) else len(entities)
            degree += page_degree
            hidden = [entity for entity in entities if str(entity.get("id")) not in visible]
            sampled = len(entities)
            scale = page_degree / sampled if sampled and page_degree > sampled else 1
            if scale > 1 or has_next:
                approximate = True
            grouped = {}
            for entity in hidden:
                kind = str(entity.get("type") or "Entity")
                grouped[kind] = grouped.get(kind, 0) + 1
            for kind, count in grouped.items():
                scaled = max(1, round(count * scale)) if count else 0
                categories[kind] = categories.get(kind, 0) + scaled
                types["related"] = types.get("related", 0) + scaled
                combos.setdefault(kind, {})
                combos[kind]["related"] = combos[kind].get("related", 0) + scaled
                total += scaled
        return {
            "total": total, "categories": categories, "types": types, "combos": combos,
            "approximate": approximate, "degree": degree,
        }

    def expand(self, ids, visible, categories=None, types=None, limit=None, order="neighbors", offset=0, q=None, body=None):
        page = _page_size(body, limit)
        offset = max(0, int(offset or 0))
        if not ids or page <= 0:
            message = None
            reasons = []
            if ids and page <= 0:
                message = "canvas is full"
                reasons = ["node_budget"]
            report = _truncation(message, reasons)
            return {"nodes": [], "edges": [], "offset": offset, "more": False,
                    "truncated": report["truncated"], "truncation": report, "degrees": {}}
        nodes, edges, degrees = [], [], {}
        more = False
        seen = set()
        anchors = [str(item) for item in ids][:20]
        shape = self._shape()
        for anchor in anchors:
            query = self._neighbor_query(anchor, categories, types, q, shape=shape)
            found, page_more, total = self._collect(
                query, page - len(nodes), set(visible or []) | set(ids) | seen, offset, full=False, shape=shape)
            if isinstance(total, int):
                degrees[anchor] = total
            if page_more:
                more = True
            for entity in found:
                ident = str(entity.get("id"))
                if ident in seen or ident == anchor:
                    continue
                seen.add(ident)
                nodes.append(_entity_node(entity, full=False))
                edges.append({
                    "id": f"{anchor}:related:{ident}",
                    "type": "related",
                    "source": anchor,
                    "target": ident,
                    "props": {},
                })
            if len(nodes) >= page:
                break
        reasons, message = [], None
        if more:
            reasons.append("expand_limit")
            message = f"showing {len(nodes):,} of more neighbors"
        if len(ids) > 20:
            reasons.append("expand_limit")
            message = message or "showing neighbors of the first 20 nodes"
        report = _truncation(message, reasons, nodes_returned=len(nodes), edges_returned=len(edges))
        return {
            "nodes": nodes, "edges": edges, "offset": offset, "more": more,
            "truncated": report["truncated"], "truncation": report, "degrees": degrees,
        }

    def edges_between(self, ids, body=None):
        wanted = [str(item) for item in ids or []]
        if len(wanted) < 2:
            report = _truncation(None, [])
            return {"edges": [], "truncated": False, "truncation": report}
        keep = set(wanted)
        edges, seen = [], set()
        capped = wanted[:8]
        shape = self._shape()
        for anchor in capped:
            query = self._neighbor_query(anchor, shape=shape)
            payload = self._run(query, 50, None, False, shape)
            paths, _, _, _ = _graph_search(payload)
            for path in paths:
                for entity in path:
                    ident = str(entity.get("id"))
                    if ident == anchor or ident not in keep:
                        continue
                    edge_id = f"{anchor}:related:{ident}"
                    back = f"{ident}:related:{anchor}"
                    if edge_id in seen or back in seen:
                        continue
                    seen.add(edge_id)
                    edges.append({"id": edge_id, "type": "related", "source": anchor, "target": ident, "props": {}})
        reasons = ["expand_limit"] if len(wanted) > len(capped) else []
        message = "edges were checked for the first 8 nodes" if reasons else None
        report = _truncation(message, reasons, edges_returned=len(edges))
        return {"edges": edges, "truncated": report["truncated"], "truncation": report}


class GenericAdapter:
    """Introspection only. Instance search is not invented for an unknown API."""

    message = "This GraphQL source has no instance adapter. The data model comes from introspection."

    def search(self, *args, **kwargs):
        raise ValueError(self.message)

    def nodes(self, ids):
        raise ValueError(self.message)

    def neighbor_summary(self, ids, visible):
        raise ValueError(self.message)

    def expand(self, *args, **kwargs):
        raise ValueError(self.message)

    def edges_between(self, ids, body=None):
        report = _truncation(None, [])
        return {"edges": [], "truncated": False, "truncation": report}


# One rebuild gate per endpoint for the whole process. A new GraphQLSource for the
# same endpoint shares it, so a Data connection save keeps the rebuild spacing and
# the last 429 cool-down.
_GATES: dict = {}
_GATES_LOCK = threading.Lock()


def _rebuild_gate_for(endpoint):
    with _GATES_LOCK:
        gate = _GATES.get(endpoint)
        if gate is None:
            gate = _GATES[endpoint] = {"lock": threading.Lock(), "next": 0.0, "calm": 0.0}
        return gate


class GraphQLSource:
    """The same calls as the Neo4j source: status, schema, search, nodes, expand."""

    kind = "graphql"

    def __init__(self, spec, client=None, cache=None):
        self.id = spec["id"]
        self.label = spec["label"]
        self.adapter_name = spec.get("adapter") or "generic"
        self.endpoint = spec["endpoint"]
        self.project_id = spec.get("project_id") or "*"
        self.token_env = spec.get("token_env") or "GRAPHQL_TOKEN"
        ttl = float(os.environ.get("CACHE_TTL", 60))
        schema_ttl = float(os.environ.get("SCHEMA_TTL", 600))
        inflight = int(os.environ.get("GRAPHQL_MAX_INFLIGHT", 4))
        schema_timeout = _seconds(os.environ.get("GRAPHQL_SCHEMA_TIMEOUT"), 90)
        auth = spec.get("auth_url") or os.environ.get("GRAPHQL_AUTH_URL") or wiz_auth_url(self.endpoint)
        self.client = client or GraphQLClient(
            self.endpoint, self.token_env, client_id=spec.get("client_id"),
            auth_url=auth, max_inflight=inflight, result_ttl=ttl, schema_ttl=schema_ttl,
            schema_timeout=schema_timeout)
        self.adapter = WizAdapter(self) if self.adapter_name == "wiz" else GenericAdapter()
        # The shape and the full schema live in their own slots, not in the result
        # cache, so result pages cannot evict 11 MB. A source with no cache folder
        # keeps them in memory only.
        self.disk_cache = cache if cache is not None else DiskCache.memory_only()
        # Wiz allows about three schema calls a minute. A background rebuild waits
        # this long after the last one, and waits out a 429 cool-down.
        self.rebuild_gap = _gap_seconds(os.environ.get("GX_CACHE_GAP"), 20.0)
        # The gate lock and the next rebuild time belong to the endpoint, not to this
        # object. A Data connection save makes a new source, and it keeps the spacing.
        self._gate = _rebuild_gate_for(self.endpoint)
        ttl_mem = self.client.schema_ttl
        secrets = self.client._secret_material
        self.slots = {}
        if self.adapter_name == "wiz":
            self.slots["shape"] = Slot(
                self.disk_cache, "graphql-shape", lambda: self._cache_key("graphql-shape"),
                self.adapter.build_shape, secrets=secrets, label=f"{self.label} search shape",
                gate=self._rebuild_gate, on_load=self.adapter.adopt_shape, mem_ttl=ttl_mem)
        self.slots["full"] = Slot(
            self.disk_cache, "graphql-full", lambda: self._cache_key("graphql-full"),
            self._build_full, secrets=secrets, label=f"{self.label} full schema",
            gate=self._rebuild_gate, on_load=self._adopt_full, mem_ttl=ttl_mem)

    def _cache_key(self, kind):
        """The connection a cache entry belongs to. The credential is a hash."""
        return cache_key(kind, endpoint=self.endpoint, adapter=self.adapter_name,
                         project=self.project_id, principal=self.client.cache_principal())

    @property
    def _next_rebuild(self):
        return self._gate["next"]

    @_next_rebuild.setter
    def _next_rebuild(self, value):
        self._gate["next"] = float(value)

    @contextlib.contextmanager
    def _rebuild_gate(self):
        """One background schema rebuild at a time, spaced, and never inside a 429 cool-down."""
        gate = self._gate
        with gate["lock"]:
            while True:
                now = time.time()
                wait = max(gate["next"] - now, self.client._calm_until - now, gate["calm"] - now)
                if wait <= 0:
                    break
                time.sleep(min(wait, 1.0))
            try:
                yield
            finally:
                gate["next"] = time.time() + self.rebuild_gap
                gate["calm"] = max(gate["calm"], self.client._calm_until)

    def _build_full(self):
        self.client.cache.drop(self.client._key(INTROSPECTION, {}))
        return self.client.execute(INTROSPECTION, {}, cache_ttl=0, timeout=self.client.schema_timeout)

    def _adopt_full(self, payload):
        fields = mutation_field_names(payload)
        if fields:
            self.client.mutation_fields = fields

    def cache_slots(self):
        return dict(self.slots)

    def primary_cache(self):
        """The slot GET /api/schema reads."""
        return "shape" if "shape" in self.slots else "full"

    def model_cache(self):
        """The slot the data-model window reads. A probe that held the whole schema is reused."""
        if "shape" in self.slots and self.adapter.kind in ("answer", "full"):
            return "shape"
        return "full"

    def clear_cache(self):
        for slot in self.slots.values():
            slot.clear()
        if isinstance(self.adapter, WizAdapter):
            self.adapter.forget()
        self.client.cache.drop(self.client._key(INTROSPECTION, {}))
        return list(self.slots)

    def status(self, consume_config_created=False):
        host = urlsplit(self.endpoint).netloc
        out = {
            "connected": False,
            "read_only": True,
            "endpoint": self.endpoint,
            "adapter": self.adapter_name,
            "nodes": None,
            "edges": None,
            "server": host,
            "database": self.label,
        }
        try:
            self.client.execute("{ __typename }")
            out["connected"] = True
        except TokenExpired:
            out["error"] = "token expired"
        except GraphQLConfigError as exc:
            out["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            out["error"] = self.client._scrub(exc)[:300]
        if out["connected"] and self.adapter_name == "wiz":
            try:
                total = self.adapter.entity_total()
            except Exception:  # noqa: BLE001
                total = None
            count = total.get("count") if isinstance(total, dict) else None
            if isinstance(count, int) and not isinstance(count, bool):
                out["nodes"] = count
                if total.get("floor"):
                    out["nodes_floor"] = True
        return out

    def schema(self, refresh=False, full=False):
        """The data model and the search hints.

        A Wiz tenant returns the light model by default. It reads only the
        shape probe, so the search fields are set and labels, types,
        categories, and links are empty. GraphQL type names are not canvas
        categories there, because a Wiz entity type such as VIRTUAL_MACHINE
        is. full=True, and every generic source, reads full introspection
        with the longer schema timeout. The legacy shape (the gx-graphql
        fixture) keeps the full model, because its type names are its entity
        types. It reuses the probe answer when that already holds the whole
        schema. A refresh drops only the cache that this call reads.
        """
        wiz = self.adapter_name == "wiz"
        hints = {"entities": [], "filters": [], "filter_mode": "none", "name_search": "typed"}
        payload = None
        if wiz:
            light = self.adapter._schema_payload(refresh=refresh and not full)
            shape = self.adapter._shape(light)
            entities, filters = wiz_search_hints(shape)
            hints = {
                "entities": entities,
                "filters": filters,
                "filter_mode": wiz_filter_mode(shape),
                "name_search": "inventory" if inventory_route(shape, self.project_id) else "typed",
            }
            if not full and not shape.legacy:
                return {"labels": {}, "types": {}, "categories": {}, "links": [], **hints}
            if self.adapter.kind in ("answer", "full") and not (full and refresh):
                payload = light
        if payload is None:
            payload = self._full_schema(refresh)
        model = schema_from_introspection(payload)
        return {
            "labels": model["labels"],
            "types": model["types"],
            "categories": model["categories"],
            "links": model["links"],
            **hints,
        }

    def _full_schema(self, refresh=False):
        """Full introspection from the full slot. A refresh reads it again and saves it."""
        return self.slots["full"].get(refresh=refresh)

    def search(self, q="", kind="node", category=None, prop=None, value=None, limit=20, offset=0, fuzzy=False):
        return self.adapter.search(q, kind, category, prop, value, limit, offset, fuzzy)

    def nodes(self, ids):
        return self.adapter.nodes(ids)

    def neighbor_summary(self, ids, visible):
        return self.adapter.neighbor_summary(ids, visible)

    def expand(self, ids, visible, categories=None, types=None, limit=None, order="neighbors", offset=0, q=None, body=None):
        return self.adapter.expand(ids, visible, categories, types, limit, order, offset, q, body)

    def edges_between(self, ids, body=None):
        return self.adapter.edges_between(ids, body)

    def cypher(self, query, params=None, limit=None, body=None):
        raise ValueError("This source has no Cypher. Switch to Neo4j to run Cypher.")

    def run_document(self, document, variables=None, limit=200):
        """Run one saved graph search and return its entities and their edges.

        graphSearch returns paths. Each path lists the selected levels of the
        query tree, so path_edges_plan reads the tree to place each entity
        under its nearest selected ancestor. An edge crosses an unselected
        level when the label is INDIRECT_EDGE. A tree that cannot be read, or
        a path that does not fit the plan, gives no edges for that path and
        the edges_unknown reason. The entities are never chained in order.
        first is capped at one page of 200. The cursor walks at most
        MAX_PAGES, and limit is how many entities still fit on the canvas.
        A page that sends a cursor sends quick false, because Wiz refuses
        pagination in quick mode. The shape probe runs first, so the mutation
        guard knows the mutation field names before the first saved query.
        """
        if self.adapter_name != "wiz":
            raise ValueError(GenericAdapter.message)
        document = str(document or "")
        assert_query_only(document)
        # A document that cannot be GraphQL is an error, not an empty graph.
        assert_well_formed(document)
        if not self.client.mutation_fields:
            try:
                self.adapter._schema_payload()
            except Exception:  # noqa: BLE001
                # The tenant refuses a mutation field on Query, so the query still goes.
                pass
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 200
        limit = max(0, min(limit, 200 * MAX_PAGES))
        if limit <= 0:
            report = _truncation("canvas is full", ["node_budget"], nodes_returned=0, edges_returned=0)
            return {"nodes": [], "edges": [], "truncated": True, "truncation": report}
        variables = dict(variables or {})
        page_first = None
        if "first" in variables:
            try:
                asked = int(variables["first"])
            except (TypeError, ValueError):
                asked = 200
            page_first = max(1, min(asked, 200))
        can_page = "$after" in document
        try:
            plan = path_edges_plan(graph_search_query(document, variables))
        except (ValueError, TypeError, RecursionError):
            # A tree the plan cannot read gives no edges, not a failed query.
            plan = None
        found, seen = [], set()
        # One edge per pair of entities, in path order. The first direction
        # wins. A direct link replaces an indirect one for the same pair.
        links, unknown = {}, 0
        cursor = variables.get("after")
        pages, total, more = 0, None, False
        while len(found) < limit and pages < MAX_PAGES:
            page_vars = dict(variables)
            if page_first is not None:
                page_vars["first"] = max(1, min(page_first, limit - len(found)))
            if can_page:
                page_vars["after"] = cursor
            # Wiz refuses a cursor in quick mode. A later page sends quick false.
            if page_vars.get("after") is not None and page_vars.get("quick") is True:
                page_vars["quick"] = False
            try:
                payload = self.client.execute(document, page_vars)
            except QueryTimeout as exc:
                raise QueryTimeout(
                    f"The saved query did not finish within {int(self.client.timeout)} s.") from exc
            except GraphQLCallError as exc:
                raise GraphQLCallError(explain_wiz_error(str(exc))) from exc
            paths, page_total, has_next, end = _graph_search(payload, slots=True)
            if isinstance(page_total, int) and not isinstance(page_total, bool):
                total = page_total
            pages += 1
            for path in paths:
                edges = _path_edges(plan, path)
                if edges is None:
                    unknown += 1
                    edges = []
                for source, label, target, props in edges:
                    pair = (min(source, target), max(source, target))
                    held = links.get(pair)
                    if held and not (held[1] == INDIRECT_EDGE and label != INDIRECT_EDGE):
                        continue
                    links[pair] = (source, label, target, props)
            for entity in (entity for path in paths for entity in path if entity is not None):
                ident = str(entity.get("id") or "")
                if not ident or ident in seen:
                    continue
                if len(found) >= limit:
                    more = True
                    break
                seen.add(ident)
                found.append(entity)
            if len(found) >= limit:
                more = more or bool(has_next)
                break
            if not can_page or not has_next or not end or end == cursor:
                more = False
                break
            cursor = end
        else:
            more = True
        # The canvas limit can cut nodes. An edge needs both of its ends.
        edges = [
            {"id": f"{source}:{label}:{target}", "type": label, "source": source, "target": target, "props": props}
            for source, label, target, props in links.values()
            if source in seen and target in seen
        ]
        reasons, notes = [], []
        if more:
            reasons.append("row_limit")
            notes.append(f"query stopped at {len(found):,} of more entities")
        if unknown:
            reasons.append("edges_unknown")
            notes.append("edges are not drawn because the query shape is not known"
                         if plan is None else
                         f"edges are not drawn for {unknown:,} result path{'' if unknown == 1 else 's'}")
        message = "; ".join(notes) or None
        report = _truncation(message, reasons, nodes_returned=len(found), edges_returned=len(edges))
        return {
            "nodes": [_entity_node(entity, full=True) for entity in found],
            "edges": edges,
            "total": total,
            "truncated": report["truncated"],
            "truncation": report,
        }
