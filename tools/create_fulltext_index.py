"""Create the full-text index the explorer uses for fast search (one-time admin step).

This is the ONLY script in the prototype that changes a database, and it only adds an index
(no data is written). It needs a user allowed to create indexes, so run it yourself; the
explorer server stays read-only and auto-detects the index by name ("gx_search").

    NEO4J_URI=... NEO4J_USER=... NEO4J_PASSWORD=... python3 tools/create_fulltext_index.py          # prints the statement
    NEO4J_URI=... NEO4J_USER=... NEO4J_PASSWORD=... python3 tools/create_fulltext_index.py --yes    # creates it
    A gitignored .env supplies any of those variables that are unset. The environment wins.

Neo4j full-text indexes need an explicit label list; this uses every label currently in the
database and the search fields from cartography_styles.json. Re-run with --replace after new
Cartography modules add labels.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from neo4j_env import Neo4jConfigError, neo4j_settings  # noqa: E402

NAME = "gx_search"


def q(x):
    return "`" + x.replace("`", "``") + "`"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="actually create the index")
    ap.add_argument("--replace", action="store_true", help="drop and recreate an existing gx_search index")
    a = ap.parse_args()
    fields = json.loads((ROOT / "cartography_styles.json").read_text())["search_fields"]
    try:
        settings = neo4j_settings()
    except Neo4jConfigError as e:
        sys.exit(str(e))
    with GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password)) as d, \
            d.session(database=settings.database) as s:
        labels = [r["label"] for r in s.run("CALL db.labels() YIELD label RETURN label ORDER BY label")]
        stmt = (f"CREATE FULLTEXT INDEX {NAME} IF NOT EXISTS FOR (n:{'|'.join(q(l) for l in labels)}) "
                f"ON EACH [{', '.join('n.' + q(f) for f in fields)}] OPTIONS {{indexConfig: {{`fulltext.analyzer`: 'standard-no-stop-words'}}}}")
        print(f"{len(labels)} labels x {len(fields)} properties on {settings.uri}")
        if not a.yes:
            print(stmt[:600] + (" ..." if len(stmt) > 600 else ""))
            print("\nDry run. Re-run with --yes to create it.")
            return
        if a.replace:
            s.run(f"DROP INDEX {NAME} IF EXISTS").consume()
        t = time.time()
        s.run(stmt).consume()
        s.run("CALL db.awaitIndexes(3600)").consume()
        print(f"index {NAME} online after {time.time() - t:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
