"""A disk cache for data models, so a restart on a known connection starts fast.

Each entry is one JSON file in GX_CACHE_DIR (default: .cache in the repository,
gitignored). The file name is a hash of the key. The key names the connection
(Neo4j URI, database, user and server version, or a GraphQL endpoint, adapter,
project and a hash of the credential) and never holds a password, a token, or
a client secret. Each file records the cache format version, when the entry
was built, and how long the build took.

A Slot serves one entry. A cached value is returned at once. When it is older
than GX_CACHE_MAX_AGE, one background thread rebuilds it and swaps it in.
A failed rebuild keeps the old value. Only one build per key runs at a time
in this process. A file that is corrupt, unreadable, or from another version
is ignored and rebuilt. A folder that cannot be written turns the disk cache
off with one log line, and the data model is then kept in memory only.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path

import diagnostics

ROOT = Path(__file__).resolve().parent
# Raise this when the shape of a cached payload changes. Older files are then rebuilt.
# Version 2 stores a hash of the key, not the key. A version 1 file named a
# Neo4j host and a GraphQL endpoint in clear text, so it is rebuilt.
CACHE_VERSION = 2
DEFAULT_MAX_AGE = 24 * 3600.0
# A failed background rebuild is not started again by a read for this long.
FAIL_BACKOFF = 60.0
# The header of an entry is read from this many bytes. The payload comes after it.
_HEAD_BYTES = 8192
_WRITE_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()
_RUNNING: dict = {}
_DEFAULTS: dict = {}
_DEFAULTS_LOCK = threading.Lock()


def _positive_seconds(raw, default):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value <= 0:
        return float(default)
    return value


def cache_key(kind, **parts):
    """One canonical string for an entry. The parts must hold no secret."""
    return json.dumps({"kind": kind, **parts}, sort_keys=True, separators=(",", ":"), default=str)


def key_digest(key):
    """A one-way id of a cache key. The file stores it in place of the key.

    The key names a Neo4j host or a GraphQL endpoint. A file on disk must not.
    """
    return hashlib.sha256(("gx-cache-key|" + str(key)).encode("utf-8")).hexdigest()


def principal_hash(value):
    """A one-way id of a credential, for a cache key. The value itself is never stored."""
    text = "" if value is None else str(value)
    return hashlib.sha256(("gx-cache-principal|" + text).encode("utf-8")).hexdigest()[:32]


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# A known secret shorter than this matches only a whole string value. A short
# password such as "password" is also a common word inside a property name.
_SUBSTRING_SECRET = 12


def holds_secret(payload, secrets=()):
    """True when a string value in payload looks like a credential.

    Only string values are checked. Keys are property, field, and type names,
    and a data model needs them, so a key named password or token is not a
    leak. A value that a structured pattern (Bearer, token=, a URI password)
    would redact is a leak. A known secret is a leak as a whole value, and a
    secret of 12 or more characters is a leak anywhere inside a value.
    """
    known = [str(value) for value in (secrets or ()) if value]
    known.extend(diagnostics.known_secrets())
    whole = {value for value in known if len(value) >= 4}
    inside = [value for value in whole if len(value) >= _SUBSTRING_SECRET]
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str):
            if item in whole or any(value in item for value in inside):
                return True
            if diagnostics.looks_structured(item):
                return True
    return False


class DiskCache:
    """The folder of cache files. Writes are atomic and mode 0600."""

    def __init__(self, folder=None, max_age=None, memory=False):
        self.memory = bool(memory)
        if self.memory:
            self.folder = None
        elif folder is not None:
            self.folder = Path(folder).expanduser()
        else:
            raw = (os.environ.get("GX_CACHE_DIR") or "").strip()
            self.folder = Path(raw).expanduser() if raw else ROOT / ".cache"
        if max_age is None:
            max_age = os.environ.get("GX_CACHE_MAX_AGE")
        self.max_age = _positive_seconds(max_age, DEFAULT_MAX_AGE)
        self.enabled = not self.memory
        self.reason = "memory only" if self.memory else ""
        self._scope = f"mem:{id(self)}" if self.memory else str(self.folder)

    @classmethod
    def memory_only(cls):
        """A cache that keeps nothing on disk. Each source gets its own."""
        return cls(memory=True)

    def scope(self, key):
        return self._scope + "|" + key

    def path_for(self, key):
        if self.folder is None:
            return None
        name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.folder / f"{name}.json"

    def _disable(self, exc):
        if not self.enabled:
            return
        self.enabled = False
        reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
        self.reason = reason
        line = (f"Schema cache is off for this run: could not write {self.folder} ({reason}). "
                "The explorer still works and keeps the data model in memory.")
        print(line, flush=True)
        diagnostics.record("explorer", line)

    def _valid(self, data, key):
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or data.get("key") != key_digest(key):
            return False
        if not _number(data.get("built_at")) or not _number(data.get("build_seconds")):
            return False
        return data["build_seconds"] >= 0

    def read(self, key):
        """The entry for key, or None when it is missing, corrupt, unreadable, or from another version."""
        path = self.path_for(key)
        if path is None:
            return None
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not self._valid(data, key) or data.get("payload") is None:
            return None
        return {"payload": data["payload"], "built_at": float(data["built_at"]),
                "build_seconds": float(data["build_seconds"]), "size": len(raw), "on_disk": True}

    def header(self, key):
        """The entry fields without the payload. It reads only the start of the file."""
        path = self.path_for(key)
        if path is None:
            return None
        try:
            with open(path, "rb") as handle:
                head = handle.read(_HEAD_BYTES)
            size = path.stat().st_size
        except OSError:
            return None
        text = head.decode("utf-8", errors="replace")
        cut = text.find(',"payload":')
        if cut < 0:
            return None
        try:
            data = json.loads(text[:cut] + "}")
        except ValueError:
            return None
        if not self._valid(data, key):
            return None
        return {"built_at": float(data["built_at"]), "build_seconds": float(data["build_seconds"]),
                "size": size, "on_disk": True}

    def write(self, key, kind, payload, built_at=None, build_seconds=0.0, secrets=()):
        """Save one entry. Returns (saved, note, size).

        The payload is refused when redaction would change its text, because
        then it may hold a credential. A failed write turns the cache off.
        """
        built_at = time.time() if built_at is None else float(built_at)
        try:
            body = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            return False, f"not saved: the data model is not JSON ({type(exc).__name__})", 0
        size = len(body)
        if holds_secret(payload, secrets):
            return False, "not saved: the data model looks like it holds a credential", size
        if not self.enabled:
            return False, None if self.memory else f"not saved: the cache is off ({self.reason})", size
        head = json.dumps({"version": CACHE_VERSION, "key": key_digest(key), "kind": kind,
                           "built_at": built_at, "build_seconds": round(float(build_seconds), 3)},
                          separators=(",", ":"))
        text = head[:-1] + ',"payload":' + body + "}"
        path = self.path_for(key)
        tmp = None
        with _WRITE_LOCK:
            try:
                self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=self.folder)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                tmp = None
            except OSError as exc:
                self._disable(exc)
                return False, f"not saved: the cache is off ({self.reason})", len(text)
            finally:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
        return True, None, len(text)

    def drop(self, key):
        path = self.path_for(key)
        if path is None:
            return
        with _WRITE_LOCK:
            with contextlib.suppress(OSError):
                path.unlink()


def default_cache():
    """The process-wide cache for GX_CACHE_DIR and GX_CACHE_MAX_AGE as they are now."""
    folder = (os.environ.get("GX_CACHE_DIR") or "").strip()
    age = (os.environ.get("GX_CACHE_MAX_AGE") or "").strip()
    with _DEFAULTS_LOCK:
        cache = _DEFAULTS.get((folder, age))
        if cache is None:
            cache = _DEFAULTS[(folder, age)] = DiskCache()
        return cache


class _Run:
    """One build of one key. Readers that arrive while it runs wait for it."""

    def __init__(self):
        self.event = threading.Event()
        self.entry = None
        self.error = None

    def ok(self, entry):
        self.entry = entry
        self.event.set()

    def fail(self, error):
        self.error = error
        self.event.set()

    def wait(self, timeout=900):
        if not self.event.wait(timeout):
            raise TimeoutError("The data model rebuild did not finish.")
        if self.error is not None:
            raise self.error
        return self.entry


class Slot:
    """One cached value: read it, rebuild it in the background, and report on it.

    build() returns the value. key() names the connection. gate() is an
    optional context manager that a background build runs inside, so a
    rate-limited API can wait out a cool-down and space its rebuilds.
    on_load(value) runs each time a value is adopted. mem_ttl is how long a
    value that is on disk stays in memory after its last read. The next read
    takes the file again, so mem_ttl never starts a rebuild of a saved value.
    A value that is not on disk (no folder, a refused write, or a full disk)
    is old after mem_ttl and is then rebuilt in the background. mem_ttl 0
    keeps no such value in memory.
    """

    def __init__(self, cache, kind, key, build, *, secrets=None, label=None, gate=None,
                 on_load=None, mem_ttl=0.0):
        self.cache = cache
        self.kind = kind
        self.key_fn = key
        self.build = build
        self.secrets = secrets or (lambda: ())
        self.label = label or kind
        self.gate = gate
        self.on_load = on_load
        self.mem_ttl = float(mem_ttl or 0)
        self._lock = threading.Lock()
        # (key, entry, last read)
        self._mem = None
        self._error = None
        self._failed_at = 0.0
        self._note = None
        self._noted = set()

    # ---- reading --------------------------------------------------------------------
    def _adopt(self, key, entry):
        with self._lock:
            if entry.get("on_disk") or self.mem_ttl > 0:
                self._mem = (key, entry, time.time())
            else:
                self._mem = None
        if self.on_load is not None:
            try:
                self.on_load(entry["payload"])
            except Exception:  # noqa: BLE001
                pass

    def _held(self, key):
        with self._lock:
            mem = self._mem
            if not mem or mem[0] != key:
                return None
            entry = mem[1]
            if self.mem_ttl and entry.get("on_disk") and time.time() - mem[2] > self.mem_ttl:
                self._mem = None
                return None
            self._mem = (key, entry, time.time())
            return entry

    def _max_age(self, entry):
        if not entry.get("on_disk") and self.mem_ttl > 0:
            return min(self.cache.max_age, self.mem_ttl)
        return self.cache.max_age

    def _stale(self, entry):
        return time.time() - entry["built_at"] > self._max_age(entry)

    def _fresher(self, key, entry):
        """A newer entry that another Slot on this key saved, or None.

        It reads the file header first, so the payload is read only when it is newer.
        """
        head = self.cache.header(key)
        if head is None or head["built_at"] <= entry["built_at"]:
            return None
        newer = self.cache.read(key)
        if newer is None or newer["built_at"] <= entry["built_at"]:
            return None
        return newer

    def peek(self, revalidate=True):
        """The cached value, or None. An old value starts one background rebuild.

        Before it starts one, it checks the file. Another Slot on the same key
        can have saved a newer entry, and that entry is then taken instead.
        """
        key = self.key_fn()
        entry = self._held(key)
        if entry is None:
            entry = self.cache.read(key)
            if entry is not None:
                self._adopt(key, entry)
        if entry is None:
            return None
        if revalidate and self._stale(entry):
            newer = self._fresher(key, entry)
            if newer is not None:
                self._adopt(key, newer)
                entry = newer
            if self._stale(entry):
                self._kick(key, auto=True)
        return entry["payload"]

    def get(self, refresh=False):
        """The value. A miss or a refresh builds it in this thread and saves it."""
        if not refresh:
            value = self.peek()
            if value is not None:
                return value
        return self._build_now(self.key_fn())

    # ---- building -------------------------------------------------------------------
    def _secrets(self):
        try:
            return [value for value in (self.secrets() or ()) if value]
        except Exception:  # noqa: BLE001
            return []

    def _store(self, key, value, seconds):
        built_at = time.time()
        saved, note, size = self.cache.write(key, self.kind, value, built_at, seconds, self._secrets())
        entry = {"payload": value, "built_at": built_at, "build_seconds": round(seconds, 3),
                 "size": size, "on_disk": saved}
        with self._lock:
            self._note = note
            self._error = None
            self._failed_at = 0.0
        # A cache that turned itself off already logged one line. Only a refusal is logged here.
        if note and note not in self._noted and self.cache.enabled:
            self._noted.add(note)
            line = f"{self.label}: {note}."
            print(line, flush=True)
            diagnostics.record("explorer", line)
        self._adopt(key, entry)
        return entry

    def _fail(self, exc, background):
        message = diagnostics.redact(str(exc) or type(exc).__name__, self._secrets())[:300]
        with self._lock:
            self._error = message
            self._failed_at = time.time()
        if background:
            line = f"{self.label}: the background rebuild failed. The saved copy stays. ({message})"
            print(line, flush=True)
            diagnostics.record("explorer", line)

    def _claim(self, key):
        scope = self.cache.scope(key)
        with _RUN_LOCK:
            run = _RUNNING.get(scope)
            if run is not None:
                return scope, run, False
            run = _RUNNING[scope] = _Run()
            return scope, run, True

    @staticmethod
    def _release(scope, run):
        with _RUN_LOCK:
            if _RUNNING.get(scope) is run:
                _RUNNING.pop(scope, None)

    def _build_now(self, key):
        scope, run, leader = self._claim(key)
        if not leader:
            entry = run.wait()
            self._adopt(key, entry)
            return entry["payload"]
        try:
            started = time.perf_counter()
            value = self.build()
            entry = self._store(key, value, time.perf_counter() - started)
        except BaseException as exc:
            self._fail(exc, background=False)
            run.fail(exc)
            raise
        finally:
            self._release(scope, run)
        run.ok(entry)
        return value

    def _background(self, key, scope, run):
        try:
            with (self.gate() if self.gate is not None else contextlib.nullcontext()):
                started = time.perf_counter()
                value = self.build()
                seconds = time.perf_counter() - started
            entry = self._store(key, value, seconds)
        except BaseException as exc:  # noqa: BLE001
            self._fail(exc, background=True)
            run.fail(exc)
        else:
            diagnostics.record("explorer", f"{self.label}: rebuilt in {seconds:.1f} s.")
            run.ok(entry)
        finally:
            self._release(scope, run)

    def _kick(self, key, auto):
        if auto:
            with self._lock:
                if self._failed_at and time.time() - self._failed_at < FAIL_BACKOFF:
                    return "held"
        scope, run, leader = self._claim(key)
        if not leader:
            return "running"
        thread = threading.Thread(target=self._background, args=(key, scope, run),
                                  name=f"gx-cache-{self.kind}", daemon=True)
        thread.start()
        return "started"

    def refresh_background(self):
        """Rebuild now in a background thread. Returns started or running."""
        return self._kick(self.key_fn(), auto=False)

    def revalidate(self):
        """Rebuild in the background unless a recent rebuild failed."""
        return self._kick(self.key_fn(), auto=True)

    def clear(self):
        key = self.key_fn()
        self.cache.drop(key)
        with self._lock:
            self._mem = None
            self._error = None
            self._failed_at = 0.0
            self._note = None

    # ---- reporting ------------------------------------------------------------------
    def info(self):
        """What is cached and how old it is. It never builds and never reads the payload."""
        try:
            key = self.key_fn()
        except Exception as exc:  # noqa: BLE001
            return {"kind": self.kind, "label": self.label, "cached": False, "on_disk": False,
                    "rebuilding": False, "stale": False, "built_at": None, "age": None,
                    "build_seconds": None, "size": None, "max_age": self.cache.max_age,
                    "enabled": self.cache.enabled,
                    "error": diagnostics.redact(str(exc), self._secrets())[:300], "failed_at": None, "note": None}
        with self._lock:
            mem = self._mem if self._mem and self._mem[0] == key else None
            error, note, failed_at = self._error, self._note, self._failed_at
        entry = mem[1] if mem else self.cache.header(key)
        with _RUN_LOCK:
            rebuilding = self.cache.scope(key) in _RUNNING
        now = time.time()
        out = {"kind": self.kind, "label": self.label, "cached": entry is not None,
               "on_disk": bool(entry and entry.get("on_disk")), "rebuilding": rebuilding,
               "stale": bool(entry and now - entry["built_at"] > self._max_age(entry)),
               "built_at": _iso(entry["built_at"]) if entry else None,
               "age": max(0, int(now - entry["built_at"])) if entry else None,
               "build_seconds": entry["build_seconds"] if entry else None,
               "size": entry.get("size") if entry else None,
               "max_age": self.cache.max_age, "enabled": self.cache.enabled,
               "error": error, "failed_at": _iso(failed_at) if error and failed_at else None,
               "note": note}
        return out
