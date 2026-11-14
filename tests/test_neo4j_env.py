#!/usr/bin/env python3
"""Neo4j credentials stay in the environment and out of the page, the config file, and git.

The live section starts a read-only server on a free port against the Neo4j that is
already running. It does not wipe anything. If Neo4j is down, that section is skipped.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from argparse import Namespace
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neo4j_env  # noqa: E402
import run_local  # noqa: E402
import server  # noqa: E402
from neo4j_env import (  # noqa: E402
    Neo4jConfigError,
    load_credential_env,
    neo4j_settings,
    parse_dotenv,
    redact_uri,
    update_dotenv,
)

SECRET = "s3cret-token"
WRONG = "wrong-password-zz"
report = []


def check(ok, name, detail=""):
    report.append(bool(ok))
    print(("ok  " if ok else "FAIL") + " " + name + ((" — " + detail) if detail else ""), flush=True)


def missing_env():
    return Path(tempfile.mkdtemp(prefix="gx-no-env-")) / "missing.env"


def test_parse_and_precedence():
    text = """
# comment
export NEO4J_USER=reader
NEO4J_PASSWORD="s3cret with spaces"
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_DATABASE='analytics'
MAX_NODES=1
NOT A LINE
"""
    parsed = dict(parse_dotenv(text))
    check(parsed["NEO4J_PASSWORD"] == "s3cret with spaces" and parsed["NEO4J_DATABASE"] == "analytics"
          and parsed["NEO4J_USER"] == "reader" and "MAX_NODES" in parsed,
          "a .env file is KEY=value, with quotes and export")
    raw = 'NEO4J_PASSWORD="a\\\\b\\"c"\n'
    escaped = dict(parse_dotenv(raw))
    check(escaped.get("NEO4J_PASSWORD") == 'a\\b"c', "double quotes keep a password literal")

    folder = tempfile.TemporaryDirectory()
    path = Path(folder.name) / ".env"
    path.write_text(
        "NEO4J_URI=bolt://from-file:7687\nNEO4J_USER=file-user\n"
        "NEO4J_PASSWORD=from-file\nNEO4J_DATABASE=from-file\nMAX_NODES=1\n",
        encoding="utf-8")
    os.chmod(path, 0o600)
    env = {"NEO4J_PASSWORD": "from-env", "NEO4J_URI": "bolt://from-env:7687"}
    load_credential_env(path, env)
    check(env["NEO4J_PASSWORD"] == "from-env" and env["NEO4J_URI"] == "bolt://from-env:7687"
          and env["NEO4J_USER"] == "file-user" and env["NEO4J_DATABASE"] == "from-file"
          and "MAX_NODES" not in env,
          "the environment wins, and .env fills only the credential keys that are unset")
    folder.cleanup()


def test_refusals():
    absent = missing_env()
    try:
        neo4j_settings(environ={"NEO4J_URI": "bolt://127.0.0.1:7687"}, path=absent)
        check(False, "a missing password is refused")
    except Neo4jConfigError as e:
        text = str(e)
        check(e.missing_password and "NEO4J_PASSWORD" in text and SECRET not in text,
              "a missing password names the variable", text)
    try:
        neo4j_settings(environ={
            "NEO4J_URI": f"bolt://neo4j:{SECRET}@[::1]:7687",
            "NEO4J_PASSWORD": "other",
        }, path=absent)
        check(False, "a URI with a password is refused")
    except Neo4jConfigError as e:
        text = str(e)
        check((not e.missing_password) and SECRET not in text and "NEO4J_PASSWORD" in text,
              "a URI with a password is refused and the message leaves it out")
    shown = redact_uri(f"neo4j+s://reader:{SECRET}@[::1]:7687")
    check(shown == "neo4j+s://[::1]:7687" and SECRET not in shown, "a logged URI drops the userinfo", shown)
    try:
        neo4j_settings(environ={"NEO4J_URI": "localhost:7687", "NEO4J_PASSWORD": "x"}, path=absent)
        check(False, "a URI without a scheme is refused")
    except Neo4jConfigError as e:
        check("bolt://" in str(e) and "x" not in str(e).split("password")[-1], "a URI without a scheme is refused")
    absent.parent.rmdir()


def test_empty_password_is_sent():
    absent = missing_env()
    try:
        settings = neo4j_settings(environ={
            "NEO4J_URI": "bolt://127.0.0.1:7687",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "",
            "NEO4J_DATABASE": "neo4j",
        }, path=absent)
        check(settings.password == "" and settings.user == "neo4j" and settings.database == "neo4j",
              "an empty password is kept")
    finally:
        absent.parent.rmdir()
    folder = tempfile.TemporaryDirectory()
    path = Path(folder.name) / ".env"
    path.write_text("NEO4J_URI=bolt://10.0.0.8:7687\nNEO4J_PASSWORD=\n", encoding="utf-8")
    os.chmod(path, 0o600)
    env = {}
    settings = neo4j_settings(environ=env, path=path)
    check(settings.password == "" and env.get("NEO4J_PASSWORD") == "" and settings.uri == "bolt://10.0.0.8:7687",
          "an empty password in .env is set")
    folder.cleanup()


def test_prompt_keeps_an_empty_password():
    import getpass
    keys = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_DATABASE", "GX_ENV")
    saved = {k: os.environ.get(k) for k in keys}
    folder = tempfile.TemporaryDirectory()
    path = Path(folder.name) / ".env"
    path.write_text("NEO4J_URI=bolt://10.1.2.3:7687\nNEO4J_USER=neo4j\n", encoding="utf-8")
    os.chmod(path, 0o600)
    for key in keys:
        os.environ.pop(key, None)
    os.environ["GX_ENV"] = str(path)
    prompted = []

    def fake_getpass(prompt):
        prompted.append(prompt)
        return ""

    real_isatty = sys.stdin.isatty
    real_getpass = getpass.getpass
    sys.stdin.isatty = lambda: True
    getpass.getpass = fake_getpass
    try:
        settings = run_local._settings_or_prompt()
        check(settings.password == "" and settings.uri == "bolt://10.1.2.3:7687"
              and settings.user == "neo4j" and len(prompted) == 1 and "Enter if there is none" in prompted[0],
              "Enter at the prompt sets an empty password")
    finally:
        getpass.getpass = real_getpass
        sys.stdin.isatty = real_isatty
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        folder.cleanup()


def test_file_mode_warning():
    folder = tempfile.TemporaryDirectory()
    path = Path(folder.name) / ".env"
    path.write_text("NEO4J_PASSWORD=from-file\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    neo4j_env._WARNED_PERMS = False
    buf = io.StringIO()
    with redirect_stderr(buf):
        load_credential_env(path, {})
    warned = buf.getvalue()
    check("chmod 600" in warned and "from-file" not in warned, "a shared .env gets a permission warning")
    os.chmod(path, 0o600)
    neo4j_env._WARNED_PERMS = False
    buf = io.StringIO()
    with redirect_stderr(buf):
        load_credential_env(path, {})
    check(buf.getvalue() == "", "a private .env stays quiet")
    folder.cleanup()


def test_config_file_drops_secrets():
    norm = server._normalize_config({
        "nodes": 12, "edges": 20, "expand_limit": 5, "cypher_limit": 8,
        "NEO4J_PASSWORD": SECRET, "password": SECRET, "uri": f"bolt://neo4j:{SECRET}@db",
    })
    blob = json.dumps(norm)
    check(bool(norm) and norm["nodes"] >= 1 and SECRET not in blob and "password" not in norm and "uri" not in norm,
          "the working-set file drops credentials")


def test_cli_flags_win():
    env = {"NEO4J_URI": "bolt://from-env:7687", "NEO4J_PASSWORD": "from-env"}
    args = Namespace(neo4j_uri=None, neo4j_user=None, neo4j_database="cartography", neo4j_password=None)
    target = run_local.credential_target(args, env)
    check(target == "bolt://from-env:7687" and env["NEO4J_DATABASE"] == "cartography"
          and env["NEO4J_PASSWORD"] == "from-env",
          "NEO4J_URI in the environment selects that server")
    flags = Namespace(neo4j_uri="bolt://from-flag:7687", neo4j_user="reader",
                      neo4j_database=None, neo4j_password="from-flag")
    target = run_local.credential_target(flags, env)
    check(target == "bolt://from-flag:7687" and env["NEO4J_USER"] == "reader"
          and env["NEO4J_PASSWORD"] == "from-flag",
          "a CLI flag wins over the environment")
    empty = {}
    check(run_local.credential_target(Namespace(
        neo4j_uri=None, neo4j_user=None, neo4j_database=None, neo4j_password=None), empty) == "",
          "with no URI the local Docker Neo4j stays the default")


def test_gitignore():
    ignored = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True)
    tmp = subprocess.run(["git", "check-ignore", ".env.tmp"], cwd=ROOT, capture_output=True, text=True)
    example = subprocess.run(["git", "check-ignore", ".env.example"], cwd=ROOT, capture_output=True, text=True)
    check(ignored.returncode == 0 and ignored.stdout.strip() == ".env", ".env is gitignored", ignored.stdout.strip())
    check(tmp.returncode == 0 and tmp.stdout.strip() == ".env.tmp", ".env.tmp is gitignored", tmp.stdout.strip())
    check(example.returncode != 0, ".env.example stays in git")


def test_update_dotenv_keeps_other_lines():
    folder = tempfile.mkdtemp(prefix="gx-dotenv-")
    path = Path(folder) / ".env"
    path.write_text("# keep me\nMAX_NODES=9\nNEO4J_PASSWORD=old\nNEO4J_PASSWORD=older\n", encoding="utf-8")
    try:
        update_dotenv(
            {"NEO4J_PASSWORD": "s3cret with spaces", "NEO4J_URI": "bolt://127.0.0.1:7687"},
            remove=["GONE"], path=path)
        text = path.read_text(encoding="utf-8")
        parsed = dict(parse_dotenv(text))
        check(parsed.get("NEO4J_PASSWORD") == "s3cret with spaces" and text.count("NEO4J_PASSWORD=") == 1
              and "MAX_NODES=9" in text and "# keep me" in text and "NEO4J_URI=bolt://127.0.0.1:7687" in text,
              "updating .env keeps comments and replaces every copy of a key")
        update_dotenv({"NEO4J_PASSWORD": ""}, path=path)
        text = path.read_text(encoding="utf-8")
        check("NEO4J_PASSWORD=\n" in text and dict(parse_dotenv(text)).get("NEO4J_PASSWORD") == "",
              "an empty password is stored as an empty value")
        try:
            update_dotenv({"NEO4J_PASSWORD": "a\nb"}, path=path)
            check(False, "a line break in a setting is refused")
        except Neo4jConfigError:
            check("a\nb" not in path.read_text(encoding="utf-8"), "a line break in a setting is refused")
        update_dotenv({}, remove=["NEO4J_PASSWORD"], path=path)
        text = path.read_text(encoding="utf-8")
        check("NEO4J_PASSWORD" not in text and "MAX_NODES=9" in text, "removing a key leaves the other lines")
        mode = stat.S_IMODE(path.stat().st_mode)
        check(mode & 0o077 == 0, "the env file is owner-read/write", oct(mode))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _free_port():
    port = 0
    for _ in range(20):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        if port not in (8744, 8755):
            return port
    return port


def _child_env(folder, dotenv, port):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GRAPHQL_") or key.startswith("NEO4J_") or key in ("GX_ENV", "GX_CONFIG"):
            env.pop(key, None)
    env.update({
        "GX_ENV": str(dotenv),
        "GX_CONFIG": str(Path(folder) / "gx.local.json"),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "QUIET": "1",
    })
    return env


def _start(env):
    return subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_server_refuses_a_bad_file():
    folder = tempfile.mkdtemp(prefix="gx-cred-")
    dotenv = Path(folder) / ".env"
    dotenv.write_text(f"NEO4J_URI=bolt://neo4j:{SECRET}@127.0.0.1:7687\nNEO4J_PASSWORD={SECRET}\n", encoding="utf-8")
    os.chmod(dotenv, 0o600)
    port = _free_port()
    proc = _start(_child_env(folder, dotenv, port))
    try:
        try:
            out = proc.communicate(timeout=15)[0]
        except subprocess.TimeoutExpired:
            _stop(proc)
            out = ""
        check(proc.returncode not in (0, None) and SECRET not in out and "NEO4J_USER" in out,
              "a password inside the URI stops the server and stays out of the log")
        check(not (Path(folder) / "gx.local.json").exists(), "a refused start does not write the working set")
    finally:
        _stop(proc)
        shutil.rmtree(folder, ignore_errors=True)


def _neo4j_password():
    passwords = []
    if os.environ.get("NEO4J_PASSWORD"):
        passwords.append(os.environ["NEO4J_PASSWORD"])
    if run_local.PASSWORD not in passwords:
        passwords.append(run_local.PASSWORD)
    uri = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    from neo4j import GraphDatabase
    for pw in passwords:
        try:
            with GraphDatabase.driver(uri, auth=(user, pw)) as d:
                d.verify_connectivity()
            return uri, user, pw
        except Exception:
            continue
    return None


def _wait_http(port, proc):
    """Return the status body once the server answers, including an auth error."""
    url = f"http://127.0.0.1:{port}/api/status"
    for _ in range(40):
        if proc.poll() is not None:
            return None
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            return e.read().decode()
        except Exception:
            time.sleep(0.25)
    return None


def _wait_status(port, proc):
    raw = _wait_http(port, proc)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _request(port, path, body=None, timeout=40):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _chrome_executable():
    path = Path("/Users/shey/Library/Caches/ms-playwright/chromium-1254/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    return str(path) if path.is_file() else None


def test_connection_from_the_page():
    """A plain start stays up. Data connection writes secrets only into .env."""
    folder = tempfile.mkdtemp(prefix="gx-conn-")
    dotenv = Path(folder) / ".env"
    port = _free_port()
    proc = _start(_child_env(folder, dotenv, port))
    log = ""
    try:
        status = _wait_status(port, proc)
        body = json.dumps(status or {})
        check(proc.poll() is None and bool(status) and status.get("connected") is False and status.get("nodes") is None
              and SECRET not in body and WRONG not in body,
              "a plain start stays up and reports Neo4j as not connected",
              "" if status else "server did not answer")
        code, raw = _request(port, "/api/connection")
        check(code == 200 and '"password":' not in raw and '"token":' not in raw
              and SECRET not in raw and WRONG not in raw and "password_set" in raw,
              "the connection form omits the password and the token")
        code, raw = _request(port, "/api/connection", {
            "neo4j": {"uri": f"bolt://neo4j:{SECRET}@127.0.0.1:7687", "user": "neo4j", "password": WRONG},
        })
        leaked = dotenv.is_file() and SECRET in dotenv.read_text(encoding="utf-8")
        check(code == 400 and SECRET not in raw and not leaked,
              "a password inside the URI is refused and is not saved")
        code, raw = _request(port, "/api/connection", {
            "neo4j": {"uri": "bolt://127.0.0.1:7687", "user": "neo4j", "password": WRONG},
            "graphql": {"endpoint": f"https://user:{SECRET}@api.us2.app.wiz.io/graphql"},
        })
        leaked = dotenv.is_file() and SECRET in dotenv.read_text(encoding="utf-8")
        check(code == 400 and SECRET not in raw and not leaked,
              "a token in the GraphQL endpoint is refused and is not saved")
        code, raw = _request(port, "/api/connection", {
            "neo4j": {
                "uri": "bolt://127.0.0.1:7687", "user": "neo4j", "database": "neo4j",
                "password": WRONG,
            },
            "graphql": {
                "endpoint": "https://api.us2.app.wiz.io/graphql",
                "adapter": "wiz", "label": "Wiz", "token": SECRET, "project_id": "*",
            },
        })
        saved = dotenv.read_text(encoding="utf-8") if dotenv.is_file() else ""
        cfg_path = Path(folder) / "gx.local.json"
        cfg = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
        check(code == 200 and SECRET not in raw and WRONG not in raw,
              "saving the connection answers without the secrets")
        check(WRONG in saved and SECRET in saved and "NEO4J_PASSWORD=" in saved
              and "GRAPHQL_ENDPOINT=https://api.us2.app.wiz.io/graphql" in saved,
              "the gitignored env file stores the password and the token")
        check(WRONG not in cfg and SECRET not in cfg and "password" not in cfg.lower(),
              "the working set has no password or token")
        mode = stat.S_IMODE(dotenv.stat().st_mode) if dotenv.is_file() else 0
        check(dotenv.is_file() and mode & 0o077 == 0, "the saved env file is owner-read/write", oct(mode))
        code, kept_raw = _request(port, "/api/connection", {
            "neo4j": {"uri": "bolt://127.0.0.1:7687", "user": "neo4j", "database": "neo4j"},
            "graphql": {"endpoint": "https://api.us2.app.wiz.io/graphql", "adapter": "wiz", "label": "Wiz"},
        })
        kept = dotenv.read_text(encoding="utf-8") if dotenv.is_file() else ""
        check(code == 200 and WRONG in kept and SECRET in kept and SECRET not in kept_raw and WRONG not in kept_raw,
              "a blank password and token keep the saved values")
        _browser_saves_connection(port)
        code, cleared_raw = _request(port, "/api/connection", {
            "neo4j": {"uri": "bolt://127.0.0.1:7687", "user": "neo4j", "database": "neo4j"},
            "graphql": {"clear": True},
        })
        cleared = dotenv.read_text(encoding="utf-8") if dotenv.is_file() else ""
        check(code == 200 and "GRAPHQL_ENDPOINT" not in cleared and SECRET not in cleared
              and f"NEO4J_PASSWORD={WRONG}" in cleared and SECRET not in cleared_raw,
              "clearing GraphQL leaves the Neo4j password in .env")
        view_code, view = _request(port, "/api/connection")
        status_code, status_raw = _request(port, "/api/status")
        check(view_code == 200 and status_code == 200 and SECRET not in view and WRONG not in view
              and SECRET not in status_raw and WRONG not in status_raw,
              "status and the connection form still omit both secrets")
    finally:
        _stop(proc)
        log = proc.stdout.read() if proc.stdout else ""
        check(SECRET not in log and WRONG not in log, "the server log leaves out the password and the token")
        shutil.rmtree(folder, ignore_errors=True)


def _browser_saves_connection(port):
    """Drive the Data connection dialog. Skip when Playwright is not installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("skip browser connection form (playwright is not installed)")
        return
    chrome = _chrome_executable()
    try:
        with sync_playwright() as p:
            kwargs = {"headless": False}
            if chrome:
                kwargs["executable_path"] = chrome
            browser = p.chromium.launch(**kwargs)
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_function(
                """() => window.cy && typeof window.cy.nodes === 'function'
                    && document.querySelector('#source-pick')
                    && !document.querySelector('#source-pick').disabled""",
                timeout=40000)
            page.click("[data-act=connection]")
            page.wait_for_selector("[data-csave]")
            empty = page.evaluate(
                """() => ({
                    pw: document.querySelector('[data-cpw]').value,
                    token: document.querySelector('[data-gt]').value,
                    pwType: document.querySelector('[data-cpw]').type,
                    tokenType: document.querySelector('[data-gt]').type,
                })""")
            check(empty["pw"] == "" and empty["token"] == "" and empty["pwType"] == "password" and empty["tokenType"] == "password",
                  "the dialog leaves both secrets blank")
            page.fill("[data-cu]", "bolt://127.0.0.1:7687")
            page.fill("[data-cuser]", "neo4j")
            page.fill("[data-cpw]", WRONG)
            page.fill("[data-ge]", "https://api.us2.app.wiz.io/graphql")
            page.select_option("[data-ga]", "wiz")
            page.fill("[data-gt]", SECRET)
            page.click("[data-csave]")
            page.wait_for_function(
                """() => {
                    const t = document.querySelector('#toast');
                    return t && (t.textContent || '').includes('Connection saved');
                }""",
                timeout=30000)
            leaked = page.evaluate(
                """(secrets) => {
                    const text = document.body.innerText || '';
                    const store = JSON.stringify(localStorage);
                    return {
                        text: secrets.some(s => text.includes(s)),
                        store: secrets.some(s => store.includes(s)),
                    };
                }""",
                [SECRET, WRONG])
            fetched = page.evaluate(
                """async (secrets) => {
                    const conn = await (await fetch('/api/connection')).text();
                    const status = await (await fetch('/api/status')).text();
                    const raw = conn + status;
                    return secrets.some(s => raw.includes(s));
                }""",
                [SECRET, WRONG])
            check(not leaked["text"] and not leaked["store"] and not fetched,
                  "the page does not show the password or the token after save")
            check(not any(SECRET in e or WRONG in e for e in errors),
                  "the page raised no errors that include a secret")
            browser.close()
    except Exception as e:
        text = str(e).replace(SECRET, "[redacted]").replace(WRONG, "[redacted]")
        check(False, "the Data connection dialog saves from the page", text[:300])


def test_graphql_only_and_empty_password():
    """GraphQL can be saved before Neo4j, and an empty password is a real password."""
    folder = tempfile.mkdtemp(prefix="gx-conn-empty-")
    dotenv = Path(folder) / ".env"
    port = _free_port()
    proc = _start(_child_env(folder, dotenv, port))
    try:
        ready = _wait_status(port, proc)
        check(bool(ready) and ready.get("connected") is False, "the empty-password case starts without Neo4j",
              "" if ready else "server did not answer")
        code, raw = _request(port, "/api/connection", {
            "graphql": {
                "endpoint": "https://api.us2.app.wiz.io/graphql",
                "adapter": "wiz", "label": "Wiz", "token": SECRET,
            },
        })
        text = dotenv.read_text(encoding="utf-8") if dotenv.is_file() else ""
        cfg_path = Path(folder) / "gx.local.json"
        cfg = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
        check(code == 200 and "NEO4J_PASSWORD" not in text and "GRAPHQL_TOKEN=" in text and SECRET in text
              and SECRET not in raw and SECRET not in cfg,
              "a GraphQL-only save writes the token and leaves Neo4j unset")
        code, raw = _request(port, "/api/connection", {
            "neo4j": {
                "uri": "bolt://127.0.0.1:7687", "user": "neo4j", "database": "neo4j",
                "password": "",
            },
        })
        text = dotenv.read_text(encoding="utf-8") if dotenv.is_file() else ""
        parsed = dict(parse_dotenv(text))
        check(code == 200 and parsed.get("NEO4J_PASSWORD") == "" and "NEO4J_PASSWORD=" in text
              and SECRET in text and SECRET not in raw and "password_set" in raw,
              "No password stores an empty password and keeps the token")
    finally:
        _stop(proc)
        log = proc.stdout.read() if proc.stdout else ""
        check(SECRET not in log, "the empty-password run leaves the token out of the log")
        shutil.rmtree(folder, ignore_errors=True)


def test_live_and_browser():
    found = _neo4j_password()
    if not found:
        print("skip live credential check (Neo4j is down)")
        return
    uri, user, password = found
    folder = tempfile.mkdtemp(prefix="gx-cred-live-")
    dotenv = Path(folder) / ".env"
    dotenv.write_text(
        f"NEO4J_URI={uri}\nNEO4J_USER={user}\nNEO4J_PASSWORD={WRONG}\nNEO4J_DATABASE=neo4j\n",
        encoding="utf-8")
    os.chmod(dotenv, 0o600)
    port = _free_port()
    proc = _start(_child_env(folder, dotenv, port))
    try:
        refused = _wait_http(port, proc)
    finally:
        _stop(proc)
    log = proc.stdout.read() if proc.stdout else ""
    check(refused is not None and WRONG not in refused and WRONG not in log,
          "a wrong password stays out of the status error and the log")
    port = _free_port()
    env = _child_env(folder, dotenv, port)
    env["NEO4J_PASSWORD"] = password  # the environment wins over the wrong value in .env
    proc = _start(env)
    try:
        status = _wait_status(port, proc)
        body = json.dumps(status or {})
        check(bool(status) and status.get("connected") and password not in body and WRONG not in body
              and status.get("uri") == uri,
              "the environment password wins and the status leaves it out",
              "" if status else "server did not answer")
    finally:
        _stop(proc)

    dotenv.write_text(
        f"NEO4J_URI={uri}\nNEO4J_USER={user}\nNEO4J_PASSWORD={password}\nNEO4J_DATABASE=neo4j\n",
        encoding="utf-8")
    os.chmod(dotenv, 0o600)
    port = _free_port()
    proc = _start(_child_env(folder, dotenv, port))
    try:
        status = _wait_status(port, proc)
        body = json.dumps(status or {})
        check(bool(status) and status.get("connected") and password not in body and status.get("database") == "neo4j",
              ".env supplies the password when the environment does not",
              "" if status else "server did not answer")
        if not status:
            return
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.wait_for_function(
                """() => (document.querySelector('#conn') || {}).textContent
                    && document.querySelector('#conn').textContent.includes('nodes')"""
            )
            leaked = page.evaluate(
                """(secret) => {
                    const status = JSON.stringify(window.__gxStatus || '');
                    return {
                        text: document.body.innerText.includes(secret),
                        store: JSON.stringify(localStorage).includes(secret),
                        conn: (document.querySelector('#conn').textContent || '').includes(secret),
                    };
                }""",
                password,
            )
            fetched = page.evaluate(
                """async (secret) => {
                    const raw = await (await fetch('/api/status')).text();
                    return {hit: raw.includes(secret), raw: raw.slice(0, 180)};
                }""",
                password,
            )
            leaked_on_page = leaked["text"] or leaked["store"] or leaked["conn"] or fetched["hit"]
            check(not leaked_on_page, "the page does not show the password", "leaked" if leaked_on_page else "")
            check(not any(password in e for e in errors), "the page raised no errors that include the password")
            browser.close()
    finally:
        _stop(proc)
        shutil.rmtree(folder, ignore_errors=True)


def main():
    test_parse_and_precedence()
    test_refusals()
    test_empty_password_is_sent()
    test_prompt_keeps_an_empty_password()
    test_file_mode_warning()
    test_config_file_drops_secrets()
    test_cli_flags_win()
    test_gitignore()
    test_update_dotenv_keeps_other_lines()
    test_server_refuses_a_bad_file()
    test_connection_from_the_page()
    test_graphql_only_and_empty_password()
    test_live_and_browser()
    failed = sum(1 for ok in report if not ok)
    print(f"\n{len(report) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
