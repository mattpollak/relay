"""relay mailbox: a tiny HTTP store for relay's multi-machine sync.

It holds the same flat set of files as a sync folder (see relay's
references/formats.md) and never decrypts anything: batches are encrypted and
signed by the machines; membership records are signed. What the mailbox adds
over a shared folder is *enforcement*:

- Every request needs a per-machine bearer token (stored here only as a hash).
- A token is bound to one relay host ID; it can only create or delete files
  whose names start with that host ID. Nobody can plant files under another
  machine's name, and nobody without a token can even list the files.
- Files are create-only (409 if the name exists) and at most 64 MB.

Standard library only. Meant to run behind something that terminates TLS and
restricts who can connect at all (e.g. ``tailscale serve`` on a tailnet).

API (all under /v1, ``Authorization: Bearer <token>``)::

    GET    /healthz                  -> "ok" (no auth; for health checks)
    GET    /v1/whoami                -> {"label", "host_id"|null}
    POST   /v1/bind    {"host_id"}   -> bind this token to a host ID (once)
    GET    /v1/files                 -> {"files": [{"name", "size", "sha256"}]}
    GET    /v1/files/<name>          -> file bytes
    PUT    /v1/files/<name>          -> create (201), 409 if it exists
    DELETE /v1/files/<name>          -> 204 (own files only)

CLI::

    relay-mailbox serve --data DIR [--host 127.0.0.1] [--port 8484]
    relay-mailbox add-token --data DIR LABEL [--host ID]   # prints a new token once
    relay-mailbox list-tokens --data DIR
    relay-mailbox revoke-token --data DIR LABEL
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_FILE_BYTES = 64 * 1024 * 1024
MARKER = "relay-sync.json"
_HOST = r"[a-z2-7]{6}"
_HOST_RE = re.compile(rf"^{_HOST}$")
_EVENT_ID = rf"\d{{13}}-\d{{4}}-{_HOST}"
_OWNED_RES = (
    re.compile(rf"^(?P<host>{_HOST})\.batch\.{_EVENT_ID}(-h{_HOST}|-r\d{{8}}T\d{{6}}Z)?(-\d+)?\.json$"),
    re.compile(rf"^(?P<host>{_HOST})\.member\.\d{{13}}-(founder|approve|revoke)-{_HOST}\.json$"),
    re.compile(rf"^(?P<host>{_HOST})\.request\.json$"),
)
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

log = logging.getLogger("relay-mailbox")


def owner_of(name: str) -> str | None:
    """The host a file name belongs to; None for the shared marker. Raises
    ValueError for names the mailbox doesn't store."""
    if name == MARKER:
        return None
    for pattern in _OWNED_RES:
        m = pattern.match(name)
        if m:
            return m["host"]
    raise ValueError(name)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    """Files on disk plus the token table. One lock serializes writes."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.files_dir = self.data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.tokens_path = self.data_dir / "tokens.json"
        self.lock = threading.Lock()

    # -- tokens -----------------------------------------------------------

    def _load_tokens(self) -> dict:
        try:
            data = json.loads(self.tokens_path.read_text())
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_tokens(self, tokens: dict) -> None:
        tmp = self.tokens_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(tokens, fh, indent=1)
        os.replace(tmp, self.tokens_path)

    def add_token(self, label: str, host_id: str | None = None) -> str:
        """New token. ``host_id`` pre-binds it — the only way to give a host ID
        that belonged to a revoked token to a new token (an admin decision)."""
        if not _LABEL_RE.match(label):
            raise ValueError("label: 1-32 of letters, digits, . _ -")
        if host_id is not None and not _HOST_RE.match(host_id):
            raise ValueError("host ID: 6 characters a-z, 2-7")
        with self.lock:
            tokens = self._load_tokens()
            if any(t["label"] == label and not t.get("revoked") for t in tokens.values()):
                raise ValueError(f"a token labelled {label!r} already exists")
            if host_id is not None and any(
                t["host_id"] == host_id and not t.get("revoked") for t in tokens.values()
            ):
                raise ValueError(f"host {host_id} already has an active token; revoke it first")
            token = secrets.token_urlsafe(32)
            tokens[_hash_token(token)] = {"label": label, "host_id": host_id}
            self._save_tokens(tokens)
        return token

    def revoke_token(self, label: str) -> bool:
        """Revoke a token. Its host ID stays reserved: no other token can bind
        to it unless an admin re-issues one with ``add-token --host``."""
        with self.lock:
            tokens = self._load_tokens()
            hit = False
            for entry in tokens.values():
                if entry["label"] == label and not entry.get("revoked"):
                    entry["revoked"] = True
                    hit = True
            if hit:
                self._save_tokens(tokens)
            return hit

    def list_tokens(self) -> list[dict]:
        return sorted(self._load_tokens().values(), key=lambda t: (t["label"], bool(t.get("revoked"))))

    def authenticate(self, token: str) -> dict | None:
        tokens = self._load_tokens()
        digest = _hash_token(token)
        for h, entry in tokens.items():
            if hmac.compare_digest(h, digest):
                return None if entry.get("revoked") else entry | {"hash": h}
        return None

    def bind(self, token_hash: str, host_id: str) -> str | None:
        """Bind a token to a host ID. Returns an error message or None."""
        if not _HOST_RE.match(host_id):
            return "invalid host_id"
        with self.lock:
            tokens = self._load_tokens()
            entry = tokens.get(token_hash)
            if entry is None:
                return "unknown token"
            if entry["host_id"] == host_id:
                return None
            if entry["host_id"] is not None:
                return "this token is already bound to another host"
            if any(t["host_id"] == host_id for t in tokens.values()):
                # includes revoked tokens: a revoked machine's ID stays reserved
                return "that host ID is already bound to another token (or was revoked)"
            entry["host_id"] = host_id
            self._save_tokens(tokens)
        return None

    # -- files ------------------------------------------------------------

    def list_files(self) -> list[dict]:
        out = []
        for p in sorted(self.files_dir.iterdir()):
            if p.name.startswith(".") or not p.is_file() or p.is_symlink():
                continue
            try:
                owner_of(p.name)
            except ValueError:
                continue
            out.append({"name": p.name, "size": p.stat().st_size, "sha256": self._sha(p)})
        return out

    def _sha(self, path: Path) -> str:
        meta = path.with_name(f".{path.name}.sha256")
        try:
            return meta.read_text().strip()
        except FileNotFoundError:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            meta.write_text(digest)
            return digest

    def read(self, name: str) -> bytes | None:
        owner_of(name)  # validates
        p = self.files_dir / name
        if not p.is_file() or p.is_symlink():
            return None
        return p.read_bytes()

    def create(self, name: str, body: bytes) -> bool:
        owner_of(name)
        target = self.files_dir / name
        with self.lock:
            if target.exists() or target.is_symlink():
                return False
            tmp = self.files_dir / f".{name}.{secrets.token_hex(6)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.rename(tmp, target)
            target.with_name(f".{name}.sha256").write_text(hashlib.sha256(body).hexdigest())
        return True

    def delete(self, name: str) -> bool:
        owner_of(name)
        target = self.files_dir / name
        with self.lock:
            if not target.is_file():
                return False
            target.unlink()
            target.with_name(f".{name}.sha256").unlink(missing_ok=True)
        return True


class Handler(BaseHTTPRequestHandler):
    store: Store  # set on the server class
    server_version = "relay-mailbox/0.1"
    sys_version = ""

    # -- helpers ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # route to logging, never the token
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, payload: dict | bytes | None = None, content_type: str = "application/json") -> None:
        body = b""
        if isinstance(payload, dict):
            body = json.dumps(payload).encode()
        elif isinstance(payload, bytes):
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, {"error": message})

    def _auth(self) -> dict | None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._error(HTTPStatus.UNAUTHORIZED, "missing bearer token")
            return None
        entry = self.store.authenticate(header[len("Bearer "):].strip())
        if entry is None:
            self._error(HTTPStatus.UNAUTHORIZED, "invalid token")
            return None
        return entry

    def _file_name(self) -> str | None:
        prefix = "/v1/files/"
        if not self.path.startswith(prefix):
            return None
        name = self.path[len(prefix):]
        try:
            owner_of(name)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "not a relay sync file name")
            return ""
        return name

    def _read_body(self, limit: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return None
        if length < 0 or length > limit:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"at most {limit} bytes")
            return None
        return self.rfile.read(length)

    # -- verbs ------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/healthz":  # for container health checks; reveals nothing
            return self._send(HTTPStatus.OK, b"ok\n", "text/plain")
        entry = self._auth()
        if entry is None:
            return
        if self.path == "/v1/whoami":
            return self._send(HTTPStatus.OK, {"label": entry["label"], "host_id": entry["host_id"]})
        if self.path == "/v1/files":
            return self._send(HTTPStatus.OK, {"files": self.store.list_files()})
        name = self._file_name()
        if name is None:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if not name:
            return
        data = self.store.read(name)
        if data is None:
            return self._error(HTTPStatus.NOT_FOUND, "no such file")
        self._send(HTTPStatus.OK, data, "application/octet-stream")

    def do_POST(self) -> None:
        entry = self._auth()
        if entry is None:
            return
        if self.path != "/v1/bind":
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        body = self._read_body(1024)
        if body is None:
            return
        try:
            host_id = json.loads(body).get("host_id")
        except (json.JSONDecodeError, AttributeError):
            return self._error(HTTPStatus.BAD_REQUEST, "expected {\"host_id\": ...}")
        error = self.store.bind(entry["hash"], str(host_id))
        if error:
            return self._error(HTTPStatus.CONFLICT, error)
        log.info("token %r bound to host %s", entry["label"], host_id)
        self._send(HTTPStatus.OK, {"label": entry["label"], "host_id": host_id})

    def _owned_write_check(self, entry: dict, name: str) -> bool:
        owner = owner_of(name)
        if entry["host_id"] is None:
            self._error(HTTPStatus.FORBIDDEN, "token isn't bound to a host yet; POST /v1/bind first")
            return False
        if owner is not None and owner != entry["host_id"]:
            self._error(HTTPStatus.FORBIDDEN, "you can only write files that start with your own host ID")
            return False
        return True

    def do_PUT(self) -> None:
        entry = self._auth()
        if entry is None:
            return
        name = self._file_name()
        if name is None:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if not name or not self._owned_write_check(entry, name):
            return
        body = self._read_body(MAX_FILE_BYTES)
        if body is None:
            return
        if not self.store.create(name, body):
            return self._error(HTTPStatus.CONFLICT, "a file with that name already exists")
        self._send(HTTPStatus.CREATED, {"name": name, "sha256": hashlib.sha256(body).hexdigest()})

    def do_DELETE(self) -> None:
        entry = self._auth()
        if entry is None:
            return
        name = self._file_name()
        if name is None:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if not name or not self._owned_write_check(entry, name):
            return
        if owner_of(name) is None:
            return self._error(HTTPStatus.FORBIDDEN, "the marker can't be deleted")
        if not self.store.delete(name):
            return self._error(HTTPStatus.NOT_FOUND, "no such file")
        self._send(HTTPStatus.NO_CONTENT)


def make_server(data_dir: Path, host: str = "127.0.0.1", port: int = 8484) -> ThreadingHTTPServer:
    store = Store(data_dir)
    handler = type("BoundHandler", (Handler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay-mailbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "add-token", "list-tokens", "revoke-token"):
        p = sub.add_parser(name)
        p.add_argument("--data", default=os.environ.get("RELAY_MAILBOX_DATA", "/data"))
        if name == "serve":
            p.add_argument("--host", default=os.environ.get("RELAY_MAILBOX_HOST", "127.0.0.1"))
            p.add_argument("--port", type=int, default=int(os.environ.get("RELAY_MAILBOX_PORT", "8484")))
        if name in ("add-token", "revoke-token"):
            p.add_argument("label")
        if name == "add-token":
            p.add_argument("--host", help="pre-bind to this relay host ID (re-issue access for a machine)")
    args = parser.parse_args(argv)
    store = Store(Path(args.data))
    if args.cmd == "add-token":
        try:
            token = store.add_token(args.label, args.host)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(token)
        print(f"(token for {args.label!r}; shown once — store it in your password manager)", file=sys.stderr)
        return 0
    if args.cmd == "list-tokens":
        for t in store.list_tokens():
            state = "revoked" if t.get("revoked") else "active"
            print(f"{t['label']}\t{t['host_id'] or '(not bound yet)'}\t{state}")
        return 0
    if args.cmd == "revoke-token":
        if store.revoke_token(args.label):
            print(f"revoked {args.label}")
            return 0
        print(f"no token labelled {args.label!r}", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = make_server(Path(args.data), args.host, args.port)
    log.info("relay-mailbox listening on %s:%s (data %s)", args.host, args.port, args.data)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
