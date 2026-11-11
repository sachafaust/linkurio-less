"""pytest plugin: run Cartography's integration tests against a local, password-protected Neo4j and keep the data.

- adds auth from NEO4J_USER/NEO4J_PASSWORD when a test creates a driver without auth
- turns the per-module "MATCH (n) DETACH DELETE n" teardown into a no-op so fixture data accumulates
"""
import os
import neo4j
from neo4j import GraphDatabase, Session

URL = os.environ["NEO4J_URL"]
assert "localhost" in URL or "127.0.0.1" in URL, "local test database only"
_driver = GraphDatabase.driver.__func__ if hasattr(GraphDatabase.driver, "__func__") else GraphDatabase.driver
AUTH = (os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])


def _patched_driver(cls, uri, *a, **kw):
    if not a and "auth" not in kw:
        kw["auth"] = AUTH
    return _driver(cls, uri, *a, **kw)


GraphDatabase.driver = classmethod(_patched_driver)
_run = Session.run


def _patched_run(self, query, *a, **kw):
    q = query if isinstance(query, str) else getattr(query, "text", "")
    if " ".join(q.split()).rstrip(";").upper() == "MATCH (N) DETACH DELETE N":
        return _run(self, "RETURN 1")
    return _run(self, query, *a, **kw)


Session.run = _patched_run
