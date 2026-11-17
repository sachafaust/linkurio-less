#!/usr/bin/env python3
"""Canvas budget: session caps, the Cypher LIMIT wrapper, and the live-scale plan.

The API section starts a second read-only server against the Neo4j that is already
running (NEO4J_PASSWORD, or the local test password). It does not wipe anything.
If Neo4j is down, that section is skipped and the pure checks still run.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from seed_cartography import (  # noqa: E402
    LABEL_WEIGHTS,
    LIVE_ACCOUNTS,
    LIVE_EDGES,
    LIVE_NODES,
    LIVE_SUPER,
    edge_total,
    iter_edges,
    node_total,
    plan_live_scale,
)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{(': ' + detail) if detail else ''}")
        FAILS.append(name)


def test_session_caps():
    print("session caps")
    ceiling = server.session_caps({
        "on_screen": {"nodes": 1, "edges": 1},
        "budget": {"nodes": server.MAX_NODES + 50, "edges": server.MAX_EDGES + 50},
    })
    check("a session budget cannot rise above the server ceiling",
          ceiling["max_nodes"] == server.MAX_NODES and ceiling["max_edges"] == server.MAX_EDGES
          and ceiling["remain_nodes"] == server.MAX_NODES - 1
          and ceiling["remain_edges"] == server.MAX_EDGES - 1,
          repr(ceiling))
    low = max(1, server.MAX_NODES // 2)
    lowered = server.session_caps({
        "on_screen": {"nodes": 1, "edges": 0},
        "budget": {"nodes": low, "edges": 1},
    })
    check("a session budget can lower the ceiling",
          lowered["max_nodes"] == low and lowered["remain_nodes"] == low - 1 and lowered["remain_edges"] == 1,
          repr(lowered))
    hostile = server.session_caps({"on_screen": {"nodes": -5, "edges": "nope"}})
    check("a hostile on_screen count stays inside the ceiling",
          hostile["remain_nodes"] == server.MAX_NODES and hostile["remain_edges"] == server.MAX_EDGES
          and hostile["max_nodes"] == server.MAX_NODES,
          repr(hostile))
    raised = server.session_caps({"budget": {"expand_limit": server.EXPAND_MAX + 20, "cypher_limit": server.CYPHER_ROW_LIMIT + 20}})
    check("expand and query caps cannot rise above the server ceiling",
          raised["expand_limit"] == server.EXPAND_MAX and raised["cypher_limit"] == server.CYPHER_ROW_LIMIT,
          repr(raised))
    page = server.session_caps({"budget": {"expand_limit": 2, "cypher_limit": 3}})
    check("expand and query caps can be lowered",
          page["expand_limit"] == min(2, server.EXPAND_MAX) and page["cypher_limit"] == min(3, server.CYPHER_ROW_LIMIT),
          repr(page))


def test_suggest():
    print("hardware suggestion")
    gb = 1024 ** 3
    mid = server.suggest_limits(8, 16 * gb, "unknown")
    check("16 GB and 8 cores suggests 4,000 nodes",
          mid == {"nodes": 4000, "edges": 10000, "expand_limit": 50, "cypher_limit": 500}, repr(mid))
    check("8 GB keeps the 2,000 node working set",
          server.suggest_limits(8, 8 * gb, "unknown")["nodes"] == 2000)
    small = server.suggest_limits(8, 4 * gb, "unknown")
    check("under 8 GB suggests 1,000 nodes", small["nodes"] == 1000 and small["expand_limit"] == 25, repr(small))
    soft = server.suggest_limits(8, 16 * gb, "software")
    check("a software GPU cuts the suggestion",
          soft["nodes"] == 2000 and soft["expand_limit"] == 25 and soft["edges"] < mid["edges"], repr(soft))
    hard = server.suggest_limits(8, 32 * gb, "hardware")
    plain = server.suggest_limits(8, 32 * gb, "unknown")
    check("a hardware GPU on a large machine raises the node count",
          plain["nodes"] == 8000 and hard["nodes"] == 10000 and hard["edges"] > plain["edges"], repr((plain, hard)))
    check("two cores shrink a 16 GB machine",
          server.suggest_limits(2, 16 * gb, "unknown")["nodes"] == 2000)
    check("four cores sit between two and eight",
          server.suggest_limits(4, 16 * gb, "unknown")["nodes"] == 3000)
    tiny = server.suggest_limits(1, 2 * gb, "software")
    check("a small machine still gets a usable page",
          tiny["nodes"] >= 500 and tiny["edges"] >= 500 and tiny["expand_limit"] >= 1, repr(tiny))
    clamped = server.clamp_suggestion(hard, {"max_nodes": 6, "max_edges": 4, "expand_limit": 3, "cypher_limit": 5})
    check("a suggestion clamps to a tiny ceiling",
          clamped == {"nodes": 6, "edges": 4, "expand_limit": 3, "cypher_limit": 5}, repr(clamped))
    heard = server.suggest_for({"cpu": 8, "device_memory_gb": 4, "gpu": "unknown"})
    check("a browser that reports 4 GB uses that RAM",
          heard["nodes"] == min(1000, server.MAX_NODES) and heard["resources"]["gpu"] == "unknown" and "CPU" in heard["reason"],
          repr(heard))
    swift = server.suggest_for({"cpu": 8, "gpu": "hardware", "renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device))"})
    check("SwiftShader counts as a software GPU even if the page says hardware",
          swift["resources"]["gpu"] == "software" and swift["nodes"] <= server.MAX_NODES, repr(swift["resources"]))


def test_hard_limit():
    print("Cypher hard limit")
    bare = server.apply_hard_limit("MATCH (n) RETURN n", 10)
    check("a query with no LIMIT is wrapped",
          bare.startswith("CALL {") and "LIMIT 10" in bare and bare.rstrip().endswith("LIMIT 10"),
          bare)
    smaller = server.apply_hard_limit("MATCH (n) RETURN n LIMIT 3", 10)
    check("a smaller LIMIT is kept, with the outer cap",
          "LIMIT 3" in smaller and "RETURN * LIMIT 10" in smaller, smaller)
    larger = server.apply_hard_limit("MATCH (n) RETURN n LIMIT 99999", 10)
    check("a larger LIMIT is lowered",
          "99999" not in larger and "LIMIT 10" in larger, larger)
    literal = server.apply_hard_limit("RETURN 'LIMIT 99999' AS s LIMIT 4", 10)
    check("LIMIT inside a string is left alone",
          "'LIMIT 99999'" in literal and "LIMIT 4" in literal and "RETURN * LIMIT 10" in literal, literal)
    try:
        server.apply_hard_limit("MATCH (n) RETURN n; MATCH (m) RETURN m", 10)
        check("two statements are rejected", False, "no error")
    except PermissionError as e:
        check("two statements are rejected", "one statement" in str(e), str(e))


def test_plan():
    print("live-scale plan")
    big = plan_live_scale(LIVE_NODES, LIVE_EDGES)
    check("15M nodes and 60M edges are exact",
          big["nodes"] == LIVE_NODES and big["edges"] == LIVE_EDGES
          and node_total(big) == LIVE_NODES and edge_total(big) == LIVE_EDGES,
          f"{node_total(big)} / {edge_total(big)}")
    check("canonical accounts and supernode",
          big["accounts"] == LIVE_ACCOUNTS == 1500 and big["super_resources"] == LIVE_SUPER == 2_000_000,
          repr((big["accounts"], big["super_resources"])))
    counts = {s["label"]: s["count"] for s in big["labels"]}
    check("label counts sum to the resources and match the canonical weights",
          sum(counts.values()) == big["resources"]
          and all(counts.get(label) == weight for label, _, weight in LABEL_WEIGHTS),
          repr(counts))

    small = plan_live_scale(400, 1600)
    edges = list(iter_edges(small))
    check("400 nodes / 1600 edges, every edge unique",
          len(edges) == 1600 and len(set(edges)) == 1600, f"len={len(edges)} unique={len(set(edges))}")
    resource = [e for e in edges if e[2] == "RESOURCE"]
    targets = [e[4] for e in resource]
    check("one RESOURCE edge per resource, to a distinct target",
          len(targets) == small["resources"] and len(set(targets)) == len(targets),
          f"{len(targets)} targets, {small['resources']} resources")
    owned = [e for e in resource if e[1] == "aws-account:0"]
    check("account 0 owns the planned supernode share",
          len(owned) == small["super_resources"], f"{len(owned)} vs {small['super_resources']}")
    try:
        plan_live_scale(10, 10**9)
        check("an impossible edge count is rejected", False)
    except ValueError:
        check("an impossible edge count is rejected", True)


def _post(url, path, body):
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw}
        return e.code, payload


def _neo4j():
    from neo4j import GraphDatabase
    uri = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    passwords = []
    if os.environ.get("NEO4J_PASSWORD"):
        passwords.append(os.environ["NEO4J_PASSWORD"])
    if "tinkerlab-test" not in passwords:
        passwords.append("tinkerlab-test")
    last = None
    for pw in passwords:
        try:
            driver = GraphDatabase.driver(uri, auth=(user, pw))
            driver.verify_connectivity()
            driver.close()
            return uri, user, pw
        except Exception as e:  # noqa: BLE001
            last = e
    print(f"  skip API (Neo4j is not reachable: {last})")
    return None


def test_graph_counts():
    print("graph counts")
    sample = {
        "nodes": [
            {"count": 10},
            {"count": 10, "label": "Person"},
            {"count": 4, "label": "Employee"},
        ],
        "relationships": [
            {"count": 7},
            {"relationshipType": "KNOWS", "count": 7},
            {"count": 3, "relationshipType": "KNOWS", "startLabel": "Person"},
            {"count": 3, "relationshipType": "KNOWS", "endLabel": "Person"},
        ],
    }
    got = server.graph_count_totals(sample)
    check("store totals ignore per-label and pattern rows",
          got == {"nodes": 10, "edges": 7,
                  "labels": {"Person": 10, "Employee": 4}, "types": {"KNOWS": 7}},
          repr(got))
    check("a payload with no totals is ignored",
          server.graph_count_totals({"nodes": [{"count": 1, "label": "Person"}], "relationships": []}) is None
          and server.graph_count_totals(None) is None
          and server.graph_count_totals({"nodes": [], "relationships": [{"count": 1}]}) is None)
    found = _neo4j()
    if not found:
        return
    from neo4j_env import Settings
    uri, user, pw = found
    g = server.Graph(Settings(uri, user, pw, os.environ.get("NEO4J_DATABASE") or "neo4j"))
    try:
        got = g.store_counts()
        n, r = g._count_with_cypher()
        check("db.stats.retrieve totals match the count store",
              got is not None and got["nodes"] == n and got["edges"] == r
              and sum(got["types"].values()) == r,
              repr(None if got is None else (got["nodes"], got["edges"], n, r)))
    finally:
        g.driver.close()


def test_config_file():
    print("config file")
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "gx.local.json"
    old = os.environ.get("GX_CONFIG")
    os.environ["GX_CONFIG"] = str(path)
    server._WORKING = None
    server._CONFIG_CREATED = False
    try:
        first = server.ensure_config()
        sug = server.suggest_for({})
        check("a missing config is created from this machine",
              path.is_file() and first["source"] == "auto" and first["gpu"] == "unknown"
              and first["nodes"] == sug["nodes"] and first["edges"] == sug["edges"]
              and server._CONFIG_CREATED is True,
              repr(first))
        server._WORKING = None
        server._CONFIG_CREATED = False
        second = server.ensure_config()
        check("the next launch reads the file",
              second["nodes"] == first["nodes"] and second["edges"] == first["edges"]
              and second["source"] == "auto" and server._CONFIG_CREATED is False,
              repr(second))
        saved = server.save_config({
            "nodes": 99999999, "edges": 99999999, "expand_limit": 999999, "cypher_limit": 999999,
            "lru": True, "reason": "custom",
        })
        on_disk = json.loads(path.read_text())
        check("a saved budget is clamped and written",
              saved["nodes"] == server.MAX_NODES and saved["edges"] == server.MAX_EDGES
              and saved["expand_limit"] == server.EXPAND_MAX and saved["cypher_limit"] == server.CYPHER_ROW_LIMIT
              and saved["source"] == "user" and saved["lru"] is True
              and on_disk["nodes"] == server.MAX_NODES and on_disk["source"] == "user",
              repr(saved))
        server._WORKING = None
        again = server.ensure_config()
        check("a saved budget is what the next launch loads",
              again["nodes"] == saved["nodes"] and again["source"] == "user" and again["lru"] is True,
              repr(again))
        path.write_text(json.dumps(first))
        server._WORKING = None
        server.ensure_config()
        refined = server.refine_config({"gpu": "hardware", "cpu": 32, "device_memory_gb": 0, "renderer": "Apple M4"})
        check("the first browser with a GPU updates an automatic config",
              refined["updated"] is True and refined["config"]["gpu"] == "hardware"
              and refined["config"]["source"] == "auto" and "hardware GPU" in refined["config"]["reason"],
              repr(refined["config"]))
        kept = server.refine_config({"gpu": "software", "renderer": "llvmpipe"})
        check("a later GPU does not replace a config that already has one",
              kept["updated"] is False and kept["config"]["gpu"] == "hardware"
              and kept["config"]["nodes"] == refined["config"]["nodes"])
        server.save_config({"nodes": 1000, "edges": 2500, "expand_limit": 25, "cypher_limit": 200, "reason": "edited"})
        blocked = server.refine_config({"gpu": "hardware", "renderer": "Apple M4"})
        check("a budget saved from the UI is not replaced",
              blocked["updated"] is False and blocked["config"]["source"] == "user"
              and blocked["config"]["nodes"] == min(1000, server.MAX_NODES))
        path.write_text("{not json")
        server._WORKING = None
        server.ensure_config()
        check("a broken config file is left unchanged", path.read_text() == "{not json")
        ignored = subprocess.run(["git", "check-ignore", "gx.local.json"], cwd=ROOT, capture_output=True, text=True)
        check("the local config is gitignored", ignored.returncode == 0 and ignored.stdout.strip() == "gx.local.json",
              ignored.stdout.strip() or ignored.stderr.strip())
    finally:
        server._WORKING = None
        server._CONFIG_CREATED = False
        if old is None:
            os.environ.pop("GX_CONFIG", None)
        else:
            os.environ["GX_CONFIG"] = old
        tmp.cleanup()


def test_api():
    print("API caps (read-only, no wipe)")
    found = _neo4j()
    if not found:
        return
    uri, user, pw = found
    cfg_dir = tempfile.TemporaryDirectory()
    cfg_path = Path(cfg_dir.name) / "gx.local.json"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = {
        **os.environ,
        "NEO4J_URI": uri, "NEO4J_USER": user, "NEO4J_PASSWORD": pw,
        "HOST": "127.0.0.1", "PORT": str(port), "QUIET": "1",
        "MAX_NODES": "6", "MAX_EDGES": "4", "EXPAND_LIMIT": "3", "CYPHER_ROW_LIMIT": "5",
        "GX_CONFIG": str(cfg_path),
        "GX_CACHE_DIR": str(Path(cfg_dir.name) / "cache"),
    }
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        status = None
        for _ in range(40):
            if proc.poll() is not None:
                err = proc.stderr.read().decode()[-500:]
                check("tiny-budget server starts", False, err)
                return
            try:
                with urllib.request.urlopen(url + "/api/status", timeout=2) as r:
                    status = json.loads(r.read().decode())
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        if status is None:
            check("tiny-budget server starts", False, "status never answered")
            return
        budget = status.get("budget") or {}
        check("status reports the server budget",
              budget.get("max_nodes") == 6 and budget.get("max_edges") == 4
              and budget.get("expand_limit") == 3 and budget.get("cypher_limit") == 5,
              repr(budget))
        code_n, node_count = _post(url, "/api/cypher", {"query": "MATCH (n) RETURN count(n) AS n"})
        code_r, rel_count = _post(url, "/api/cypher", {"query": "MATCH ()-[r]->() RETURN count(r) AS n"})

        def _cell(body):
            rows = body.get("rows") or []
            return rows[0][0] if rows and rows[0] else None

        check("status totals match the count store",
              code_n == 200 and code_r == 200
              and status.get("nodes") == _cell(node_count)
              and status.get("edges") == _cell(rel_count),
              f"status {status.get('nodes')}/{status.get('edges')} "
              f"vs count {_cell(node_count)}/{_cell(rel_count)}")
        cfg = budget.get("config") or {}
        check("the first launch writes a config file",
              budget.get("config_created") is True and budget.get("config_file") == "gx.local.json"
              and cfg.get("source") == "auto" and cfg.get("gpu") == "unknown"
              and cfg.get("nodes") == 6 and cfg.get("edges") == 4
              and cfg.get("expand_limit") == 3 and cfg.get("cypher_limit") == 5
              and cfg_path.is_file(),
              repr(cfg))
        code, refined = _post(url, "/api/budget/refine", {
            "gpu": "hardware", "cpu": 8, "device_memory_gb": 0, "renderer": "Apple M4",
        })
        check("the browser GPU is written into an automatic config",
              code == 200 and refined.get("updated") is True
              and refined.get("config", {}).get("gpu") == "hardware"
              and refined.get("config", {}).get("nodes") == 6,
              repr(refined))
        code, second_gpu = _post(url, "/api/budget/refine", {"gpu": "software", "renderer": "llvmpipe"})
        check("a second browser does not replace that GPU",
              code == 200 and second_gpu.get("updated") is False
              and second_gpu.get("config", {}).get("gpu") == "hardware")
        code, data = _post(url, "/api/cypher", {"query": "MATCH (n) RETURN n"})
        nodes = data.get("nodes") or []
        check("MATCH (n) RETURN n stays inside the canvas cap",
              code == 200 and len(nodes) <= 6, f"HTTP {code}, {len(nodes)} nodes")
        if status.get("nodes", 0) > len(nodes):
            reasons = (data.get("truncation") or {}).get("reasons") or []
            check("a query with no LIMIT says it stopped",
                  data.get("truncated") and ("row_limit" in reasons or "node_budget" in reasons),
                  repr(data.get("truncation")))
        code, data = _post(url, "/api/cypher", {
            "query": "MATCH (n) RETURN n",
            "on_screen": {"nodes": 5, "edges": 0},
            "budget": {"nodes": 6, "edges": 4},
        })
        check("on_screen leaves room for one more node",
              code == 200 and len(data.get("nodes") or []) <= 1, f"HTTP {code}, {len(data.get('nodes') or [])} nodes")
        seed_nodes = nodes or data.get("nodes") or []
        if not seed_nodes:
            code, one = _post(url, "/api/cypher", {"query": "MATCH (n) RETURN n LIMIT 1"})
            seed_nodes = one.get("nodes") or []
        if seed_nodes:
            code, exp = _post(url, "/api/expand", {
                "ids": [seed_nodes[0]["id"]], "visible": [], "limit": 100,
                "on_screen": {"nodes": 1, "edges": 0},
                "budget": {"nodes": 6, "edges": 4},
            })
            check("expand limit 100 still returns at most the expand cap",
                  code == 200 and len(exp.get("nodes") or []) <= 3 and len(exp.get("edges") or []) <= 4,
                  f"HTTP {code}, {len(exp.get('nodes') or [])} nodes / {len(exp.get('edges') or [])} edges")
        code, lied = _post(url, "/api/cypher", {
            "query": "MATCH (n) RETURN n",
            "budget": {"nodes": 99999, "edges": 99999},
        })
        check("a lying budget cannot exceed the server ceiling",
              code == 200 and len(lied.get("nodes") or []) <= 6 and len(lied.get("edges") or []) <= 4,
              f"{len(lied.get('nodes') or [])} nodes / {len(lied.get('edges') or [])} edges")
        code, sug = _post(url, "/api/budget/suggest", {"gpu": "hardware", "cpu": 32, "device_memory_gb": 32, "renderer": "Apple M2"})
        check("a suggestion cannot rise above the tiny server ceiling",
              code == 200 and sug.get("nodes", 99) <= 6 and sug.get("edges", 99) <= 4
              and sug.get("expand_limit", 99) <= 3 and sug.get("cypher_limit", 99) <= 5
              and "CPU" in (sug.get("reason") or ""),
              repr(sug))
        if seed_nodes:
            code, one_page = _post(url, "/api/expand", {
                "ids": [seed_nodes[0]["id"]], "visible": [], "limit": 100,
                "on_screen": {"nodes": 1, "edges": 0},
                "budget": {"nodes": 6, "edges": 4, "expand_limit": 1},
            })
            check("a saved expand cap of 1 is what the server returns",
                  code == 200 and len(one_page.get("nodes") or []) <= 1,
                  f"HTTP {code}, {len(one_page.get('nodes') or [])} nodes")
        for q in ("MATCH (n) DETACH DELETE n", "CREATE (n:X)"):
            code, body = _post(url, "/api/cypher", {"query": q})
            check(f"write stays forbidden: {q.split()[0]}", code == 403, f"HTTP {code} {body}")
        code, saved = _post(url, "/api/budget", {
            "nodes": 2, "edges": 2, "expand_limit": 1, "cypher_limit": 2, "lru": True, "reason": "from the UI",
        })
        disk = json.loads(cfg_path.read_text())
        check("the UI save is what the config file stores",
              code == 200 and saved.get("config", {}).get("source") == "user"
              and saved.get("config", {}).get("nodes") == 2 and saved.get("config", {}).get("lru") is True
              and disk.get("nodes") == 2 and disk.get("source") == "user",
              repr(saved))
        code, capped = _post(url, "/api/cypher", {
            "query": "MATCH (n) RETURN n",
            "budget": {"nodes": 99999, "edges": 99999},
        })
        check("a request cannot rise above the saved budget",
              code == 200 and len(capped.get("nodes") or []) <= 2,
              f"HTTP {code}, {len(capped.get('nodes') or [])} nodes")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        cfg_dir.cleanup()


def main():
    test_session_caps()
    test_suggest()
    test_config_file()
    test_hard_limit()
    test_plan()
    test_graph_counts()
    test_api()
    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print("\nall budget checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
