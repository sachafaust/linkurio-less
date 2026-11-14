#!/usr/bin/env python3
"""The data model is saved per Neo4j connection and kept out of git."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def test_key_strips_secrets():
    key = server.connection_cache_key("bolt://neo4j:s3cret@127.0.0.1:7687", "neo4j")
    check("connection key drops userinfo", key == "bolt://127.0.0.1:7687|neo4j", key)
    other = server.connection_cache_key("bolt://127.0.0.1:7687", "other")
    check("database is part of the key", other != key, other)


def test_roundtrip(tmp):
    path = Path(tmp) / "gx.schema.json"
    body = {"labels": {"A": 1}, "types": {"T": 2}, "categories": {"A": {"count": 1, "labels": ["A"], "props": {}}},
            "links": [{"source": "A", "type": "T", "target": "A"}]}
    server.store_cached_schema("bolt://neo4j:s3cret@127.0.0.1:7687", "neo4j", body, "2026-11-14T00:00:00Z", path)
    text = path.read_text(encoding="utf-8")
    check("saved file has no password", "s3cret" not in text)
    server.store_cached_schema("bolt://10.0.0.2:7687", "neo4j", body, "2026-11-14T01:00:00Z", path)
    loaded = server.load_cached_schema("bolt://127.0.0.1:7687", "neo4j", path)
    check("first connection is still there", loaded and loaded["saved_at"] == "2026-11-14T00:00:00Z" and loaded["schema"]["labels"]["A"] == 1)
    check("other database misses", server.load_cached_schema("bolt://127.0.0.1:7687", "other", path) is None)
    path.write_text("{not json", encoding="utf-8")
    check("a broken file is ignored", server.load_cached_schema("bolt://127.0.0.1:7687", "neo4j", path) is None)


def test_graph_uses_disk_until_refresh(tmp):
    old = os.environ.get("GX_CONFIG")
    os.environ["GX_CONFIG"] = str(Path(tmp) / "gx.local.json")
    try:
        body = {"labels": {"A": 1}, "types": {}, "categories": {"A": {"count": 1, "labels": ["A"], "props": {}}}, "links": []}
        server.store_cached_schema("bolt://user:s3cret@127.0.0.1:7687", "neo4j", body, "2026-11-14T00:00:00Z")
        g = server.Graph.__new__(server.Graph)
        g.uri = "bolt://user:s3cret@127.0.0.1:7687"
        g.db = "neo4j"
        g.schema_cache = server.TTLCache(ttl=600, size=4)
        g.read = lambda *a, **k: (_ for _ in ()).throw(AssertionError("schema was recomputed"))
        hit = g.schema()
        check("disk hit skips Neo4j", hit["cache"]["source"] == "disk" and hit["categories"]["A"]["count"] == 1, str(hit.get("cache")))
        again = g.schema()
        check("the next call stays in memory", again["cache"]["source"] == "disk")
        try:
            g.schema(refresh=True)
            check("refresh reads Neo4j again", False, "refresh returned without reading")
        except AssertionError:
            check("refresh reads Neo4j again", True)
        raw = server.schema_cache_path().read_text(encoding="utf-8")
        check("cache file has no password", "s3cret" not in raw)
    finally:
        if old is None:
            os.environ.pop("GX_CONFIG", None)
        else:
            os.environ["GX_CONFIG"] = old


def test_gitignore():
    ignored = subprocess.run(["git", "check-ignore", "gx.schema.json"], cwd=ROOT, capture_output=True, text=True)
    check("gx.schema.json is gitignored", ignored.returncode == 0 and ignored.stdout.strip() == "gx.schema.json",
          ignored.stdout.strip() or ignored.stderr.strip())


def main():
    test_key_strips_secrets()
    with tempfile.TemporaryDirectory() as tmp:
        test_roundtrip(tmp)
        test_graph_uses_disk_until_refresh(tmp)
    test_gitignore()
    print(f"\n{len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
