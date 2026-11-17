"""Saved queries for each data connection, kept in one gitignored JSON file.

The file is gx.queries.json next to gx.local.json. GX_QUERIES names another
path. Its shape is:

    {"version": 1, "sources": {"<source key>": {
        "label": "...", "kind": "cypher" | "graphql",
        "queries": [...], "hidden_builtins": ["builtin:cartography-queries:0", ...]}}}

The source key is a hash of the connection: the Neo4j URI and database, or
the GraphQL endpoint, adapter, and project. Two connections never share a
list. The file holds query text only. It never holds a password or a token.

Built-in queries come from data/*.json and are never written here. A
built-in id comes from its "id" field or from a slug of its name, never from
its place in the file. Editing a built-in saves a user copy that names the
built-in in "replaces" and hides the built-in, so a later git pull does not
overwrite the edit. Deleting that copy shows the built-in again. Deleting a
built-in hides it. Restore shows it again.

Every change reads the file, changes it, and writes it again under a thread
lock and an fcntl lock on gx.queries.json.lock, so two explorers that share
the file do not lose each other's changes. The write goes to a unique
temporary file with mode 0600 and then os.replace moves it into place, so a
failed write leaves the old file as it was.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

VERSION = 1
BODY_MAX = 100 * 1024
FILE_MAX = 5 * 1024 * 1024
NAME_MAX = 120
DESCRIPTION_MAX = 2000
TAG_MAX = 40
TAGS_MAX = 20
IMPORT_MAX = 1000
KINDS = ("cypher", "graphql")

_LOCK = threading.RLock()
_ID_RE = re.compile(r"q_[0-9a-f]{16}")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_KIND_NAME = {"cypher": "Cypher", "graphql": "GraphQL"}


class QueryStoreError(ValueError):
    """A saved query or the store file was refused. The message is safe to show."""


class QueryNotFound(QueryStoreError):
    """No saved query has that id on this connection."""


def source_key(*parts) -> str:
    """The first 16 hex characters of a sha256 over the connection parts."""
    text = "\n".join(str(part or "") for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_query_id(value) -> bool:
    return isinstance(value, str) and (_ID_RE.fullmatch(value) is not None or value.startswith("builtin:"))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(used) -> str:
    while True:
        qid = "q_" + secrets.token_hex(8)
        if qid not in used:
            return qid


# ---------- one query ---------------------------------------------------------------------

def _text(body, key, limit, label, required=False):
    raw = body.get(key)
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise QueryStoreError(f"The {label} must be text.")
    value = raw.strip()
    if required and not value:
        raise QueryStoreError(f"The query needs a {label}.")
    if len(value) > limit:
        raise QueryStoreError(f"The {label} can have at most {limit} characters.")
    return value


def _tags(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise QueryStoreError("Tags must be a list of short words.")
    out = []
    for item in raw:
        if not isinstance(item, str):
            raise QueryStoreError("Each tag must be text.")
        tag = " ".join(item.split())
        if not tag:
            continue
        if len(tag) > TAG_MAX:
            raise QueryStoreError(f"A tag can have at most {TAG_MAX} characters.")
        if tag not in out:
            out.append(tag)
    if len(out) > TAGS_MAX:
        raise QueryStoreError(f"A query can have at most {TAGS_MAX} tags.")
    return out


def _no_constant(name):
    raise QueryStoreError(f"Variables must be a JSON object. {name} is not a JSON value.")


def _finite(value):
    """True when no number in value is NaN or infinite. JSON has no such number."""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def _variables(raw):
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw, parse_constant=_no_constant)
        except json.JSONDecodeError as exc:
            raise QueryStoreError(f"Variables must be a JSON object. The text is not JSON: {exc.msg}.") from exc
    if not isinstance(raw, dict):
        raise QueryStoreError("Variables must be a JSON object.")
    if not _finite(raw):
        raise QueryStoreError("Variables must be a JSON object. A number is too large or is not a number.")
    return raw


def _kind_of(body, source):
    """The kind the body asks for. It must be the kind the connection runs."""
    want = source["kind"]
    kind = body.get("kind")
    if kind in (None, ""):
        if want == "cypher" and "cypher" not in body and "graphql" in body:
            kind = "graphql"
        elif want == "graphql" and "graphql" not in body and "cypher" in body:
            kind = "cypher"
        else:
            kind = want
    if kind not in KINDS:
        raise QueryStoreError('The query kind must be "cypher" or "graphql".')
    if kind != want:
        raise QueryStoreError(
            f"This connection runs {_KIND_NAME[want]}. A {_KIND_NAME[kind]} query cannot be saved here.")
    return kind


def check_body(source, body, validate):
    """Check the kind, the body, and the variables. Return the clean body fields.

    ``validate(kind, text)`` is the same check the run route uses. It raises
    with the run-time message, so a query is refused at save time with that
    message.
    """
    if not isinstance(body, dict):
        raise QueryStoreError("A saved query must be a JSON object.")
    kind = _kind_of(body, source)
    raw = body.get(kind)
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise QueryStoreError(f"The {_KIND_NAME[kind]} text must be a string.")
    text = raw.strip()
    if not text:
        raise QueryStoreError(f"The query has no {_KIND_NAME[kind]} text.")
    out = {"kind": kind, kind: text}
    size = len(text.encode("utf-8"))
    if kind == "graphql":
        out["variables"] = _variables(body.get("variables"))
        size += len(json.dumps(out["variables"]).encode("utf-8"))
    if size > BODY_MAX:
        raise QueryStoreError("The query body is larger than 100 KB.")
    validate(kind, text)
    return out


def normalize(source, body, validate):
    """A complete user query from a request body. The id and times are added later."""
    if not isinstance(body, dict):
        raise QueryStoreError("A saved query must be a JSON object.")
    out = {
        "name": _text(body, "name", NAME_MAX, "name", required=True),
        "description": _text(body, "description", DESCRIPTION_MAX, "description"),
        "tags": _tags(body.get("tags")),
    }
    out.update(check_body(source, body, validate))
    return out


def _fields(query):
    """The stored fields of a query, without flags."""
    keys = ("id", "name", "description", "tags", "kind", "cypher", "graphql", "variables", "replaces",
            "created", "updated")
    return {key: query[key] for key in keys if key in query}


def public(query, builtin=False):
    out = _fields(query)
    out["builtin"] = builtin
    out["editable"] = not builtin
    return out


def _body_key(query):
    """One hashable key for the kind, the name, the text, and the variables."""
    kind = query.get("kind")
    try:
        variables = json.dumps(query.get("variables", {}), sort_keys=True, default=str)
    except ValueError:
        variables = repr(query.get("variables"))
    return (kind, query.get("name"), query.get(kind), variables)


# ---------- the file ----------------------------------------------------------------------

def _empty():
    return {"version": VERSION, "sources": {}}


def _read(path: Path):
    """The parsed file, or a new document when there is no file.

    A file that is not JSON, or that has another version, raises. The caller
    then leaves it on disk as it is.
    """
    if not path.is_file():
        return _empty()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QueryStoreError(f"could not read {path.name}") from exc
    try:
        # NaN and Infinity are not JSON. An older file can hold them, so they read as null.
        data = json.loads(raw, parse_constant=lambda _name: None)
    except json.JSONDecodeError as exc:
        raise QueryStoreError(f"{path.name} is not valid JSON, so it was left unchanged.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise QueryStoreError(f"{path.name} is not a saved-query file, so it was left unchanged.")
    if data.get("version") != VERSION:
        raise QueryStoreError(
            f"{path.name} has version {data.get('version')!r}. This explorer reads version {VERSION}, "
            "so the file was left unchanged.")
    return data


def _write(path: Path, data):
    text = json.dumps(data, indent=1, sort_keys=False) + "\n"
    encoded = text.encode("utf-8")
    if len(encoded) > FILE_MAX:
        raise QueryStoreError(f"{path.name} would be larger than 5 MB, so the change was not saved.")
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A unique name, so two writers never share one temporary file.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = None
        os.chmod(path, 0o600)
    except OSError as exc:
        raise QueryStoreError(f"could not write {path.name}") from exc
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


@contextlib.contextmanager
def _file_lock(path: Path):
    """Hold an fcntl lock on a file next to the store, so other processes wait."""
    if fcntl is None:
        yield
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise QueryStoreError(f"could not lock {path.name}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _bucket(data, source, create=False):
    sources = data["sources"]
    bucket = sources.get(source["key"])
    if not isinstance(bucket, dict):
        if not create:
            return {"label": source["label"], "kind": source["kind"], "queries": [], "hidden_builtins": []}
        bucket = {}
        sources[source["key"]] = bucket
    bucket["label"] = source["label"]
    bucket["kind"] = source["kind"]
    if not isinstance(bucket.get("queries"), list):
        bucket["queries"] = []
    if not isinstance(bucket.get("hidden_builtins"), list):
        bucket["hidden_builtins"] = []
    bucket["queries"] = [q for q in bucket["queries"] if isinstance(q, dict) and _ID_RE.fullmatch(str(q.get("id")))]
    bucket["hidden_builtins"] = [h for h in bucket["hidden_builtins"] if isinstance(h, str)]
    return bucket


def _change(path, source, fn):
    """Read, change one source's bucket, and write, all under the lock."""
    path = Path(path)
    with _LOCK, _file_lock(path):
        data = _read(path)
        bucket = _bucket(data, source, create=True)
        result, changed = fn(bucket)
        if changed:
            _write(path, data)
        return result


# ---------- the API -----------------------------------------------------------------------

def list_queries(path, source, builtins):
    """Built-ins that are not hidden, then the user queries.

    A file that cannot be read lists the built-ins only. The file stays on disk.
    """
    with _LOCK:
        try:
            data = _read(path)
        except QueryStoreError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)
            data = _empty()
        bucket = _bucket(data, source)
    hidden = set(bucket["hidden_builtins"])
    out = [dict(item) for item in builtins if item["id"] not in hidden]
    out.extend(public(query) for query in bucket["queries"])
    return out


def create(path, source, body, validate, builtins=()):
    """Save a new user query. An unused q_ id and a valid created time are kept, so Undo restores both.

    A body whose "replaces" names a known built-in hides that built-in again.
    That is how Undo brings back a deleted copy of a built-in.
    """
    clean = normalize(source, body, validate)
    known = {item["id"] for item in builtins or ()}
    asked_base = body.get("replaces")
    replaces = asked_base if isinstance(asked_base, str) and asked_base in known else None

    def fn(bucket):
        used = {q["id"] for q in bucket["queries"]}
        asked = body.get("id")
        qid = asked if isinstance(asked, str) and _ID_RE.fullmatch(asked) and asked not in used else _new_id(used)
        now = _now()
        created = body.get("created")
        query = {"id": qid, **clean,
                 "created": created if isinstance(created, str) and _TIME_RE.fullmatch(created) else now,
                 "updated": now}
        if replaces:
            query["replaces"] = replaces
            if replaces not in bucket["hidden_builtins"]:
                bucket["hidden_builtins"].append(replaces)
        bucket["queries"].append(query)
        return public(query), True

    return _change(path, source, fn)


def _builtin(builtins, qid):
    for item in builtins:
        if item["id"] == qid:
            return item
    raise QueryNotFound("No saved query has that id on this connection.")


def update(path, source, qid, body, validate, builtins):
    """Change a user query in place, or save a copy of a built-in and hide the built-in.

    Fields that the body leaves out keep their old value.
    """
    if not isinstance(body, dict):
        raise QueryStoreError("A saved query must be a JSON object.")
    if isinstance(qid, str) and qid.startswith("builtin:"):
        base = _fields(_builtin(builtins, qid))
        clean = normalize(source, {**base, **body}, validate)

        def fn_builtin(bucket):
            used = {q["id"] for q in bucket["queries"]}
            now = _now()
            query = {"id": _new_id(used), **clean, "replaces": qid, "created": now, "updated": now}
            bucket["queries"].append(query)
            if qid not in bucket["hidden_builtins"]:
                bucket["hidden_builtins"].append(qid)
            return {"query": public(query), "hid": qid}, True

        return _change(path, source, fn_builtin)

    def fn(bucket):
        for index, query in enumerate(bucket["queries"]):
            if query["id"] == qid:
                merged = {**query, **body}
                clean = normalize(source, merged, validate)
                saved = {"id": qid, **clean, "created": query.get("created") or _now(), "updated": _now()}
                if isinstance(query.get("replaces"), str):
                    saved["replaces"] = query["replaces"]
                bucket["queries"][index] = saved
                return {"query": public(saved), "hid": None}, True
        raise QueryNotFound("No saved query has that id on this connection.")

    return _change(path, source, fn)


def delete(path, source, qid, builtins):
    """Remove a user query, or hide a built-in. Return what went, so the page can offer Undo."""
    if isinstance(qid, str) and qid.startswith("builtin:"):
        item = _builtin(builtins, qid)

        def fn_hide(bucket):
            if qid in bucket["hidden_builtins"]:
                return {"builtin": True, "query": dict(item)}, False
            bucket["hidden_builtins"].append(qid)
            return {"builtin": True, "query": dict(item)}, True

        return _change(path, source, fn_hide)

    known = {item["id"]: item for item in builtins or ()}

    def fn(bucket):
        for index, query in enumerate(bucket["queries"]):
            if query["id"] == qid:
                del bucket["queries"][index]
                out = {"builtin": False, "query": public(query), "restored_builtin": None}
                # A copy of a built-in hid it. With the last copy gone, the built-in shows again.
                base = query.get("replaces")
                if (isinstance(base, str) and base in known and base in bucket["hidden_builtins"]
                        and not any(q.get("replaces") == base for q in bucket["queries"])):
                    bucket["hidden_builtins"] = [h for h in bucket["hidden_builtins"] if h != base]
                    out["restored_builtin"] = {"id": base, "name": known[base].get("name", "")}
                return out, True
        raise QueryNotFound("No saved query has that id on this connection.")

    return _change(path, source, fn)


def restore_builtins(path, source, builtins, ids=None):
    """Show hidden built-ins again: all of them, or only ``ids``."""
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, str) for i in ids)):
        raise QueryStoreError("ids must be a list of query ids.")
    known = {item["id"] for item in builtins}

    def fn(bucket):
        hidden = bucket["hidden_builtins"]
        wanted = set(hidden) if ids is None else set(ids)
        back = [h for h in hidden if h in wanted]
        if not back:
            return {"restored": 0}, False
        bucket["hidden_builtins"] = [h for h in hidden if h not in wanted]
        return {"restored": sum(1 for h in back if h in known)}, True

    return _change(path, source, fn)


def export_queries(path, source):
    """The user queries of this connection, ready to import on another machine."""
    with _LOCK:
        bucket = _bucket(_read(path), source)
    out = []
    for query in bucket["queries"]:
        item = _fields(query)
        item.pop("id", None)
        item.pop("replaces", None)
        out.append(item)
    return out


def import_queries(path, source, entries, validate):
    """Add every valid entry. Report how many were added and why each other entry was skipped."""
    if isinstance(entries, dict) and isinstance(entries.get("queries"), list):
        entries = entries["queries"]
    if not isinstance(entries, list):
        raise QueryStoreError("An import must be a JSON array of queries.")
    if len(entries) > IMPORT_MAX:
        raise QueryStoreError(f"An import can have at most {IMPORT_MAX} queries. This file has {len(entries)}.")
    ready, skipped = [], []
    for index, entry in enumerate(entries):
        name = entry.get("name") if isinstance(entry, dict) and isinstance(entry.get("name"), str) else ""
        if not isinstance(entry, dict):
            skipped.append({"index": index, "name": "", "code": "invalid", "reason": "The entry is not a JSON object."})
            continue
        try:
            ready.append((index, normalize(source, entry, validate)))
        except (QueryStoreError, PermissionError, ValueError) as exc:
            skipped.append({"index": index, "name": name[:NAME_MAX], "code": "invalid", "reason": str(exc)})

    def fn(bucket):
        added = 0
        # Build the id set and the body keys once, so a large import stays linear.
        used = {q["id"] for q in bucket["queries"]}
        bodies = {_body_key(q) for q in bucket["queries"]}
        for index, clean in ready:
            key = _body_key(clean)
            if key in bodies:
                skipped.append({"index": index, "name": clean["name"], "code": "duplicate",
                                "reason": "The same query is already saved."})
                continue
            qid = _new_id(used)
            used.add(qid)
            bodies.add(key)
            now = _now()
            bucket["queries"].append({"id": qid, **clean, "created": now, "updated": now})
            added += 1
        skipped.sort(key=lambda item: item["index"])
        return {"added": added, "skipped": skipped}, added > 0

    return _change(path, source, fn)
