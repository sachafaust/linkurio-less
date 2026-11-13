#!/usr/bin/env python3
"""Check an already-running explorer against a graph of at least 15M nodes and 60M edges.

This never wipes the database and never loads one. Without REQUIRE_LIVE_SCALE=1 it
exits 0 and prints the command that builds that graph.

    python3 run_local.py --live-scale
    REQUIRE_LIVE_SCALE=1 python3 tests/test_live_scale.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

LIVE_NODES = 15_000_000
LIVE_EDGES = 60_000_000
URL = os.environ.get("GX_URL", "http://127.0.0.1:8744").rstrip("/")
FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{(': ' + detail) if detail else ''}")
        FAILS.append(name)


def call(path, body=None, timeout=60):
    if body is None:
        req = urllib.request.Request(URL + path)
    else:
        req = urllib.request.Request(
            URL + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
            return r.status, data, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"error": raw}
        return e.code, data, 0.0


def main():
    if os.environ.get("REQUIRE_LIVE_SCALE") != "1":
        print("SKIP live-scale checks. The minimum graph is 15,000,000 nodes and 60,000,000 edges.")
        print("Load it with: python3 run_local.py --live-scale")
        print("Then run: REQUIRE_LIVE_SCALE=1 GX_URL=http://127.0.0.1:8744 python3 tests/test_live_scale.py")
        return 0
    print(f"live-scale checks against {URL}")
    try:
        code, status, _ = call("/api/status", timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL explorer did not answer at {URL}: {e}")
        return 1
    if code != 200:
        print(f"FAIL /api/status HTTP {code}: {status}")
        return 1
    nodes, edges = status.get("nodes") or 0, status.get("edges") or 0
    check("database holds at least 15M nodes and 60M edges",
          nodes >= LIVE_NODES and edges >= LIVE_EDGES, f"{nodes:,} nodes / {edges:,} edges")
    if nodes < LIVE_NODES or edges < LIVE_EDGES:
        print("\nThe connected database is smaller than the minimum. Load it with: python3 run_local.py --live-scale")
        return 1
    budget = status.get("budget") or {}
    expand_limit = int(budget.get("expand_limit") or 50)
    max_nodes = int(budget.get("max_nodes") or 2000)
    max_edges = int(budget.get("max_edges") or 5000)
    cypher_limit = int(budget.get("cypher_limit") or 500)

    code, hub, _ = call("/api/cypher", {
        "query": "MATCH (a:AWSAccount) RETURN a ORDER BY COUNT { (a)--() } DESC LIMIT 1",
    }, timeout=120)
    hub_nodes = hub.get("nodes") or []
    check("an AWSAccount is present to expand", code == 200 and len(hub_nodes) == 1, f"HTTP {code}")
    if not hub_nodes:
        return 1
    degree = hub_nodes[0].get("degree") or 0
    code, page, elapsed = call("/api/expand", {
        "ids": [hub_nodes[0]["id"]], "visible": [], "limit": expand_limit,
        "on_screen": {"nodes": 1, "edges": 0},
        "budget": {"nodes": max_nodes, "edges": max_edges},
    }, timeout=30)
    reasons = (page.get("truncation") or {}).get("reasons") or []
    check("expand of the largest account stays inside the page caps",
          code == 200 and elapsed < 30
          and len(page.get("nodes") or []) <= expand_limit
          and len(page.get("edges") or []) <= max_edges,
          f"HTTP {code}, {elapsed:.1f}s, {len(page.get('nodes') or [])} nodes / {len(page.get('edges') or [])} edges")
    if degree > expand_limit:
        check("a hub expand says the page was cut",
              page.get("truncated") and ("expand_limit" in reasons or "node_budget" in reasons),
              repr(page.get("truncation")))

    code, flooded, elapsed = call("/api/cypher", {"query": "MATCH (n) RETURN n"}, timeout=30)
    reasons = (flooded.get("truncation") or {}).get("reasons") or []
    got = len(flooded.get("nodes") or [])
    check("MATCH (n) RETURN n stops at the row cap",
          code == 200 and elapsed < 30 and got <= min(cypher_limit, max_nodes)
          and flooded.get("truncated") and "row_limit" in reasons,
          f"HTTP {code}, {elapsed:.1f}s, {got} nodes, {reasons}")

    code, full, _ = call("/api/cypher", {
        "query": "MATCH (n) RETURN n",
        "on_screen": {"nodes": max_nodes, "edges": max_edges},
        "budget": {"nodes": max_nodes, "edges": max_edges},
    }, timeout=30)
    reasons = (full.get("truncation") or {}).get("reasons") or []
    check("a full canvas returns no nodes and names the node budget",
          code == 200 and len(full.get("nodes") or []) == 0 and "node_budget" in reasons,
          f"HTTP {code}, {len(full.get('nodes') or [])} nodes, {reasons}")

    code, denied, _ = call("/api/cypher", {"query": "MATCH (n) DETACH DELETE n"}, timeout=30)
    check("DETACH DELETE stays forbidden", code == 403, f"HTTP {code} {denied}")

    code, schema, elapsed = call("/api/schema?refresh=1", timeout=180)
    check("the data model is counts and bounded samples",
          code == 200 and elapsed < 180 and isinstance(schema.get("categories"), dict) and schema.get("categories"),
          f"HTTP {code}, {elapsed:.1f}s, categories={len((schema.get('categories') or {}))}")
    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print(f"\nlive-scale checks passed ({nodes:,} nodes / {edges:,} edges)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
