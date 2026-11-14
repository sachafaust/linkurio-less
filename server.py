"""Read-only graph explorer backend (stdlib HTTP server + official neo4j driver).

Serves ./static and a small JSON API. Every Cypher statement runs in a READ
transaction (session default_access_mode=READ + execute_read), and user-written
Cypher is also screened for write clauses before it is sent.

Neo4j is one data source. A GraphQL endpoint configured below is a second
source, shown on its own and never copied into Neo4j. The handlers call the
same methods on whichever source is active: status, schema, search, nodes,
neighbor_summary, expand, edges_between, and cypher. The bearer token stays
in the environment or in the gitignored .env file.

Config (environment variables; a gitignored .env fills any of the four that are unset):
    NEO4J_URI       bolt://localhost:7687   (host only: put the user and password in the variables below)
    NEO4J_USER      neo4j
    NEO4J_PASSWORD  (required; empty is allowed when Neo4j has no password)
    NEO4J_DATABASE  neo4j
    GX_ENV          path to a different .env file
    HOST / PORT     127.0.0.1 / 8744
    SEARCH_FULLTEXT_INDEX  name of a Neo4j full-text index for search (auto-detects "gx_search",
                    made by tools/create_fulltext_index.py)
    MAX_NODES / MAX_EDGES   safety ceiling for one response. When unset, the ceiling comes
                    from this machine's RAM. The working set lives in gx.local.json (or
                    GX_CONFIG). The first launch creates that file from CPU and RAM. The
                    page can add the GPU and save a new working set, up to the ceiling.
    EXPAND_LIMIT    hard maximum neighbors per expand. When unset, it comes from RAM.
                    EXPAND_MAX can only lower that. The page's budget.expand_limit can
                    lower it further.
    CYPHER_ROW_LIMIT  hard row cap for /api/cypher. When unset, it comes from RAM. The
                    statement is wrapped so a query with no LIMIT cannot stream the graph.
                    The page's budget.cypher_limit can lower it.
    HUB_SAMPLE      neighbors read per node for summaries / degree ranking (20000)
    EDGE_WALK_MAX   edge fill will not walk a node with more relationships than this (2000)
    SEARCH_SCAN_CAP nodes scanned per search without a full-text index (200000)
    CACHE_TTL / SCHEMA_TTL  seconds to cache neighbor summaries (60) / the schema (600)

GraphQL (optional second source; omit it to use Neo4j only):
    GRAPHQL_ENDPOINT    https://api.us2.app.wiz.io/graphql
    GRAPHQL_TOKEN       bearer token. Data connection can save it in the gitignored
                        .env. It is never written to gx.local.json, logs, or the page.
    GRAPHQL_ADAPTER     wiz (default on a wiz.io host) or generic
    GRAPHQL_LABEL / GRAPHQL_ID / GRAPHQL_TOKEN_ENV / GRAPHQL_PROJECT_ID
    A graphql list in gx.local.json can name the endpoint and the token's
    variable. A token value in that file is removed. GRAPHQL_MAX_INFLIGHT
    caps concurrent requests (4).
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit


def _use_project_venv():
    """Run server.py with the project venv when this Python has no neo4j driver."""
    if __name__ != "__main__":
        return
    try:
        import neo4j  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    root = Path(__file__).resolve().parent
    folder = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python3"
    venv_py = root / ".venv" / folder / name
    if venv_py.is_file() and Path(sys.executable).resolve() != venv_py.resolve() and not os.environ.get("GX_BOOTSTRAPPED"):
        os.environ["GX_BOOTSTRAPPED"] = "1"
        os.execv(str(venv_py), [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]])
    pip = ".venv\\Scripts\\pip" if os.name == "nt" else ".venv/bin/pip"
    py = ".venv\\Scripts\\python" if os.name == "nt" else ".venv/bin/python3"
    print(
        "The neo4j driver is not installed for this Python.\n"
        "From this folder:\n"
        "  python3 -m venv .venv\n"
        f"  {pip} install -r requirements.txt\n"
        f"  {py} server.py",
        file=sys.stderr,
    )
    raise SystemExit(1)


_use_project_venv()

import neo4j
from neo4j import GraphDatabase, READ_ACCESS
from neo4j.graph import Node, Path as NPath, Relationship

from graphql_source import (
    GraphQLCallError,
    GraphQLConfigError,
    GraphQLSource,
    QueryRejected,
    RateLimited,
    ENV_KEYS,
    TokenExpired,
    _ENV_RE,
    _ID_RE,
    _clean_endpoint,
    load_graphql_env,
    resolve_graphql_sources,
    sources_from_config,
    without_secrets,
)
from neo4j_env import (
    CREDENTIAL_KEYS,
    Neo4jConfigError,
    dotenv_path,
    load_credential_env,
    neo4j_settings,
    redact_uri,
    update_dotenv,
)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CFG = json.loads((ROOT / "data" / "cartography_styles.json").read_text())

# Generic / ontology labels that Cartography adds next to the specific one. They are
# kept on the node, but never chosen as the node's display category.
GENERIC = set(CFG["generic_labels"])
PRIORITY = {lab: i for i, lab in enumerate(CFG["category_priority"])}
SEARCH_FIELDS = CFG["search_fields"]

WRITE_RE = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|LOAD\s+CSV|FOREACH|GRANT|REVOKE|DENY|ALTER|RENAME|START\s+DATABASE|STOP\s+DATABASE|TERMINATE)\b"
    r"|\bIN\s+TRANSACTIONS\b|\bCALL\s+(dbms|apoc\.(create|merge|refactor|periodic|do|nodes\.delete|trigger|schema|load|import|export|cypher\.run(Write|Schema)?))",
    re.IGNORECASE,
)


def strip_cypher_literals(q: str) -> str:
    """Remove string literals and comments so keywords inside them don't trip the screen."""
    q = re.sub(r"//[^\n]*", " ", q)
    q = re.sub(r"/\*.*?\*/", " ", q, flags=re.S)
    q = re.sub(r"'(?:\\.|[^'\\])*'", "''", q)
    q = re.sub(r'"(?:\\.|[^"\\])*"', '""', q)
    q = re.sub(r"`[^`]*`", "``", q)
    return q


def check_read_only(q: str) -> None:
    m = WRITE_RE.search(strip_cypher_literals(q))
    if m:
        raise PermissionError(f"Read-only explorer: '{m.group(0).strip()}' is not allowed")


def category_of(labels) -> str:
    labels = list(labels)
    if not labels:
        return "(no label)"
    ranked = [l for l in labels if l in PRIORITY]
    if ranked:
        return min(ranked, key=PRIORITY.get)
    specific = [l for l in labels if l not in GENERIC] or labels
    return max(specific, key=lambda l: (len(l), l))


def jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if hasattr(v, "iso_format"):
        return v.iso_format()
    return str(v)


def node_json(n: Node, degree=None):
    labels = sorted(n.labels)
    d = {"id": n.element_id, "labels": labels, "category": category_of(labels),
         "props": {k: jsonable(v) for k, v in n.items()}}
    if degree is not None:
        d["degree"] = degree
    return d


def rel_json(r: Relationship):
    return {"id": r.element_id, "type": r.type, "source": r.start_node.element_id, "target": r.end_node.element_id,
            "props": {k: jsonable(v) for k, v in r.items()}}


class TTLCache:
    """Tiny thread-safe TTL + size-bounded cache for read results (schema, neighbor summaries)."""

    def __init__(self, ttl: float, size: int = 512):
        self.ttl, self.size, self.d, self.lock = ttl, size, {}, threading.Lock()

    def get(self, key):
        with self.lock:
            v = self.d.get(key)
            if v and time.time() - v[0] < self.ttl:
                return v[1]
            self.d.pop(key, None)
            return None

    def put(self, key, val):
        with self.lock:
            if len(self.d) >= self.size:
                self.d.pop(next(iter(self.d)))
            self.d[key] = (time.time(), val)
        return val


# Above this many relationships a node is a "hub": summaries are computed from per-type degrees
# (O(1) in Neo4j's degree store) plus a sample of neighbors instead of reading every neighbor.
HUB_SAMPLE = int(os.environ.get("HUB_SAMPLE", 20000))
SCAN_CAP = int(os.environ.get("SEARCH_SCAN_CAP", 200000))
# Edge fill starts at an endpoint and walks its relationships. Past this degree the walk
# is skipped (a hub would time out) and the edge is kept only if the other end is small.
EDGE_WALK_MAX = int(os.environ.get("EDGE_WALK_MAX", 2000))
FILTER_FIELDS = list(dict.fromkeys([*SEARCH_FIELDS, "account", "tags", "tag"]))
_GIB = 1024 ** 3
_SOFTWARE_GPU = re.compile(r"swiftshader|llvmpipe|softpipe|software|basic render", re.I)


def cyq(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _env_int(name):
    """A positive integer from the environment, or None when the variable is unset."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n >= 1 else None


def machine_resources():
    """CPU count and physical RAM of the machine running the server."""
    cpu = os.cpu_count() or 1
    ram = 0
    try:
        ram = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        ram = 0
    if ram <= 0:
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram = int(line.split()[1]) * 1024
                        break
        except (OSError, ValueError, IndexError):
            ram = 0
    if ram <= 0:
        ram = 8 * _GIB
    return {"cpu": int(cpu), "ram_bytes": int(ram)}


def hardware_ceiling(ram_bytes):
    """The most one response may return on a machine with this much RAM.

    The working-set suggestion sits under this, so the page can raise the
    suggestion without a single call being able to pull in the whole graph.
    """
    gb = max(1.0, float(ram_bytes) / _GIB)
    nodes = min(50_000, max(2_000, int(gb * 1_000)))
    edges = min(150_000, max(5_000, nodes * 3))
    if gb < 8:
        nodes, edges, expand, cypher = min(nodes, 4_000), min(edges, 10_000), 100, 1_000
    elif gb < 16:
        nodes, edges, expand, cypher = min(nodes, 8_000), min(edges, 20_000), 200, 2_000
    elif gb < 32:
        nodes, edges, expand, cypher = min(nodes, 16_000), min(edges, 40_000), 300, 2_000
    else:
        expand, cypher = 500, 5_000
    return {"max_nodes": int(nodes), "max_edges": int(edges), "expand_limit": int(expand), "cypher_limit": int(cypher)}


def _round_step(n, step):
    n = max(step, int(n))
    return ((n + step // 2) // step) * step


def suggest_limits(cpu, ram_bytes, gpu="unknown"):
    """A working set for an interactive canvas on this hardware.

    RAM picks the tier. Fewer CPU cores shrink it. A software GPU shrinks it
    again. A hardware GPU on a 16 GB, 8-core machine raises the node count.
    Edges are 2.5 per node. Expand and query caps stay a page, not a dump.
    """
    cpu = max(1, int(cpu or 1))
    gb = max(1.0, float(ram_bytes) / _GIB)
    if gpu not in ("hardware", "software", "unknown"):
        gpu = "unknown"
    if gb < 8:
        nodes, expand, cypher = 1_000, 25, 200
    elif gb < 16:
        nodes, expand, cypher = 2_000, 50, 500
    elif gb < 32:
        nodes, expand, cypher = 4_000, 50, 500
    elif gb < 64:
        nodes, expand, cypher = 8_000, 100, 1_000
    else:
        nodes, expand, cypher = 12_000, 100, 1_000
    if cpu <= 2:
        nodes = max(500, nodes // 2)
        expand = min(expand, 25)
        cypher = min(cypher, 200)
    elif cpu <= 4:
        nodes = max(500, (nodes * 3) // 4)
        expand = min(expand, 50)
    if gpu == "software":
        nodes = max(500, nodes // 2)
        expand = min(expand, 25)
        cypher = min(cypher, 200)
    elif gpu == "hardware" and cpu >= 8 and gb >= 16:
        nodes = min(nodes + nodes // 4, 16_000)
    nodes = _round_step(nodes, 100)
    edges = _round_step((nodes * 5) // 2, 100)
    return {"nodes": nodes, "edges": edges, "expand_limit": int(expand), "cypher_limit": int(cypher)}


def clamp_suggestion(limits, ceilings):
    nodes = max(1, min(int(ceilings["max_nodes"]), int(limits["nodes"])))
    edges = max(1, min(int(ceilings["max_edges"]), int(limits["edges"])))
    expand = max(1, min(int(ceilings["expand_limit"]), int(limits["expand_limit"]), nodes))
    cypher = max(1, min(int(ceilings["cypher_limit"]), int(limits["cypher_limit"]), nodes))
    return {"nodes": nodes, "edges": edges, "expand_limit": expand, "cypher_limit": cypher}


def classify_gpu(kind, renderer):
    renderer = str(renderer or "").strip()[:120]
    if _SOFTWARE_GPU.search(renderer):
        return "software", renderer
    if kind in ("hardware", "software", "unknown"):
        return kind, renderer
    return "unknown", renderer


def _combine_resources(body):
    """Server RAM and CPU, tightened by what the browser reports.

    navigator.deviceMemory is capped at 8 and only a smaller value is a real
    measurement. A reported 8 means "8 or more", so the server's RAM stands.
    """
    base = machine_resources()
    cpu, ram = base["cpu"], base["ram_bytes"]
    body = body or {}
    client_cpu = _as_int(body.get("cpu"), 0)
    if client_cpu > 0:
        cpu = max(1, min(cpu, client_cpu))
    try:
        device_gb = float(body.get("device_memory_gb") or 0)
    except (TypeError, ValueError):
        device_gb = 0
    if 0 < device_gb < 8:
        ram = min(ram, int(device_gb * _GIB))
    gpu, renderer = classify_gpu(body.get("gpu"), body.get("renderer"))
    return cpu, ram, gpu, renderer


def _suggest_reason(cpu, ram_bytes, gpu, renderer, limits, raw):
    gb = ram_bytes / _GIB
    gb_s = f"{gb:.0f}" if abs(gb - round(gb)) < 0.05 else f"{gb:.1f}"
    gpu_s = {"hardware": "a hardware GPU", "software": "a software GPU", "unknown": "no GPU details"}[gpu]
    if renderer and gpu != "unknown":
        gpu_s += f" ({renderer})"
    text = (f"{cpu} CPU cores, {gb_s} GB RAM, {gpu_s}. "
            f"Suggested {limits['nodes']:,} nodes, {limits['edges']:,} edges, "
            f"{limits['expand_limit']} neighbors per expand, {limits['cypher_limit']:,} rows per query.")
    if limits != raw:
        text += (f" This process allows {MAX_NODES:,} nodes and {MAX_EDGES:,} edges, "
                 f"so the suggestion stays inside that.")
    return text


def suggest_for(body=None):
    """Suggestion for the browser that called, clamped to this process's ceiling."""
    cpu, ram, gpu, renderer = _combine_resources(body)
    raw = suggest_limits(cpu, ram, gpu)
    ceilings = {"max_nodes": MAX_NODES, "max_edges": MAX_EDGES,
                "expand_limit": EXPAND_MAX, "cypher_limit": CYPHER_ROW_LIMIT}
    limits = clamp_suggestion(raw, ceilings)
    gb = ram / _GIB
    return {**limits, "reason": _suggest_reason(cpu, ram, gpu, renderer, limits, raw),
            "resources": {"cpu": cpu, "ram_bytes": ram, "ram_gb": round(gb, 1), "gpu": gpu, "renderer": renderer},
            "ceiling": ceilings}


def budget_payload(consume_created=False):
    """Ceilings, the machine, and the working set stored in the config file.

    consume_created is for the HTTP status call. The startup probe also reads
    status, and it must leave the flag for the first page load.
    """
    created = False
    with _CONFIG_LOCK:
        if _WORKING is None:
            _ensure_config_locked()
        created = _take_config_created() if consume_created else _CONFIG_CREATED
        cfg = dict(_WORKING)
    base = machine_resources()
    gb = base["ram_bytes"] / _GIB
    return {"max_nodes": MAX_NODES, "max_edges": MAX_EDGES,
            "expand_limit": EXPAND_MAX, "cypher_limit": CYPHER_ROW_LIMIT,
            "resources": {"cpu": base["cpu"], "ram_bytes": base["ram_bytes"], "ram_gb": round(gb, 1)},
            "suggestion": suggest_for({}),
            "config": cfg, "config_file": config_path().name, "config_created": created}


_HW = hardware_ceiling(machine_resources()["ram_bytes"])
MAX_NODES = _env_int("MAX_NODES") or _HW["max_nodes"]
MAX_EDGES = _env_int("MAX_EDGES") or _HW["max_edges"]
EXPAND_LIMIT = _env_int("EXPAND_LIMIT") or _HW["expand_limit"]
_expand_max_env = _env_int("EXPAND_MAX")
if _expand_max_env:
    EXPAND_LIMIT = min(EXPAND_LIMIT, _expand_max_env)
# A page can be smaller than EXPAND_LIMIT, never larger.
EXPAND_MAX = EXPAND_LIMIT
CYPHER_ROW_LIMIT = _env_int("CYPHER_ROW_LIMIT") or _HW["cypher_limit"]

# The working set. None until the first launch creates gx.local.json, or until a
# saved file is read. A request may lower it. It cannot rise past the ceiling.
_CONFIG_LOCK = threading.RLock()
_WORKING = None
_CONFIG_CREATED = False


def config_path():
    raw = (os.environ.get("GX_CONFIG") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return ROOT / "gx.local.json"


def _ceilings():
    return {"max_nodes": MAX_NODES, "max_edges": MAX_EDGES,
            "expand_limit": EXPAND_MAX, "cypher_limit": CYPHER_ROW_LIMIT}


def _pos_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def _normalize_config(data):
    """Clamp a stored working set onto this process's ceiling.

    Returns None when nodes and edges are not usable, so a foreign JSON file
    is left on disk instead of being replaced. Credential fields are dropped.
    Neo4j auth comes from the environment.
    """
    if not isinstance(data, dict):
        return None
    nodes, edges = _pos_int(data.get("nodes")), _pos_int(data.get("edges"))
    if not nodes or not edges:
        return None
    ceilings = _ceilings()
    expand = _pos_int(data.get("expand_limit")) or min(50, ceilings["expand_limit"])
    cypher = _pos_int(data.get("cypher_limit")) or min(500, ceilings["cypher_limit"])
    clamped = clamp_suggestion(
        {"nodes": nodes, "edges": edges, "expand_limit": expand, "cypher_limit": cypher}, ceilings)
    gpu = data.get("gpu") if data.get("gpu") in ("hardware", "software", "unknown") else "unknown"
    source = data.get("source") if data.get("source") in ("auto", "user") else "user"
    reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
    result = {**clamped, "lru": bool(data.get("lru")), "reason": reason[:500],
              "source": source, "gpu": gpu, "renderer": str(data.get("renderer") or "")[:120]}
    graphql = sources_from_config(data)
    if graphql:
        result["graphql"] = graphql
    return result


def _config_from_suggestion(sug, source, gpu, renderer):
    return _normalize_config({
        "nodes": sug["nodes"], "edges": sug["edges"],
        "expand_limit": sug["expand_limit"], "cypher_limit": sug["cypher_limit"],
        "lru": False, "reason": sug.get("reason") or "", "source": source,
        "gpu": gpu, "renderer": renderer or "",
    })


def _write_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _ensure_config_locked():
    """Load gx.local.json, or create it from CPU and RAM the first time."""
    global _WORKING, _CONFIG_CREATED
    path = config_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            cleaned, removed = without_secrets(raw)
            if removed:
                try:
                    _write_config(path, cleaned)
                except OSError as e:
                    print(f"WARNING: could not remove a GraphQL token from {path.name}: {e}")
                raw = cleaned
        norm = _normalize_config(raw) if isinstance(raw, dict) else None
        if norm:
            _WORKING = norm
            _CONFIG_CREATED = False
            return norm
        print(f"WARNING: {path} is not a working-set config. It was left unchanged.")
    sug = suggest_for({})
    data = _config_from_suggestion(sug, "auto", "unknown", "")
    if not path.is_file():
        try:
            _write_config(path, data)
            _CONFIG_CREATED = True
        except OSError as e:
            print(f"WARNING: could not write {path}: {e}")
            _CONFIG_CREATED = False
    else:
        _CONFIG_CREATED = False
    _WORKING = data
    return data


def ensure_config():
    with _CONFIG_LOCK:
        return dict(_ensure_config_locked())


def _take_config_created():
    global _CONFIG_CREATED
    created = _CONFIG_CREATED
    _CONFIG_CREATED = False
    return created


def _store_config_locked(body, source):
    global _WORKING
    current = _WORKING or _ensure_config_locked()
    body = body or {}
    reason = body["reason"] if isinstance(body.get("reason"), str) else (current.get("reason") or "")
    gpu = current.get("gpu") or "unknown"
    renderer = current.get("renderer") or ""
    if source == "auto":
        if body.get("gpu") in ("hardware", "software", "unknown"):
            gpu = body.get("gpu")
        if isinstance(body.get("renderer"), str):
            renderer = body.get("renderer")
    merged = {
        "nodes": body.get("nodes", current["nodes"]),
        "edges": body.get("edges", current["edges"]),
        "expand_limit": body.get("expand_limit", current["expand_limit"]),
        "cypher_limit": body.get("cypher_limit", current["cypher_limit"]),
        "lru": body["lru"] if "lru" in body else current.get("lru"),
        "reason": reason, "source": source, "gpu": gpu, "renderer": renderer,
    }
    if current.get("graphql"):
        merged["graphql"] = current["graphql"]
    norm = _normalize_config(merged)
    if norm is None:
        raise ValueError("nodes and edges must be at least 1")
    norm["source"] = source if source in ("auto", "user") else "user"
    path = config_path()
    if path.is_file():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path.name} is not valid JSON, so it was left unchanged") from e
        except OSError as e:
            raise ValueError(f"could not read {path.name}") from e
    try:
        _write_config(path, norm)
    except OSError as e:
        raise ValueError(f"could not write {path.name}") from e
    _WORKING = norm
    return dict(norm)


def save_config(body):
    """Store the working set the UI saved. The next launch reads the same file."""
    with _CONFIG_LOCK:
        return _store_config_locked(body, "user")


def refine_config(body):
    """Fold the browser GPU into a config that the first launch wrote without one.

    A working set saved from the UI, or one that already names a GPU, stays as it is.
    """
    sug = suggest_for(body or {})
    gpu = (sug.get("resources") or {}).get("gpu") or "unknown"
    with _CONFIG_LOCK:
        if _WORKING is None:
            _ensure_config_locked()
        current = _WORKING
        if current.get("source") != "auto" or current.get("gpu") != "unknown" or gpu == "unknown":
            return {"updated": False, "config": dict(current)}
        saved = _store_config_locked({
            "nodes": sug["nodes"], "edges": sug["edges"],
            "expand_limit": sug["expand_limit"], "cypher_limit": sug["cypher_limit"],
            "lru": False, "reason": sug.get("reason") or "",
            "gpu": gpu, "renderer": (sug.get("resources") or {}).get("renderer") or "",
        }, "auto")
        return {"updated": True, "config": saved}


def _active_cap(hard, key):
    """The configured working set, when one is loaded, else the hard ceiling."""
    cfg = _WORKING or {}
    raw = cfg.get(key)
    if raw is None or raw == "":
        return hard
    value = _as_int(raw, hard)
    floor = 1 if key in ("expand_limit", "cypher_limit") else 0
    return max(floor, min(hard, value))


def session_caps(body, max_nodes=None, max_edges=None):
    """What this call may still return.

    The hard ceilings are MAX_NODES, MAX_EDGES, EXPAND_MAX, and CYPHER_ROW_LIMIT.
    A loaded config file lowers those to the saved working set. The page may
    send a still lower session budget. on_screen is what the canvas already
    holds, so the call receives only the room that is left. A missing or
    hostile count still cannot make one response larger than that cap.
    """
    body = body or {}
    hard_n = MAX_NODES if max_nodes is None else max_nodes
    hard_e = MAX_EDGES if max_edges is None else max_edges
    ceil_n = _active_cap(hard_n, "nodes") if max_nodes is None else hard_n
    ceil_e = _active_cap(hard_e, "edges") if max_edges is None else hard_e
    sess = body.get("budget") or {}

    def lower(ceiling, key):
        if sess.get(key) is None or sess.get(key) == "":
            return ceiling
        return max(0, min(ceiling, _as_int(sess.get(key), ceiling)))

    cap_n, cap_e = lower(ceil_n, "nodes"), lower(ceil_e, "edges")
    on = body.get("on_screen") or {}
    have_n = max(0, _as_int(on.get("nodes"), 0))
    have_e = max(0, _as_int(on.get("edges"), 0))
    expand_hard = _active_cap(EXPAND_MAX, "expand_limit")
    cypher_hard = _active_cap(CYPHER_ROW_LIMIT, "cypher_limit")
    return {"max_nodes": cap_n, "max_edges": cap_e,
            "remain_nodes": max(0, cap_n - have_n), "remain_edges": max(0, cap_e - have_e),
            "expand_limit": max(1, lower(expand_hard, "expand_limit")),
            "cypher_limit": max(1, lower(cypher_hard, "cypher_limit"))}


def truncation(message, reasons, **extra):
    reasons = [r for r in reasons if r]
    out = {"truncated": bool(reasons), "message": message if reasons else None, "reasons": reasons}
    out.update(extra)
    return out


def mask_cypher(q: str) -> str:
    """Copy of q with comments and string contents blanked. Length and indexes stay the same."""
    out = list(q)
    i, n = 0, len(q)
    while i < n:
        if q.startswith("//", i):
            j = q.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if q.startswith("/*", i):
            j = q.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if q[i] in "'\"":
            quote, j = q[i], i + 1
            while j < n:
                if q[j] == "\\":
                    j += 2
                    continue
                if q[j] == quote:
                    j += 1
                    break
                j += 1
            for k in range(i + 1, max(i + 1, j - 1)):
                out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def apply_hard_limit(query: str, limit: int) -> str:
    """Wrap one read statement so Neo4j itself stops after `limit` rows.

    A LIMIT already on the statement is kept when it is smaller, and lowered
    when it is larger. The outer LIMIT is a second cap in case the inner one
    binds only to the last UNION part.
    """
    q = query.strip()
    if q.endswith(";"):
        q = q[:-1].rstrip()
    if not q:
        raise ValueError("Empty query")
    masked = mask_cypher(q)
    if ";" in masked:
        raise PermissionError("Read-only explorer: one statement at a time")
    limit = int(limit)
    m = re.search(r"\bLIMIT\s+(\d+)\s*$", masked, re.IGNORECASE)
    if m:
        if int(m.group(1)) > limit:
            q = q[:m.start(1)] + str(limit) + q[m.end(1):]
        inner = q
    else:
        inner = q + f"\nLIMIT {limit}"
    return f"CALL {{\n{inner}\n}}\nRETURN * LIMIT {limit}"


def graph_count_totals(data):
    """Read store totals from ``db.stats.retrieve('GRAPH COUNTS')``.

    The total node count is the entry with no label. The total relationship
    count is the entry with no relationship type. Entries that also name a
    start or end label are patterns, not type totals. Returns None when
    either total is missing.
    """
    if not isinstance(data, dict):
        return None
    nodes, rels = data.get("nodes"), data.get("relationships")
    if not isinstance(nodes, list) or not isinstance(rels, list):
        return None
    node_total, labels = None, {}
    for entry in nodes:
        if not isinstance(entry, dict) or "count" not in entry:
            continue
        count = int(entry["count"])
        label = entry.get("label")
        if label is None:
            node_total = count
        else:
            labels[str(label)] = count
    rel_total, types = None, {}
    for entry in rels:
        if not isinstance(entry, dict) or "count" not in entry:
            continue
        if entry.get("startLabel") is not None or entry.get("endLabel") is not None:
            continue
        count = int(entry["count"])
        kind = entry.get("relationshipType")
        if kind is None:
            rel_total = count
        else:
            types[str(kind)] = count
    if node_total is None or rel_total is None:
        return None
    return {"nodes": node_total, "edges": rel_total, "labels": labels, "types": types}


class Graph:
    def __init__(self, settings=None):
        settings = settings or neo4j_settings()
        self.uri = settings.uri
        self.db = settings.database
        self.id = "neo4j"
        self.label = "Neo4j"
        self.kind = "neo4j"
        self.driver = GraphDatabase.driver(self.uri, auth=(settings.user, settings.password))
        self._fulltext = os.environ.get("SEARCH_FULLTEXT_INDEX") or None
        self._ft_checked = bool(self._fulltext)
        self.cache = TTLCache(ttl=float(os.environ.get("CACHE_TTL", 60)))
        self.schema_cache = TTLCache(ttl=float(os.environ.get("SCHEMA_TTL", 600)), size=4)
        self._counts_warned = False

    def read(self, query: str, timeout: float = 30, **params):
        """Run one statement in a READ transaction and return the records."""
        def work(tx):
            return list(tx.run(query, **params))
        with self.driver.session(database=self.db, default_access_mode=READ_ACCESS) as s:
            return s.execute_read(neo4j.unit_of_work(timeout=timeout)(work))

    @property
    def fulltext(self):
        """Use SEARCH_FULLTEXT_INDEX, or auto-detect the index made by tools/create_fulltext_index.py."""
        if not self._ft_checked:
            self._ft_checked = True
            try:
                rows = self.read("SHOW FULLTEXT INDEXES YIELD name, state WHERE name = 'gx_search' AND state = 'ONLINE' RETURN name")
                self._fulltext = rows[0]["name"] if rows else None
            except Exception:  # noqa: BLE001
                self._fulltext = None
        return self._fulltext

    def store_counts(self):
        """Totals from ``CALL db.stats.retrieve('GRAPH COUNTS')``.

        Neo4j answers that from the count store. None means the procedure is
        missing or not permitted, and the caller counts with Cypher instead.
        A connection failure is not caught here.
        """
        try:
            rows = self.read("CALL db.stats.retrieve('GRAPH COUNTS') YIELD data RETURN data")
        except neo4j.exceptions.Neo4jError as e:
            if not self._counts_warned:
                self._counts_warned = True
                print(f"WARNING: db.stats.retrieve('GRAPH COUNTS') failed ({e}). Counting with Cypher.")
            return None
        parsed = graph_count_totals(rows[0]["data"] if rows else None)
        if parsed is None and not self._counts_warned:
            self._counts_warned = True
            print("WARNING: db.stats.retrieve('GRAPH COUNTS') returned no totals. Counting with Cypher.")
        return parsed

    def _count_with_cypher(self):
        n = self.read("MATCH (n) RETURN count(n) AS n")[0]["n"]
        r = self.read("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return n, r

    # ---- API ---------------------------------------------------------------
    def status(self, consume_config_created=False):
        info = self.driver.get_server_info()
        counts = self.store_counts()
        if counts:
            n, r = counts["nodes"], counts["edges"]
        else:
            n, r = self._count_with_cypher()
        return {"connected": True, "uri": self.uri, "database": self.db, "server": info.agent,
                "nodes": n, "edges": r, "read_only": True, "fulltext_index": self.fulltext,
                "budget": budget_payload(consume_created=consume_config_created)}

    def schema(self, refresh=False):
        hit = None if refresh else self.schema_cache.get("schema")
        if hit:
            return hit
        counts = self.store_counts()
        if counts:
            label_counts, type_counts = counts["labels"], counts["types"]
            labels, types = sorted(label_counts), sorted(type_counts)
        else:
            labels = [r["label"] for r in self.read("CALL db.labels() YIELD label RETURN label ORDER BY label")]
            types = [r["relationshipType"] for r in self.read("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType")]
            label_counts = {l: self.read(f"MATCH (n:{cyq(l)}) RETURN count(n) AS c")[0]["c"] for l in labels}
            type_counts = {t: self.read(f"MATCH ()-[r:{cyq(t)}]->() RETURN count(r) AS c")[0]["c"] for t in types}
        cats = {}
        for l in labels:
            if not label_counts.get(l):
                continue
            for rec in self.read(f"MATCH (n:{cyq(l)}) RETURN n LIMIT 30"):
                n = rec["n"]
                c = category_of(n.labels)
                e = cats.setdefault(c, {"count": 0, "labels": sorted(n.labels), "props": {}})
                for k, v in n.items():
                    e["props"].setdefault(k, type(v).__name__)
        for c in cats:
            cats[c]["count"] = label_counts.get(c, 0)
        links = {}
        for t in types:
            if not type_counts.get(t):
                continue
            for rec in self.read(f"MATCH (a)-[r:{cyq(t)}]->(b) RETURN labels(a) AS la, labels(b) AS lb LIMIT 200"):
                key = (category_of(rec["la"]), t, category_of(rec["lb"]))
                links[key] = links.get(key, 0) + 1
        return self.schema_cache.put("schema", {
            "labels": label_counts, "types": {t: c for t, c in type_counts.items() if c}, "categories": cats,
            "links": [{"source": a, "type": t, "target": b} for (a, t, b) in sorted(links)]})

    def search(self, q: str, kind="node", category=None, prop=None, value=None, limit=20, offset=0, fuzzy=False):
        ql = (q or "").strip().lower()
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        if kind == "edge":
            recs = self.read(
                "MATCH (a)-[r]->(b) WHERE ($cat IS NULL OR type(r) = $cat) AND "
                "($q = '' OR toLower(type(r)) CONTAINS $q OR any(k IN keys(r) WHERE toLower(toStringOrNull(r[k])) CONTAINS $q)) "
                "AND ($prop IS NULL OR toLower(toStringOrNull(r[$prop])) = toLower($val)) "
                "RETURN r, a, b SKIP $off LIMIT $lim", q=ql, cat=category, prop=prop, val=value, off=offset, lim=limit)
            return {"edges": [{**rel_json(r["r"]), "sourceNode": node_json(r["a"]), "targetNode": node_json(r["b"])} for r in recs]}
        if self.fulltext and ql:
            words = re.findall(r"\w+", ql)
            term = " AND ".join(f"{w}~" if fuzzy else f"(*{w}* OR {w})" for w in words) or ql
            recs = self.read(
                "CALL db.index.fulltext.queryNodes($idx, $term) YIELD node AS n, score "
                "WHERE ($cat IS NULL OR $cat IN labels(n)) AND ($prop IS NULL OR toLower(toStringOrNull(n[$prop])) = toLower($val)) "
                "RETURN n, COUNT { (n)--() } AS deg ORDER BY score DESC SKIP $off LIMIT $lim",
                idx=self.fulltext, term=term, cat=category, prop=prop, val=value, off=offset, lim=limit)
            return {"nodes": [node_json(r["n"], r["deg"]) for r in recs], "total": None, "index": self.fulltext}
        # No full-text index: scan the caption/id fields, but stop after SCAN_CAP candidates and rank
        # only the first matches, so one keystroke can't scan a multi-million-node database.
        match = f"MATCH (n:{cyq(category)})" if category else "MATCH (n)"
        cond = ("($q = '' OR any(k IN $fields WHERE toLower(toStringOrNull(n[k])) CONTAINS $q)) "
                "AND ($prop IS NULL OR toLower(toStringOrNull(n[$prop])) = toLower($val))")
        recs = self.read(
            f"CALL () {{ {match} RETURN n LIMIT $cap }} WITH n WHERE {cond} "
            "WITH n LIMIT 2000 "
            "WITH n, CASE WHEN any(k IN $fields WHERE toLower(toStringOrNull(n[k])) = $q) THEN 0 "
            "WHEN any(k IN $fields WHERE toLower(toStringOrNull(n[k])) STARTS WITH $q) THEN 1 ELSE 2 END AS rank "
            "WITH collect(n) AS found, collect(rank) AS ranks "
            "UNWIND range(0, size(found) - 1) AS i WITH found[i] AS n, ranks[i] AS rank, size(found) AS total "
            "ORDER BY rank SKIP $off LIMIT $lim RETURN n, COUNT { (n)--() } AS deg, total",
            q=ql, fields=SEARCH_FIELDS, prop=prop, val=value, off=offset, lim=limit, cap=SCAN_CAP)
        total = recs[0]["total"] if recs else 0
        return {"nodes": [node_json(r["n"], r["deg"]) for r in recs], "total": total, "total_capped": total >= 2000}

    def nodes(self, ids):
        recs = self.read("UNWIND $ids AS id MATCH (n) WHERE elementId(n) = id RETURN n, COUNT { (n)--() } AS deg", ids=ids)
        return {"nodes": [node_json(r["n"], r["deg"]) for r in recs]}

    def neighbor_summary(self, ids, visible):
        key = ("ns", tuple(sorted(ids)))
        base = self.cache.get(key)
        if base is None:
            deg = self.read("UNWIND $ids AS id MATCH (n) WHERE elementId(n) = id RETURN sum(COUNT { (n)--() }) AS d", ids=ids)[0]["d"] or 0
            # Read at most HUB_SAMPLE neighbor rows; for hubs, scale the sample up to the true degree.
            recs = self.read(
                "UNWIND $ids AS id MATCH (n) WHERE elementId(n) = id "
                "CALL (n) { MATCH (n)-[r]-(m) RETURN type(r) AS t, labels(m) AS l, elementId(m) AS mid LIMIT $cap } "
                "RETURN t, l, collect(mid) AS mids", ids=ids, cap=HUB_SAMPLE)
            base = self.cache.put(key, {"deg": deg, "rows": [(r["t"], category_of(r["l"]), r["mids"]) for r in recs]})
        vis = set(visible)
        sampled = sum(len(m) for _, _, m in base["rows"])
        scale = base["deg"] / sampled if sampled and base["deg"] > sampled else 1.0
        cats, types, combos, total = {}, {}, {}, 0
        for t, c, mids in base["rows"]:
            hidden = sum(1 for m in mids if m not in vis)
            if not hidden:
                continue
            n = round(hidden * scale)
            cats[c] = cats.get(c, 0) + n
            types[t] = types.get(t, 0) + n
            combos.setdefault(c, {})
            combos[c][t] = combos[c].get(t, 0) + n
            total += n
        return {"total": total, "categories": cats, "types": types, "combos": combos, "approximate": scale > 1, "degree": base["deg"]}

    def expand(self, ids, visible, categories=None, types=None, limit=None, order="neighbors", offset=0, q=None, body=None):
        caps = session_caps(body)
        offset = max(0, _as_int(offset, 0))
        if not ids or caps["remain_nodes"] <= 0:
            msg = None if ids and caps["remain_nodes"] > 0 else (
                f"canvas is full ({caps['max_nodes']:,} nodes, {caps['max_edges']:,} edges)" if ids else None)
            return {"nodes": [], "edges": [], "offset": offset, "more": False, "truncated": bool(msg),
                    "truncation": truncation(msg, ["node_budget"] if msg else [])}
        # One extra row tells us whether the page is complete without a count of a hub.
        page = max(1, min(_as_int(limit, caps["expand_limit"]), caps["expand_limit"], caps["remain_nodes"]))
        vis = list({*visible, *ids})
        text = ""
        params = dict(ids=ids, types=types or None, cats=categories or None, vis=vis, off=offset, lim=page + 1)
        if q and str(q).strip():
            text = " AND any(k IN $fields WHERE toLower(toStringOrNull(m[k])) CONTAINS $q)"
            params["q"] = str(q).strip().lower()
            params["fields"] = FILTER_FIELDS
        filt = ("($types IS NULL OR type(r) IN $types) AND ($cats IS NULL OR any(l IN labels(m) WHERE l IN $cats)) "
                "AND NOT elementId(m) IN $vis" + text)
        if order in ("most", "least"):
            # Rank by degree, but only among the first HUB_SAMPLE candidates (degree lookups are O(1) each).
            params["cap"] = HUB_SAMPLE
            recs = self.read(
                "UNWIND $ids AS id MATCH (n) WHERE elementId(n) = id "
                f"CALL (n) {{ MATCH (n)-[r]-(m) WHERE {filt} RETURN DISTINCT m LIMIT $cap }} "
                "WITH DISTINCT m WITH m, COUNT { (m)--() } AS deg "
                f"ORDER BY deg {'DESC' if order == 'most' else 'ASC'} SKIP $off LIMIT $lim RETURN m, deg",
                **params)
        else:
            # Streamed: no global ORDER BY, so a hub with millions of neighbors returns the first page at once.
            recs = self.read(
                "UNWIND $ids AS id MATCH (n) WHERE elementId(n) = id "
                f"MATCH (n)-[r]-(m) WHERE {filt} "
                "WITH DISTINCT m SKIP $off LIMIT $lim RETURN m, COUNT { (m)--() } AS deg",
                **params)
        more = len(recs) > page
        recs = recs[:page]
        new_nodes = [node_json(r["m"], r["deg"]) for r in recs]
        new_ids = [n["id"] for n in new_nodes]
        edge_cap = caps["remain_edges"]
        edges, edges_more = self.edges_for(new_ids, ids, vis, types, edge_cap)
        reasons = []
        message = None
        if more:
            reasons.append("node_budget" if page >= caps["remain_nodes"] and page < caps["expand_limit"] else "expand_limit")
            message = f"showing {len(new_nodes):,} of more neighbors"
        if edges_more:
            reasons.append("edge_budget")
            extra = f"edges stopped at {len(edges):,}"
            message = f"{message}; {extra}" if message else extra
        report = truncation(message, reasons, nodes_returned=len(new_nodes), edges_returned=len(edges))
        return {"nodes": new_nodes, "edges": edges, "offset": offset, "more": more,
                "truncated": report["truncated"], "truncation": report}

    def edges_for(self, new_ids, src_ids, visible, types=None, edge_limit=None):
        """Edges between the new nodes and anything already on screen.

        The walk starts at an endpoint whose degree is <= EDGE_WALK_MAX, so a hub
        (the call that times out when it is scanned) is never the start of the scan.
        The result stops at the edge budget.
        """
        if edge_limit is None:
            edge_limit = MAX_EDGES
        if not new_ids or edge_limit <= 0:
            return [], bool(new_ids and edge_limit <= 0)
        ends = list({*visible, *src_ids, *new_ids})
        lim = int(edge_limit) + 1
        recs = self.read(
            "UNWIND $new AS id MATCH (a) WHERE elementId(a) = id AND COUNT { (a)--() } <= $walk "
            "MATCH (a)-[r]-(b) WHERE elementId(b) IN $ends "
            "AND ($types IS NULL OR type(r) IN $types OR NOT elementId(b) IN $src) "
            "RETURN DISTINCT r LIMIT $lim",
            new=new_ids, src=list(src_ids), ends=ends, types=types or None, walk=EDGE_WALK_MAX, lim=lim)
        # New nodes that are themselves hubs were skipped above. Pick those edges up
        # from the other end, and only when that end is small enough to walk.
        if len(recs) <= edge_limit:
            more_recs = self.read(
                "UNWIND $src AS id MATCH (b) WHERE elementId(b) = id AND COUNT { (b)--() } <= $walk "
                "MATCH (b)-[r]-(a) WHERE elementId(a) IN $new "
                "AND ($types IS NULL OR type(r) IN $types) "
                "RETURN DISTINCT r LIMIT $lim",
                src=list(src_ids), new=new_ids, types=types or None, walk=EDGE_WALK_MAX, lim=lim)
            seen = {r["r"].element_id for r in recs}
            for r in more_recs:
                if r["r"].element_id not in seen:
                    recs.append(r)
                    seen.add(r["r"].element_id)
        more = len(recs) > edge_limit
        return [rel_json(r["r"]) for r in recs[:edge_limit]], more

    def edges_between(self, ids, body=None):
        caps = session_caps(body)
        edge_limit = caps["remain_edges"]
        if len(ids) < 2 or edge_limit <= 0:
            msg = None if edge_limit > 0 else f"canvas is full ({caps['max_edges']:,} edges)"
            report = truncation(msg, ["edge_budget"] if msg else [])
            return {"edges": [], "truncated": report["truncated"], "truncation": report}
        recs = self.read(
            "UNWIND $ids AS id MATCH (a) WHERE elementId(a) = id AND COUNT { (a)--() } <= $walk "
            "MATCH (a)-[r]-(b) WHERE elementId(b) IN $ids "
            "RETURN DISTINCT r LIMIT $lim",
            ids=list(ids), walk=EDGE_WALK_MAX, lim=int(edge_limit) + 1)
        more = len(recs) > edge_limit
        edges = [rel_json(r["r"]) for r in recs[:edge_limit]]
        message = f"edges stopped at {len(edges):,}" if more else None
        report = truncation(message, ["edge_budget"] if more else [], edges_returned=len(edges))
        return {"edges": edges, "truncated": report["truncated"], "truncation": report}

    def cypher(self, query: str, params=None, limit=None, body=None):
        check_read_only(query)
        caps = session_caps(body)
        if caps["remain_nodes"] <= 0:
            report = truncation(
                f"canvas is full ({caps['max_nodes']:,} nodes, {caps['max_edges']:,} edges)", ["node_budget"])
            return {"nodes": [], "edges": [], "columns": [], "rows": [], "truncated": True, "truncation": report}
        row_cap = max(1, min(_as_int(limit, caps["cypher_limit"]), caps["cypher_limit"], caps["remain_nodes"]))
        wrapped = apply_hard_limit(query, row_cap + 1)
        recs = self.read(wrapped, **(params or {}))
        row_more = len(recs) > row_cap
        recs = recs[:row_cap]
        nodes, edges, rows, cols = {}, {}, [], []

        def walk(v):
            if isinstance(v, Node):
                nodes[v.element_id] = v
            elif isinstance(v, Relationship):
                edges[v.element_id] = v
                nodes[v.start_node.element_id] = v.start_node
                nodes[v.end_node.element_id] = v.end_node
            elif isinstance(v, NPath):
                for x in v.nodes:
                    walk(x)
                for x in v.relationships:
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)

        for rec in recs:
            cols = list(rec.keys())
            for v in rec.values():
                walk(v)
            rows.append([jsonable(v) if not isinstance(v, (Node, Relationship, NPath)) else f"<{type(v).__name__}>" for v in rec.values()])
        node_ids = list(nodes)
        node_more = len(node_ids) > caps["remain_nodes"]
        if node_more:
            node_ids = node_ids[:caps["remain_nodes"]]
            keep = set(node_ids)
            edges = {i: r for i, r in edges.items() if r.start_node.element_id in keep and r.end_node.element_id in keep}
            nodes = {i: n for i, n in nodes.items() if i in keep}
        edge_ids = list(edges)
        edge_more = len(edge_ids) > caps["remain_edges"]
        if edge_more:
            edge_ids = edge_ids[:caps["remain_edges"]]
            edges = {i: edges[i] for i in edge_ids}
        degs = {}
        if nodes:
            for r in self.read("MATCH (n) WHERE elementId(n) IN $ids RETURN elementId(n) AS id, COUNT { (n)--() } AS d", ids=list(nodes)):
                degs[r["id"]] = r["d"]
        reasons = []
        message = None
        if row_more or node_more:
            reasons.append("row_limit" if row_more else "node_budget")
            message = f"query stopped at {len(rows):,} of more rows"
        if edge_more:
            reasons.append("edge_budget")
            extra = f"edges stopped at {len(edges):,}"
            message = f"{message}; {extra}" if message else extra
        report = truncation(message, reasons, rows_returned=len(rows), nodes_returned=len(nodes), edges_returned=len(edges))
        return {"nodes": [node_json(nodes[i], degs.get(i)) for i in node_ids if i in nodes],
                "edges": [rel_json(edges[i]) for i in edge_ids if edges[i].start_node is not None],
                "columns": cols, "rows": rows[:50], "truncated": report["truncated"], "truncation": report}


GRAPH: Graph | None = None
SOURCES: dict = {}
ACTIVE = "neo4j"
_CONN_LOCK = threading.Lock()
_TOKEN_ENV_BLOCKED = set(CREDENTIAL_KEYS) | (set(ENV_KEYS) - {"GRAPHQL_TOKEN"})


class UnconfiguredNeo4j:
    """Stand-in so the page can open before Neo4j has a password.

    An empty password is a real password. Only a missing one uses this object.
    """

    id = "neo4j"
    label = "Neo4j"
    kind = "neo4j"
    message = (
        "Neo4j has no password yet. Open Data connection and save the URI, user, and password. "
        "An empty password is allowed when Neo4j has none."
    )

    def status(self, consume_config_created=False):
        return {
            "connected": False, "read_only": True,
            "uri": redact_uri(os.environ.get("NEO4J_URI") or ""),
            "database": os.environ.get("NEO4J_DATABASE") or "",
            "server": "", "nodes": None, "edges": None, "error": self.message,
            "budget": budget_payload(consume_created=consume_config_created),
        }

    def _refuse(self, *args, **kwargs):
        raise ValueError(self.message)

    schema = search = nodes = neighbor_summary = expand = edges_between = cypher = _refuse

# Both sources implement these. The handlers call them and do not branch on
# the kind, except the source list itself.
SOURCE_METHODS = (
    "status", "schema", "search", "nodes", "neighbor_summary",
    "expand", "edges_between", "cypher",
)


def bind_sources(graph, config):
    """Neo4j plus any configured GraphQL source. The active source starts as Neo4j."""
    global GRAPH, SOURCES, ACTIVE
    GRAPH = graph
    sources = {"neo4j": graph}
    for spec in resolve_graphql_sources(config):
        sources[spec["id"]] = GraphQLSource(spec)
        print(f"GraphQL source {spec['label']} ({spec['adapter']}) at {urlparse(spec['endpoint']).netloc}.")
    SOURCES = sources
    if ACTIVE not in SOURCES:
        ACTIVE = "neo4j"


def current():
    if SOURCES:
        return SOURCES.get(ACTIVE) or SOURCES.get("neo4j") or GRAPH
    return GRAPH


def source_catalog():
    rows = []
    for sid, src in SOURCES.items():
        row = {
            "id": getattr(src, "id", sid),
            "label": getattr(src, "label", sid),
            "kind": getattr(src, "kind", "neo4j"),
        }
        adapter = getattr(src, "adapter_name", None)
        if adapter:
            row["adapter"] = adapter
        rows.append(row)
    return rows


def _secret_values():
    names = {"GRAPHQL_TOKEN", "NEO4J_PASSWORD"}
    extra = os.environ.get("GRAPHQL_TOKEN_ENV")
    if extra:
        names.add(extra)
    for src in SOURCES.values():
        env_name = getattr(src, "token_env", None)
        if env_name:
            names.add(env_name)
    # An empty password is not a secret string. Scrubbing "" would blank the page.
    return [value for name in names if (value := os.environ.get(name))]


def _scrub_public(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {key: _scrub_public(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_public(item, secrets) for item in value]
    return value


def public_status(consume=False):
    """Status for the active source, plus the list of sources. No token."""
    src = current()
    kind = getattr(src, "kind", "neo4j")
    try:
        if kind == "neo4j":
            status = src.status(consume_config_created=consume)
        else:
            status = src.status()
            status["budget"] = budget_payload(consume_created=consume)
    except Exception as exc:  # noqa: BLE001
        status = {
            "connected": False, "read_only": True, "nodes": None, "edges": None,
            "server": "", "database": getattr(src, "label", ""),
            "error": str(exc)[:300],
            "budget": budget_payload(consume_created=consume),
        }
    status["source"] = {
        "id": getattr(src, "id", ACTIVE),
        "label": getattr(src, "label", ACTIVE),
        "kind": kind,
    }
    adapter = getattr(src, "adapter_name", None)
    if adapter:
        status["source"]["adapter"] = adapter
    status["sources"] = source_catalog()
    return _scrub_public(status, _secret_values())


def select_source(source_id):
    """Switch the view. The other source is left untouched."""
    global ACTIVE
    source_id = str(source_id or "")
    if source_id not in SOURCES:
        raise ValueError("Unknown data source")
    ACTIVE = source_id
    return public_status(consume=False)


def _plain(value, label):
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label} cannot contain a line break")
    return text


def connection_view():
    """Settings the page can show. The password and the token stay out."""
    load_credential_env()
    load_graphql_env()
    token_env = (os.environ.get("GRAPHQL_TOKEN_ENV") or "GRAPHQL_TOKEN").strip() or "GRAPHQL_TOKEN"
    if not _ENV_RE.fullmatch(token_env):
        token_env = "GRAPHQL_TOKEN"
    endpoint = (os.environ.get("GRAPHQL_ENDPOINT") or "").strip()
    if endpoint and _clean_endpoint(endpoint) is None:
        endpoint = ""
    view = {
        "file": dotenv_path().name,
        "neo4j": {
            "uri": redact_uri((os.environ.get("NEO4J_URI") or "bolt://localhost:7687").strip()),
            "user": (os.environ.get("NEO4J_USER") or "neo4j").strip() or "neo4j",
            "database": (os.environ.get("NEO4J_DATABASE") or "neo4j").strip() or "neo4j",
            "password_set": "NEO4J_PASSWORD" in os.environ,
        },
        "graphql": {
            "endpoint": endpoint,
            "label": os.environ.get("GRAPHQL_LABEL") or "",
            "adapter": os.environ.get("GRAPHQL_ADAPTER") or "",
            "id": os.environ.get("GRAPHQL_ID") or "",
            "token_env": token_env,
            "project_id": os.environ.get("GRAPHQL_PROJECT_ID") or "",
            "token_set": bool(os.environ.get(token_env)),
        },
    }
    return _scrub_public(view, _secret_values())


def _close_driver(graph):
    driver = getattr(graph, "driver", None)
    if driver is None:
        return
    try:
        driver.close()
    except Exception:  # noqa: BLE001
        pass


def _reconnect():
    """Rebuild Neo4j and GraphQL from the environment after Data connection saves."""
    global GRAPH
    old = GRAPH
    try:
        settings = neo4j_settings()
    except Neo4jConfigError as e:
        if not e.missing_password:
            raise ValueError(str(e)) from e
        graph = UnconfiguredNeo4j()
    else:
        graph = Graph(settings)
    cfg = ensure_config()
    bind_sources(graph, cfg)
    if old is not None and old is not graph:
        _close_driver(old)


def save_connection(body):
    """Write Neo4j and GraphQL into the gitignored env file and use them now.

    A blank password or token keeps the saved value. A password of "" is stored
    when Neo4j has none. Secrets are not written to gx.local.json.
    """
    if not isinstance(body, dict):
        raise ValueError("connection settings must be an object")
    with _CONN_LOCK:
        load_credential_env()
        load_graphql_env()
        neo = body.get("neo4j") if isinstance(body.get("neo4j"), dict) else {}
        graphql = body.get("graphql") if isinstance(body.get("graphql"), dict) else {}
        clear = bool(graphql.get("clear"))
        endpoint_raw = _plain(graphql.get("endpoint") or "", "endpoint").strip()
        saving_graphql = bool(endpoint_raw) and not clear
        password_in_body = "password" in neo
        password_already = "NEO4J_PASSWORD" in os.environ
        if not password_in_body and not password_already and not saving_graphql and not clear:
            raise ValueError(
                "Save a Neo4j password, or a GraphQL endpoint. "
                "An empty password is allowed when Neo4j has none.")
        updates = {}
        remove = []
        skip_neo = (not password_in_body) and (not password_already) and (saving_graphql or clear)
        if not skip_neo and (password_in_body or password_already or neo.get("uri") or neo.get("user") or neo.get("database")):
            uri = _plain(neo.get("uri") or os.environ.get("NEO4J_URI") or "bolt://localhost:7687", "URI").strip()
            user = _plain(neo.get("user") or os.environ.get("NEO4J_USER") or "neo4j", "user").strip() or "neo4j"
            if "database" in neo:
                database = _plain(neo.get("database"), "database").strip() or "neo4j"
            else:
                database = _plain(os.environ.get("NEO4J_DATABASE") or "neo4j", "database").strip() or "neo4j"
            if "://" not in uri:
                raise ValueError("NEO4J_URI must be a bolt or neo4j URL, such as bolt://127.0.0.1:7687")
            parts = urlsplit(uri)
            if parts.username is not None or parts.password is not None:
                raise ValueError(
                    "NEO4J_URI includes a user or a password. Keep the URI as the host, "
                    "and set the user and password in their own fields.")
            updates["NEO4J_URI"] = uri
            updates["NEO4J_USER"] = user
            updates["NEO4J_DATABASE"] = database
            if password_in_body:
                updates["NEO4J_PASSWORD"] = _plain(neo.get("password"), "password")
        if clear:
            remove.extend([
                "GRAPHQL_ENDPOINT", "GRAPHQL_TOKEN", "GRAPHQL_ADAPTER", "GRAPHQL_LABEL",
                "GRAPHQL_ID", "GRAPHQL_TOKEN_ENV", "GRAPHQL_PROJECT_ID",
            ])
            custom = (os.environ.get("GRAPHQL_TOKEN_ENV") or "").strip()
            if custom and _ENV_RE.fullmatch(custom) and custom not in remove:
                remove.append(custom)
        elif saving_graphql:
            cleaned = _clean_endpoint(endpoint_raw)
            if not cleaned:
                raise ValueError(
                    "The GraphQL endpoint must be an http or https URL without a user, a password, or a token.")
            updates["GRAPHQL_ENDPOINT"] = cleaned
            if "label" in graphql:
                label = _plain(graphql.get("label") or "", "label").strip()
                if label:
                    updates["GRAPHQL_LABEL"] = label[:80]
                else:
                    remove.append("GRAPHQL_LABEL")
            if "adapter" in graphql:
                adapter = _plain(graphql.get("adapter") or "", "adapter").strip().lower()
                if adapter in ("wiz", "generic"):
                    updates["GRAPHQL_ADAPTER"] = adapter
                elif adapter == "":
                    remove.append("GRAPHQL_ADAPTER")
                else:
                    raise ValueError("The GraphQL adapter is wiz or generic.")
            if "id" in graphql:
                ident = _plain(graphql.get("id") or "", "id").strip()
                if ident == "neo4j":
                    raise ValueError("A GraphQL source cannot use the id neo4j.")
                if ident and not _ID_RE.fullmatch(ident):
                    raise ValueError("The GraphQL id must start with a letter and use letters, numbers, _ or -.")
                if ident:
                    updates["GRAPHQL_ID"] = ident
                else:
                    remove.append("GRAPHQL_ID")
            if "project_id" in graphql:
                project = _plain(graphql.get("project_id") or "", "project").strip()
                if project:
                    updates["GRAPHQL_PROJECT_ID"] = project[:200]
                else:
                    remove.append("GRAPHQL_PROJECT_ID")
            previous_env = (os.environ.get("GRAPHQL_TOKEN_ENV") or "GRAPHQL_TOKEN").strip() or "GRAPHQL_TOKEN"
            token_env = _plain(graphql.get("token_env") or previous_env, "token variable").strip() or "GRAPHQL_TOKEN"
            if not _ENV_RE.fullmatch(token_env) or token_env in _TOKEN_ENV_BLOCKED:
                raise ValueError("The token variable name is not usable.")
            if graphql.get("token_env"):
                updates["GRAPHQL_TOKEN_ENV"] = token_env
            if "token" in graphql and str(graphql.get("token") or "") != "":
                updates[token_env] = _plain(graphql.get("token"), "token")
            elif token_env != previous_env and os.environ.get(previous_env) and token_env not in os.environ:
                updates[token_env] = os.environ[previous_env]
        remove = [key for key in remove if key not in updates]
        try:
            update_dotenv(updates, remove=remove)
        except Neo4jConfigError as e:
            raise ValueError(str(e)) from e
        for key in remove:
            os.environ.pop(key, None)
        for key, value in updates.items():
            os.environ[key] = "" if value is None else str(value)
        _reconnect()
        print("Data connection saved.")
        return {"connection": connection_view(), "status": public_status(consume=False)}


class Handler(BaseHTTPRequestHandler):
    server_version = "GraphExplorer/0.1"

    def log_message(self, fmt, *args):
        if os.environ.get("QUIET") != "1":
            sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _api_error(self, exc):
        secrets = _secret_values()
        if isinstance(exc, TokenExpired):
            return self._json({"error": "token expired"}, 401)
        if isinstance(exc, RateLimited):
            return self._json({"error": "The GraphQL API asked us to slow down (HTTP 429)."}, 429)
        if isinstance(exc, QueryRejected):
            return self._json({"error": _scrub_public(str(exc), secrets)}, 400)
        if isinstance(exc, PermissionError):
            return self._json({"error": _scrub_public(str(exc), secrets)}, 403)
        if isinstance(exc, (ValueError, GraphQLConfigError)):
            return self._json({"error": _scrub_public(str(exc), secrets)}, 400)
        if isinstance(exc, neo4j.exceptions.Neo4jError):
            return self._json({"error": _scrub_public(f"{exc.code}: {exc.message}", secrets)}, 400)
        if isinstance(exc, GraphQLCallError):
            return self._json({"error": _scrub_public(str(exc), secrets)}, 502)
        return self._json({"error": _scrub_public(str(exc), secrets)}, 500)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        src = current()
        try:
            if u.path == "/api/status":
                return self._json(public_status(consume=True))
            if u.path == "/api/sources":
                return self._json(_scrub_public(source_catalog(), _secret_values()))
            if u.path == "/api/connection":
                return self._json(connection_view())
            if u.path == "/api/schema":
                return self._json(src.schema(refresh=q.get("refresh") == "1"))
            if u.path == "/api/styles":
                return self._json(CFG)
            if u.path == "/api/queries":
                return self._json(json.loads((ROOT / "data" / "queries.json").read_text()))
            if u.path == "/api/search":
                return self._json(src.search(q.get("q", ""), q.get("type", "node"), q.get("category") or None, q.get("prop") or None,
                                             q.get("value"), int(q.get("limit", 20)), int(q.get("offset", 0)), q.get("fuzzy") == "1"))
        except Exception as e:  # noqa: BLE001
            if isinstance(e, (TokenExpired, RateLimited, QueryRejected, GraphQLCallError, GraphQLConfigError, ValueError)):
                return self._api_error(e)
            return self._json({"error": _scrub_public(str(e), _secret_values())}, 500)
        path = (STATIC / (u.path.lstrip("/") or "index.html")).resolve()
        if u.path == "/":
            path = STATIC / "index.html"
        if not str(path).startswith(str(STATIC)) or not path.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, path.read_bytes(), mimetypes.guess_type(str(path))[0] or "application/octet-stream")

    def do_POST(self):
        u = urlparse(self.path)
        src = current()
        try:
            b = self._body()
            if u.path == "/api/connection":
                return self._json(save_connection(b))
            if u.path == "/api/source":
                return self._json(select_source(b.get("id")))
            if u.path == "/api/nodes":
                return self._json(src.nodes(b["ids"]))
            if u.path == "/api/neighbors":
                return self._json(src.neighbor_summary(b["ids"], b.get("visible", [])))
            if u.path == "/api/budget/suggest":
                return self._json(suggest_for(b))
            if u.path == "/api/budget/refine":
                return self._json(refine_config(b))
            if u.path == "/api/budget":
                return self._json({"config": save_config(b), "config_file": config_path().name})
            if u.path == "/api/expand":
                return self._json(src.expand(b.get("ids") or [], b.get("visible") or [], b.get("categories"), b.get("types"),
                                             b.get("limit", EXPAND_LIMIT), b.get("order", "neighbors"), b.get("offset", 0),
                                             b.get("q") or None, b))
            if u.path == "/api/edges-between":
                return self._json(src.edges_between(b.get("ids") or [], b))
            if u.path == "/api/cypher":
                return self._json(src.cypher(b["query"], b.get("params"), int(b.get("limit", CYPHER_ROW_LIMIT)), b))
        except Exception as e:  # noqa: BLE001
            return self._api_error(e)
        self._send(404, b"not found", "text/plain")


def main():
    graph = None
    try:
        settings = neo4j_settings()
    except Neo4jConfigError as e:
        if not e.missing_password:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"WARNING: {e}", file=sys.stderr)
        print("Neo4j is not configured. Open the page and use Data connection to save it.", file=sys.stderr)
        graph = UnconfiguredNeo4j()
    else:
        graph = Graph(settings)
    cfg = ensure_config()
    print(f"Working set: {cfg['nodes']} nodes / {cfg['edges']} edges "
          f"({cfg['source']}) from {config_path()}.")
    bind_sources(graph, cfg)
    if isinstance(graph, Graph):
        try:
            st = GRAPH.status()
            print(f"Connected to {st['uri']} ({st['server']}), db={st['database']}: {st['nodes']} nodes / {st['edges']} edges. READ-ONLY.")
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: cannot reach Neo4j yet: {_scrub_public(str(e), _secret_values())}")
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", 8744))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Open http://{host}:{port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
