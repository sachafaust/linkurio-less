"""A short scrubbed log the page can open.

Callers remove secrets before record(). This buffer stores the line it is
given and does not read the environment.
"""
from __future__ import annotations

import threading
import time

_MAX = 400
_lock = threading.Lock()
_rows: list[dict] = []


def record(source, message):
    text = " ".join(str(message or "").split())
    if not text:
        return
    kind = source if source in ("server", "explorer") else "explorer"
    row = {"t": time.strftime("%H:%M:%S"), "source": kind, "message": text[:500]}
    with _lock:
        _rows.append(row)
        if len(_rows) > _MAX:
            del _rows[: len(_rows) - _MAX]


def snapshot(source=None):
    with _lock:
        rows = [dict(row) for row in _rows]
    if source in ("server", "explorer"):
        return [row for row in rows if row["source"] == source]
    return rows
