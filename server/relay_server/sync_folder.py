"""Folder backend: exchange signed, encrypted batches through a shared directory.

Layout (every file name is unique to the host that writes it, so a syncing
service like OneDrive or Syncthing never has to resolve a file conflict)::

    <folder>/relay-sync.json                 marker {"format": 1}
    <folder>/members/<ms>-<by>-<kind>-<subject>.json   signed membership records
    <folder>/requests/<host_id>.json         self-signed join requests
    <folder>/batches/<host_id>/<last-event-id>.json    encrypted batches

Membership: the first host to set up sync in an empty folder writes a
self-signed ``founder`` record. Every later ``approve`` / ``revoke`` must be
signed by a host that's a member at that point. Each host pins the founder it
first saw, so replacing the founder record later is detected, not trusted.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import sync_crypto as sc
from .workstreams import atomic_write

FORMAT = 1
MARKER = "relay-sync.json"
_HOST_RE = re.compile(r"^[a-z2-7]{6}$")
_BATCH_RE = re.compile(r"^\d{13}-\d{4}-[a-z2-7]{6}(-h[a-z2-7]{6})?\.json$")


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


class FolderError(Exception):
    pass


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _write_new(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=1) + "\n")


class SyncFolder:
    def __init__(self, root: Path | str):
        self.root = Path(root)

    # -- layout -----------------------------------------------------------

    def is_initialized(self) -> bool:
        return (self.root / MARKER).is_file()

    def check(self) -> None:
        if not self.root.is_dir():
            raise FolderError(f"sync folder {self.root} isn't available (unmounted, or not synced yet?)")
        if self.is_initialized():
            marker = _read(self.root / MARKER)
            if not isinstance(marker, dict) or marker.get("format") != FORMAT:
                raise FolderError(f"{self.root / MARKER} is from an unsupported relay version")

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.is_initialized():
            _write_new(self.root / MARKER, {"format": FORMAT, "app": "relay"})

    def unrelated_content(self) -> list[str]:
        """Entries that aren't relay's (a non-empty folder someone else uses)."""
        if not self.root.is_dir():
            return []
        ours = {MARKER, "members", "requests", "batches"}
        return sorted(p.name for p in self.root.iterdir() if p.name not in ours and not p.name.startswith("."))

    # -- membership -------------------------------------------------------

    def membership_records(self) -> list[dict]:
        out = []
        d = self.root / "members"
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                rec = _read(p)
                if isinstance(rec, dict):
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
        _write_new(self.root / "members" / f"{ms:013d}-{by}-{kind}-{subject}.json", signed)
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
        record = {
            "host_id": host_id, "key": key.public_b64, "name": name, "hostname": hostname,
            "at_ms": int(time.time() * 1000),
        }
        _write_new(self.root / "requests" / f"{host_id}.json", sc.sign_record(key, record))

    def requests(self) -> list[dict]:
        out = []
        d = self.root / "requests"
        if d.is_dir():
            for p in sorted(d.glob("*.json")):
                rec = _read(p)
                if (
                    isinstance(rec, dict) and p.stem == rec.get("host_id")
                    and isinstance(rec.get("key"), str) and sc.verify_record(rec, rec["key"])
                ):
                    out.append(rec)
        return out

    def remove_request(self, host_id: str) -> None:
        try:
            (self.root / "requests" / f"{host_id}.json").unlink()
        except FileNotFoundError:
            pass

    # -- batches ----------------------------------------------------------

    def write_batch(self, sender_id: str, envelope: dict, suffix: str = "") -> str:
        name = f"{envelope['last']}{suffix}.json"
        rel = f"batches/{sender_id}/{name}"
        _write_new(self.root / rel, envelope)
        return rel

    def list_batches(self, exclude_host: str) -> list[tuple[str, str]]:
        """[(sender_id, relative path)] for other hosts' batches, oldest first."""
        out = []
        d = self.root / "batches"
        if not d.is_dir():
            return out
        for host_dir in sorted(d.iterdir()):
            if not host_dir.is_dir() or host_dir.name == exclude_host or not _HOST_RE.match(host_dir.name):
                continue
            for p in sorted(host_dir.iterdir()):
                if _BATCH_RE.match(p.name):
                    out.append((host_dir.name, f"batches/{host_dir.name}/{p.name}"))
        return out

    def read_batch(self, rel: str) -> dict | None:
        data = _read(self.root / rel)
        return data if isinstance(data, dict) else None


def folder_writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)
