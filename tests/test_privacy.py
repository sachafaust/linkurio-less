#!/usr/bin/env python3
"""Privacy scan: no tracked file names a tenant, an account, or a machine.

The repository is public, so every tracked file is published. This scan reads
each file that git tracks and refuses:

- a Wiz host with a region or tenant subdomain, such as a real API region,
- the Wiz token hosts anywhere except graphql_source.py,
- a 12-digit AWS account id, or an ARN that holds one,
- a private IPv4 address,
- a JWT, an AWS access key id, or a private key,
- a macOS home folder path.

It also checks that the files which hold credentials, saved queries, and
cached data models are gitignored and not tracked. It starts no server and
needs no network. It reads about 7 MB and takes about a second.
"""
from __future__ import annotations

import gzip
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAILS = []

# ---- the rules ------------------------------------------------------------------------

# A host on a Wiz domain, with at least one label before wiz.io or wiz.us.
WIZ_HOST = re.compile(r"(?<![A-Za-z0-9<>_.-])((?:[A-Za-z0-9<>_-]+\.)+)wiz\.(io|us)(?![A-Za-z0-9-])", re.I)
# Labels that name no region and no tenant. A label that starts with "example"
# is a test placeholder, for example api.example-region.app.wiz.io.
WIZ_SAFE_LABELS = {"api", "app", "auth", "<region>"}
# The public token hosts may appear in one file only: the offerings table.
WIZ_AUTH_HOST = re.compile(r"auth\.app\.wiz\.(io|us)", re.I)
WIZ_AUTH_HOME = "graphql_source.py"

ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z.])[0-9]{12}(?![0-9A-Za-z])")
# AWS never issues the all-zero account id. The UI suite uses it as the fixture hub anchor.
ACCOUNT_PLACEHOLDER = "0" * 12
ARN_ACCOUNT = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:[0-9]{12}(?![0-9])")
PRIVATE_IP = re.compile(
    r"(?<![0-9.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![0-9])")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
# Long-term (AKIA) and temporary (ASIA) access key ids. AWS documentation
# examples end in EXAMPLE, and the fixture snapshot uses them.
ACCESS_KEY = re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")
# The marker is split, so this file does not match its own rule.
PRIVATE_KEY = re.compile("-----BEGIN [A-Z ]*" + "PRIVATE KEY-----")
HOME_PATH = re.compile(r"/Users/[A-Za-z0-9._-]+/")

# ---- the exemptions -------------------------------------------------------------------

# data/cartography-fixtures.json.gz is the Cartography test-fixture snapshot.
# Cartography's own demo seeds and test fixtures made it. Its account ids,
# ARNs, and private IP ranges are fixture values such as the AWS example account, and
# its access key ids are the AWS documentation examples. The other rules
# still run on its text.
FIXTURE = "data/cartography-fixtures.json.gz"
FIXTURE_SKIPS = {"account id", "ARN account", "private IP"}
# seed_cartography.py is the synthetic seeder. A seeded random generator
# makes every account id, ARN, and role id it writes ("Everything here is
# fake"). Its account and ARN templates are code, not data.
SEEDER = "seed_cartography.py"
SEEDER_SKIPS = {"account id", "ARN account"}
EXEMPT = {FIXTURE: FIXTURE_SKIPS, SEEDER: SEEDER_SKIPS}

# Files that must stay out of git. Each name is checked with git check-ignore.
MUST_IGNORE = [
    ".env", ".env.tmp", ".cache/0123abcd.json", "gx.local.json", "gx.local.json.tmp",
    "gx.queries.json", "gx.queries.json.lock", "gx.queries.json.tmp",
    # The explorer no longer writes gx.schema.json. An old copy on a disk stays out of git.
    "gx.schema.json", "gx.schema.json.tmp",
    # The UI suite writes these two. tests/out/ is where older runs wrote.
    "tests/screenshots/app-01-landing.png", "tests/results.json", "tests/out/results.json",
]
# Generated outputs that a run writes. None of them may be tracked.
MUST_NOT_TRACK = [".env", "gx.local.json", "gx.queries.json", "gx.schema.json", "tests/results.json"]


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{(': ' + str(detail)) if detail else ''}")
        FAILS.append(name)


def wiz_host_ok(labels):
    """True when no label before wiz.io or wiz.us can name a region or a tenant."""
    parts = [part.lower() for part in labels.rstrip(".").split(".") if part]
    return all(part in WIZ_SAFE_LABELS or part.startswith("example") for part in parts)


def findings(path, text):
    """Each rule a text breaks, as (rule, line, sample)."""
    skips = EXEMPT.get(path, set())
    out = []

    def hit(rule, match):
        if rule in skips:
            return
        line = text.count("\n", 0, match.start()) + 1
        out.append((rule, line, match.group(0)[:80]))

    for match in WIZ_HOST.finditer(text):
        if not wiz_host_ok(match.group(1)):
            hit("Wiz region host", match)
    if path != WIZ_AUTH_HOME:
        for match in WIZ_AUTH_HOST.finditer(text):
            hit("Wiz auth host outside the offerings table", match)
    for match in ACCOUNT_ID.finditer(text):
        if match.group(0) != ACCOUNT_PLACEHOLDER:
            hit("account id", match)
    for match in ARN_ACCOUNT.finditer(text):
        hit("ARN account", match)
    for match in PRIVATE_IP.finditer(text):
        hit("private IP", match)
    for match in JWT.finditer(text):
        hit("JWT", match)
    for match in ACCESS_KEY.finditer(text):
        if not match.group(0).endswith("EXAMPLE"):
            hit("AWS access key id", match)
    for match in PRIVATE_KEY.finditer(text):
        hit("private key", match)
    for match in HOME_PATH.finditer(text):
        hit("home folder path", match)
    return out


def tracked():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [name for name in out.stdout.decode("utf-8").split("\0") if name]


def read_text(path):
    """The text git would publish. A gzip file is read decompressed. Other binaries are read as Latin-1."""
    raw = (ROOT / path).read_bytes()
    if path.endswith(".gz"):
        raw = gzip.decompress(raw)
    if b"\0" in raw[:8192] or path.endswith((".png", ".jpg", ".ttf")):
        return raw.decode("latin-1")
    return raw.decode("utf-8", errors="replace")


# ---- the checks -----------------------------------------------------------------------

def test_rules_catch_samples():
    """Each rule catches a planted sample. The samples are built here, so no file holds one."""
    print("rules")
    region = "us" + "2"
    samples = {
        "Wiz region host": f"https://api.{region}.app.wiz.io/graphql",
        "Wiz region host (gov)": f"https://api.{region}.app.wiz.us/graphql",
        "Wiz region host (case)": f"https://API.{region.upper()}.APP.WIZ.IO/graphql",
        "Wiz tenant host": "https://acme." + "wiz.io/graphql",
        "account id": "account " + "4" * 6 + "5" * 6,
        "ARN account": "arn:aws-us-gov:iam::" + "4" * 6 + "5" * 6 + ":role/x",
        "private IP": "10." + "20.30.40",
        "JWT": "eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10,
        "AWS access key id": "AKIA" + "Q" * 16,
        "private key": "-----BEGIN RSA " + "PRIVATE KEY-----",
        "home folder path": "/Users/" + "someone/code",
    }
    for name, sample in samples.items():
        check(f"the scan refuses a {name}", bool(findings("sample.txt", sample)), sample)
    auth = "auth.app." + "wiz.us"
    check("the auth host is refused outside the offerings table", bool(findings("README.md", auth)))
    check("the auth host is allowed in the offerings table", not findings(WIZ_AUTH_HOME, auth))
    safe = [
        "https://api.example-region.app.wiz.io/graphql", "https://API.EXAMPLE-REGION.APP.WIZ.US/graphql",
        "https://api.<region>.app.wiz.io/graphql", "https://app.wiz.io/", "a wiz.io or wiz.us host",
        "https://notwiz.io/graphql", "https://wiz.io.evil.example/graphql", "127.0.0.1", "0.0.0.0/0",
        "AKIAIOSFODNN7EXAMPLE", "1763000000000", "0.123456789012", "account " + "0" * 12,
    ]
    for sample in safe:
        check(f"the scan allows {sample}", not findings("sample.txt", sample), findings("sample.txt", sample))


def test_tracked_files():
    print("tracked files")
    names = tracked()
    check("git lists the tracked files", len(names) > 20, len(names))
    found = []
    for name in names:
        path = ROOT / name
        if not path.is_file():
            continue
        found.extend((name, *item) for item in findings(name, read_text(name)))
    shown = "; ".join(f"{name}:{line} {rule} {sample!r}" for name, rule, line, sample in found[:25])
    check("no tracked file names a Wiz region, an account, a private address, a credential, or a home folder",
          not found, f"{len(found)} hit(s): {shown}")
    for name in EXEMPT:
        check(f"the exemption {name} is a tracked file", name in names)


def test_ignore_rules():
    print("ignore rules")
    for name in MUST_IGNORE:
        code = subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=ROOT).returncode
        check(f"{name} is gitignored", code == 0)
    names = set(tracked())
    for name in MUST_NOT_TRACK:
        check(f"{name} is not tracked", name not in names)
    check("no file under .cache/ or tests/screenshots/ is tracked",
          not [n for n in names if n.startswith((".cache/", "tests/screenshots/", "tests/out/"))])


def main():
    test_rules_catch_samples()
    test_tracked_files()
    test_ignore_rules()
    if FAILS:
        print(f"\n{len(FAILS)} failed")
        return 1
    print("\nall privacy checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
