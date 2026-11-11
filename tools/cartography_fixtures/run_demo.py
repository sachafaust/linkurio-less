"""Run Cartography's demo seeder (demo/__main__.py logic) but skip seed modules that fail to import."""
import importlib, inspect, logging, os, pkgutil, sys, time, traceback
import neo4j
sys.path.insert(0, os.environ["CARTO_SRC"]); os.chdir(os.environ["CARTO_SRC"])
from cartography.config import Config
from cartography.intel import create_indexes
from demo import seeds
from demo.seeds.base import AsyncSeed, Seed
logging.basicConfig(level=logging.WARNING)
uri = os.environ.get("NEO4J_URL", "bolt://localhost:7687")
assert "localhost" in uri or "127.0.0.1" in uri, "local test database only"
drv = neo4j.GraphDatabase.driver(uri, auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]))
s = drv.session()
cfg = Config(neo4j_uri=uri, neo4j_user=os.environ["NEO4J_USER"], neo4j_password=os.environ["NEO4J_PASSWORD"],
             ontology_users_source="duo", ontology_devices_source="snipeit", analysis_job_directory=None)
s.run("MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()
create_indexes.run(s, cfg); s.run("CALL db.awaitIndexes(600)").consume()
report = {}
classes = []
for _, name, _ in pkgutil.iter_modules(seeds.__path__):
    if name == "base": continue
    try:
        mod = importlib.import_module(f"demo.seeds.{name}")
    except Exception as e:
        report[name] = f"import failed: {type(e).__name__}: {e}"; continue
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, (Seed, AsyncSeed)) and obj not in (Seed, AsyncSeed) and obj.__module__.startswith("demo.seeds"):
            classes.append(obj)
for c in sorted(classes, key=lambda c: c.__name__):
    t = time.time()
    try:
        c(s, 0).run(); report[c.__name__] = f"ok ({time.time()-t:.1f}s)"
    except Exception as e:
        report[c.__name__] = f"failed: {type(e).__name__}: {str(e)[:160]}"
for step in ("ontology", "analysis"):
    try:
        if step == "ontology":
            from cartography.intel import ontology; ontology.run(s, cfg)
        else:
            import cartography.intel.analysis; cartography.intel.analysis.run(s, cfg)
        report[step] = "ok"
    except Exception as e:
        report[step] = f"failed: {type(e).__name__}: {str(e)[:160]}"
n = s.run("MATCH (n) RETURN count(n) AS n").single()["n"]; r = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
for k, v in report.items(): print(f"{k}: {v}")
print(f"TOTAL nodes={n} rels={r}")
