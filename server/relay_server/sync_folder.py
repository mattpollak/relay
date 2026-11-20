"""Folder backend: exchange signed, encrypted files through one shared directory.

The folder is **one flat directory** of files. Everything relay shares is a
compressed, encrypted, signed batch of structured events; which workstream,
what kind of data and which IDs are all *inside* the ciphertext. The only
plaintext files are the small signed records machines need before they can
decrypt anything (membership and join requests)::

    relay-sync.json                                   {"format": 2, "app": "relay"}
    <by>.member.<ms>-<kind>-<subject>.json            signed membership records
    <host>.request.json                               self-signed join request
    <host>.batch.<last-event-id>[-h<id>|-r<time>][-<n>].json   encrypted batches

Every name starts with the host that writes it, so a file-syncing service never
sees two machines edit the same file.

Anyone with write access to the shared folder (the sync service, a NAS admin)
controls what's in it — including symlinks and special files on services that
keep them. So relay treats the folder as untrusted input:

1. **Create, never overwrite.** New files are written under a temporary
   dot-name with O_EXCL|O_NOFOLLOW and renamed into place only if the target
   name is free. A planted symlink makes the write fail; it's never followed.
2. **Delete only what this machine wrote.** Each file relay writes is recorded
   (name + SHA-256) in a local manifest; a file is removed only if it's in the
   manifest and still has that content. Planted or swapped files are never
   touched, and unlinking a name never follows a link.
3. **Read only regular files**, opened with O_NOFOLLOW|O_NONBLOCK and checked
   with fstat (no symlinks, FIFOs or devices), up to a size limit.

There are no subdirectories, so nothing can redirect relay elsewhere.

Membership: the first host in an empty folder writes a self-signed ``founder``
record. Later ``approve`` / ``revoke`` records must be signed by a current
member. See :meth:`SyncFolder.resolve_members`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from . import sync_crypto as sc

FORMAT = 2
MARKER = "relay-sync.json"
MAX_FILE_BYTES = 64 * 1024 * 1024
_HOST = r"[a-z2-7]{6}"
_HOST_RE = re.compile(rf"^{_HOST}$")
_EVENT_ID = rf"\d{{13}}-\d{{4}}-{_HOST}"
_BATCH_RE = re.compile(rf"^(?P<host>{_HOST})\.batch\.(?P<id>{_EVENT_ID})(-h{_HOST}|-r\d{{8}}T\d{{6}}Z)?(-\d+)?\.json$")
_MEMBER_RE = re.compile(rf"^(?P<host>{_HOST})\.member\.\d{{13}}-(founder|approve|revoke)-{_HOST}\.json$")
_REQUEST_RE = re.compile(rf"^(?P<host>{_HOST})\.request\.json$")

MAX_NAME = 32


def safe_name(value: object, fallback: str) -> str:
    """A name written by another machine (or anyone with folder access), made
    safe to show: one short line of printable characters."""
    if not isinstance(value, str):
        return fallback
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    text = text.replace("`", "'")
    if len(text) > MAX_NAME:
        text = text[: MAX_NAME - 1] + "…"
    return text or fallback


class SyncError(Exception):
    """A sync operation can't run; the message is meant for the user."""


class FolderError(SyncError):
    """The shared folder is missing, unsupported or looks tampered with."""


class Manifest(Protocol):
    """Local record of the files this machine wrote to the shared folder."""

    def record(self, name: str, sha256: str) -> None: ...
    def lookup(self, name: str) -> str | None: ...
    def forget(self, name: str) -> None: ...
    def names(self) -> list[str]: ...


class MemoryManifest:
    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    def record(self, name: str, sha256: str) -> None:
        self._files[name] = sha256

    def lookup(self, name: str) -> str | None:
        return self._files.get(name)

    def forget(self, name: str) -> None:
        self._files.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._files)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class SyncFolder:
    def __init__(self, root: Path | str, manifest: Manifest | None = None):
        self.root = Path(root)
        self.manifest: Manifest = manifest if manifest is not None else MemoryManifest()

    # -- low-level I/O (never follows links) ------------------------------

    def _open_root(self) -> int:
        try:
            st = os.lstat(self.root)
        except FileNotFoundError:
            raise FolderError(f"sync folder {self.root} isn't available (unmounted, or not synced yet?)") from None
        if stat.S_ISLNK(st.st_mode):
            raise FolderError(f"sync folder {self.root} is a symlink; point relay at the real folder")
        if not stat.S_ISDIR(st.st_mode):
            raise FolderError(f"sync folder {self.root} isn't a directory")
        return os.open(self.root, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)

    def _names(self) -> list[str]:
        fd = self._open_root()
        try:
            return sorted(os.listdir(fd))
        finally:
            os.close(fd)

    def _read_bytes(self, name: str) -> bytes | None:
        """Contents of a regular file directly in the folder, else None."""
        dfd = self._open_root()
        try:
            try:
                fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=dfd)
            except OSError:
                return None  # missing, a symlink (ELOOP), or unreadable
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
                    return None
                chunks, total = [], 0
                while True:
                    chunk = os.read(fd, 1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(fd)
        finally:
            os.close(dfd)

    def _read_json(self, name: str) -> Any:
        raw = self._read_bytes(name)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _create(self, name: str, data: dict) -> str:
        """Create a new file; never overwrites anything. Returns its name."""
        if "/" in name or name.startswith("."):
            raise FolderError(f"bad file name {name!r}")
        body = (json.dumps(data, indent=1) + "\n").encode()
        dfd = self._open_root()
        tmp = f".{name}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dfd)
            try:
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                os.unlink(tmp, dir_fd=dfd)
                raise FolderError(f"{name} already exists in the sync folder; refusing to replace it")
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except FileExistsError as exc:
            raise FolderError(f"couldn't create {name}: {exc}") from exc
        finally:
            os.close(dfd)
        self.manifest.record(name, hashlib.sha256(body).hexdigest())
        return name

    def delete_own(self, name: str) -> bool:
        """Delete a file this machine wrote, if it still has the content written.
        Anything else — planted, swapped, or not ours — is left alone."""
        expected = self.manifest.lookup(name)
        if expected is None:
            return False
        raw = self._read_bytes(name)
        if raw is None or hashlib.sha256(raw).hexdigest() != expected:
            if raw is None:
                self.manifest.forget(name)  # already gone (or replaced by a non-file)
            return False
        dfd = self._open_root()
        try:
            os.unlink(name, dir_fd=dfd)  # removes the name itself; never follows a link
        except FileNotFoundError:
            pass
        finally:
            os.close(dfd)
        self.manifest.forget(name)
        return True

    # -- layout -----------------------------------------------------------

    def is_initialized(self) -> bool:
        return self._read_bytes(MARKER) is not None

    def check(self) -> None:
        os.close(self._open_root())
        if self.is_initialized():
            marker = self._read_json(MARKER)
            if not isinstance(marker, dict) or marker.get("format") != FORMAT:
                raise FolderError(
                    f"{self.root / MARKER} is from a different relay version (format "
                    f"{marker.get('format') if isinstance(marker, dict) else '?'}); use a new, empty folder"
                )

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.is_initialized():
            self._create(MARKER, {"format": FORMAT, "app": "relay"})

    def unrelated_content(self) -> list[str]:
        """Entries that aren't relay's (a non-empty folder someone else uses)."""
        try:
            names = self._names()
        except FolderError:
            return []
        ours = (_BATCH_RE, _MEMBER_RE, _REQUEST_RE)
        return [
            n for n in names
            if n != MARKER and not n.startswith(".") and not any(r.match(n) for r in ours)
        ]

    def _matching(self, pattern: re.Pattern, keep: Callable[[re.Match], bool] = lambda m: True) -> list[str]:
        return [n for n in self._names() if (m := pattern.match(n)) and keep(m)]

    # -- membership -------------------------------------------------------

    def membership_records(self) -> list[dict]:
        out = []
        for name in self._matching(_MEMBER_RE):
            rec = self._read_json(name)
            if isinstance(rec, dict) and rec.get("by") == name.split(".", 1)[0]:
                out.append(rec)
        return out

    def write_membership(self, key: sc.HostKey, kind: str, by: str, subject: str,
                         subject_key: str | None, subject_name: str | None) -> dict:
        ms = int(time.time() * 1000)
        record = {
            "kind": kind, "by": by, "subject": subject, "subject_key": subject_key,
            "subject_name": subject_name, "at_ms": ms,
        }
        signed = sc.sign_record(key, record)
        self._create(f"{by}.member.{ms:013d}-{kind}-{subject}.json", signed)
        return signed

    def resolve_members(
        self, pinned: dict | None, accepted: set[str] | None = None,
        stored_records: list[dict] | None = None,
    ) -> dict:
        """Fold membership records into the current member set.

        pinned: {"host_id", "key"} of the founder this host trusts, or None
        to trust (and return for pinning) the folder's only valid founder.
        accepted: signatures of records this host accepted before. Records
        seen for the first time are applied after those, and must be signed
        by a host that is a member *now* — so a revoked host can't slip in a
        back-dated approval.
        stored_records: this host's own copies of records it accepted before.
        They're applied even if their files have vanished from the folder, so
        deleting a revoke record from the shared folder can't undo it.
        Returns {"founder", "members": {id: {key, name}}, "revoked": set,
        "accepted": set, "accepted_records": [...], "missing": int, "error"}.
        """
        stored = [r for r in (stored_records or []) if isinstance(r, dict) and isinstance(r.get("sig"), str)]
        accepted = set(accepted or ()) | {r["sig"] for r in stored}
        folder_records = self.membership_records()
        in_folder = {r.get("sig") for r in folder_records}
        records = folder_records + [r for r in stored if r["sig"] not in in_folder]
        missing = sum(1 for r in stored if r["sig"] not in in_folder)
        founders = [
            r for r in records
            if r.get("kind") == "founder" and r.get("by") == r.get("subject")
            and isinstance(r.get("subject_key"), str) and sc.verify_record(r, r["subject_key"])
        ]
        result: dict = {
            "founder": None, "members": {}, "revoked": set(), "accepted": set(),
            "accepted_records": [], "missing": missing, "error": None,
        }
        if pinned:
            match = [f for f in founders if f["subject"] == pinned["host_id"] and f["subject_key"] == pinned["key"]]
            if not match:
                result["error"] = (
                    "the sync folder's founder record doesn't match the one this machine trusted at setup; "
                    "refusing to sync (was the folder replaced or tampered with?)"
                )
                return result
            founder = match[0]
        else:
            if len(founders) != 1:
                result["error"] = (
                    "sync folder has no valid founder record" if not founders
                    else "sync folder has more than one founder record; refusing to guess"
                )
                return result
            founder = founders[0]
        result["founder"] = {"host_id": founder["subject"], "key": founder["subject_key"]}
        members = {founder["subject"]: {"key": founder["subject_key"], "name": founder.get("subject_name")}}
        revoked: set[str] = set()

        def order(r: dict) -> tuple:
            return (r["at_ms"], str(r.get("by")), str(r.get("subject")))

        others = [r for r in records if r.get("kind") in ("approve", "revoke") and isinstance(r.get("at_ms"), int)]
        known = sorted((r for r in others if r.get("sig") in accepted), key=order)
        new = sorted((r for r in others if r.get("sig") not in accepted), key=order)
        now_accepted = {founder["sig"]}
        accepted_records = [founder]
        for r in known + new:
            signer = members.get(r.get("by"))
            if signer is None or not sc.verify_record(r, signer["key"]):
                continue
            subject = r.get("subject")
            if not isinstance(subject, str) or not _HOST_RE.match(subject):
                continue
            if r["kind"] == "approve":
                if subject in revoked or not isinstance(r.get("subject_key"), str):
                    continue
                members[subject] = {"key": r["subject_key"], "name": r.get("subject_name")}
            else:
                members.pop(subject, None)
                revoked.add(subject)
            now_accepted.add(r["sig"])
            accepted_records.append(r)
        result["members"] = members
        result["revoked"] = revoked
        result["accepted"] = now_accepted
        result["accepted_records"] = accepted_records
        return result

    # -- join requests ----------------------------------------------------

    def write_request(self, key: sc.HostKey, host_id: str, name: str, hostname: str) -> None:
        fname = f"{host_id}.request.json"
        if self.manifest.lookup(fname) is not None and self._read_bytes(fname) is not None:
            return  # already asked
        record = {
            "host_id": host_id, "key": key.public_b64, "name": name, "hostname": hostname,
            "at_ms": int(time.time() * 1000),
        }
        self._create(fname, sc.sign_record(key, record))

    def requests(self) -> list[dict]:
        out = []
        for name in self._matching(_REQUEST_RE):
            rec = self._read_json(name)
            if (
                isinstance(rec, dict) and name.split(".", 1)[0] == rec.get("host_id")
                and isinstance(rec.get("key"), str) and sc.verify_record(rec, rec["key"])
            ):
                out.append(rec)
        return out

    def remove_own_request(self, host_id: str) -> None:
        self.delete_own(f"{host_id}.request.json")

    # -- batches ----------------------------------------------------------

    def write_batch(self, sender_id: str, envelope: dict, suffix: str = "") -> str:
        name = f"{sender_id}.batch.{envelope['last']}{suffix}.json"
        if not _BATCH_RE.match(name):
            raise FolderError(f"bad batch name {name!r}")
        return self._create(name, envelope)

    def own_batches(self, host_id: str) -> list[str]:
        """Batch files this machine wrote (from its manifest)."""
        return [n for n in self.manifest.names() if (m := _BATCH_RE.match(n)) and m["host"] == host_id]

    def list_batches(self, exclude_host: str) -> list[tuple[str, str]]:
        """[(sender_id, file name)] for other hosts' batches, oldest first."""
        out = []
        for name in self._matching(_BATCH_RE, lambda m: m["host"] != exclude_host):
            out.append((name.split(".", 1)[0], name))
        return sorted(out, key=lambda x: (x[0], _BATCH_RE.match(x[1])["id"], x[1]))

    def read_batch(self, name: str) -> dict | None:
        if not _BATCH_RE.match(name):
            return None
        data = self._read_json(name)
        return data if isinstance(data, dict) else None


def folder_writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)
