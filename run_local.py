#!/usr/bin/env python3
"""One command to run the Graph Explorer on your machine with the same mock data used in development.

    python3 run_local.py                # venv + Neo4j in Docker + Cartography fixture data + server + browser

What it does, in order:
  1. checks Python 3.9+ (macOS's built-in /usr/bin/python3 is fine) and Docker (unless you pass --neo4j-uri to use your own Neo4j)
  2. creates .venv/ in this folder and installs requirements.txt (+ requirements-tests.txt with --with-tests/--test)
  3. starts Neo4j 5.26 community in Docker: container "gx-neo4j", volume "gx-neo4j-data", ports bound to
     127.0.0.1 only, test password "tinkerlab-test"; reuses the container if it already exists.
     Also starts container "gx-graphql" (a small Wiz-shaped fixture on 127.0.0.1:8791) when
     GRAPHQL_ENDPOINT is unset, so the page has a GraphQL source beside Neo4j.
  4. loads data (wipes the database first):
       default        the Cartography fixture snapshot (data/cartography-fixtures.json.gz, 6,939 nodes / 11,054 rels,
                      real Cartography schema, produced from Cartography's own demo seeds + test fixtures)
       --synthetic N  the synthetic Cartography-shaped generator, about N nodes (optional; the UI tests use the fixture snapshot)
       --both         fixtures + synthetic estate in one database
       --live-scale   the minimum supported graph: 15,000,000 nodes and 60,000,000 edges
       --keep-data    don't touch the data
  5. starts server.py on http://127.0.0.1:8744 and opens the browser (Ctrl+C stops the server; Neo4j keeps running)

Other commands:
  --test        run the budget tests, the GraphQL tests (mocked API plus the gx-graphql fixture),
                the UI suite (40 checks) and a quick browser + API benchmark, then keep serving.
                The UI tests run against the fixture snapshot. With --live-scale, --test runs the
                budget tests, the GraphQL tests, and the live-scale checks against the 15M / 60M
                graph instead of the UI suite.
  --stop        stop the server, the Neo4j container, and the GraphQL fixture (data kept)
  --reset       remove the container and its volume (all data)
  --status      show what is running

Your own / live Neo4j (local or remote; no Docker, data is NOT touched unless you add --load-data):
  export NEO4J_URI=neo4j+s://host:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=... NEO4J_DATABASE=neo4j
  python3 run_local.py
  The same four variables can live in a gitignored .env (copy .env.example). The environment wins over that file.
  Flags still work: --neo4j-uri, --neo4j-user, --neo4j-database, --neo4j-password.
  Leave the password out of the command to be prompted, so it stays out of your shell history.
  Enter at that prompt sends an empty password. NEO4J_PASSWORD= in the environment or in .env does the same.
  The explorer opens the database read-only.

Everything stays on your machine: Neo4j and the explorer listen on 127.0.0.1 only, the data is fake/fixture data.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
RUN = HERE / ".run"
CONTAINER, VOLUME, IMAGE = "gx-neo4j", "gx-neo4j-data", "neo4j:5.26-community"
PASSWORD = "tinkerlab-test"  # test-only password for the throwaway local container
GQL_CONTAINER = "gx-graphql"
GQL_IMAGE = "python:3.12-alpine"
GQL_PORT = 8791
GQL_CLIENT_ID = "gx-graphql"
GQL_CLIENT_SECRET = "tinkerlab-graphql"  # test-only secret for the local fixture
GQL_ACCESS = "tinkerlab-access"
SYNTH_UNIT = 1099           # nodes in one copy of the synthetic estate


def say(msg):
    print(f"\033[1m==>\033[0m {msg}", flush=True)


def die(msg, code=1):
    print(f"\033[31merror:\033[0m {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def venv_python():
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def port_in_use(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def docker_bin():
    """docker on PATH, or Docker Desktop's usual locations when the shell PATH doesn't include them (macOS)."""
    found = shutil.which("docker")
    if found:
        return found
    for c in ("/usr/local/bin/docker", "/opt/homebrew/bin/docker", os.path.expanduser("~/.docker/bin/docker"),
              "/Applications/Docker.app/Contents/Resources/bin/docker"):
        if os.access(c, os.X_OK):
            return c
    return None


def docker(*args, check=True, capture=True):
    r = subprocess.run([docker_bin() or "docker", *args], capture_output=capture, text=True)
    if check and r.returncode != 0:
        die(f"docker {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
    return r


# ---------------------------------------------------------------- stage 1: bootstrap (system Python)
def best_python():
    """Prefer a newer Python for the venv if one is installed (never required): 3.9 works, newer gets newer wheels.
    RUN_LOCAL_PYTHON=/path/to/python forces the interpreter (e.g. RUN_LOCAL_PYTHON=/usr/bin/python3)."""
    if os.environ.get("RUN_LOCAL_PYTHON"):
        return os.environ["RUN_LOCAL_PYTHON"]
    cands = [f"python3.{m}" for m in (13, 12, 11, 10)] + ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
    best, best_v = sys.executable, sys.version_info[:2]
    for c in cands:
        exe = shutil.which(c) if not c.startswith("/") else (c if os.access(c, os.X_OK) else None)
        if not exe:
            continue
        try:
            out = subprocess.run([exe, "-c", "import sys, venv, ensurepip; print('%d.%d' % sys.version_info[:2])"],
                                 capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            v = tuple(int(x) for x in out.stdout.strip().split("."))
            if (3, 9) <= v and v > best_v:
                best, best_v = exe, v
    return best


def bootstrap(args, argv):
    if sys.version_info < (3, 9):
        die(f"Python 3.9+ is needed (this is {sys.version.split()[0]}). On macOS: `xcode-select --install` or `brew install python`.")
    want_tests = args.with_tests or (args.test and not args.live_scale)
    py = venv_python()
    if not py.exists():
        base = best_python()
        say(f"creating virtualenv {VENV} with {base}")
        subprocess.run([base, "-m", "venv", str(VENV)], check=True)
    stamp = VENV / ".gx-installed"
    reqs = [HERE / "requirements.txt"] + ([HERE / "requirements-tests.txt"] if want_tests else [])
    key = ";".join(f"{r.name}:{r.stat().st_mtime_ns}" for r in reqs)
    if not stamp.exists() or key not in stamp.read_text().splitlines():
        say("installing Python packages: " + ", ".join(r.name for r in reqs))
        subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
        for r in reqs:
            subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(r)], check=True)
        old = stamp.read_text().splitlines() if stamp.exists() else []
        stamp.write_text("\n".join(sorted(set(old) | {key})))
    if want_tests and not (VENV / ".gx-browser").exists():
        say("installing Playwright's Chromium (one-time, ~150 MB)")
        subprocess.run([str(py), "-m", "playwright", "install", "chromium"], check=True)
        (VENV / ".gx-browser").write_text("ok")
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), "--_in-venv", *argv])


# ---------------------------------------------------------------- stage 2: inside the venv
def neo4j_ready(uri, password, timeout, user="neo4j", database=None):
    from neo4j import GraphDatabase
    from neo4j.exceptions import AuthError, ConfigurationError
    from neo4j_env import redact_uri
    shown = redact_uri(uri)
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        try:
            with GraphDatabase.driver(uri, auth=(user, password)) as d:
                d.verify_connectivity()
                with d.session(database=database) as s:
                    s.run("RETURN 1").consume()
            return True
        except (AuthError, ConfigurationError, ValueError) as e:
            die(f"cannot use Neo4j at {shown} as user {user}: {e}")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    die(f"Neo4j at {shown} did not become ready in {timeout}s: {last}")


def check_docker():
    if not docker_bin():
        die("Docker is not installed. Install Docker Desktop (macOS: https://docs.docker.com/desktop/setup/install/mac-install/ "
            "or `brew install --cask docker`, then start it) or Docker Engine on Linux.\n"
            "       Or use your own local Neo4j 5: set NEO4J_URI and NEO4J_PASSWORD, then python3 run_local.py")
    r = docker("info", "--format", "{{.ServerVersion}}", check=False)
    if r.returncode != 0:
        die("Docker is installed but the daemon is not running: start Docker Desktop (macOS: `open -a Docker`, wait for the whale "
            "icon to settle), or `sudo systemctl start docker` on Linux.\n"
            f"       docker said: {(r.stderr or r.stdout).strip()[:300]}")


def docker_daemon():
    """True when the docker CLI can talk to a running daemon."""
    if not docker_bin():
        return False
    return docker("info", "--format", "{{.ServerVersion}}", check=False).returncode == 0


def inspect_container(name):
    r = docker("inspect", "-f", "{{.State.Status}}|{{range $p, $b := .HostConfig.PortBindings}}{{$p}}={{(index $b 0).HostPort}} {{end}}", name, check=False)
    if r.returncode != 0:
        return None, {}
    status, _, ports = r.stdout.strip().partition("|")
    parsed = {}
    for item in ports.split():
        if "=" in item:
            key, value = item.split("=", 1)
            parsed[key] = value
    return status, parsed


def container_state():
    return inspect_container(CONTAINER)


def mem_mb(text):
    """Parse a Neo4j size like 512M or 4G into megabytes. Unknown text is 0."""
    if not text:
        return 0
    s = str(text).strip().upper()
    mult = 1.0
    if s.endswith("G"):
        mult, s = 1024.0, s[:-1]
    elif s.endswith("M"):
        s = s[:-1]
    elif s.endswith("K"):
        mult, s = 1 / 1024, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0


def container_mem():
    r = docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", CONTAINER, check=False)
    env = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env.get("NEO4J_server_memory_heap_max__size"), env.get("NEO4J_server_memory_pagecache_size")


def start_neo4j(args):
    check_docker()
    status, ports = container_state()
    if args.live_scale and status:
        heap, page = container_mem()
        if mem_mb(page) < 4096 or mem_mb(heap) < 2048:
            die(f"container {CONTAINER} was created with heap {heap or 'default'} and page cache {page or 'default'}. "
                "A 15M node / 60M edge graph needs a new container (heap 4G, page cache 8G). "
                "That wants about 16 GB of RAM for Docker and about 30 GB of free disk. "
                f"Run `python3 run_local.py --reset` first. That deletes the local database volume.")
    if status and ports.get("7687/tcp") and int(ports["7687/tcp"]) != args.neo4j_port and args.neo4j_port_set:
        die(f"container {CONTAINER} already exists on bolt port {ports['7687/tcp']}; run --reset first to change the port "
            f"(or drop --neo4j-port).")
    if status:
        args.neo4j_port = int(ports.get("7687/tcp", args.neo4j_port))
        args.neo4j_http_port = int(ports.get("7474/tcp", args.neo4j_http_port))
        if status != "running":
            say(f"starting existing container {CONTAINER}")
            docker("start", CONTAINER)
        else:
            say(f"reusing running container {CONTAINER} (bolt 127.0.0.1:{args.neo4j_port})")
    else:
        for p, what in ((args.neo4j_port, "bolt"), (args.neo4j_http_port, "Neo4j Browser")):
            if port_in_use(p):
                die(f"port {p} ({what}) is already in use on 127.0.0.1 (another Neo4j?). "
                    f"Pick free ports: --neo4j-port 17687 --neo4j-http-port 17474")
        say(f"starting Neo4j ({IMAGE}) in Docker as {CONTAINER}, volume {VOLUME}, 127.0.0.1:{args.neo4j_port}")
        docker("run", "-d", "--name", CONTAINER,
               "-p", f"127.0.0.1:{args.neo4j_port}:7687", "-p", f"127.0.0.1:{args.neo4j_http_port}:7474",
               "-v", f"{VOLUME}:/data",
               "-e", f"NEO4J_AUTH=neo4j/{PASSWORD}",
               "-e", f"NEO4J_server_memory_heap_initial__size={args.neo4j_heap_initial}",
               "-e", f"NEO4J_server_memory_heap_max__size={args.neo4j_heap}",
               "-e", f"NEO4J_server_memory_pagecache_size={args.neo4j_pagecache}",
               IMAGE, capture=True)
    uri = f"bolt://127.0.0.1:{args.neo4j_port}"
    say(f"waiting for Neo4j at {uri} ...")
    neo4j_ready(uri, PASSWORD, args.wait)
    return uri, PASSWORD


def graphql_endpoint(port=GQL_PORT):
    return f"http://127.0.0.1:{port}/graphql"


def graphql_auth_url(port=GQL_PORT):
    return f"http://127.0.0.1:{port}/oauth/token"


def _graphql_container_ok(port):
    status, ports = inspect_container(GQL_CONTAINER)
    if not status:
        return False
    mounted = docker("inspect", "-f", "{{range .Mounts}}{{.Destination}} {{end}}", GQL_CONTAINER, check=False)
    host_port = ports.get("8791/tcp")
    return host_port == str(port) and "/fixture.py" in (mounted.stdout or "")


def start_graphql_fixture(port=GQL_PORT):
    """Start or reuse gx-graphql and return its GraphQL URL.

    The container is stateless. A container created with a different port or
    without the fixture script is replaced. Neo4j is left alone.
    """
    if not docker_daemon():
        raise RuntimeError("Docker is not running, so the GraphQL fixture container cannot start.")
    script = HERE / "tools" / "graphql_fixture.py"
    if not script.is_file():
        raise RuntimeError(f"missing GraphQL fixture script: {script}")
    status, _ports = inspect_container(GQL_CONTAINER)
    if port_in_use(port) and status is None:
        raise RuntimeError(
            f"port {port} is already in use on 127.0.0.1, and it is not {GQL_CONTAINER}.")
    if not _graphql_container_ok(port):
        docker("rm", "-f", GQL_CONTAINER, check=False)
        if port_in_use(port):
            raise RuntimeError(f"port {port} is already in use on 127.0.0.1.")
        say(f"starting GraphQL fixture ({GQL_IMAGE}) as {GQL_CONTAINER} on 127.0.0.1:{port}")
        started = docker(
            "run", "-d", "--name", GQL_CONTAINER,
            "-p", f"127.0.0.1:{port}:8791",
            "-v", f"{script}:/fixture.py:ro",
            GQL_IMAGE, "python", "/fixture.py", "--host", "0.0.0.0", "--port", "8791",
            check=False)
        if started.returncode != 0:
            detail = (started.stderr or started.stdout or "").strip()[:400]
            raise RuntimeError(f"could not start {GQL_CONTAINER}: {detail}")
    else:
        say(f"reusing GraphQL fixture {GQL_CONTAINER} on 127.0.0.1:{port}")
        restarted = docker("restart", GQL_CONTAINER, check=False)
        if restarted.returncode != 0:
            detail = (restarted.stderr or restarted.stdout or "").strip()[:400]
            raise RuntimeError(f"could not restart {GQL_CONTAINER}: {detail}")
    deadline = time.time() + 90
    health = f"http://127.0.0.1:{port}/health"
    last = "no response"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health, timeout=2) as response:
                if response.status == 200:
                    return graphql_endpoint(port)
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.4)
    logs = docker("logs", "--tail", "30", GQL_CONTAINER, check=False)
    detail = (logs.stderr or logs.stdout or str(last)).strip()[:400]
    raise RuntimeError(f"GraphQL fixture on port {port} did not become ready: {detail}")


def graphql_fixture_env(env, endpoint):
    """Point a server that has no GraphQL endpoint at the local fixture."""
    if (env.get("GRAPHQL_ENDPOINT") or "").strip():
        return env
    env["GRAPHQL_ENDPOINT"] = endpoint
    env["GRAPHQL_AUTH_URL"] = graphql_auth_url()
    env["GRAPHQL_CLIENT_ID"] = GQL_CLIENT_ID
    env["GRAPHQL_CLIENT_SECRET"] = GQL_CLIENT_SECRET
    env["GRAPHQL_ADAPTER"] = "wiz"
    env["GRAPHQL_LABEL"] = "Wiz fixture"
    env["GRAPHQL_ID"] = "wiz"
    env.pop("GRAPHQL_TOKEN", None)
    return env


def seed(env, *flags):
    py = str(venv_python())
    say("seed_cartography.py " + " ".join(flags))
    r = subprocess.run([py, str(HERE / "seed_cartography.py"), *flags], env=env)
    if r.returncode != 0:
        die("seeding failed (see above)")


def load_data(args, env):
    if args.keep_data:
        return say("keeping the existing data (--keep-data)")
    if args.live_scale:
        say(f"loading the live-scale graph: {args.nodes:,} nodes / {args.edges:,} edges. "
            "This streams in batches and takes a long time at the full size (disk for the store, and Docker RAM for the page cache).")
        seed(env, "--wipe", "--live-scale", "--nodes", str(args.nodes), "--edges", str(args.edges))
    elif args.synthetic is not None:
        scale = max(1, math.ceil(args.synthetic / SYNTH_UNIT))
        seed(env, "--wipe", "--scale", str(scale), *(["--hub", str(args.hub)] if args.hub else []))
    elif args.both:
        seed(env, "--wipe", "--fixtures")
        seed(env, *(["--hub", str(args.hub)] if args.hub else []))
    else:
        seed(env, "--wipe", "--fixtures")


def stop_server():
    pidf = RUN / "server.pid"
    if pidf.exists():
        try:
            pid = int(pidf.read_text())
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):  # wait until it has exited and released the port
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            say("stopped the explorer server")
        except (ProcessLookupError, ValueError):
            pass
        pidf.unlink(missing_ok=True)


def start_server(args, env, detach):
    if port_in_use(args.port):
        die(f"port {args.port} is already in use (an explorer already running? `python3 run_local.py --stop`), or use --port 8755")
    RUN.mkdir(exist_ok=True)
    env = {**env, "PORT": str(args.port), "HOST": "127.0.0.1"}
    # Omit these and the server sizes the ceiling from the machine. The first launch
    # writes the working set to gx.local.json. The page can change it for the next launch.
    if args.max_nodes is not None:
        env["MAX_NODES"] = str(args.max_nodes)
    if args.max_edges is not None:
        env["MAX_EDGES"] = str(args.max_edges)
    log = open(RUN / "server.log", "a")
    p = subprocess.Popen([str(venv_python()), str(HERE / "server.py")], env=env, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=detach)
    (RUN / "server.pid").write_text(str(p.pid))
    url = f"http://127.0.0.1:{args.port}/"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "api/status", timeout=2)
            break
        except Exception:  # noqa: BLE001
            if p.poll() is not None:
                die(f"server.py exited; see {RUN / 'server.log'}")
            time.sleep(0.5)
    return p, url


def run_tests(args, env, url):
    py = str(venv_python())
    say("budget tests (tests/test_budget.py)")
    b = subprocess.run([py, str(HERE / "tests" / "test_budget.py")], env=env)
    say("GraphQL tests (tests/test_graphql.py): mocked API, then the gx-graphql fixture")
    g = subprocess.run([py, str(HERE / "tests" / "test_graphql.py")], env=env)
    if args.live_scale:
        say("live-scale tests (tests/test_live_scale.py): the database must hold at least 15M nodes and 60M edges")
        t = subprocess.run([py, str(HERE / "tests" / "test_live_scale.py")],
                           env={**env, "REQUIRE_LIVE_SCALE": "1", "GX_URL": url.rstrip("/")})
        return b.returncode or g.returncode or t.returncode
    say("UI test suite (tests/run_ui_tests.py): starts its own server on a free port and drives headless Chromium")
    t = subprocess.run([py, str(HERE / "tests" / "run_ui_tests.py")], env=env)
    say(f"browser benchmark (tests/bench_ui.py --sizes {args.bench_sizes})")
    subprocess.run([py, str(HERE / "tests" / "bench_ui.py"), "--url", url, "--sizes", args.bench_sizes, "--gl", "none",
                    "--out", str(RUN / "bench-ui.json"), "--label", "local"], env=env)
    say("API benchmark against this database (tests/bench_server.py)")
    subprocess.run([py, str(HERE / "tests" / "bench_server.py"), "--url", url.rstrip("/"), "--repeat", "3",
                    "--out", str(RUN / "bench-server.json")], env=env)
    say(f"results: tests/results.json, {RUN / 'bench-ui.json'}, {RUN / 'bench-server.json'}, tests/screenshots/app-*.png")
    return b.returncode or g.returncode or t.returncode


def credential_target(args, environ=None):
    """Apply CLI credential flags over the environment. Empty means start the local Docker Neo4j."""
    environ = os.environ if environ is None else environ
    if args.neo4j_uri:
        environ["NEO4J_URI"] = args.neo4j_uri
    if args.neo4j_user is not None:
        environ["NEO4J_USER"] = args.neo4j_user
    if args.neo4j_database is not None:
        environ["NEO4J_DATABASE"] = args.neo4j_database
    if args.neo4j_password is not None:
        environ["NEO4J_PASSWORD"] = args.neo4j_password
    return (environ.get("NEO4J_URI") or "").strip()


def _settings_or_prompt():
    """Read Neo4j settings. On a terminal, ask for a missing password once."""
    from neo4j_env import Neo4jConfigError, neo4j_settings, redact_uri
    try:
        return neo4j_settings()
    except Neo4jConfigError as e:
        if not e.missing_password or not sys.stdin.isatty():
            raise
        import getpass
        os.environ.pop("NEO4J_PASSWORD", None)
        user = (os.environ.get("NEO4J_USER") or "neo4j").strip() or "neo4j"
        host = redact_uri((os.environ.get("NEO4J_URI") or "").strip())
        os.environ["NEO4J_PASSWORD"] = getpass.getpass(
            f"Neo4j password for {user} at {host} (Enter if there is none): ")
        return neo4j_settings()


def main_in_venv(args):
    if args.stop or args.reset or args.status:
        stop_server() if (args.stop or args.reset) else None
        if docker_bin():
            if args.status:
                status, ports = container_state()
                print(f"container {CONTAINER}: {status or 'not created'} {ports or ''}")
                gql_status, gql_ports = inspect_container(GQL_CONTAINER)
                print(f"container {GQL_CONTAINER}: {gql_status or 'not created'} {gql_ports or ''}")
                print(f"explorer on 127.0.0.1:{args.port}: {'up' if port_in_use(args.port) else 'down'}")
            elif args.reset:
                docker("rm", "-f", CONTAINER, check=False)
                docker("volume", "rm", VOLUME, check=False)
                docker("rm", "-f", GQL_CONTAINER, check=False)
                say(f"removed container {CONTAINER}, volume {VOLUME}, and container {GQL_CONTAINER}")
            else:
                docker("stop", CONTAINER, check=False)
                docker("stop", GQL_CONTAINER, check=False)
                say(f"stopped containers {CONTAINER} and {GQL_CONTAINER} (Neo4j data kept in volume {VOLUME})")
        return 0

    from graphql_source import load_graphql_env
    from neo4j_env import Neo4jConfigError, load_credential_env
    load_credential_env()
    load_graphql_env()
    using_own = bool(credential_target(args))
    if using_own:
        try:
            settings = _settings_or_prompt()
        except Neo4jConfigError as e:
            die(str(e))
        uri, user, password, database = settings.uri, settings.user, settings.password, settings.database
        say(f"using your Neo4j at {uri} as {user}, database {database} (no Docker)")
        neo4j_ready(uri, password, args.wait, user, database)
        if not args.load_data:
            if args.test and not args.live_scale:
                die("--test needs the mock data; with your own Neo4j add --load-data (local databases only, it wipes them)")
            args.keep_data = True
            if args.live_scale and args.test:
                say("leaving your data untouched and checking it against the 15M node / 60M edge minimum")
            else:
                say("leaving your data untouched (add --load-data to wipe a LOCAL database and load the mock data)")
    else:
        uri, password = start_neo4j(args)
        user, database = "neo4j", args.neo4j_database or "neo4j"
    env = {**os.environ, "NEO4J_URI": uri, "NEO4J_USER": user, "NEO4J_PASSWORD": password,
           "NEO4J_DATABASE": database, "QUIET": "1"}
    if not using_own or args.test:
        if docker_daemon():
            try:
                fixture = start_graphql_fixture()
            except RuntimeError as exc:
                die(str(exc))
            if graphql_fixture_env(env, fixture)["GRAPHQL_ENDPOINT"] == fixture:
                say(f"GraphQL fixture: {fixture}  (client {GQL_CLIENT_ID})")
            else:
                say(f"GraphQL fixture is up for tests at {fixture}")
        elif args.test:
            say("Docker is not running; the GraphQL fixture section will skip")
    load_data(args, env)

    stop_server()
    serve_after = not args.no_serve
    proc, url = start_server(args, env, detach=args.detach or not serve_after)
    if using_own:
        say(f"Graph Explorer is up: {url}")
    else:
        say(f"Graph Explorer is up: {url}   (Neo4j Browser: http://127.0.0.1:{args.neo4j_http_port}, user neo4j)")
    rc = 0
    if args.test:
        rc = run_tests(args, env, url)
    if not serve_after:
        stop_server()
        return rc
    if not args.no_browser:
        webbrowser.open(url)
    if args.detach:
        say(f"server runs in the background (log: {RUN / 'server.log'}); stop it with: python3 run_local.py --stop")
        return rc
    say("Ctrl+C to stop the explorer (Neo4j keeps running; `python3 run_local.py --stop` stops it too)")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        (RUN / "server.pid").unlink(missing_ok=True)
        print()
    return rc


def parse(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    d = ap.add_argument_group("data (the database is wiped first)")
    d.add_argument("--synthetic", type=int, metavar="N", help="synthetic Cartography-shaped graph of about N nodes instead of the fixtures")
    d.add_argument("--both", action="store_true", help="fixtures + one synthetic estate (1,099 nodes)")
    d.add_argument("--live-scale", action="store_true",
                   help="load the minimum supported graph: 15,000,000 nodes and 60,000,000 edges, and size Neo4j for it")
    d.add_argument("--nodes", type=int, default=15_000_000, help="with --live-scale, exact node count (default 15000000)")
    d.add_argument("--edges", type=int, default=60_000_000, help="with --live-scale, exact relationship count (default 60000000)")
    d.add_argument("--hub", type=int, default=0, metavar="N", help="add a supernode with N EC2 instances (with --synthetic/--both)")
    d.add_argument("--keep-data", action="store_true", help="don't reload data")
    n = ap.add_argument_group("Neo4j")
    n.add_argument("--neo4j-port", type=int, default=7687, help="host bolt port for the Docker container (default 7687)")
    n.add_argument("--neo4j-http-port", type=int, default=7474, help="host port for Neo4j Browser (default 7474)")
    n.add_argument("--neo4j-heap", default=None, help="Neo4j max heap in the container (default 2G, or 4G with --live-scale)")
    n.add_argument("--neo4j-pagecache", default=None, help="Neo4j page cache in the container (default 1G, or 8G with --live-scale)")
    n.add_argument("--neo4j-uri", help="use your own Neo4j instead of Docker. NEO4J_URI in the environment or in .env does the same. Example: neo4j+s://host:7687 or bolt://localhost:7687. Data is left untouched")
    n.add_argument("--neo4j-user", default=None, help="Neo4j user (default: NEO4J_USER, otherwise neo4j)")
    n.add_argument("--neo4j-password", default=None, help="password. An empty value is sent as an empty password. Prefer NEO4J_PASSWORD or .env so it stays out of shell history. Prompted when it is unset; Enter sends an empty password")
    n.add_argument("--neo4j-database", default=None, help="database name (default: NEO4J_DATABASE, otherwise neo4j)")
    n.add_argument("--load-data", action="store_true", help="with --neo4j-uri: wipe it and load the mock data (refused for non-local servers)")
    n.add_argument("--wait", type=int, default=180, help="seconds to wait for Neo4j (default 180)")
    s = ap.add_argument_group("explorer")
    s.add_argument("--port", type=int, default=8744, help="explorer port on 127.0.0.1 (default 8744)")
    s.add_argument("--max-nodes", type=int, default=None, help="safety ceiling for nodes on screen (default: from this machine's RAM)")
    s.add_argument("--max-edges", type=int, default=None, help="safety ceiling for edges on screen (default: from this machine's RAM)")
    s.add_argument("--no-browser", action="store_true", help="don't open the browser")
    s.add_argument("--detach", action="store_true", help="run the server in the background and return")
    s.add_argument("--no-serve", action="store_true", help="stop the server when done (useful with --test in CI)")
    t = ap.add_argument_group("tests")
    t.add_argument("--with-tests", action="store_true", help="also install Playwright + Chromium")
    t.add_argument("--test", action="store_true", help="run the budget tests, the GraphQL tests, and the UI suite, or with --live-scale the 15M/60M checks")
    t.add_argument("--bench-sizes", default="10000", help="graph sizes for the browser benchmark (default 10000)")
    c = ap.add_argument_group("commands")
    c.add_argument("--stop", action="store_true", help="stop the server, the Neo4j container, and the GraphQL fixture")
    c.add_argument("--reset", action="store_true", help="remove the Neo4j container and its volume")
    c.add_argument("--status", action="store_true", help="show what is running")
    ap.add_argument("--_in-venv", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    joined = " ".join(argv)
    args.neo4j_port_set = "--neo4j-port" in joined
    if args.live_scale and (args.synthetic is not None or args.both):
        ap.error("use either --live-scale or --synthetic/--both")
    if args.synthetic is not None and args.both:
        ap.error("use either --synthetic N or --both")
    if (args.max_nodes is not None and args.max_nodes < 1) or (args.max_edges is not None and args.max_edges < 1):
        ap.error("--max-nodes and --max-edges must be at least 1")
    if args.nodes < 4 or args.edges < 1:
        ap.error("--nodes must be at least 4 and --edges at least 1")
    # Defaults differ for the 15M/60M graph: heap 4G and page cache 8G, set only when the flag is omitted.
    if args.neo4j_heap is None:
        args.neo4j_heap = "4G" if args.live_scale else "2G"
    if args.neo4j_pagecache is None:
        args.neo4j_pagecache = "8G" if args.live_scale else "1G"
    args.neo4j_heap_initial = "2G" if args.live_scale else "1G"
    return args


if __name__ == "__main__":
    argv = sys.argv[1:]
    a = parse(argv)
    if a._in_venv:
        sys.exit(main_in_venv(a) or 0)
    if a.stop or a.reset or a.status:  # no venv needed
        sys.exit(main_in_venv(a) or 0)
    bootstrap(a, argv)
