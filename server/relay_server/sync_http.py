"""HTTP storage backend: the same flat file set as a sync folder, kept by a
relay mailbox (see the repo's ``mailbox/``).

Only imported when sync uses ``backend: http``. Standard library only. The
mailbox sees nothing but signed, encrypted files; it additionally enforces
that each machine's token can only create or delete files under its own host
ID. TLS is required except for localhost (validated at setup).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .sync_folder import FolderError, MAX_FILE_BYTES

TOKEN_FILE = "sync.token"
TIMEOUT = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies the Authorization header onto redirects — to any host and
    from https to http. The mailbox never redirects, so refuse them all."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def load_token(data_dir: Path, block: dict) -> str:
    """The mailbox token: from ``token_command`` (e.g. ``op read op://...``)
    if configured, else ``<data_dir>/sync.token`` (mode 0600)."""
    command = block.get("token_command")
    if command:
        try:
            out = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FolderError(f"token_command failed: {exc}") from exc
        if out.returncode != 0 or not out.stdout.strip():
            raise FolderError("token_command didn't print a token")
        return out.stdout.strip()
    path = Path(data_dir) / TOKEN_FILE
    try:
        token = path.read_text().strip()
    except FileNotFoundError:
        raise FolderError("no mailbox token on this machine; run `/relay:sync setup` again") from None
    if not token:
        raise FolderError(f"{path} is empty")
    return token


def save_token(data_dir: Path, token: str) -> None:
    import os

    path = Path(data_dir) / TOKEN_FILE
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token.strip() + "\n")
    os.replace(tmp, path)


class HttpStorage:
    def __init__(self, url: str, token: str, timeout: float = TIMEOUT):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def describe(self) -> str:
        return self.url

    def _request(self, method: str, path: str, body: bytes | None = None, ok=(200,)) -> tuple[int, bytes]:
        req = urllib.request.Request(
            f"{self.url}{path}", data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/octet-stream"},
        )
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                data = resp.read(MAX_FILE_BYTES + 1)
                return resp.status, data
        except urllib.error.HTTPError as exc:
            data = exc.read(4096)
            if exc.code in ok:
                return exc.code, data
            try:
                message = json.loads(data).get("error", "")
            except (json.JSONDecodeError, AttributeError):
                message = ""
            if exc.code == 401:
                raise FolderError("the mailbox rejected this machine's token (revoked or mistyped?)") from None
            if 300 <= exc.code < 400:
                raise FolderError(
                    f"the sync mailbox answered with a redirect ({exc.code}); refusing to follow it "
                    "(check the url, e.g. https vs http or a proxy in front of the mailbox)"
                ) from None
            return exc.code, message.encode()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FolderError(f"can't reach the sync mailbox at {self.url} ({reason})") from None

    def whoami(self) -> dict:
        status, data = self._request("GET", "/v1/whoami")
        if status != 200:
            raise FolderError(f"mailbox error {status}: {data.decode(errors='replace')}")
        return json.loads(data)

    def check(self) -> None:
        self.whoami()

    def prepare(self, host_id: str) -> None:
        """Bind this machine's token to its host ID (once). No-op without one."""
        if not host_id:
            return
        info = self.whoami()
        if info.get("host_id") == host_id:
            return
        if info.get("host_id"):
            raise FolderError(
                f"this mailbox token belongs to another machine ({info['host_id']}); "
                "create a separate token for each machine"
            )
        status, data = self._request("POST", "/v1/bind", json.dumps({"host_id": host_id}).encode())
        if status != 200:
            raise FolderError(f"mailbox refused to bind this machine: {data.decode(errors='replace')}")

    def names(self) -> list[str]:
        status, data = self._request("GET", "/v1/files")
        if status != 200:
            raise FolderError(f"mailbox error {status}: {data.decode(errors='replace')}")
        files = json.loads(data).get("files", [])
        return sorted(f["name"] for f in files if isinstance(f, dict) and isinstance(f.get("name"), str))

    def read(self, name: str) -> bytes | None:
        status, data = self._request("GET", f"/v1/files/{urllib.parse.quote(name, safe='')}", ok=(200, 404))
        if status != 200 or len(data) > MAX_FILE_BYTES:
            return None
        return data

    def create(self, name: str, body: bytes) -> None:
        status, data = self._request(
            "PUT", f"/v1/files/{urllib.parse.quote(name, safe='')}", body, ok=(201, 409),
        )
        if status == 409:
            raise FolderError(f"{name} already exists in the mailbox; refusing to replace it")
        if status != 201:
            raise FolderError(f"mailbox refused {name} ({status}: {data.decode(errors='replace')})")

    def delete(self, name: str) -> None:
        status, data = self._request("DELETE", f"/v1/files/{urllib.parse.quote(name, safe='')}", ok=(204, 404))
        if status not in (204, 404):
            raise FolderError(f"mailbox refused to delete {name} ({status}: {data.decode(errors='replace')})")
