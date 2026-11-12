#!/bin/sh
# Rebuild data/cartography-fixtures.json.gz from Cartography's own demo seeds + integration-test fixtures.
#
# Needs: git, Python 3.10+, and an EMPTY-able LOCAL Neo4j 5 (it is WIPED). Easiest: the run_local.py container:
#   python3 run_local.py --no-browser --detach          # then:
#   sh tools/cartography_fixtures/regenerate.sh
# Env: NEO4J_URI (default bolt://127.0.0.1:7687), NEO4J_USER (neo4j), NEO4J_PASSWORD (tinkerlab-test),
#      CARTO_SRC (default ./.cartography, cloned if missing), CARTO_REF (optional commit/branch).
# No cloud credentials are used. Note: Cartography's Semgrep demo seed makes one real HTTPS call to semgrep.dev
# with a fake token (it gets a 403); see CARTOGRAPHY-MOCK-DATA.md in the research folder.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
PROTO=$(cd "$HERE/../.." && pwd)
NEO4J_URI=${NEO4J_URI:-bolt://127.0.0.1:7687}; NEO4J_USER=${NEO4J_USER:-neo4j}; NEO4J_PASSWORD=${NEO4J_PASSWORD:-tinkerlab-test}
case "$NEO4J_URI" in *localhost*|*127.0.0.1*) ;; *) echo "refusing non-local NEO4J_URI $NEO4J_URI" >&2; exit 1;; esac
CARTO_SRC=${CARTO_SRC:-$PROTO/.cartography}
if [ ! -d "$CARTO_SRC/.git" ]; then git clone -q --depth 50 https://github.com/cartography-cncf/cartography.git "$CARTO_SRC"; fi
if [ -n "${CARTO_REF:-}" ]; then git -C "$CARTO_SRC" fetch -q --depth 50 origin "$CARTO_REF" && git -C "$CARTO_SRC" checkout -q FETCH_HEAD; fi
echo "cartography at $(git -C "$CARTO_SRC" rev-parse --short HEAD)"
PY="$CARTO_SRC/.venv/bin/python"
[ -x "$PY" ] || python3 -m venv "$CARTO_SRC/.venv"
"$PY" -m pip install -q -e "$CARTO_SRC" pytest pytest-cov pytest-asyncio testcontainers moto
export NEO4J_URL="$NEO4J_URI" NEO4J_URI NEO4J_USER NEO4J_PASSWORD CARTO_SRC
echo "1/3 demo seeder (tolerant runner; wipes the database)"
"$PY" "$HERE/run_demo.py"
echo "2/3 AWS, GCP, Okta, GitHub integration-test fixtures (failures are expected: exact-set asserts see accumulated data)"
(cd "$CARTO_SRC" && PYTHONPATH="$HERE:$CARTO_SRC" "$PY" -m pytest -p keep_data_plugin -q -o addopts="" --continue-on-collection-errors \
  tests/integration/cartography/intel/aws tests/integration/cartography/intel/gcp \
  tests/integration/cartography/intel/okta tests/integration/cartography/intel/github) || true
echo "3/3 export to data/cartography-fixtures.json.gz"
"$PY" "$HERE/export_graph.py"
