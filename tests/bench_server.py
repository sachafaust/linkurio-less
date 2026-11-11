"""Database-side benchmark: latency of the explorer API against whatever Neo4j the server points at.

Runs each call REPEAT times (first call reported separately as "cold") and prints a JSON table.
Usage: python3 tests/bench_server.py --url http://127.0.0.1:8744 --out tests/bench-server.json
"""
import argparse
import json
import statistics
import time
import urllib.parse
import urllib.request


def call(url, path, body=None):
    t = time.perf_counter()
    if body is None:
        r = urllib.request.urlopen(url + path, timeout=600)
    else:
        r = urllib.request.urlopen(urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}), timeout=600)
    data = json.loads(r.read())
    return (time.perf_counter() - t) * 1000, data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8744")
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--out")
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    u = a.url.rstrip("/")
    _, st = call(u, "/api/status")
    _, hub = call(u, "/api/cypher", {"query": "MATCH (a:AWSAccount) RETURN a ORDER BY COUNT { (a)--() } DESC LIMIT 1"})
    hub = hub["nodes"][0]
    _, leaf = call(u, "/api/cypher", {"query": "MATCH (i:EC2Instance) RETURN i LIMIT 1"})
    leaf = leaf["nodes"][0]
    # a big visible set: the first 5,000 nodes of the hub
    _, page = call(u, "/api/expand", {"ids": [hub["id"]], "visible": [], "limit": 5000})
    visible = [n["id"] for n in page["nodes"]]
    ops = [
        ("schema (cold, then cached)", lambda: call(u, "/api/schema")),
        ("search 'fleet-1234' (no index: scan)", lambda: call(u, "/api/search?" + urllib.parse.urlencode({"q": "fleet-1234"}))),
        ("search 'payments' ", lambda: call(u, "/api/search?" + urllib.parse.urlencode({"q": "payments"}))),
        ("neighbor summary of the hub", lambda: call(u, "/api/neighbors", {"ids": [hub["id"]], "visible": []})),
        ("expand hub, first 50", lambda: call(u, "/api/expand", {"ids": [hub["id"]], "visible": [], "limit": 50})),
        ("expand hub, 50 most connected", lambda: call(u, "/api/expand", {"ids": [hub["id"]], "visible": [], "limit": 50, "order": "most"})),
        ("expand hub, next 50 with 5,000 on screen", lambda: call(u, "/api/expand", {"ids": [hub["id"]], "visible": visible, "limit": 50})),
        ("expand a leaf (EC2 instance)", lambda: call(u, "/api/expand", {"ids": [leaf["id"]], "visible": [], "limit": 50})),
        ("edges among 5,000 visible nodes", lambda: call(u, "/api/edges-between", {"ids": visible})),
    ]
    out = {"label": a.label, "when": time.strftime("%Y-%m-%d %H:%M %Z"), "db_nodes": st["nodes"], "db_edges": st["edges"],
           "hub_degree": hub.get("degree"), "results": []}
    for name, fn in ops:
        times, err = [], None
        for _ in range(a.repeat):
            try:
                ms, _ = fn()
                times.append(ms)
            except Exception as e:  # noqa: BLE001
                err = str(e)[:160]
                break
        row = {"op": name, "cold_ms": round(times[0]) if times else None,
               "median_ms": round(statistics.median(times[1:] or times)) if times else None, "error": err}
        out["results"].append(row)
        print(json.dumps(row), flush=True)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
