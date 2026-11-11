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
    SEARCH_FULLTEXT_INDEX  optional name of a Neo4j full-text index to use for search
    EXPAND_LIMIT    default neighbor limit per expand (50)
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
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


class Graph:
    def __init__(self):
        self.uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        self.db = os.environ.get("NEO4J_DATABASE", "neo4j")
        self.driver = GraphDatabase.driver(self.uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))
        self.fulltext = os.environ.get("SEARCH_FULLTEXT_INDEX") or None
        self._schema_cache = None

    def read(self, query: str, timeout: float = 30, **params):
        """Run one statement in a READ transaction and return the records."""
        def work(tx):
            return list(tx.run(query, **params))
        with self.driver.session(database=self.db, default_access_mode=READ_ACCESS) as s:
            return s.execute_read(neo4j.unit_of_work(timeout=timeout)(work))

    # ---- API ---------------------------------------------------------------
    def status(self):
        info = self.driver.get_server_info()
        n = self.read("MATCH (n) RETURN count(n) AS n")[0]["n"]
        r = self.read("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"connected": True, "uri": self.uri, "database": self.db, "server": info.agent,
                "nodes": n, "edges": r, "read_only": True}

    def schema(self, refresh=False):
        if self._schema_cache and not refresh:
            return self._schema_cache
        labels = [r["label"] for r in self.read("CALL db.labels() YIELD label RETURN label ORDER BY label")]
        types = [r["relationshipType"] for r in self.read("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType")]
        label_counts = {l: self.read(f"MATCH (n:`{l}`) RETURN count(n) AS c")[0]["c"] for l in labels}
        type_counts = {t: self.read(f"MATCH ()-[r:`{t}`]->() RETURN count(r) AS c")[0]["c"] for t in types}
        # Category-level model: sample nodes per label to learn property keys and types.
        cats = {}
        for l in labels:
            for rec in self.read(f"MATCH (n:`{l}`) RETURN n LIMIT 30"):
                n = rec["n"]
                c = category_of(n.labels)
                e = cats.setdefault(c, {"count": 0, "labels": sorted(n.labels), "props": {}})
                for k, v in n.items():
                    e["props"].setdefault(k, type(v).__name__)
        for c in cats:
            cats[c]["count"] = label_counts.get(c, 0)
        # Which categories connect through which edge types (sampled).
        links = {}
        for t in types:
            for rec in self.read(f"MATCH (a)-[r:`{t}`]->(b) RETURN labels(a) AS la, labels(b) AS lb LIMIT 200"):
                key = (category_of(rec["la"]), t, category_of(rec["lb"]))
                links[key] = links.get(key, 0) + 1
        self._schema_cache = {"labels": label_counts, "types": type_counts, "categories": cats,
                              "links": [{"source": a, "type": t, "target": b} for (a, t, b) in sorted(links)]}
        return self._schema_cache

    def search(self, q: str, kind="node", category=None, prop=None, value=None, limit=20, offset=0, fuzzy=False):
        ql = (q or "").strip().lower()
        if kind == "edge":
            recs = self.read(
                "MATCH (a)-[r]->(b) WHERE ($cat IS NULL OR type(r) = $cat) AND "
                "($q = '' OR toLower(type(r)) CONTAINS $q OR any(k IN keys(r) WHERE toLower(toString(r[k])) CONTAINS $q)) "
                "AND ($prop IS NULL OR toLower(toString(r[$prop])) = toLower($val)) "
                "RETURN r, a, b SKIP $off LIMIT $lim", q=ql, cat=category, prop=prop, val=value, off=offset, lim=limit)
            return {"edges": [{**rel_json(r["r"]), "sourceNode": node_json(r["a"]), "targetNode": node_json(r["b"])} for r in recs]}
        if self.fulltext and ql:
            term = " ".join(f"{w}~" if fuzzy else f"*{w}*" for w in re.findall(r"\w+", ql)) or ql
            recs = self.read(
                "CALL db.index.fulltext.queryNodes($idx, $term) YIELD node AS n, score "
                "WHERE ($cat IS NULL OR $cat IN labels(n)) AND ($prop IS NULL OR toLower(toString(n[$prop])) = toLower($val)) "
                "RETURN n, COUNT { (n)--() } AS deg ORDER BY score DESC SKIP $off LIMIT $lim",
                idx=self.fulltext, term=term, cat=category, prop=prop, val=value, off=offset, lim=limit)
        else:
            # No full-text index: scan the configured caption/id fields (fine for a few 100k nodes).
            recs = self.read(
                "MATCH (n) WHERE ($cat IS NULL OR $cat IN labels(n)) "
                "AND ($q = '' OR any(k IN $fields WHERE n[k] IS NOT NULL AND toLower(toString(n[k])) CONTAINS $q)) "
                "AND ($prop IS NULL OR toLower(toString(n[$prop])) = toLower($val)) "
                "WITH n ORDER BY CASE WHEN any(k IN $fields WHERE toLower(toString(n[k])) = $q) THEN 0 "
                "WHEN any(k IN $fields WHERE toLower(toString(n[k])) STARTS WITH $q) THEN 1 ELSE 2 END "
                "SKIP $off LIMIT $lim RETURN n, COUNT { (n)--() } AS deg",
                q=ql, fields=SEARCH_FIELDS, cat=category, prop=prop, val=value, off=offset, lim=limit)
        total = None
        if offset == 0 and not self.fulltext:
            total = self.read(
                "MATCH (n) WHERE ($cat IS NULL OR $cat IN labels(n)) AND ($q = '' OR any(k IN $fields WHERE n[k] IS NOT NULL AND toLower(toString(n[k])) CONTAINS $q)) "
                "AND ($prop IS NULL OR toLower(toString(n[$prop])) = toLower($val)) RETURN count(n) AS c",
                q=ql, fields=SEARCH_FIELDS, cat=category, prop=prop, val=value)[0]["c"]
        return {"nodes": [node_json(r["n"], r["deg"]) for r in recs], "total": total}

    def nodes(self, ids):
        recs = self.read("MATCH (n) WHERE elementId(n) IN $ids RETURN n, COUNT { (n)--() } AS deg", ids=ids)
        return {"nodes": [node_json(r["n"], r["deg"]) for r in recs]}

    def neighbor_summary(self, ids, visible):
        recs = self.read(
            "MATCH (n)-[r]-(m) WHERE elementId(n) IN $ids "
            "RETURN type(r) AS t, labels(m) AS l, elementId(m) IN $vis AS shown, count(*) AS c",
            ids=ids, vis=visible)
        cats, types, combos = {}, {}, {}
        total = 0
        for r in recs:
            if r["shown"]:
                continue  # Expand menus count neighbors that are not on screen yet
            c = category_of(r["l"])
            cats[c] = cats.get(c, 0) + r["c"]
            types[r["t"]] = types.get(r["t"], 0) + r["c"]
            combos.setdefault(c, {})
            combos[c][r["t"]] = combos[c].get(r["t"], 0) + r["c"]
            total += r["c"]
        return {"total": total, "categories": cats, "types": types, "combos": combos}

    def expand(self, ids, visible, categories=None, types=None, limit=50, order="neighbors"):
        orderby = {"most": "deg DESC", "least": "deg ASC"}.get(order, "id")
        recs = self.read(
            "MATCH (n)-[r]-(m) WHERE elementId(n) IN $ids "
            "AND ($types IS NULL OR type(r) IN $types) "
            "AND ($cats IS NULL OR any(l IN labels(m) WHERE l IN $cats)) "
            "WITH DISTINCT m WITH m, COUNT { (m)--() } AS deg, elementId(m) AS id "
            f"ORDER BY {orderby} LIMIT $lim RETURN m, deg",
            ids=ids, types=types, cats=categories, lim=int(limit))
        new_nodes = [node_json(r["m"], r["deg"]) for r in recs]
        all_ids = list({*visible, *ids, *(n["id"] for n in new_nodes)})
        new_ids = [n["id"] for n in new_nodes]
        erecs = self.read(
            "MATCH (a)-[r]->(b) WHERE (elementId(a) IN $new OR elementId(b) IN $new OR elementId(a) IN $src OR elementId(b) IN $src) "
            "AND elementId(a) IN $all AND elementId(b) IN $all "
            "AND ($types IS NULL OR type(r) IN $types OR NOT (elementId(a) IN $src OR elementId(b) IN $src)) RETURN DISTINCT r",
            new=new_ids, src=ids, all=all_ids, types=types)
        return {"nodes": new_nodes, "edges": [rel_json(r["r"]) for r in erecs]}

    def edges_between(self, ids):
        recs = self.read("MATCH (a)-[r]->(b) WHERE elementId(a) IN $ids AND elementId(b) IN $ids RETURN r", ids=ids)
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
                                               b.get("limit", int(os.environ.get("EXPAND_LIMIT", 50))), b.get("order", "neighbors")))
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
