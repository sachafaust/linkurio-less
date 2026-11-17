#!/usr/bin/env python3
"""The schema cache: restart hits, key isolation, freshness, and the cache API.

Every check uses its own temp cache folder. The live part starts a mock Wiz
tenant and the explorer on free ports. It does not use port 8744, a real
tenant, or a real Neo4j. Neo4j is a fake that counts each statement. The last
part drives a headed Chrome page and prints "HEADED cache ok".
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import diagnostics  # noqa: E402
import graphql_source as gql  # noqa: E402
import server  # noqa: E402
import test_graphql as tg  # noqa: E402
from neo4j_env import Settings  # noqa: E402

try:
    import schema_cache as sc  # noqa: E402
except ImportError as exc:  # noqa: BLE001
    sc = None
    IMPORT_ERROR = exc

TOKEN = tg.TOKEN
PASSWORD = "neo4j-pass-do-not-cache"
SECRET = "client-secret-do-not-cache"
CLIENT = "client-id-do-not-cache"
MINTED = "minted-access-do-not-cache"
FAILS = []


def safe(text):
    out = str(text)
    for value in (TOKEN, SECRET, PASSWORD, CLIENT, MINTED):
        out = out.replace(value, "[redacted]")
    return out[:700]


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        shown = safe(detail)
        print(f"  FAIL {name}{(': ' + shown) if shown else ''}")
        FAILS.append(name)


def wait_until(fn, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())


def files(folder):
    path = Path(folder)
    return sorted(path.glob("*.json")) if path.is_dir() else []


def blob(folder):
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files(folder))


def entry_of(folder, kind):
    """The cache file and its JSON for one kind, or (None, None)."""
    for path in files(folder):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("kind") == kind:
            return path, data
    return None, None


# ---- a fake Neo4j --------------------------------------------------------------------

class FakeNode:
    def __init__(self, labels, props):
        self.labels = frozenset(labels)
        self._props = props

    def items(self):
        return self._props.items()


class FakeNeo4j:
    """Answers the statements Graph.schema and Graph.store_counts send. calls keeps each one."""

    def __init__(self, labels=None, types=None, props=None, delay=0.0):
        self.labels = labels or {"AWSAccount": 2, "EC2Instance": 3}
        self.types = types or {"RESOURCE": 3}
        self.props = props or {"name": "x", "id": "y"}
        self.delay = delay
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, query, timeout=30, **params):
        with self.lock:
            self.calls.append(query)
        if "GRAPH COUNTS" in query:
            nodes = [{"count": sum(self.labels.values())}] + [
                {"label": key, "count": value} for key, value in self.labels.items()]
            rels = [{"count": sum(self.types.values())}] + [
                {"relationshipType": key, "count": value} for key, value in self.types.items()]
            return [{"data": {"nodes": nodes, "relationships": rels}}]
        if self.delay:
            time.sleep(self.delay)
        match = re.match(r"MATCH \(n:`([^`]+)`\) RETURN n LIMIT 30", query)
        if match:
            return [{"n": FakeNode([match.group(1)], dict(self.props))}]
        if re.match(r"MATCH \(a\)-\[r:`([^`]+)`\]->\(b\)", query):
            names = sorted(self.labels)
            return [{"la": [names[0]], "lb": [names[-1]]}]
        return []

    def schema_reads(self):
        with self.lock:
            return [q for q in self.calls if "LIMIT 30" in q or "LIMIT 200" in q or "db.labels" in q]


class FakeDriver:
    def __init__(self, agent):
        self.agent = agent

    def get_server_info(self):
        return SimpleNamespace(agent=self.agent)

    def close(self):
        return None


def make_graph(cache, uri="bolt://127.0.0.1:7687", database="neo4j", user="neo4j",
               agent="Neo4j/5.26.0", fake=None):
    graph = server.Graph(Settings(uri, user, PASSWORD, database), cache=cache)
    graph.driver.close()
    graph.driver = FakeDriver(agent)
    graph.read = fake if fake is not None else FakeNeo4j()
    return graph


# ---- a Wiz source on a mock tenant ---------------------------------------------------

def wiz_source(cache, tenant=None, endpoint="https://api.example-region.app.wiz.us/graphql", project_id="*",
               client_id=None, token=TOKEN, oauth=False, log=None, **kwargs):
    tenant = tenant or tg.LiveTenant()

    def opener(request, timeout=None):
        if request.full_url.endswith("/oauth/token"):
            return tg.Resp(200, json.dumps({"access_token": MINTED}).encode())
        body = json.loads(request.data.decode())
        document = body.get("query") or ""
        if log is not None:
            log.append((time.time(), document))
        code, payload = tenant.answer(document, body.get("variables") or {})
        return tg.Resp(code, json.dumps(payload).encode())

    kwargs.setdefault("max_retries", 0)
    if oauth:
        kwargs.update(client_secret=SECRET, auth_url="https://auth.example.test/oauth/token")
        token = None
    built = tg.client(opener, token=token, client_id=client_id, **kwargs)
    src = gql.GraphQLSource({
        "id": "wiz", "label": "Wiz", "endpoint": endpoint, "adapter": "wiz",
        "token_env": "GRAPHQL_TOKEN", "project_id": project_id,
    }, client=built, cache=cache)
    src.rebuild_gap = 0.0
    return src, tenant


def counts(tenant):
    return len(tenant.shape_probes()), len(tenant.full_introspections())


# ---- checks --------------------------------------------------------------------------

def test_files_and_docs():
    print("files and docs")
    ignored = subprocess.run(["git", "check-ignore", "-q", ".cache/abc.json"], cwd=ROOT)
    check(".cache/ is gitignored", ignored.returncode == 0)
    doc = server.__doc__ or ""
    check("server.py documents GX_CACHE_DIR and GX_CACHE_MAX_AGE",
          "GX_CACHE_DIR" in doc and "GX_CACHE_MAX_AGE" in doc)
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    check(".env.example lists the cache variables", "GX_CACHE_DIR" in example and "GX_CACHE_MAX_AGE" in example)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README has a Schema cache section", "## Schema cache" in readme)
    default = sc.DiskCache()
    check("the default cache folder is .cache in the repository", default.folder == ROOT / ".cache", default.folder)


def test_neo4j_restart(tmp):
    print("neo4j restart")
    folder = Path(tmp) / "neo"
    first = make_graph(sc.DiskCache(folder))
    model = first.schema()
    check("a first start reads the data model from Neo4j",
          len(first.read.schema_reads()) > 0 and "AWSAccount" in (model.get("categories") or {}), model)
    check("the first start saves one cache file", len(files(folder)) == 1, files(folder))
    second = make_graph(sc.DiskCache(folder))
    started = time.perf_counter()
    again = second.schema()
    took = time.perf_counter() - started
    check("a restart serves the saved data model and sends no schema query",
          second.read.schema_reads() == [] and again == model and took < 1.0,
          {"reads": second.read.schema_reads(), "took": took})
    info = second.schema_slot.info()
    check("the restart reports the saved entry",
          info.get("cached") and info.get("on_disk") and info.get("build_seconds") is not None
          and info.get("size") and not info.get("stale") and not info.get("rebuilding"), info)
    _, before = entry_of(folder, "neo4j-schema")
    time.sleep(0.02)
    third = make_graph(sc.DiskCache(folder))
    third.schema(refresh=True)
    _, after = entry_of(folder, "neo4j-schema")
    check("schema(refresh=True) reads Neo4j again and rewrites the entry",
          len(third.read.schema_reads()) > 0 and before and after and after["built_at"] > before["built_at"],
          {"before": (before or {}).get("built_at"), "after": (after or {}).get("built_at")})
    changed = FakeNeo4j(labels={"AWSAccount": 2, "EC2Instance": 3, "S3Bucket": 4})
    fourth = make_graph(sc.DiskCache(folder), fake=changed)
    served = fourth.schema()
    check("a new label in the count store serves the saved model at once",
          "S3Bucket" not in (served.get("labels") or {}))
    ok = wait_until(lambda: "S3Bucket" in (fourth.schema().get("labels") or {}), 5)
    check("and rebuilds it in the background", ok, fourth.schema_slot.info())


def test_neo4j_key_isolation(tmp):
    print("neo4j key isolation")
    folder = Path(tmp) / "neo-keys"
    make_graph(sc.DiskCache(folder)).schema()
    variants = [
        ("URI", {"uri": "bolt://other-host.example:7687"}),
        ("database", {"database": "other"}),
        ("server version", {"agent": "Neo4j/5.27.0"}),
        ("user", {"user": "reader"}),
    ]
    for name, kwargs in variants:
        graph = make_graph(sc.DiskCache(folder), **kwargs)
        graph.schema()
        check(f"a different Neo4j {name} misses the cache", len(graph.read.schema_reads()) > 0)
    same = make_graph(sc.DiskCache(folder))
    same.schema()
    check("the same Neo4j connection hits the cache", same.read.schema_reads() == [])
    check("each connection has its own file", len(files(folder)) == 5, files(folder))
    text = blob(folder)
    check("a Neo4j cache file holds no password", PASSWORD not in text)


def test_userinfo_and_old_file(tmp):
    """Checks carried over from the old gx.schema.json test. That file is gone, and these still hold."""
    print("userinfo and the old gx.schema.json")
    folder = Path(tmp) / "neo-userinfo"
    config = Path(tmp) / "neo-userinfo-config"
    config.mkdir()
    old = os.environ.get("GX_CONFIG")
    os.environ["GX_CONFIG"] = str(config / "gx.local.json")
    try:
        plain = make_graph(sc.DiskCache(folder))
        plain.schema()
        # The Neo4j driver refuses a URI with userinfo, so the key is checked on a Graph that holds one.
        with_user = make_graph(sc.DiskCache(folder))
        with_user.uri = "bolt://reader:s3cret-userinfo@127.0.0.1:7687"
        with_user.schema()
        check("a URI with userinfo hits the entry of the same URI without it",
              with_user.read.schema_reads() == [] and len(files(folder)) == 1, files(folder))
        check("a cache file holds no userinfo password", "s3cret-userinfo" not in blob(folder))
        check("the explorer writes no gx.schema.json beside the working set",
              not (config / "gx.schema.json").exists(), sorted(p.name for p in config.iterdir()))
    finally:
        if old is None:
            os.environ.pop("GX_CONFIG", None)
        else:
            os.environ["GX_CONFIG"] = old
    ignored = subprocess.run(["git", "check-ignore", "-q", "gx.schema.json"], cwd=ROOT)
    check("an old gx.schema.json on disk stays gitignored", ignored.returncode == 0)


def test_graphql_restart_and_keys(tmp):
    print("graphql restart and keys")
    folder = Path(tmp) / "gql"
    tenant = tg.LiveTenant()
    first, _ = wiz_source(sc.DiskCache(folder), tenant)
    first.schema()
    first.schema(full=True)
    check("a first start sends the shape probe and the full introspection once",
          counts(tenant) == (1, 1), counts(tenant))
    second, _ = wiz_source(sc.DiskCache(folder), tenant)
    light = second.schema()
    whole = second.schema(full=True)
    second.search("alpha")
    check("a restart reads the probe and the full schema from disk",
          counts(tenant) == (1, 1) and light.get("entities") == sorted(tg.LIVE_ENTITY_TYPES[1:])
          and "Query" in (whole.get("categories") or {}),
          counts(tenant))
    check("a disk hit teaches the mutation guard the mutation names",
          bool(second.client.mutation_fields) and second.client.mutation_fields == first.client.mutation_fields)
    check("a disk hit keeps the probe kind", second.adapter.kind == first.adapter.kind, second.adapter.kind)
    variants = [
        ("endpoint", {"endpoint": "https://api.example-region-2.app.wiz.us/graphql"}),
        ("project", {"project_id": "project-2"}),
        ("client id", {"client_id": CLIENT}),
        ("second client id", {"client_id": CLIENT + "-2"}),
        ("token", {"token": TOKEN + "-other"}),
    ]
    for name, kwargs in variants:
        before = counts(tenant)[0]
        src, _ = wiz_source(sc.DiskCache(folder), tenant, **kwargs)
        src.schema()
        check(f"a different {name} misses the cache", counts(tenant)[0] == before + 1, counts(tenant))
    before = counts(tenant)[0]
    src, _ = wiz_source(sc.DiskCache(folder), tenant, client_id=CLIENT)
    src.schema()
    check("the same client id hits the cache", counts(tenant)[0] == before, counts(tenant))
    text = blob(folder)
    check("no GraphQL cache file holds the token or the client id",
          TOKEN not in text and CLIENT not in text, len(text))


def test_no_host_on_disk(tmp):
    """A cache file stores a hash of its key, never the Neo4j host or the Wiz endpoint."""
    print("no host on disk")
    folder = Path(tmp) / "no-host"
    neo_host, wiz_host = "internal-db.corp.example", "api.example-internal.app.wiz.us"
    make_graph(sc.DiskCache(folder), uri=f"neo4j+s://{neo_host}:7687", user="svc-reader").schema()
    src, _ = wiz_source(sc.DiskCache(folder), endpoint=f"https://{wiz_host}/graphql", project_id="proj-x")
    src.search("alpha", category="VIRTUAL_MACHINE")
    text = blob(folder)
    check("a cache file names no host, endpoint, user, or project",
          len(files(folder)) >= 2 and neo_host not in text and wiz_host not in text
          and "svc-reader" not in text and "proj-x" not in text, sorted(p.name for p in files(folder)))
    _, data = entry_of(folder, "neo4j-schema")
    check("the stored key is a hash", data and len(data.get("key", "")) == 64
          and all(c in "0123456789abcdef" for c in data["key"]), data and data.get("key"))
    again = make_graph(sc.DiskCache(folder), uri=f"neo4j+s://{neo_host}:7687", user="svc-reader")
    reads = again.read.schema_reads()
    again.schema()
    check("a restart still reads the hashed entry", again.read.schema_reads() == reads)
    # A version 1 file stored the key in clear text. It must be rebuilt, not trusted.
    old_folder = Path(tmp) / "v1"
    key = sc.cache_key("test", host=neo_host)
    cache = sc.DiskCache(old_folder)
    old_folder.mkdir(parents=True)
    cache.path_for(key).write_text(json.dumps({"version": 1, "key": key, "kind": "test", "built_at": time.time(),
                                               "build_seconds": 0.1, "payload": {"n": 0}}))
    built = []
    value = sc.Slot(sc.DiskCache(old_folder), "test", lambda: key, lambda: built.append(1) or {"n": 1}).get()
    check("a version 1 file with a plain key is rebuilt, and the host leaves the disk",
          value == {"n": 1} and built == [1] and neo_host not in blob(old_folder), value)


def test_version(tmp):
    print("cache version")
    folder = Path(tmp) / "version"
    builds = []

    def build():
        builds.append(1)
        return {"n": len(builds)}

    def key():
        return sc.cache_key("test", n=1)

    sc.Slot(sc.DiskCache(folder), "test", key, build).get()
    old = sc.CACHE_VERSION
    sc.CACHE_VERSION = old + 1
    try:
        value = sc.Slot(sc.DiskCache(folder), "test", key, build).get()
        _, data = entry_of(folder, "test")
        check("a new cache version ignores the old file and rebuilds it",
              len(builds) == 2 and value == {"n": 2}, builds)
        check("the rebuilt file carries the new version in the same place",
              data and data.get("version") == old + 1 and len(files(folder)) == 1, data)
    finally:
        sc.CACHE_VERSION = old
    sc.Slot(sc.DiskCache(folder), "test", key, build).get()
    check("an entry from a newer version is ignored too", len(builds) == 3, builds)


def test_stale_while_revalidate(tmp):
    print("stale while revalidate")
    folder = Path(tmp) / "swr"
    cache = sc.DiskCache(folder, max_age=60)

    def key():
        return sc.cache_key("swr", n=1)

    cache.write(key(), "swr", {"n": "old"}, built_at=time.time() - 3600, build_seconds=80.0)
    gate = threading.Event()
    builds = []

    def build():
        builds.append(1)
        gate.wait(5)
        return {"n": "new"}

    slot = sc.Slot(cache, "swr", key, build)
    started = time.perf_counter()
    value = slot.get()
    took = time.perf_counter() - started
    info = slot.info()
    check("an old entry is served at once", value == {"n": "old"} and took < 0.5, took)
    check("the old entry starts one background rebuild",
          info.get("rebuilding") and info.get("stale") and info.get("age", 0) >= 3500
          and info.get("build_seconds") == 80.0, info)
    check("a second read while it runs starts no other rebuild",
          slot.get() == {"n": "old"} and wait_until(lambda: len(builds) == 1, 1) and len(builds) == 1, builds)
    gate.set()
    done = wait_until(lambda: not slot.info().get("rebuilding"), 5)
    check("the rebuild swaps the new entry in",
          done and slot.get() == {"n": "new"} and not slot.info().get("stale"), slot.info())
    _, data = entry_of(folder, "swr")
    check("the rebuild rewrites the disk entry",
          data and data["payload"] == {"n": "new"} and time.time() - data["built_at"] < 10, data)


def test_single_rebuild(tmp):
    print("single rebuild")
    folder = Path(tmp) / "single"
    cache = sc.DiskCache(folder, max_age=60)

    def key():
        return sc.cache_key("single", n=1)

    cache.write(key(), "single", {"n": 0}, built_at=time.time() - 3600, build_seconds=1.0)
    gate = threading.Event()
    builds = []
    lock = threading.Lock()

    def build():
        with lock:
            builds.append(1)
            n = len(builds)
        gate.wait(5)
        return {"n": n}

    slots = [sc.Slot(sc.DiskCache(folder, max_age=60), "single", key, build) for _ in range(3)]
    results = []
    threads = []
    for slot in slots:
        for _ in range(3):
            threads.append(threading.Thread(target=lambda s=slot: results.append(s.refresh_background())))
        threads.append(threading.Thread(target=slot.get))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    wait_until(lambda: len(builds) >= 1, 2)
    check("nine refresh calls and three stale reads on one key start one rebuild",
          len(builds) == 1 and results.count("started") <= 1, {"builds": len(builds), "results": results})
    waited = []
    thread = threading.Thread(target=lambda: waited.append(slots[0].get(refresh=True)))
    thread.start()
    time.sleep(0.2)
    gate.set()
    thread.join(5)
    check("a foreground refresh that waits for the running rebuild reuses it",
          len(builds) == 1 and waited == [{"n": 1}], {"builds": len(builds), "waited": waited})


def test_failed_rebuild(tmp):
    print("failed rebuild")
    folder = Path(tmp) / "failed"
    cache = sc.DiskCache(folder, max_age=60)

    def key():
        return sc.cache_key("failed", n=1)

    cache.write(key(), "failed", {"n": "old"}, built_at=time.time() - 3600, build_seconds=1.0)
    builds = []

    def build():
        builds.append(1)
        raise RuntimeError(f"the source went away {TOKEN}")

    slot = sc.Slot(cache, "failed", key, build, secrets=lambda: [TOKEN], label="test model")
    value = slot.get()
    done = wait_until(lambda: not slot.info().get("rebuilding") and builds, 5)
    info = slot.info()
    _, data = entry_of(folder, "failed")
    logged = [row["message"] for row in diagnostics.snapshot() if "test model" in row["message"]]
    check("a failed rebuild keeps the old entry",
          done and value == {"n": "old"} and slot.get() == {"n": "old"} and data["payload"] == {"n": "old"}, info)
    check("a failed rebuild is logged without the token",
          bool(info.get("error")) and TOKEN not in str(info.get("error"))
          and logged and all(TOKEN not in line for line in logged), {"info": info, "log": logged})
    slot.get()
    time.sleep(0.2)
    check("the next stale read does not retry at once", len(builds) == 1, builds)


def test_corrupt(tmp):
    print("corrupt files")
    folder = Path(tmp) / "corrupt"
    folder.mkdir(parents=True)

    def key():
        return sc.cache_key("corrupt", n=1)

    path = sc.DiskCache(folder).path_for(key())
    other = json.dumps({"version": sc.CACHE_VERSION, "key": sc.cache_key("corrupt", n=2), "kind": "corrupt",
                        "built_at": time.time(), "build_seconds": 1.0, "payload": {"ok": False}})
    nopayload = json.dumps({"version": sc.CACHE_VERSION, "key": key(), "kind": "corrupt",
                            "built_at": time.time(), "build_seconds": 1.0})
    badtime = json.dumps({"version": sc.CACHE_VERSION, "key": key(), "kind": "corrupt",
                          "built_at": "yesterday", "build_seconds": 1.0, "payload": {"ok": False}})
    cases = [
        ("broken JSON", b"{not json"),
        ("JSON list", b"[1, 2]"),
        ("truncated", b'{"version": 1, "key": '),
        ("binary", b"\xff\xfe\x00\x01"),
        ("foreign key", other.encode()),
        ("payload-less", nopayload.encode()),
        ("bad built_at", badtime.encode()),
    ]
    for name, raw in cases:
        path.write_bytes(raw)
        builds = []
        slot = sc.Slot(sc.DiskCache(folder), "corrupt", key, lambda b=builds: b.append(1) or {"ok": True})
        try:
            value, error = slot.get(), None
        except Exception as exc:  # noqa: BLE001
            value, error = None, exc
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        check(f"a {name} cache file is ignored and rebuilt",
              error is None and value == {"ok": True} and builds == [1] and data.get("payload") == {"ok": True},
              {"error": error, "value": value})
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0)
    try:
        builds = []
        slot = sc.Slot(sc.DiskCache(folder), "corrupt", key, lambda: builds.append(1) or {"ok": "again"})
        try:
            value, error = slot.get(), None
        except Exception as exc:  # noqa: BLE001
            value, error = None, exc
        check("an unreadable cache file is ignored and rebuilt",
              error is None and value == {"ok": "again"} and builds == [1], error)
    finally:
        if path.exists():
            os.chmod(path, 0o600)


def test_read_only(tmp):
    print("read-only and full disk")
    folder = Path(tmp) / "ro"
    folder.mkdir(parents=True)
    os.chmod(folder, 0o500)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            cache = sc.DiskCache(folder)
            values = [sc.Slot(cache, "ro", lambda n=n: sc.cache_key("ro", n=n), lambda n=n: {"n": n}).get()
                      for n in range(3)]
            graph = make_graph(cache)
            model = graph.schema()
        lines = [line for line in out.getvalue().splitlines() if "schema cache" in line.lower()]
        check("a read-only cache folder still serves each data model",
              values == [{"n": 0}, {"n": 1}, {"n": 2}] and "AWSAccount" in (model.get("categories") or {}))
        check("a read-only folder turns the cache off with one log line",
              not cache.enabled and len(lines) == 1, lines)
        check("nothing is written to a read-only folder", list(folder.iterdir()) == [])
    finally:
        os.chmod(folder, 0o700)
    full_folder = Path(tmp) / "full"
    real_replace = os.replace

    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    out = io.StringIO()
    os.replace = no_space
    try:
        with contextlib.redirect_stdout(out):
            cache = sc.DiskCache(full_folder)
            values = [sc.Slot(cache, "full", lambda n=n: sc.cache_key("full", n=n), lambda n=n: {"n": n}).get()
                      for n in range(2)]
    finally:
        os.replace = real_replace
    lines = [line for line in out.getvalue().splitlines() if "schema cache" in line.lower()]
    leftovers = list(full_folder.iterdir()) if full_folder.is_dir() else []
    check("a full disk turns the cache off with one log line and leaves no temp file",
          values == [{"n": 0}, {"n": 1}] and not cache.enabled and len(lines) == 1 and leftovers == [],
          {"lines": lines, "left": leftovers})
    blocker = Path(tmp) / "a-file"
    blocker.write_text("x", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cache = sc.DiskCache(blocker / "sub")
        value = sc.Slot(cache, "blocked", lambda: sc.cache_key("blocked"), lambda: {"ok": 1}).get()
    check("a cache folder under a file turns the cache off and still serves",
          value == {"ok": 1} and not cache.enabled)


def test_cooldown_and_spacing(tmp):
    print("429 cool-down and spacing")
    src, tenant = wiz_source(sc.DiskCache(Path(tmp) / "calm", max_age=3600))
    src.schema()
    before = counts(tenant)[0]
    src.client._calm_until = time.time() + 1.5
    state = src.slots["shape"].refresh_background()
    time.sleep(0.8)
    held = counts(tenant)[0]
    check("a background refresh does not send while the 429 cool-down runs",
          state == "started" and held == before and src.slots["shape"].info().get("rebuilding"),
          {"state": state, "before": before, "held": held})
    ok = wait_until(lambda: counts(tenant)[0] > before and not src.slots["shape"].info().get("rebuilding"), 6)
    check("the refresh runs once the cool-down ends", ok, counts(tenant))
    log = []
    spaced, tenant2 = wiz_source(sc.DiskCache(Path(tmp) / "spaced", max_age=3600), log=log)
    spaced.rebuild_gap = 0.6
    spaced.schema()
    spaced.schema(full=True)
    log.clear()
    spaced.slots["shape"].refresh_background()
    spaced.slots["full"].refresh_background()
    ok = wait_until(lambda: not spaced.slots["shape"].info().get("rebuilding")
                    and not spaced.slots["full"].info().get("rebuilding"), 8)
    probes = [stamp for stamp, document in log if tg.is_shape_probe(document)]
    fulls = [stamp for stamp, document in log if tg.is_full_introspection(document)]
    check("the probe rebuild and the full introspection rebuild are spaced",
          ok and probes and fulls and fulls[0] - probes[-1] >= 0.55,
          {"probes": probes, "fulls": fulls})


def test_real_429_sets_cooldown(tmp):
    print("a real 429 sets the cool-down")
    state = {"slow": False}
    log = []
    tenant = tg.LiveTenant()

    def opener(request, timeout=None):
        body = json.loads(request.data.decode())
        document = body.get("query") or ""
        log.append((time.time(), document))
        if state["slow"]:
            return tg.Resp(429, b'{"errors":[{"message":"slow down"}]}', {"Retry-After": "1.5"})
        code, payload = tenant.answer(document, body.get("variables") or {})
        return tg.Resp(code, json.dumps(payload).encode())

    built = tg.client(opener, max_retries=0)
    src = gql.GraphQLSource({"id": "wiz", "label": "Wiz", "endpoint": "https://calm.example.test/graphql",
                             "adapter": "wiz", "token_env": "GRAPHQL_TOKEN", "project_id": "*"},
                            client=built, cache=sc.DiskCache(Path(tmp) / "real429", max_age=3600))
    src.rebuild_gap = 0.0
    src.schema()
    state["slow"] = True
    raised = None
    try:
        src.client.execute("query { __typename }", {}, cache_ttl=0)
    except gql.RateLimited as exc:
        raised = exc
    calm = src.client._calm_until - time.time()
    check("the last 429 with Retry-After sets the cool-down when no retry is left",
          raised is not None and 1.0 < calm <= 1.5, {"calm": calm})
    state["slow"] = False
    before = len(log)
    started = time.time()
    src.slots["shape"].refresh_background()
    time.sleep(0.7)
    held = len(log) == before
    ok = wait_until(lambda: len(log) > before and not src.slots["shape"].info().get("rebuilding"), 6)
    first = log[before][0] - started if len(log) > before else None
    check("a background refresh right after a real 429 waits out Retry-After",
          held and ok and first is not None and first >= 1.0, {"held": held, "first": first})
    fresh = gql.GraphQLSource({"id": "wiz", "label": "Wiz", "endpoint": "https://calm.example.test/graphql",
                               "adapter": "wiz", "token_env": "GRAPHQL_TOKEN", "project_id": "*"},
                              client=tg.client(opener, max_retries=0),
                              cache=sc.DiskCache(Path(tmp) / "real429", max_age=3600))
    fresh.rebuild_gap = 5.0
    check("a new source on the same endpoint keeps the rebuild spacing",
          fresh._next_rebuild > time.time() - 1 and fresh._gate is src._gate, fresh._next_rebuild)
    src._next_rebuild = 0.0


def test_two_slots_one_key(tmp):
    print("two slots on one key")
    folder = Path(tmp) / "twoslots"
    cache = sc.DiskCache(folder, max_age=60)

    def key():
        return sc.cache_key("twoslots", n=1)

    cache.write(key(), "twoslots", {"n": 0}, built_at=time.time() - 3600, build_seconds=1.0)
    gate = threading.Event()
    builds = []

    def build():
        builds.append(1)
        gate.wait(5)
        return {"n": len(builds)}

    a = sc.Slot(sc.DiskCache(folder, max_age=60), "twoslots", key, build)
    b = sc.Slot(sc.DiskCache(folder, max_age=60), "twoslots", key, build)
    first_a = a.peek()
    first_b = b.peek()
    gate.set()
    done = wait_until(lambda: not a.info().get("rebuilding") and builds, 5)
    later_b = b.peek()
    time.sleep(0.3)
    check("a slot that held the stale entry takes the newer file and starts no second build",
          done and first_a == {"n": 0} and first_b == {"n": 0} and later_b == {"n": 1} and len(builds) == 1,
          {"builds": len(builds), "later": later_b})


def test_secret_guard(tmp):
    print("credential guard on the data model")
    folder = Path(tmp) / "guard"
    old = os.environ.get("NEO4J_PASSWORD")
    os.environ["NEO4J_PASSWORD"] = "password"
    try:
        fake = FakeNeo4j(props={"passwordlastused": "x", "token": "t", "secret": "s", "name": "x"})
        graph = make_graph(sc.DiskCache(folder), fake=fake)
        graph.schema()
        note = graph.schema_slot.info().get("note")
        check("NEO4J_PASSWORD=password and properties named password, token, and secret still save the model",
              len(files(folder)) == 1 and not note, {"files": files(folder), "note": note})
    finally:
        if old is None:
            os.environ.pop("NEO4J_PASSWORD", None)
        else:
            os.environ["NEO4J_PASSWORD"] = old
    check("a whole value equal to a known secret is a leak", sc.holds_secret({"a": ["password"]}, ["password"]))
    check("a short secret inside a longer value is not a leak",
          not sc.holds_secret({"a": "passwordlastused"}, ["password"]))
    check("a long secret inside a value is a leak", sc.holds_secret({"a": f"x {TOKEN} y"}, [TOKEN]))
    check("a Bearer value is a leak", sc.holds_secret({"a": "Bearer abcdefghijklmnop"}, []))
    check("a key named password is not a leak", not sc.holds_secret({"password": "str"}, []))


def test_no_secrets(tmp):
    print("no secrets on disk")
    folder = Path(tmp) / "secrets"
    graph = make_graph(sc.DiskCache(folder))
    graph.schema()
    src, _ = wiz_source(sc.DiskCache(folder), client_id=CLIENT, oauth=True)
    src.schema()
    src.schema(full=True)
    text = blob(folder)
    check("three cache files exist", len(files(folder)) == 3, files(folder))
    check("no cache file holds the token, the secret, the client id, the minted token, or the password",
          all(value not in text for value in (TOKEN, SECRET, CLIENT, MINTED, PASSWORD)))
    modes = {oct(path.stat().st_mode & 0o777) for path in files(folder)}
    check("each cache file is mode 0600 in a 0700 folder",
          modes == {"0o600"} and oct(folder.stat().st_mode & 0o777) == "0o700", modes)
    risky = Path(tmp) / "risky"
    fake = FakeNeo4j(props={"name": "x"})
    fake.labels = {"AWSAccount": 2, PASSWORD: 1}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        guarded = make_graph(sc.DiskCache(risky), fake=fake)
        model = guarded.schema()
    note = guarded.schema_slot.info().get("note") or ""
    check("a data model that holds the password as a value is served and not saved",
          PASSWORD in json.dumps(model) and files(risky) == [] and "credential" in note, note)
    view = server._cache_items(guarded) if hasattr(server, "_cache_items") else []
    check("the cache view carries the refusal note, so the page can say why", any(
        "credential" in str(item.get("note") or "") for item in view), view)
    slot = sc.Slot(sc.DiskCache(risky), "token", lambda: sc.cache_key("token"),
                   lambda: {"leak": f"x {TOKEN} y"}, secrets=lambda: [TOKEN])
    slot.get()
    check("a payload that holds a known secret is not saved", files(risky) == [])


# ---- the explorer API ----------------------------------------------------------------

def http_json(method, url, payload=None, timeout=20):
    return tg.http_json(method, url, payload) if timeout == 20 else _http_json(method, url, payload, timeout)


def _http_json(method, url, payload, timeout):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, raw, json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, raw, json.loads(raw or "{}")


class Explorer:
    """server.py on a free port with a temp config, an empty .env, and the given cache folder."""

    def __init__(self, mock_port, cache_dir, tmp, extra=None):
        self.port = tg.free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("GRAPHQL_", "NEO4J_", "GX_")):
                env.pop(key, None)
        env_file = Path(tmp) / f"env-{self.port}"
        env_file.write_text("", encoding="utf-8")
        env.update({
            "HOST": "127.0.0.1", "PORT": str(self.port), "QUIET": "1", "PYTHONUNBUFFERED": "1",
            "GX_CONFIG": str(Path(tmp) / "gx.local.json"),
            "GX_ENV": str(env_file),
            "GX_CACHE_DIR": str(cache_dir),
            "GX_CACHE_GAP": "0.3",
            "GRAPHQL_ENDPOINT": f"http://127.0.0.1:{mock_port}/graphql",
            "GRAPHQL_TOKEN": TOKEN,
            "GRAPHQL_ADAPTER": "wiz",
            "GRAPHQL_LABEL": "Wiz",
            "GRAPHQL_ID": "wiz",
        })
        env.update(extra or {})
        self.log = Path(tmp) / f"server-{self.port}.log"
        self.handle = open(self.log, "wb")
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env,
                                     stdout=self.handle, stderr=subprocess.STDOUT)
        self.status = None
        for _ in range(120):
            if self.proc.poll() is not None:
                break
            try:
                code, _, body = http_json("GET", self.base + "/api/status")
                if code == 200:
                    self.status = body
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.handle.close()
        return self.log.read_text(encoding="utf-8", errors="replace")


def start_mock():
    mock, port = tg._start_tenant_mock()
    delay = {"seconds": 0.0}
    plain = mock.answer

    def answer(document, variables):
        if delay["seconds"] and ("__type(" in document or "__schema" in document):
            time.sleep(delay["seconds"])
        return plain(document, variables)

    mock.answer = answer
    return mock, port, delay


def items_of(view):
    return {item.get("what"): item for item in (view or {}).get("items") or []}


def test_api(tmp, mock, mock_port, delay):
    print("cache API")
    cache_dir = Path(tmp) / "api-cache"
    first = Explorer(mock_port, cache_dir, tmp)
    try:
        check("the explorer starts on the Wiz mock",
              first.status is not None and (first.status.get("source") or {}).get("id") == "wiz",
              first.status)
        if first.status is None:
            return
        code, raw, light = http_json("GET", first.base + "/api/schema")
        code2, _, whole = http_json("GET", first.base + "/api/schema?full=1")
        check("/api/schema says the schema is cached and how long it took",
              code == 200 and (light.get("cache") or {}).get("cached") is True
              and (light.get("cache") or {}).get("build_seconds") is not None
              and code2 == 200 and "Query" in (whole.get("categories") or {}) and TOKEN not in raw,
              light.get("cache"))
        check("the first start sends one probe and one full introspection",
              counts(mock.tenant) == (1, 1), counts(mock.tenant))
        code, raw, view = http_json("GET", first.base + "/api/cache")
        shape, full = items_of(view).get("shape") or {}, items_of(view).get("full") or {}
        check("GET /api/cache lists the shape and the full schema with age, build time, and size",
              code == 200 and shape.get("cached") and full.get("cached") and shape.get("on_disk")
              and shape.get("built_at") and shape.get("age", -1) >= 0 and shape.get("build_seconds") is not None
              and full.get("size", 0) > 0 and shape.get("rebuilding") is False and TOKEN not in raw,
              view)
    finally:
        log = first.stop()
    check("the first server log omits the token", TOKEN not in log)
    mock.tenant.requests.clear()
    second = Explorer(mock_port, cache_dir, tmp)
    try:
        if second.status is None:
            check("the explorer restarts", False, second.stop()[-400:])
            return
        http_json("GET", second.base + "/api/schema")
        code, _, whole = http_json("GET", second.base + "/api/schema?full=1")
        check("a restart on the same connection sends no schema query",
              counts(mock.tenant) == (0, 0) and code == 200 and "Query" in (whole.get("categories") or {}),
              counts(mock.tenant))
        _, before = entry_of(cache_dir, "graphql-shape")
        _, before_full = entry_of(cache_dir, "graphql-full")
        delay["seconds"] = 0.8
        started = time.perf_counter()
        code, raw, kicked = http_json("POST", second.base + "/api/cache/refresh", {"what": "all"})
        took = time.perf_counter() - started
        busy = items_of(kicked.get("cache"))
        check("POST /api/cache/refresh returns at once and reports the rebuild",
              code == 200 and took < 0.6 and sorted(kicked.get("started") or []) == ["full", "shape"]
              and busy.get("shape", {}).get("rebuilding") and TOKEN not in raw,
              {"took": took, "body": kicked})
        idle = wait_until(lambda: not any(
            item.get("rebuilding") for item in items_of(http_json("GET", second.base + "/api/cache")[2]).values()),
            15, 0.2)
        delay["seconds"] = 0.0
        _, after = entry_of(cache_dir, "graphql-shape")
        _, after_full = entry_of(cache_dir, "graphql-full")
        check("the background refresh rewrites both entries",
              idle and counts(mock.tenant)[0] >= 1 and counts(mock.tenant)[1] == 1
              and after["built_at"] > before["built_at"] and after_full["built_at"] > before_full["built_at"],
              counts(mock.tenant))
        probes = counts(mock.tenant)[0]
        code, _, _ = http_json("GET", second.base + "/api/schema?refresh=1")
        _, rewritten = entry_of(cache_dir, "graphql-shape")
        check("/api/schema?refresh=1 still reads the source and rewrites the entry",
              code == 200 and counts(mock.tenant)[0] == probes + 1 and rewritten["built_at"] > after["built_at"],
              counts(mock.tenant))
        code, _, refused = http_json("POST", second.base + "/api/cache/refresh", {"what": "everything"})
        check("an unknown refresh target is refused", code == 400 and refused.get("error"), refused)
        code, raw, cleared = http_json("POST", second.base + "/api/cache/clear", {})
        gone = items_of(cleared.get("cache"))
        check("POST /api/cache/clear drops the files for the active source",
              code == 200 and files(cache_dir) == [] and gone.get("shape", {}).get("cached") is False
              and gone.get("full", {}).get("cached") is False,
              {"files": files(cache_dir), "body": cleared})
        probes = counts(mock.tenant)[0]
        http_json("GET", second.base + "/api/schema")
        check("the next read after a clear builds and saves again",
              counts(mock.tenant)[0] == probes + 1 and entry_of(cache_dir, "graphql-shape")[0] is not None,
              counts(mock.tenant))
    finally:
        log = second.stop()
    check("the second server log omits the token", TOKEN not in log)
    return cache_dir


def test_headed(tmp, mock, mock_port, delay, cache_dir):
    print("headed cache page")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  skip headed page (playwright is not installed)")
        return
    path, data = entry_of(cache_dir, "graphql-shape")
    if not path:
        check("the headed check has a saved shape", False)
        return
    data["built_at"] = time.time() - 7200
    path.write_text(json.dumps(data), encoding="utf-8")
    delay["seconds"] = 2.0
    explorer = Explorer(mock_port, cache_dir, tmp, {"GX_CACHE_MAX_AGE": "3600"})
    where = "start"
    try:
        if explorer.status is None:
            check("the headed explorer starts", False)
            return
        with sync_playwright() as p:
            chrome = tg._chrome_executable(p)
            if not chrome:
                print("  skip headed page (no browser: set CHROME or run playwright install chromium)")
                return
            browser = p.chromium.launch(headless=False, executable_path=chrome)
            page = browser.new_context(viewport={"width": 1280, "height": 860}, locale="en-US").new_page()
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            land = "() => (document.querySelector('#land-cache [data-cache-text]') || {}).textContent || ''"
            toast_has = "(t) => (document.querySelector('#toast').textContent || '').includes(t)"
            clear_toast = "() => { document.querySelector('#toast').textContent = ''; }"
            where = "boot"
            page.goto(explorer.base + "/", wait_until="domcontentloaded")
            page.wait_for_function("() => typeof S !== 'undefined' && S.ready === true", timeout=40000)
            where = "stale note"
            page.wait_for_function(
                "() => (document.querySelector('#land-cache') || {}).textContent.includes('Refreshing in the background')",
                timeout=15000)
            stale = page.evaluate(land)
            spinning = page.evaluate("() => !!document.querySelector('#land-cache .land-spin')")
            check("a stale cached schema shows its label, its age, and a background refresh note",
                  re.search(r"Wiz search shape cached 2 h ago \(built in (<0\.1|[\d.]+) s\)", stale) and spinning, stale)
            where = "background toast"
            page.wait_for_function(toast_has, arg="Wiz search shape refreshed", timeout=20000)
            page.wait_for_function(
                "() => !(document.querySelector('#land-cache').textContent || '').includes('Refreshing')",
                timeout=10000)
            fresh = page.evaluate(land)
            check("the background refresh ends with a toast and a new age",
                  re.search(r"Wiz search shape cached \d+ s ago \(built in (<0\.1|[\d.]+) s\)", fresh), fresh)
            where = "live age"
            aged = page.evaluate("""() => {
                CACHE.at = Date.now() - 180000;
                tickCache();
                return { land: document.querySelector('#land-cache [data-cache-text]').textContent,
                         chip: document.querySelector('#cache-chip').textContent };
            }""")
            check("the landing line and the chip age move forward with no new cache event",
                  "cached 3 min ago" in aged["land"] and aged["chip"].endswith("· 3 min")
                  and aged["chip"].startswith("Wiz search shape"), aged)
            where = "old error in another slot"
            def with_old_error(route):
                response = route.fetch()
                body = response.json()
                for item in body.get("items") or []:
                    if item.get("what") == "full":
                        item["error"] = "an old full schema failure"
                route.fulfill(response=response, json=body)
            page.route("**/api/cache", with_old_error)
            page.evaluate(clear_toast)
            page.click("#land-cache [data-act=cache-refresh]")
            page.wait_for_function(
                "(t) => { const x = document.querySelector('#toast').textContent || ''; return x.includes(t) || x.includes('failed'); }",
                arg="Wiz search shape refreshed", timeout=20000)
            shown = page.evaluate("() => document.querySelector('#toast').textContent")
            page.unroute("**/api/cache")
            check("an old error in the full schema does not fail a shape refresh",
                  "Wiz search shape refreshed" in shown and "failed" not in shown, shown)
            where = "refresh button"
            probes = counts(mock.tenant)[0]
            page.evaluate(clear_toast)
            page.click("#land-cache [data-act=cache-refresh]")
            page.wait_for_selector("#land-cache .land-spin", timeout=5000)
            disabled = page.evaluate("() => document.querySelector('#land-cache [data-act=cache-refresh]').disabled")
            page.wait_for_function(toast_has, arg="Wiz search shape refreshed", timeout=20000)
            check("the Refresh button shows a spinner, reads the source, and ends with a toast",
                  disabled and counts(mock.tenant)[0] > probes, counts(mock.tenant))
            where = "data model"
            page.click("[data-act=data-model]")
            page.wait_for_function(
                "() => (document.querySelector('[data-dm-foot]') || {}).textContent.includes('node categories')",
                timeout=30000)
            page.wait_for_function(
                "() => (document.querySelector('[data-dm-cache]') || {}).textContent.includes('Wiz full schema cached')",
                timeout=10000)
            dm_text = page.evaluate("() => document.querySelector('[data-dm-cache]').textContent")
            land_text = page.evaluate(land)
            check("the data model window names the full schema, and the landing names the search shape",
                  re.search(r"Wiz full schema cached .+ ago \(built in (<0\.1|[\d.]+) s\)", dm_text)
                  and land_text.startswith("Wiz search shape cached"), {"dm": dm_text, "land": land_text})
            # Pick the last category, so a reload that jumps back to the first one shows.
            picked = page.evaluate("""() => {
                const box = document.querySelector('.modal.dm');
                const nodes = box._dm.nodes();
                const last = nodes[nodes.length - 1];
                last.emit('tap');
                const n = box._dm.getElementById(nodes[0].id());
                return { cat: last.data('cat'), first: nodes[0].data('cat'),
                         x: n.position('x'), y: n.position('y') };
            }""")
            fulls = counts(mock.tenant)[1]
            page.evaluate(clear_toast)
            page.click("[data-dm-refresh]")
            page.wait_for_selector("[data-dm-cache] .land-spin", timeout=5000)
            page.wait_for_function(toast_has, arg="Wiz full schema refreshed", timeout=25000)
            check("the data model Refresh rebuilds the full schema", counts(mock.tenant)[1] > fulls,
                  counts(mock.tenant))
            page.wait_for_function("() => document.querySelector('.modal.dm')._dm && !document.querySelector('[data-dm-cache] .land-spin')")
            page.wait_for_timeout(300)
            after = page.evaluate("""(firstCat) => {
                const box = document.querySelector('.modal.dm');
                const n = box._dm.nodes().filter(node => node.data('cat') === firstCat)[0];
                return { shown: (box.querySelector('[data-dminfo] .chip, [data-dminfo]') || {}).textContent || '',
                         x: n ? n.position('x') : null, y: n ? n.position('y') : null };
            }""", picked["first"])
            check("a data model Refresh keeps the chosen category and the layout",
                  picked["cat"] != picked["first"] and picked["cat"] in after["shown"]
                  and abs((after["x"] or 0) - picked["x"]) < 0.5 and abs((after["y"] or 0) - picked["y"]) < 0.5,
                  {"picked": picked, "after": after})
            page.click(".modal.dm [data-close]")
            where = "diagnostics"
            page.click(".land-connect [data-act=diagnostics]")
            page.wait_for_selector(".modal.diag [data-dclear]")
            page.evaluate(clear_toast)
            page.click(".modal.diag [data-dclear]")
            page.wait_for_function(toast_has, arg="Cleared the schema cache", timeout=10000)
            _, _, view = http_json("GET", explorer.base + "/api/cache")
            check("Clear cache in Diagnostics drops the saved entries",
                  files(cache_dir) == [] and not any(item.get("cached") for item in items_of(view).values()),
                  {"files": files(cache_dir), "view": view})
            page.click(".modal.diag [data-close]")
            check("the cache page raised no script error", not errors, errors)
            browser.close()
    except Exception as exc:  # noqa: BLE001
        check("the headed cache page finishes", False, where + ": " + str(exc))
        return
    finally:
        delay["seconds"] = 0.0
        log = explorer.stop()
        check("the headed server log omits the token", TOKEN not in log)
    print("  HEADED cache ok")


def main():
    if sc is None:
        print(f"  FAIL schema_cache imports: {IMPORT_ERROR}")
        print("\n1 failed")
        return 1
    saved = {key: os.environ.get(key) for key in list(os.environ)
             if key.startswith(("GRAPHQL_", "GX_CACHE"))}
    for key in saved:
        os.environ.pop(key, None)
    tmp = tempfile.mkdtemp(prefix="gx-cache-test-")
    repo_cache = ROOT / ".cache"
    before = set(files(repo_cache))
    mock = None
    try:
        test_files_and_docs()
        test_neo4j_restart(tmp)
        test_neo4j_key_isolation(tmp)
        test_userinfo_and_old_file(tmp)
        test_graphql_restart_and_keys(tmp)
        test_no_host_on_disk(tmp)
        test_version(tmp)
        test_stale_while_revalidate(tmp)
        test_single_rebuild(tmp)
        test_failed_rebuild(tmp)
        test_corrupt(tmp)
        test_read_only(tmp)
        test_cooldown_and_spacing(tmp)
        test_real_429_sets_cooldown(tmp)
        test_two_slots_one_key(tmp)
        test_secret_guard(tmp)
        test_no_secrets(tmp)
        mock, mock_port, delay = start_mock()
        cache_dir = test_api(tmp, mock, mock_port, delay)
        if cache_dir:
            test_headed(tmp, mock, mock_port, delay, cache_dir)
        check("the tests write nothing to the repository .cache folder",
              set(files(repo_cache)) == before, sorted(set(files(repo_cache)) - before))
    finally:
        if mock is not None:
            mock.shutdown()
            mock.server_close()
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
        subprocess.run(["chmod", "-R", "u+rwx", tmp], check=False)
        subprocess.run(["rm", "-rf", tmp], check=False)
    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print("\nall cache checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
