"""Read-only Neo4j graph explorer backend (stdlib HTTP server + official neo4j driver).

Serves ./static and a small JSON API. Every Cypher statement runs in a READ
transaction (session default_access_mode=READ + execute_read), and user-written
Cypher is also screened for write clauses before it is sent.

Config (environment variables):
    NEO4J_URI       bolt://localhost:7687
    NEO4J_USER      neo4j
    NEO4J_PASSWORD  (required)
    NEO4J_DATABASE  neo4j
    HOST / PORT     127.0.0.1 / 8744
    SEARCH_FULLTEXT_INDEX  name of a Neo4j full-text index for search (auto-detects "gx_search",
                    made by tools/create_fulltext_index.py)
    EXPAND_LIMIT    default neighbor limit per expand (50); EXPAND_MAX caps one page (5000)
    HUB_SAMPLE      neighbors read per node for summaries / degree ranking (20000)
    SEARCH_SCAN_CAP nodes scanned per search without a full-text index (200000)
    CACHE_TTL / SCHEMA_TTL  seconds to cache neighbor summaries (60) / the schema (600)
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
from urllib.parse import parse_qs, urlparse

import neo4j
from neo4j import GraphDatabase, READ_ACCESS
from neo4j.graph import Node, Path as NPath, Relationship

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
CFG = json.loads((ROOT / "cartography_styles.json").read_text())

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
EXPAND_MAX = int(os.environ.get("EXPAND_MAX", 5000))


def cyq(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


class Graph:
    def __init__(self):
        self.uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        self.db = os.environ.get("NEO4J_DATABASE", "neo4j")
        self.driver = GraphDatabase.driver(self.uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))
        self._fulltext = os.environ.get("SEARCH_FULLTEXT_INDEX") or None
        self._ft_checked = bool(self._fulltext)
        self.cache = TTLCache(ttl=float(os.environ.get("CACHE_TTL", 60)))
        self.schema_cache = TTLCache(ttl=float(os.environ.get("SCHEMA_TTL", 600)), size=4)

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

    # ---- API ---------------------------------------------------------------
    def status(self):
        info = self.driver.get_server_info()
        n = self.read("MATCH (n) RETURN count(n) AS n")[0]["n"]          # count store: O(1)
        r = self.read("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"connected": True, "uri": self.uri, "database": self.db, "server": info.agent,
                "nodes": n, "edges": r, "read_only": True, "fulltext_index": self.fulltext}

    def schema(self, refresh=False):
        hit = None if refresh else self.schema_cache.get("schema")
        if hit:
            return hit
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

    def expand(self, ids, visible, categories=None, types=None, limit=50, order="neighbors", offset=0):
        limit, offset = max(1, min(int(limit), EXPAND_MAX)), max(0, int(offset))
        vis = list({*visible, *ids})
        filt = ("($types IS NULL OR type(r) IN $types) AND ($cats IS NULL OR any(l IN labels(m) WHERE l IN $cats)) "
                "AND NOT elementId(m) IN $vis")
        if order in ("most", "least"):
            # Rank by degree, but only among the first HUB_SAMPLE candidates (degree lookups are O(1) each).
            recs = self.read(
                "UNWIND $ids AS id MATCH (n) WHERE elementId(n) = id "
                f"CALL (n) {{ MATCH (n)-[r]-(m) WHERE {filt} RETURN DISTINCT m LIMIT $cap }} "
                "WITH DISTINCT m WITH m, COUNT { (m)--() } AS deg "
                f"ORDER BY deg {'DESC' if order == 'most' else 'ASC'} SKIP $off LIMIT $lim RETURN m, deg",
                ids=ids, types=types, cats=categories, vis=vis, lim=limit, off=offset, cap=HUB_SAMPLE)
        else:
            # Streamed: no global ORDER BY, so a hub with millions of neighbors returns the first page at once.
            recs = self.read(
                "UNWIND $ids AS id MATCH (n) WHERE elementId(n) = id "
                f"MATCH (n)-[r]-(m) WHERE {filt} "
                "WITH DISTINCT m SKIP $off LIMIT $lim RETURN m, COUNT { (m)--() } AS deg",
                ids=ids, types=types, cats=categories, vis=vis, lim=limit, off=offset)
        new_nodes = [node_json(r["m"], r["deg"]) for r in recs]
        new_ids = [n["id"] for n in new_nodes]
        edges = self.edges_for(new_ids, ids, vis, types)
        return {"nodes": new_nodes, "edges": edges, "offset": offset, "more": len(new_nodes) == limit}

    def edges_for(self, new_ids, src_ids, visible, types=None):
        """Edges between the new nodes and anything on screen, anchored on the new nodes (index lookups)."""
        if not new_ids:
            return []
        recs = self.read(
            "UNWIND $new AS id MATCH (a) WHERE elementId(a) = id "
            "MATCH (a)-[r]-(b) WHERE (elementId(b) IN $all) "
            "AND ($types IS NULL OR type(r) IN $types OR NOT (elementId(b) IN $src)) RETURN DISTINCT r",
            new=new_ids, src=src_ids, all=list({*visible, *new_ids}), types=types)
        return [rel_json(r["r"]) for r in recs]

    def edges_between(self, ids):
        recs = self.read(
            "UNWIND $ids AS id MATCH (a) WHERE elementId(a) = id MATCH (a)-[r]->(b) WHERE elementId(b) IN $ids RETURN r", ids=ids)
        return {"edges": [rel_json(r["r"]) for r in recs]}

    def cypher(self, query: str, params=None, limit=500):
        check_read_only(query)
        recs = self.read(query, **(params or {}))
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
        for rec in recs[:limit]:
            cols = list(rec.keys())
            for v in rec.values():
                walk(v)
            rows.append([jsonable(v) if not isinstance(v, (Node, Relationship, NPath)) else f"<{type(v).__name__}>" for v in rec.values()])
        degs = {}
        if nodes:
            for r in self.read("MATCH (n) WHERE elementId(n) IN $ids RETURN elementId(n) AS id, COUNT { (n)--() } AS d", ids=list(nodes)):
                degs[r["id"]] = r["d"]
        return {"nodes": [node_json(n, degs.get(i)) for i, n in nodes.items()],
                "edges": [rel_json(r) for r in edges.values() if r.start_node is not None],
                "columns": cols, "rows": rows[:50], "truncated": len(recs) > limit}


GRAPH: Graph | None = None


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

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/status":
                return self._json(GRAPH.status())
            if u.path == "/api/schema":
                return self._json(GRAPH.schema(refresh=q.get("refresh") == "1"))
            if u.path == "/api/styles":
                return self._json(CFG)
            if u.path == "/api/queries":
                return self._json(json.loads((ROOT / "queries.json").read_text()))
            if u.path == "/api/search":
                return self._json(GRAPH.search(q.get("q", ""), q.get("type", "node"), q.get("category") or None, q.get("prop") or None,
                                               q.get("value"), int(q.get("limit", 20)), int(q.get("offset", 0)), q.get("fuzzy") == "1"))
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        path = (STATIC / (u.path.lstrip("/") or "index.html")).resolve()
        if u.path == "/":
            path = STATIC / "index.html"
        if not str(path).startswith(str(STATIC)) or not path.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, path.read_bytes(), mimetypes.guess_type(str(path))[0] or "application/octet-stream")

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/nodes":
                return self._json(GRAPH.nodes(b["ids"]))
            if u.path == "/api/neighbors":
                return self._json(GRAPH.neighbor_summary(b["ids"], b.get("visible", [])))
            if u.path == "/api/expand":
                return self._json(GRAPH.expand(b["ids"], b.get("visible", []), b.get("categories"), b.get("types"),
                                               b.get("limit", int(os.environ.get("EXPAND_LIMIT", 50))), b.get("order", "neighbors"), b.get("offset", 0)))
            if u.path == "/api/edges-between":
                return self._json(GRAPH.edges_between(b["ids"]))
            if u.path == "/api/cypher":
                return self._json(GRAPH.cypher(b["query"], b.get("params"), int(b.get("limit", 500))))
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        except neo4j.exceptions.Neo4jError as e:
            return self._json({"error": f"{e.code}: {e.message}"}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 500)
        self._send(404, b"not found", "text/plain")


def main():
    global GRAPH
    GRAPH = Graph()
    try:
        st = GRAPH.status()
        print(f"Connected to {st['uri']} ({st['server']}), db={st['database']}: {st['nodes']} nodes / {st['edges']} edges. READ-ONLY.")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: cannot reach Neo4j yet: {e}")
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", 8744))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Open http://{host}:{port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
