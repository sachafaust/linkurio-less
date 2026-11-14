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
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

import diagnostics

TOKEN_KEYS = {"token", "bearer", "authorization", "secret", "password", "access_token"}
ENV_KEYS = (
    "GRAPHQL_ENDPOINT", "GRAPHQL_TOKEN", "GRAPHQL_CLIENT_ID", "GRAPHQL_CLIENT_SECRET",
    "GRAPHQL_AUTH_URL", "GRAPHQL_ADAPTER", "GRAPHQL_LABEL", "GRAPHQL_ID",
    "GRAPHQL_TOKEN_ENV", "GRAPHQL_PROJECT_ID",
)
_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
_ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_SKIP_NAMES = {"query", "mutation", "subscription", "fragment", "on", "true", "false", "null"}
MAX_PAGES = 8
_TOKEN_WARNED = False

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
      inputFields { name type { ...TypeRef } }
      enumValues(includeDeprecated: true) { name }
      possibleTypes { name }
    }
  }
}
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

# Wiz declares graphSearch.query as GraphEntityQueryInput! A nullable $query is
# rejected as the wrong type for that variable. $after stays nullable: the first page sends null.
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


class QueryRejected(ValueError):
    """A mutation or subscription was refused before any request was sent."""


class TokenExpired(Exception):
    """The API said the access token is expired, and a refresh did not help."""


class RateLimited(Exception):
    """The GraphQL API returned HTTP 429 after the backoff was used up."""


class GraphQLCallError(Exception):
    """The GraphQL API answered with an error that is safe to show."""


class GraphQLConfigError(ValueError):
    """The source is configured, but the token or endpoint cannot be used."""


def load_graphql_env():
    """Fill unset GraphQL variables from the gitignored .env. The process environment wins."""
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


def _wiz_host(endpoint):
    host = (urlsplit(str(endpoint or "")).hostname or "").lower()
    return host in ("wiz.io", "wiz.us") or host.endswith(".wiz.io") or host.endswith(".wiz.us")


def wiz_auth_url(endpoint):
    """Auth host for a Wiz GraphQL URL. Empty when the host is not Wiz."""
    host = (urlsplit(str(endpoint or "")).hostname or "").lower()
    if host == "wiz.us" or host.endswith(".wiz.us"):
        return "https://auth.app.wiz.us/oauth/token"
    if host == "wiz.io" or host.endswith(".wiz.io"):
        return "https://auth.app.wiz.io/oauth/token"
    return ""


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
    They do not contain the token.
    """
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


def _skip_string(document, i):
    n = len(document)
    if document.startswith('"""', i):
        end = document.find('"""', i + 3)
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
                 timeout=30, sleep=None, client_id=None, client_secret=None, auth_url=None):
        self.endpoint = endpoint
        self.token_env = token_env or "GRAPHQL_TOKEN"
        self.client_id = _client_id(client_id)
        self.auth_url = str(auth_url or "").strip()
        self._fixed_token = token
        # None reads GRAPHQL_CLIENT_SECRET at request time. "" means there is none.
        self._fixed_secret = client_secret
        self._minted = ""
        self._noted = False
        self._opener = opener or urllib.request.urlopen
        self.max_retries = max(0, int(max_retries))
        self.backoff = float(backoff)
        self.result_ttl = float(result_ttl)
        self.schema_ttl = float(schema_ttl)
        self.timeout = float(timeout)
        self._sleep = sleep or time.sleep
        self._sem = threading.BoundedSemaphore(max(1, int(max_inflight)))
        self.cache = _ExpiryCache()
        self._lock = threading.Lock()
        self._inflight = {}
        self.mutation_fields = set()

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
        except urllib.error.URLError as exc:
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
            return self._mint()
        self._note("GraphQL uses the saved bearer token.")
        return self._token()

    def _key(self, document, variables):
        return document, json.dumps(variables or {}, sort_keys=True, default=str)

    def execute(self, document, variables=None, *, cache_ttl=None):
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
        if not leader:
            return slot.wait(self.timeout * (self.max_retries + 1) + 5)
        try:
            value = self._fetch(document, variables)
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

    def _fetch(self, document, variables):
        body = json.dumps({"query": document, "variables": variables}).encode()
        last = RateLimited("The GraphQL API asked us to slow down (HTTP 429).")
        attempt = 0
        auth_tries = 0
        while True:
            self._sem.acquire()
            try:
                status, headers, raw = self._send(body)
            finally:
                self._sem.release()
            if status == 401:
                detail = self._refusal(status, raw)
                diagnostics.record("explorer", detail)
                if auth_tries < 1 and self._use_oauth():
                    auth_tries += 1
                    self._minted = ""
                    diagnostics.record("explorer", "GraphQL HTTP 401. Requesting a new access token.")
                    self._mint()
                    continue
                if _token_is_expired(detail):
                    raise TokenExpired("token expired")
                raise GraphQLCallError(detail)
            if status == 429:
                if attempt >= self.max_retries:
                    raise last
                self._sleep(self._retry_delay(headers, attempt))
                attempt += 1
                continue
            payload = self._payload(raw)
            if status >= 400:
                message = self._scrub(_error_text(payload) or f"GraphQL request failed (HTTP {status}).")
                diagnostics.record("explorer", message)
                raise GraphQLCallError(message)
            if isinstance(payload, dict) and payload.get("errors") and payload.get("data") is None:
                message = self._scrub(_error_text(payload) or "GraphQL request failed.")
                diagnostics.record("explorer", message)
                if _token_is_expired(message):
                    raise TokenExpired("token expired")
                raise GraphQLCallError(message)
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

    def _send(self, body):
        token = self._bearer()
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                return status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
        except urllib.error.URLError as exc:
            raise GraphQLCallError(self._scrub(exc.reason or exc)) from exc

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


def _graph_search(payload):
    data = (payload or {}).get("data") or {}
    result = data.get("graphSearch") or {}
    paths = []
    for node in result.get("nodes") or []:
        entities = [entity for entity in (node or {}).get("entities") or [] if isinstance(entity, dict) and entity.get("id")]
        if entities:
            paths.append(entities)
    page = result.get("pageInfo") or {}
    return paths, result.get("totalCount"), bool(page.get("hasNextPage")), page.get("endCursor")


class WizAdapter:
    """Wiz graphSearch. One cursor page is fetched at a time.

    Neighbors use the relationship clause the Wiz clients send: the outer
    entity is the neighbor, and ``with`` selects the node on the canvas by id.
    The canvas edge type is ``related`` because the result is the entity, not
    the relationship name.
    """

    def __init__(self, source):
        self.source = source

    def _variables(self, query, first, after=None):
        return {
            "query": query,
            "projectId": self.source.project_id or "*",
            "first": max(1, min(int(first), 200)),
            "after": after,
        }

    def _run(self, query, variables, full):
        document = DETAIL_QUERY if full else SEARCH_QUERY
        return self.source.client.execute(document, variables)

    def _collect(self, query, limit, skip_ids, offset, full):
        """Walk cursor pages until ``limit`` new entities, and then stop.

        Pages already fetched are cached by the client, so the next call that
        skips those ids continues at the following cursor instead of pulling
        the whole result. The walk stops after MAX_PAGES even when the API
        still has one.
        """
        skip_ids = {str(item) for item in skip_ids or []}
        offset = max(0, int(offset or 0))
        found, seen, skipped = [], set(), 0
        cursor, pages, total, more = None, 0, None, False
        while len(found) < limit and pages < MAX_PAGES:
            payload = self._run(query, self._variables(query, limit, cursor), full)
            paths, page_total, has_next, end = _graph_search(payload)
            if isinstance(page_total, int):
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
        if kind == "edge":
            return {"edges": []}
        limit = max(1, min(int(limit or 20), 200))
        query = {"select": True}
        if category:
            query["type"] = [category]
        where = {}
        text = str(q or "").strip()
        if text:
            where["name"] = {"CONTAINS": [text]}
        if prop and value is not None and str(value) != "":
            where[str(prop)] = {"EQUALS": [value]}
        if where:
            query["where"] = where
        found, _, total = self._collect(query, limit, (), offset, full=False)
        return {"nodes": [_entity_node(entity, full=False) for entity in found], "total": total}

    def nodes(self, ids):
        wanted = [str(item) for item in ids or [] if item]
        if not wanted:
            return {"nodes": []}
        found = []
        # One page per chunk. The caller asks for the nodes it is opening, not the graph.
        # The id on the canvas is the entity id graphSearch returned.
        for start in range(0, min(len(wanted), 200), 50):
            chunk = wanted[start:start + 50]
            query = {"select": True, "where": {"id": {"EQUALS": chunk}}}
            payload = self._run(query, self._variables(query, len(chunk)), full=True)
            paths, _, _, _ = _graph_search(payload)
            found.extend(entity for path in paths for entity in path if str(entity.get("id")) in set(chunk))
        return {"nodes": [_entity_node(entity, full=True) for entity in found]}

    def _neighbor_query(self, anchor, categories=None, types=None, q=None):
        # Same shape as the Wiz clients: the neighbor is the outer entity, and
        # `with` names the node already on the canvas. Relationship type is
        # {EXACT: [name]}. A missing type asks for every relationship.
        rel_types = [item for item in (types or []) if item and item != "related"]
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

    def neighbor_summary(self, ids, visible):
        visible = {str(item) for item in visible or []}
        categories, types, combos, total, degree = {}, {}, {}, 0, 0
        approximate = False
        for anchor in [str(item) for item in ids or []][:20]:
            query = self._neighbor_query(anchor)
            payload = self._run(query, self._variables(query, 20), full=False)
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
        for anchor in anchors:
            query = self._neighbor_query(anchor, categories, types, q)
            found, page_more, total = self._collect(query, page - len(nodes), set(visible or []) | set(ids) | seen, offset, full=False)
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
        for anchor in capped:
            query = self._neighbor_query(anchor)
            payload = self._run(query, self._variables(query, 50), full=False)
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


class GraphQLSource:
    """The same calls as the Neo4j source: status, schema, search, nodes, expand."""

    kind = "graphql"

    def __init__(self, spec, client=None):
        self.id = spec["id"]
        self.label = spec["label"]
        self.adapter_name = spec.get("adapter") or "generic"
        self.endpoint = spec["endpoint"]
        self.project_id = spec.get("project_id") or "*"
        self.token_env = spec.get("token_env") or "GRAPHQL_TOKEN"
        ttl = float(os.environ.get("CACHE_TTL", 60))
        schema_ttl = float(os.environ.get("SCHEMA_TTL", 600))
        inflight = int(os.environ.get("GRAPHQL_MAX_INFLIGHT", 4))
        auth = spec.get("auth_url") or os.environ.get("GRAPHQL_AUTH_URL") or wiz_auth_url(self.endpoint)
        self.client = client or GraphQLClient(
            self.endpoint, self.token_env, client_id=spec.get("client_id"),
            auth_url=auth, max_inflight=inflight, result_ttl=ttl, schema_ttl=schema_ttl)
        self.adapter = WizAdapter(self) if self.adapter_name == "wiz" else GenericAdapter()

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
        return out

    def schema(self, refresh=False):
        if refresh:
            self.client.cache.drop(self.client._key(INTROSPECTION, {}))
        payload = self.client.execute(INTROSPECTION, {}, cache_ttl=self.client.schema_ttl)
        model = schema_from_introspection(payload)
        # The data-model window only reads categories, types, and links.
        return {key: model[key] for key in ("labels", "types", "categories", "links")}

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
