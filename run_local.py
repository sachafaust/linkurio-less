#!/usr/bin/env python3
"""One command to run the Graph Explorer on your machine with the same mock data used in development.

    python3 run_local.py                # venv + Neo4j in Docker + Cartography fixture data + server + browser

What it does, in order:
  1. checks Python 3.10+ and Docker (unless you pass --neo4j-uri to use your own local Neo4j)
  2. creates .venv/ in this folder and installs requirements.txt (+ requirements-tests.txt with --with-tests/--test)
  3. starts Neo4j 5.26 community in Docker: container "gx-neo4j", volume "gx-neo4j-data", ports bound to
     127.0.0.1 only, test password "tinkerlab-test"; reuses the container if it already exists
  4. loads data (wipes the database first):
       default        the Cartography fixture snapshot (data/cartography-fixtures.json.gz, 6,939 nodes / 11,054 rels,
                      real Cartography schema, produced from Cartography's own demo seeds + test fixtures)
       --synthetic N  the synthetic Cartography-shaped generator, about N nodes (what the UI tests are written for)
       --both         fixtures + synthetic estate in one database
       --keep-data    don't touch the data
  5. starts server.py on http://127.0.0.1:8744 and opens the browser (Ctrl+C stops the server; Neo4j keeps running)

Other commands:
  --test        run the UI test suite (33 checks) and a quick browser + API benchmark, then keep serving
                (the tests need the synthetic estate, so --test loads --both unless you chose a dataset)
  --stop        stop the server and the Neo4j container (data kept)
  --reset       remove the container and its volume (all data)
  --status      show what is running

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


def docker(*args, check=True, capture=True):
    r = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and r.returncode != 0:
        die(f"docker {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}")
    return r


# ---------------------------------------------------------------- stage 1: bootstrap (system Python)
def bootstrap(args, argv):
    if sys.version_info < (3, 10):
        die(f"Python 3.10+ is needed (this is {sys.version.split()[0]}). On macOS: `brew install python@3.12`.")
    want_tests = args.with_tests or args.test
    py = venv_python()
    if not py.exists():
        say(f"creating virtualenv {VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
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
def neo4j_ready(uri, password, timeout):
    from neo4j import GraphDatabase
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        try:
            with GraphDatabase.driver(uri, auth=("neo4j", password)) as d:
                d.verify_connectivity()
                with d.session() as s:
                    s.run("RETURN 1").consume()
            return True
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    die(f"Neo4j at {uri} did not become ready in {timeout}s: {last}")


def check_docker():
    if not shutil.which("docker"):
        die("Docker is not installed. Install Docker Desktop (macOS: https://docs.docker.com/desktop/setup/install/mac-install/ "
            "or `brew install --cask docker`, then start it) or Docker Engine on Linux.\n"
            "       Or use your own local Neo4j 5: python3 run_local.py --neo4j-uri bolt://localhost:7687 --neo4j-password ...")
    r = docker("info", "--format", "{{.ServerVersion}}", check=False)
    if r.returncode != 0:
        die("Docker is installed but the daemon is not running (start Docker Desktop, or `sudo systemctl start docker`).\n"
            f"       docker said: {(r.stderr or r.stdout).strip()[:300]}")


def container_state():
    r = docker("inspect", "-f", "{{.State.Status}}|{{range $p, $b := .HostConfig.PortBindings}}{{$p}}={{(index $b 0).HostPort}} {{end}}", CONTAINER, check=False)
    if r.returncode != 0:
        return None, {}
    status, _, ports = r.stdout.strip().partition("|")
    return status, dict(p.split("=") for p in ports.split())


def start_neo4j(args):
    check_docker()
    status, ports = container_state()
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
               "-e", "NEO4J_server_memory_heap_initial__size=1G", "-e", f"NEO4J_server_memory_heap_max__size={args.neo4j_heap}",
               "-e", "NEO4J_server_memory_pagecache_size=1G",
               IMAGE, capture=True)
    uri = f"bolt://127.0.0.1:{args.neo4j_port}"
    say(f"waiting for Neo4j at {uri} ...")
    neo4j_ready(uri, PASSWORD, args.wait)
    return uri, PASSWORD


def seed(env, *flags):
    py = str(venv_python())
    say("seed_cartography.py " + " ".join(flags))
    r = subprocess.run([py, str(HERE / "seed_cartography.py"), *flags], env=env)
    if r.returncode != 0:
        die("seeding failed (see above)")


def load_data(args, env):
    if args.keep_data:
        return say("keeping the existing data (--keep-data)")
    if args.synthetic is not None:
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
    say("UI test suite (tests/run_ui_tests.py): starts its own server on a free port and drives headless Chromium")
    t = subprocess.run([py, str(HERE / "tests" / "run_ui_tests.py")], env=env)
    say(f"browser benchmark (tests/bench_ui.py --sizes {args.bench_sizes})")
    subprocess.run([py, str(HERE / "tests" / "bench_ui.py"), "--url", url, "--sizes", args.bench_sizes, "--gl", "none",
                    "--out", str(RUN / "bench-ui.json"), "--label", "local"], env=env)
    say("API benchmark against this database (tests/bench_server.py)")
    subprocess.run([py, str(HERE / "tests" / "bench_server.py"), "--url", url.rstrip("/"), "--repeat", "3",
                    "--out", str(RUN / "bench-server.json")], env=env)
    say(f"results: tests/results.json, {RUN / 'bench-ui.json'}, {RUN / 'bench-server.json'}, screenshots/app-*.png")
    return t.returncode


def main_in_venv(args):
    if args.stop or args.reset or args.status:
        stop_server() if (args.stop or args.reset) else None
        if shutil.which("docker"):
            if args.status:
                status, ports = container_state()
                print(f"container {CONTAINER}: {status or 'not created'} {ports or ''}")
                print(f"explorer on 127.0.0.1:{args.port}: {'up' if port_in_use(args.port) else 'down'}")
            elif args.reset:
                docker("rm", "-f", CONTAINER, check=False)
                docker("volume", "rm", VOLUME, check=False)
                say(f"removed container {CONTAINER} and volume {VOLUME}")
            else:
                docker("stop", CONTAINER, check=False)
                say(f"stopped container {CONTAINER} (data kept in volume {VOLUME})")
        return 0

    if args.neo4j_uri:
        uri, password = args.neo4j_uri, args.neo4j_password or os.environ.get("NEO4J_PASSWORD", "")
        say(f"using your Neo4j at {uri} (no Docker)")
        neo4j_ready(uri, password, args.wait)
    else:
        uri, password = start_neo4j(args)
    env = {**os.environ, "NEO4J_URI": uri, "NEO4J_USER": args.neo4j_user, "NEO4J_PASSWORD": password, "QUIET": "1"}
    if args.test and args.synthetic is None and not args.keep_data and not args.both:
        say("--test: loading fixtures + the synthetic estate (--both); the UI tests are written for the synthetic data")
        args.both = True
    load_data(args, env)

    stop_server()
    serve_after = not args.no_serve
    proc, url = start_server(args, env, detach=args.detach or not serve_after)
    say(f"Graph Explorer is up: {url}   (Neo4j Browser: http://127.0.0.1:{args.neo4j_http_port}  user neo4j / {password})"
        if not args.neo4j_uri else f"Graph Explorer is up: {url}")
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
    d.add_argument("--hub", type=int, default=0, metavar="N", help="add a supernode with N EC2 instances (with --synthetic/--both)")
    d.add_argument("--keep-data", action="store_true", help="don't reload data")
    n = ap.add_argument_group("Neo4j")
    n.add_argument("--neo4j-port", type=int, default=7687, help="host bolt port for the Docker container (default 7687)")
    n.add_argument("--neo4j-http-port", type=int, default=7474, help="host port for Neo4j Browser (default 7474)")
    n.add_argument("--neo4j-heap", default="2G", help="Neo4j max heap in the container (default 2G)")
    n.add_argument("--neo4j-uri", help="use your own LOCAL Neo4j 5 instead of Docker, e.g. bolt://localhost:7687")
    n.add_argument("--neo4j-user", default="neo4j")
    n.add_argument("--neo4j-password", help="password for --neo4j-uri (or set NEO4J_PASSWORD)")
    n.add_argument("--wait", type=int, default=180, help="seconds to wait for Neo4j (default 180)")
    s = ap.add_argument_group("explorer")
    s.add_argument("--port", type=int, default=8744, help="explorer port on 127.0.0.1 (default 8744)")
    s.add_argument("--no-browser", action="store_true", help="don't open the browser")
    s.add_argument("--detach", action="store_true", help="run the server in the background and return")
    s.add_argument("--no-serve", action="store_true", help="stop the server when done (useful with --test in CI)")
    t = ap.add_argument_group("tests")
    t.add_argument("--with-tests", action="store_true", help="also install Playwright + Chromium")
    t.add_argument("--test", action="store_true", help="run the UI tests and benchmarks (implies --with-tests)")
    t.add_argument("--bench-sizes", default="10000", help="graph sizes for the browser benchmark (default 10000)")
    c = ap.add_argument_group("commands")
    c.add_argument("--stop", action="store_true", help="stop the server and the Neo4j container")
    c.add_argument("--reset", action="store_true", help="remove the Neo4j container and its volume")
    c.add_argument("--status", action="store_true", help="show what is running")
    ap.add_argument("--_in-venv", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.neo4j_port_set = "--neo4j-port" in " ".join(argv)
    if args.synthetic is not None and args.both:
        ap.error("use either --synthetic N or --both")
    return args


if __name__ == "__main__":
    argv = sys.argv[1:]
    a = parse(argv)
    if a._in_venv:
        sys.exit(main_in_venv(a) or 0)
    if a.stop or a.reset or a.status:  # no venv needed
        sys.exit(main_in_venv(a) or 0)
    bootstrap(a, argv)
