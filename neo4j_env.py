"""Neo4j credentials from the environment, with a gitignored .env as a fallback.

The process environment always wins. A `.env` file next to this module fills in
only the credential variables that are still unset: NEO4J_URI, NEO4J_USER,
NEO4J_PASSWORD, and NEO4J_DATABASE. Data connection in the page writes that
file. The password stays out of gx.local.json, the shell command, and the page.
GX_ENV points at a different file when a test should keep its own.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CREDENTIAL_KEYS = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_DATABASE")
_WARNED_PERMS = False


class Settings:
    __slots__ = ("uri", "user", "password", "database")

    def __init__(self, uri, user, password, database):
        self.uri = uri
        self.user = user
        self.password = password
        self.database = database


class Neo4jConfigError(Exception):
    def __init__(self, message, missing_password=False):
        super().__init__(message)
        self.missing_password = missing_password


def dotenv_path():
    raw = (os.environ.get("GX_ENV") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parent / ".env"


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        quote, inner = value[0], value[1:-1]
        if quote == "'":
            return inner
        out = []
        i = 0
        while i < len(inner):
            if inner[i] == "\\" and i + 1 < len(inner):
                out.append(inner[i + 1])
                i += 2
            else:
                out.append(inner[i])
                i += 1
        return "".join(out)
    return value


def _dotenv_key(raw):
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    key = line.split("=", 1)[0].strip()
    if not key or key[0].isdigit() or not key.replace("_", "").isalnum():
        return None
    return key


def format_dotenv_value(value):
    """Quote a value the parser reads back unchanged. An empty password stays empty."""
    text = "" if value is None else str(value)
    if text == "":
        return ""
    if any(ch in text for ch in " \t#\"'\\") or text != text.strip():
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def update_dotenv(updates, *, remove=(), path=None):
    """Replace keys in the gitignored env file and leave every other line as it is.

    ``updates`` maps a name to its new literal value. ``remove`` drops those
    names. The write is atomic and the file mode is owner-read/write.
    """
    path = Path(path) if path is not None else dotenv_path()
    try:
        existing = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except OSError as e:
        raise Neo4jConfigError(f"could not read {path.name}") from e
    pending = {str(key): "" if value is None else str(value) for key, value in dict(updates).items()}
    removing = {str(key) for key in remove}
    for value in list(pending.values()):
        if "\n" in value or "\r" in value:
            raise Neo4jConfigError("a saved setting cannot contain a line break")
    out, seen = [], set()
    for raw in existing.splitlines():
        key = _dotenv_key(raw)
        if key in removing:
            continue
        if key in pending:
            if key not in seen:
                out.append(f"{key}={format_dotenv_value(pending[key])}")
                seen.add(key)
            continue
        out.append(raw)
    for key, value in pending.items():
        if key not in seen:
            out.append(f"{key}={format_dotenv_value(value)}")
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise Neo4jConfigError(f"could not write {path.name}") from e
    return path


def parse_dotenv(text):
    """Yield credential-looking KEY, value pairs. Values are literal: no $ expansion."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        yield key, _unquote(value)


def _warn_perms(path):
    global _WARNED_PERMS
    if _WARNED_PERMS or os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        _WARNED_PERMS = True
        print(f"WARNING: {path} is readable by other users on this machine. chmod 600 {path.name}", file=sys.stderr)


def load_credential_env(path=None, environ=None):
    """Fill unset Neo4j credential variables from a .env file. Existing values stay."""
    environ = os.environ if environ is None else environ
    path = Path(path) if path is not None else dotenv_path()
    if not path.is_file():
        return path
    _warn_perms(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise Neo4jConfigError(f"could not read {path.name}") from e
    for key, value in parse_dotenv(text):
        if key in CREDENTIAL_KEYS and key not in environ:
            environ[key] = value
    return path


def redact_uri(uri):
    """Drop any userinfo from a bolt or neo4j URI. The host and port stay."""
    if not uri:
        return uri
    parts = urlsplit(uri)
    if parts.username is None and parts.password is None:
        return uri
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def neo4j_settings(environ=None, path=None):
    """Read the connection settings. Raises Neo4jConfigError when the password is unset.

    An empty password is a real password and is sent to Neo4j unchanged. Only a
    missing NEO4J_PASSWORD is refused.
    """
    environ = os.environ if environ is None else environ
    where = load_credential_env(path=path, environ=environ)
    uri = (environ.get("NEO4J_URI") or "bolt://localhost:7687").strip()
    user = (environ.get("NEO4J_USER") or "neo4j").strip() or "neo4j"
    database = (environ.get("NEO4J_DATABASE") or "neo4j").strip() or "neo4j"
    password = environ.get("NEO4J_PASSWORD")
    if "://" not in uri:
        raise Neo4jConfigError("NEO4J_URI must be a bolt or neo4j URL, such as bolt://127.0.0.1:7687")
    parts = urlsplit(uri)
    if parts.username is not None or parts.password is not None:
        raise Neo4jConfigError(
            "NEO4J_URI includes a user or a password. Keep the URI as the host, "
            "and set NEO4J_USER and NEO4J_PASSWORD.")
    if password is None:
        raise Neo4jConfigError(
            "NEO4J_PASSWORD is not set. Export it, or put it in "
            f"{where.name} (gitignored). An empty value is allowed when Neo4j has no password. "
            "The explorer reads NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, and NEO4J_DATABASE.",
            missing_password=True)
    return Settings(uri, user, password, database)
