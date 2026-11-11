"""Export the whole (local, test) Neo4j graph to data/cartography-fixtures.json.gz.

Used once after loading Cartography's demo seeds and integration-test fixtures, so the
prototype can reload real-schema mock data without cloning Cartography. Read-only.
"""
import gzip, json, os, sys
from neo4j import GraphDatabase

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "data", "cartography-fixtures.json.gz")


def plain(v):
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)  # temporal and spatial values


def main():
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    auth = (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", ""))
    with GraphDatabase.driver(uri, auth=auth) as d, d.session(default_access_mode="READ") as s:
        nodes = [{"k": r["k"], "l": r["l"], "p": {k: plain(v) for k, v in r["p"].items()}}
                 for r in s.run("MATCH (n) RETURN elementId(n) AS k, labels(n) AS l, properties(n) AS p")]
        rels = [{"s": r["s"], "d": r["d"], "t": r["t"], "p": {k: plain(v) for k, v in r["p"].items()}}
                for r in s.run("MATCH (a)-[r]->(b) RETURN elementId(a) AS s, elementId(b) AS d, type(r) AS t, properties(r) AS p")]
    ids = {n["k"]: i for i, n in enumerate(nodes)}
    for n in nodes:
        n.pop("k")
    for r in rels:
        r["s"], r["d"] = ids[r["s"]], ids[r["d"]]
    with gzip.open(OUT, "wt") as f:
        json.dump({"source": "cartography demo seeds + tests/data fixtures (cartography-cncf/cartography@372c479)", "nodes": nodes, "rels": rels}, f)
    print(f"exported {len(nodes)} nodes / {len(rels)} relationships to {os.path.abspath(OUT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
