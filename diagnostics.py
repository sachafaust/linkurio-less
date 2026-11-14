"""A short log the page can open.

Every line is filtered before it is stored and again when it is read.
Passwords, client secrets, bearer tokens, and known credential values become
``[redacted]``. The client id is not a secret: the connection form shows it.
"""
from __future__ import annotations

import os
import re
import threading
import time

_MAX = 400
_QUERY_LIMIT = 8000
_MIN_SECRET = 4
_SOURCES = ("server", "explorer", "query")
_lock = threading.Lock()
_rows: list[dict] = []
_noted: set[str] = set()
_token_envs: set[str] = set()

_ENV_NAMES = ("NEO4J_PASSWORD", "GRAPHQL_TOKEN", "GRAPHQL_CLIENT_SECRET")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

# Structured secrets only. A sentence such as "token expired" has no value to hide.
_PATTERNS = (
    re.compile(r'(?i)(authorization\s*[:=]\s*"?(?:bearer|basic)\s+)([^\s"]+)'),
    re.compile(r'(?i)(?<![A-Za-z0-9_])((?:bearer|basic)\s+)([A-Za-z0-9\-._~+/]{8,}={0,2})'),
    re.compile(
        r'(?i)(?<![A-Za-z0-9_])((?:access_token|refresh_token|id_token|client_secret|password|passwd|token|secret)=)([^&\s#]+)'
    ),
    re.compile(
        r'(?i)("(?:access_token|refresh_token|id_token|client_secret|password|passwd|token|secret|authorization)"\s*:\s*")([^"\\]*)(")'
    ),
    re.compile(
        r'(?i)(?<![A-Za-z0-9_])((?:NEO4J_PASSWORD|GRAPHQL_TOKEN|GRAPHQL_CLIENT_SECRET)\s*=\s*)(\S+)'
    ),
    re.compile(r'(?i)((?:bolt|neo4j)(?:\+s|\+ssc)?://[^/\s:@]+:)([^@\s]+)(@)'),
)


def note_token_env(name):
    """Remember a variable that holds a bearer token, so its value is filtered."""
    text = str(name or "").strip()
    if _ENV_NAME.fullmatch(text):
        with _lock:
            _token_envs.add(text)


def note_secret(value):
    """Remember a credential that may show up in a later line."""
    text = "" if value is None else str(value)
    if len(text) < _MIN_SECRET or not text.strip() or text == "[redacted]":
        return
    with _lock:
        _noted.add(text)


def _known(extra=()):
    with _lock:
        found = set(_noted)
        names = set(_token_envs)
    names.update(_ENV_NAMES)
    custom = (os.environ.get("GRAPHQL_TOKEN_ENV") or "").strip()
    if custom:
        names.add(custom)
    for name in names:
        value = os.environ.get(name) or ""
        if len(value) >= _MIN_SECRET:
            found.add(value)
    for value in extra or ():
        text = "" if value is None else str(value)
        if len(text) >= _MIN_SECRET and text != "[redacted]":
            found.add(text)
    return sorted(found, key=len, reverse=True)


def redact(text, extra=()):
    """Replace credential values and structured secrets. Other words stay."""
    out = "" if text is None else str(text)
    for secret in _known(extra):
        if secret in out:
            out = out.replace(secret, "[redacted]")
    for pattern in _PATTERNS:
        out = pattern.sub(lambda match: _mask(match), out)
    return out


def _mask(match):
    groups = match.groups()
    if len(groups) == 3:
        return f"{groups[0]}[redacted]{groups[2]}"
    return f"{groups[0]}[redacted]"


def _shape(source, message):
    """One stored line. Query entries keep their line breaks so a document can be copied."""
    kind = source if source in _SOURCES else "explorer"
    raw = redact(message)
    if kind == "query":
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        text = "\n".join(line for line in lines if line).strip()[:_QUERY_LIMIT]
    else:
        text = " ".join(raw.split())[:500]
    return kind, text


def record(source, message):
    kind, text = _shape(source, message)
    if not text:
        return
    row = {"t": time.strftime("%H:%M:%S"), "source": kind, "message": text}
    with _lock:
        _rows.append(row)
        if len(_rows) > _MAX:
            del _rows[: len(_rows) - _MAX]


def snapshot(source=None):
    with _lock:
        rows = [dict(row) for row in _rows]
    cleaned = []
    for row in rows:
        _, message = _shape(row.get("source"), row.get("message"))
        row["message"] = message
        cleaned.append(row)
    if source in _SOURCES:
        return [row for row in cleaned if row["source"] == source]
    return cleaned
