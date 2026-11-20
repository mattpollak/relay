"""Sync engine: setup (found/join), push, pull, approve, revoke.

Only imported when sync is enabled (or being set up). The folder backend is
the only transport so far; it reads and writes files in a shared directory,
so there are still no network calls from relay itself.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import EVENTS_DB, EventStore, _reconcile, get_hostname
from .sync_apply import apply_remote, event_rows
from .sync_folder import safe_name

BATCH_SIZE = 1000
NOTICES_FILE = "sync-notices.json"
_SERVER_DIR = Path(__file__).resolve().parent.parent


class SyncError(Exception):
    """A sync operation can't run; the message is meant for the user."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Crypto dependency (the optional ``sync`` extra)
# ---------------------------------------------------------------------------


def ensure_crypto():
    """Return the sync_crypto module, installing the ``sync`` extra into
    relay's own environment if it's missing (needs network once, at setup)."""
    from . import sync_crypto

    if sync_crypto.crypto_available():
        return sync_crypto
    if (_SERVER_DIR / "pyproject.toml").exists() and shutil.which("uv"):
        subprocess.run(
            ["uv", "sync", "--directory", str(_SERVER_DIR), "--extra", "sync", "--inexact", "--no-dev", "--quiet"],
            capture_output=True, text=True, timeout=300,
        )
        importlib.invalidate_caches()
        sync_crypto = importlib.reload(sync_crypto)
    if not sync_crypto.crypto_available():
        raise SyncError(
            "relay sync needs the PyNaCl library and couldn't install it. Run: "
            f"`uv sync --directory {_SERVER_DIR} --extra sync --inexact`"
        )
    return sync_crypto


# ---------------------------------------------------------------------------
# Local sync state (in events.db)
# ---------------------------------------------------------------------------


def _ensure_tables(store: EventStore) -> None:
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_seen (path TEXT PRIMARY KEY, status TEXT NOT NULL, at TEXT NOT NULL)"
    )


def _meta_json(store: EventStore, k: str, default: Any = None) -> Any:
    raw = store.get_meta(k)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _set_meta_json(store: EventStore, k: str, v: Any) -> None:
    store.set_meta(k, json.dumps(v, sort_keys=True))


def _reset_folder_state(store: EventStore) -> None:
    for k in ("sync_pin", "sync_accepted", "sync_records", "sync_pushed", "sync_recipients"):
        store.conn.execute("DELETE FROM meta WHERE k = ?", (k,))
    store.conn.execute("DELETE FROM sync_seen")


class _Ctx:
    def __init__(self, data_dir: Path, db_path: Path | None, block: dict):
        from .sync_folder import SyncFolder

        self.sc = ensure_crypto()
        self.data_dir = Path(data_dir)
        self.db_path = db_path
        self.block = block
        if block.get("backend") != "folder":
            raise SyncError(f"the {block.get('backend')} backend isn't available in this version yet")
        self.folder_path = block["folder"]
        self.folder = SyncFolder(self.folder_path)
        try:
            self.folder.check()
        except Exception as exc:
            raise SyncError(str(exc)) from exc
        self.key = self.sc.load_host_key(self.data_dir)
        if self.key is None:
            raise SyncError("this machine has no sync key; run `/relay:sync setup` again")


def _open(data_dir: Path, db_path: Path | None) -> _Ctx:
    from .sync import load_sync_config

    block = load_sync_config(data_dir)
    if block is None:
        raise SyncError("sync isn't set up")
    return _Ctx(data_dir, db_path, block)


def _membership(
    store: EventStore, ctx: Any, *, persist: bool = True, pin_founder: dict | None = None,
) -> dict:
    """Resolve membership. ctx: anything with .folder and .folder_path.

    The founder is never trusted implicitly: it's pinned only when this
    machine founded the group or the user confirmed its fingerprint at setup
    (``pin_founder``). Records this machine accepted are kept locally and
    re-applied, so deleting them from the shared folder changes nothing.
    """
    pin = _meta_json(store, "sync_pin")
    if pin and pin.get("folder") != ctx.folder_path:
        pin = None
    stored = _meta_json(store, "sync_records", []) if pin else []
    accepted = set(_meta_json(store, "sync_accepted", [])) if pin else set()
    mem = ctx.folder.resolve_members(
        {"host_id": pin["host_id"], "key": pin["key"]} if pin else pin_founder, accepted, stored,
    )
    if mem["error"]:
        return mem
    if not pin:
        if pin_founder is None:
            mem["error"] = (
                "this machine hasn't confirmed the sync folder's founder yet; run `/relay:sync setup`"
            )
            return mem
        _set_meta_json(store, "sync_pin", {"folder": ctx.folder_path, **mem["founder"]})
    if persist:
        _set_meta_json(store, "sync_records", mem["accepted_records"])
        _set_meta_json(store, "sync_accepted", sorted(mem["accepted"]))
    return mem


def _role(mem: dict, host_id: str, key_b64: str) -> str:
    if host_id in mem["revoked"]:
        return "revoked"
    member = mem["members"].get(host_id)
    if member is None:
        return "pending"
    return "member" if member["key"] == key_b64 else "key-mismatch"


def _note(store: EventStore, k: str, v: str | None) -> None:
    if v is None:
        store.conn.execute("DELETE FROM meta WHERE k = ?", (k,))
    else:
        store.set_meta(k, v)


# ---------------------------------------------------------------------------
# Setup: found or join a sync folder
# ---------------------------------------------------------------------------


class NeedsConfirmation(Exception):
    """Joining an existing folder: the user must confirm the founder's fingerprint."""

    def __init__(self, info: dict):
        self.info = info
        super().__init__(
            f"this folder already has a sync group, founded by '{info['founder_name']}' "
            f"(fingerprint {info['founder_fingerprint']}). Confirm that fingerprint on that machine "
            "before joining"
        )


def _norm_fp(fp: str) -> str:
    return "".join(ch for ch in fp.lower() if ch.isalnum())


def join_folder(
    data_dir: Path, folder_path: str, display_name: str, trust_fingerprint: str | None = None,
) -> dict:
    """Found a new sync group in an empty folder, or ask to join an existing
    one. Joining requires ``trust_fingerprint`` to equal the founder's key
    fingerprint (shown on the founding machine), otherwise NeedsConfirmation
    is raised and nothing is pinned or written.
    Returns {"role", "founded", "fingerprint", "members": [...]}."""
    from types import SimpleNamespace

    from .sync import SyncConfigError
    from .sync_folder import SyncFolder

    sc = ensure_crypto()
    key = sc.ensure_host_key(data_dir)
    folder = SyncFolder(folder_path)
    try:
        folder.check()
    except Exception as exc:
        raise SyncConfigError(str(exc)) from exc

    with EventStore(data_dir) as store, store.transaction():
        _ensure_tables(store)
        pin = _meta_json(store, "sync_pin")
        if pin and pin.get("folder") != folder_path:
            _reset_folder_state(store)
            pin = None
        host_id = store.host["host_id"]
        founded = False
        pin_founder = None
        if not folder.is_initialized():
            extra = folder.unrelated_content()
            if extra:
                raise SyncConfigError(
                    f"{folder_path} already contains other files ({', '.join(extra[:5])}); "
                    "use an empty folder just for relay"
                )
            folder.initialize()
            folder.write_membership(key, "founder", host_id, host_id, key.public_b64, display_name)
            founded = True
            pin_founder = {"host_id": host_id, "key": key.public_b64}
        elif not pin:
            preview = folder.resolve_members(None)
            if preview["error"]:
                raise SyncConfigError(preview["error"])
            founder = preview["founder"]
            founder_fp = sc.fingerprint(founder["key"])
            if trust_fingerprint is None or _norm_fp(trust_fingerprint) != _norm_fp(founder_fp):
                info = {
                    "founder_name": safe_name(preview["members"].get(founder["host_id"], {}).get("name"), founder["host_id"]),
                    "founder_host_id": founder["host_id"],
                    "founder_fingerprint": founder_fp,
                    "given": trust_fingerprint,
                }
                raise NeedsConfirmation(info)
            pin_founder = founder

        ctx = SimpleNamespace(folder=folder, folder_path=folder_path)
        mem = _membership(store, ctx, pin_founder=pin_founder)
        if mem["error"]:
            raise SyncConfigError(mem["error"])
        role = _role(mem, host_id, key.public_b64)
        if role == "revoked":
            raise SyncConfigError(
                "this machine was revoked from that sync folder. To rejoin, it needs a new identity "
                "(remove host_id from relay.json and sync.key, then run setup again)"
            )
        if role == "key-mismatch":
            raise SyncConfigError(
                "the folder knows this machine with a different key (was sync.key deleted?). "
                "Revoke this machine from another one, then set up again with a new identity"
            )
        if role == "pending":
            folder.write_request(key, host_id, display_name, get_hostname())
        return {
            "role": role,
            "founded": founded,
            "fingerprint": key.fingerprint,
            "founder_fingerprint": sc.fingerprint(mem["founder"]["key"]),
            "members": _member_list(store, mem, sc),
        }


def _member_list(store: EventStore, mem: dict, sc) -> list[dict]:
    names = _host_names(store)
    return [
        {
            "host_id": hid,
            "name": safe_name(names.get(hid) or m.get("name"), hid),
            "fingerprint": sc.fingerprint(m["key"]),
        }
        for hid, m in sorted(mem["members"].items())
    ]


def _host_names(store: EventStore) -> dict[str, str]:
    names = {}
    for key, (_eid, raw) in store.heads().items():
        if key.startswith("host/") and raw:
            info = json.loads(raw)
            names[key[5:]] = info.get("nickname") or info.get("hostname") or key[5:]
    return names


# ---------------------------------------------------------------------------
# Git snapshots
# ---------------------------------------------------------------------------


def _workstream_repos(data_dir: Path) -> dict[str, Path]:
    try:
        registry = json.loads((data_dir / "workstreams.json").read_text())
        entries = registry["workstreams"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}
    out = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        git = entry.get("git") if isinstance(entry.get("git"), dict) else {}
        for candidate in (git.get("worktree_path"), entry.get("project_dir")):
            if isinstance(candidate, str) and candidate and Path(candidate).is_dir():
                out[name] = Path(candidate)
                break
    return out


def record_git_snapshots(store: EventStore, data_dir: Path) -> int:
    """Append a git.snapshot for each workstream repo whose state changed."""
    from .git_snapshot import take_snapshot

    host_id = store.host["host_id"]
    written = 0
    cache: dict[Path, dict | None] = {}
    for ws, repo in sorted(_workstream_repos(data_dir).items()):
        resolved = repo.resolve()
        if resolved not in cache:
            try:
                cache[resolved] = take_snapshot(resolved)
            except Exception:
                cache[resolved] = None
        snap = cache[resolved]
        if snap is None:
            continue
        key = f"git/{host_id}/{ws}"
        previous = store.head_value(key) or {}
        if {k: v for k, v in previous.items() if k != "taken_at"} == snap:
            continue
        store.append("git.snapshot", key, {"value": {**snap, "taken_at": _now()}}, source="sync")
        written += 1
    return written


# ---------------------------------------------------------------------------
# Push / pull
# ---------------------------------------------------------------------------


def _wire(row: dict) -> dict:
    return {k: row[k] for k in ("id", "host_id", "type", "schema", "key", "parent", "wall_ts", "source", "payload")}


def push(data_dir: Path, db_path: Path | None) -> dict:
    ctx = _open(data_dir, db_path)
    with EventStore(data_dir) as store, store.transaction():
        _ensure_tables(store)
        me = store.host["host_id"]
        mem = _membership(store, ctx)
        if mem["error"]:
            _note(store, "sync_last_error", mem["error"])
            return {"ok": False, "error": mem["error"]}
        role = _role(mem, me, ctx.key.public_b64)
        if role != "member":
            # Never send anything before this machine is approved: until then
            # it has no proof the folder's members are the user's machines.
            reason = "waiting for approval" if role == "pending" else role
            return {"ok": True, "role": role, "batches": 0, "events": 0, "git_snapshots": 0,
                    "history_for": [], "skipped": reason}

        _reconcile(store, ctx.data_dir, db_path, source="sync")
        snapshots = record_git_snapshots(store, ctx.data_dir)

        recipients = {hid: m["key"] for hid, m in mem["members"].items()}
        previous = set(_meta_json(store, "sync_recipients", []))
        added = sorted(set(recipients) - previous - {me})
        cursor = store.get_meta("sync_pushed") or ""
        own_new = event_rows(store, "WHERE host_id = ? AND id > ?", (me, cursor))
        if added:
            # A machine joined: send it the whole history (minus what it wrote
            # itself), encrypted to everyone.
            placeholders = ",".join("?" * len(added))
            events = event_rows(store, f"WHERE host_id NOT IN ({placeholders})", tuple(added))
            suffix = f"-h{added[0]}"
        else:
            events, suffix = own_new, ""
        written = []
        for i in range(0, len(events), BATCH_SIZE):
            chunk = [_wire(e) for e in events[i : i + BATCH_SIZE]]
            envelope = ctx.sc.seal_batch(ctx.key, me, chunk, recipients, created=_now())
            written.append(ctx.folder.write_batch(me, envelope, suffix if i == 0 else f"{suffix}-{i}"))
        if own_new:
            store.set_meta("sync_pushed", own_new[-1]["id"])
        _set_meta_json(store, "sync_recipients", sorted(recipients))
        store.set_meta("sync_last_push", _now())
        _note(store, "sync_last_error", None)
        return {
            "ok": True, "role": role, "batches": len(written), "events": len(events),
            "git_snapshots": snapshots, "history_for": added,
        }


def pull(data_dir: Path, db_path: Path | None) -> dict:
    ctx = _open(data_dir, db_path)
    with EventStore(data_dir) as store, store.transaction():
        _ensure_tables(store)
        me = store.host["host_id"]
        mem = _membership(store, ctx)
        if mem["error"]:
            _note(store, "sync_last_error", mem["error"])
            return {"ok": False, "error": mem["error"]}
        role = _role(mem, me, ctx.key.public_b64)
        if role in ("revoked", "key-mismatch"):
            return {"ok": False, "role": role, "error": f"this machine is {role}; not pulling"}

        _reconcile(store, ctx.data_dir, db_path, source="sync")
        seen = {r[0] for r in store.conn.execute("SELECT path FROM sync_seen")}
        stats = {"batches": 0, "waiting": 0, "incomplete": 0, "rejected": 0, "not_for_me": 0}
        collected: list[dict] = []

        def mark(rel: str, status: str) -> None:
            store.conn.execute(
                "INSERT OR REPLACE INTO sync_seen (path, status, at) VALUES (?, ?, ?)", (rel, status, _now())
            )

        for sender, rel in ctx.folder.list_batches(exclude_host=me):
            if rel in seen:
                continue
            member = mem["members"].get(sender)
            if member is None:
                stats["waiting"] += 1  # not approved (yet): leave it for later
                continue
            envelope = ctx.folder.read_batch(rel)
            if envelope is None:
                stats["incomplete"] += 1  # still syncing in; retry next time
                continue
            if envelope.get("from") != sender or not ctx.sc.verify_batch(envelope, member["key"]):
                mark(rel, "rejected")
                stats["rejected"] += 1
                continue
            try:
                events = ctx.sc.open_batch(envelope, ctx.key, me)
            except ctx.sc.SyncCryptoError:
                mark(rel, "rejected")
                stats["rejected"] += 1
                continue
            if events is None:
                mark(rel, "not-for-me")
                stats["not_for_me"] += 1
                continue
            collected.extend(events)
            mark(rel, "ok")
            stats["batches"] += 1

        applied = apply_remote(store, ctx.data_dir, db_path, collected)
        store.set_meta("sync_last_pull", _now())
        _note(store, "sync_last_error", None)
    result = {"ok": True, "role": role, **stats, **applied}
    write_notices(Path(data_dir))
    return result


def sync_now(data_dir: Path, db_path: Path | None) -> dict:
    pulled = pull(data_dir, db_path)
    pushed = push(data_dir, db_path) if pulled.get("ok") else {"ok": False, "skipped": True}
    write_notices(Path(data_dir))
    return {"pull": pulled, "push": pushed}


# ---------------------------------------------------------------------------
# Approve / revoke
# ---------------------------------------------------------------------------


def _match(candidates: list[dict], who: str) -> list[dict]:
    w = who.strip().lower()
    exact = [c for c in candidates if w in (c["host_id"], str(c.get("name", "")).lower())]
    if exact:
        return exact
    return [c for c in candidates if c.get("fingerprint", "").replace("-", "").startswith(w.replace("-", "")) and len(w) >= 4]


def pending_requests(store: EventStore, ctx: _Ctx, mem: dict) -> list[dict]:
    out = []
    for r in ctx.folder.requests():
        hid = r["host_id"]
        if hid in mem["members"] or hid in mem["revoked"]:
            continue
        out.append({
            "host_id": hid, "name": safe_name(r.get("name"), hid), "hostname": safe_name(r.get("hostname"), "?"),
            "key": r["key"], "fingerprint": ctx.sc.fingerprint(r["key"]),
        })
    return out


def approve(data_dir: Path, db_path: Path | None, who: str) -> dict:
    ctx = _open(data_dir, db_path)
    with EventStore(data_dir) as store, store.transaction():
        _ensure_tables(store)
        me = store.host["host_id"]
        mem = _membership(store, ctx)
        if mem["error"]:
            raise SyncError(mem["error"])
        if _role(mem, me, ctx.key.public_b64) != "member":
            raise SyncError("only an approved machine can approve others")
        requests = pending_requests(store, ctx, mem)
        if not requests:
            raise SyncError("no machines are waiting to join")
        matches = _match(requests, who)
        if len(matches) != 1:
            names = ", ".join(f"{r['name']} ({r['host_id']}, {r['fingerprint']})" for r in requests)
            raise SyncError(f"{'no' if not matches else 'more than one'} waiting machine matches {who!r}; waiting: {names}")
        req = matches[0]
        ctx.folder.write_membership(ctx.key, "approve", me, req["host_id"], req["key"], req["name"])
        ctx.folder.remove_request(req["host_id"])
        _membership(store, ctx)  # accept the new record
    pushed = push(data_dir, db_path)  # sends the full history to the new machine
    return {"approved": req, "push": pushed}


def revoke(data_dir: Path, db_path: Path | None, who: str) -> dict:
    ctx = _open(data_dir, db_path)
    with EventStore(data_dir) as store, store.transaction():
        _ensure_tables(store)
        me = store.host["host_id"]
        mem = _membership(store, ctx)
        if mem["error"]:
            raise SyncError(mem["error"])
        if _role(mem, me, ctx.key.public_b64) != "member":
            raise SyncError("only an approved machine can revoke others")
        candidates = [m for m in _member_list(store, mem, ctx.sc) if m["host_id"] != me]
        candidates += pending_requests(store, ctx, mem)
        matches = _match(candidates, who)
        if len(matches) != 1:
            names = ", ".join(f"{c['name']} ({c['host_id']})" for c in candidates) or "none"
            raise SyncError(f"{'no' if not matches else 'more than one'} other machine matches {who!r}; machines: {names}")
        target = matches[0]
        ctx.folder.write_membership(ctx.key, "revoke", me, target["host_id"], None, target["name"])
        ctx.folder.remove_request(target["host_id"])
        _membership(store, ctx)
    return {"revoked": target}


# ---------------------------------------------------------------------------
# Status details + notices
# ---------------------------------------------------------------------------


def details(data_dir: Path) -> dict:
    """Membership, requests, timestamps, conflicts and notices for status.
    Read-mostly: doesn't record anything new."""
    ctx = _open(data_dir, None)
    with EventStore(data_dir) as store:
        _ensure_tables(store)
        me = store.host["host_id"] if store.host else _own_id(data_dir)
        mem = _membership(store, ctx, persist=False)
        out: dict[str, Any] = {
            "fingerprint": ctx.key.fingerprint,
            "error": mem["error"] or store.get_meta("sync_last_error"),
            "last_push": store.get_meta("sync_last_push"),
            "last_pull": store.get_meta("sync_last_pull"),
        }
        if mem["error"]:
            return out
        out["role"] = _role(mem, me, ctx.key.public_b64)
        founder = mem["founder"]
        out["founder"] = {
            "host_id": founder["host_id"],
            "name": safe_name(_host_names(store).get(founder["host_id"]), founder["host_id"]),
            "fingerprint": ctx.sc.fingerprint(founder["key"]),
        }
        if mem.get("missing"):
            out["error"] = (
                f"{mem['missing']} membership record(s) this machine accepted are missing from the shared "
                "folder (deleted?). They still apply here."
            )
        out["members"] = _member_list(store, mem, ctx.sc)
        out["requests"] = pending_requests(store, ctx, mem) if out["role"] == "member" else []
    out["conflicts"] = conflict_files(Path(data_dir))
    out["notices"] = compute_notices(Path(data_dir))
    return out


def _own_id(data_dir: Path) -> str | None:
    try:
        return json.loads((Path(data_dir) / "relay.json").read_text()).get("host_id")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def conflict_files(data_dir: Path, ws: str | None = None) -> list[str]:
    root = data_dir / "workstreams"
    if not root.is_dir():
        return []
    pattern = f"{ws}/*.conflict-*.md" if ws else "*/*.conflict-*.md"
    return sorted(str(p) for p in root.glob(pattern))


def compute_notices(data_dir: Path, only_ws: str | None = None) -> dict[str, dict]:
    """{workstream: {"lines": [...], "fetch": bool}} for other machines'
    git snapshots that show work not visible here. Local git commands only."""
    from .git_snapshot import notices_for

    db = data_dir / EVENTS_DB
    if not db.exists():
        return {}
    own = _own_id(data_dir)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT key, value FROM heads WHERE (key LIKE 'git/%' OR key LIKE 'host/%') AND value IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    names = {}
    snaps = []
    for key, raw in rows:
        value = json.loads(raw)
        if key.startswith("host/"):
            names[key[5:]] = safe_name(value.get("nickname") or value.get("hostname"), key[5:])
        else:
            _, host_id, ws = key.split("/", 2)
            if host_id != own and (only_ws is None or ws == only_ws):
                snaps.append((host_id, ws, value))
    repos = _workstream_repos(data_dir)
    out: dict[str, dict] = {}
    for host_id, ws, snap in sorted(snaps, key=lambda s: (s[1], s[0])):
        local = repos.get(ws)
        try:
            lines, fetch = notices_for(snap, names.get(host_id, host_id), local)
        except Exception:
            continue
        if lines:
            entry = out.setdefault(ws, {"lines": [], "fetch": False})
            entry["lines"] += lines
            entry["fetch"] = entry["fetch"] or fetch
    return out


def write_notices(data_dir: Path) -> None:
    """Write sync-notices.json for the SessionStart hook (read with jq)."""
    from .git_snapshot import FETCH_HINT
    from .workstreams import atomic_write

    try:
        info = details(data_dir)
    except Exception as exc:  # never fail a pull over notices
        info = {"error": str(exc)}
    workstreams = {}
    for ws, n in (info.get("notices") or {}).items():
        workstreams[ws] = n["lines"] + ([FETCH_HINT] if n["fetch"] else [])
    requests = [
        f"relay: new machine '{r['name']}' (fingerprint {r['fingerprint']}) wants to join sync. "
        f"If it's yours, approve it with `/relay:sync approve {r['name']}`."
        for r in info.get("requests") or []
    ]
    payload = {
        "generated": _now(),
        "workstreams": workstreams,
        "requests": requests,
        "role": info.get("role"),
        "error": info.get("error"),
    }
    atomic_write(data_dir / NOTICES_FILE, json.dumps(payload, indent=1) + "\n")
