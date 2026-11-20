"""Apply events from other machines to the local log and the files on disk.

Called by sync pull inside ``EventStore.transaction()`` (the cross-process
write lock), after local edits have been reconciled into the log, so nothing
unrecorded can be overwritten.

Merge rule: every key's value is the fold of all its events in event-ID
(hybrid logical clock) order, so all machines with the same events agree.
For whole-document keys (``state/``, ``file/``) two edits made from the same
version are a conflict: the newer one wins and the other version is written
next to it as ``<name>.conflict-<host>-<time>.md`` so nothing is lost.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import (
    SCHEMA_VERSION,
    TRACKED_FILES,
    WS_NAME_RE,
    _TYPES,
    _UPDATE_TYPES,
    EventStore,
    _canon,
    _read_json,
    apply_event,
    join_ws_entry,
    restore_item,
    split_ws_entry,
)
from .workstreams import atomic_write

EVENT_ID_RE = re.compile(r"^\d{13}-\d{4}-[a-z2-7]{6}$")
_KNOWN_TYPES = (
    {t for pair in _TYPES.values() for t in pair if t} | set(_UPDATE_TYPES.values())
)
_CONFLICT_PREFIXES = ("state/", "file/")
_MAX_KEY = 512
_MAX_ANCESTRY = 100_000


def _valid_event(ev: Any) -> bool:
    if not isinstance(ev, dict):
        return False
    eid = ev.get("id")
    if not isinstance(eid, str) or not EVENT_ID_RE.match(eid):
        return False
    if ev.get("host_id") != eid.rsplit("-", 1)[1]:
        return False
    parent = ev.get("parent")
    if parent is not None and (not isinstance(parent, str) or not EVENT_ID_RE.match(parent)):
        return False
    key = ev.get("key")
    return (
        isinstance(ev.get("type"), str) and len(ev["type"]) <= 64
        and isinstance(key, str) and 0 < len(key) <= _MAX_KEY and "\0" not in key
        and isinstance(ev.get("schema"), int)
        and isinstance(ev.get("payload"), dict)
        and isinstance(ev.get("wall_ts"), str)
        and (ev.get("source") is None or isinstance(ev.get("source"), str))
    )


def _foldable(event_type: str, schema: int) -> bool:
    """Unknown types or newer schemas are stored but not applied (version skew)."""
    return event_type in _KNOWN_TYPES and schema <= SCHEMA_VERSION


def event_rows(store: EventStore, where: str = "", params: tuple = ()) -> list[dict]:
    rows = store.conn.execute(f"SELECT * FROM events {where} ORDER BY id", params).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


# ---------------------------------------------------------------------------
# Ingest + refold
# ---------------------------------------------------------------------------


# Events stamped further ahead than this are refused: observing them would drag
# this machine's clock forward and let them win every later conflict.
MAX_FUTURE_MS = 24 * 3600 * 1000


def ingest(store: EventStore, events: list[dict], rejected: list[str] | None = None) -> list[dict]:
    """Insert valid events not already in the log. Returns those inserted;
    IDs of invalid or far-future events are added to ``rejected``."""
    import time

    limit = int(time.time() * 1000) + MAX_FUTURE_MS
    inserted = []
    valid = []
    for e in events:
        if _valid_event(e) and int(e["id"][:13]) <= limit:
            valid.append(e)
        elif rejected is not None:
            rejected.append(str(e.get("id")) if isinstance(e, dict) else "?")
    for ev in sorted(valid, key=lambda e: e["id"]):
        cur = store.conn.execute(
            """INSERT OR IGNORE INTO events (id, host_id, type, schema, key, parent, wall_ts, source, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ev["id"], ev["host_id"], ev["type"], ev["schema"], ev["key"], ev.get("parent"),
                ev["wall_ts"], ev.get("source"), _canon(ev["payload"]),
            ),
        )
        if cur.rowcount:
            store.observe(ev["id"])
            inserted.append(ev)
    return inserted


def refold(store: EventStore, key: str) -> tuple[str | None, Any]:
    """Recompute a key's head from all its events; returns (event_id, value)."""
    value: Any = None
    last: str | None = None
    for r in store.conn.execute(
        # same order as events.fold_order: import snapshots first, then by ID
        "SELECT id, type, schema, payload FROM events WHERE key = ? "
        "ORDER BY CASE WHEN source = 'import' THEN 0 ELSE 1 END, id", (key,)
    ):
        if _foldable(r["type"], r["schema"]):
            value = apply_event(value, r["type"], json.loads(r["payload"]))
            last = r["id"]
    if last is None:
        store.conn.execute("DELETE FROM heads WHERE key = ?", (key,))
    else:
        store.conn.execute(
            """INSERT INTO heads (key, event_id, value) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET event_id = excluded.event_id, value = excluded.value""",
            (key, last, None if value is None else _canon(value)),
        )
    return last, value


def is_ancestor(store: EventStore, ancestor: str, descendant: str) -> bool:
    cur: str | None = descendant
    for _ in range(_MAX_ANCESTRY):
        if cur is None:
            return False
        if cur == ancestor:
            return True
        row = store.conn.execute("SELECT parent FROM events WHERE id = ?", (cur,)).fetchone()
        cur = row["parent"] if row else None
    return False


def _event_value(store: EventStore, event_id: str) -> Any:
    row = store.conn.execute("SELECT type, payload FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    return apply_event(None, row["type"], json.loads(row["payload"]))


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_remote(
    store: EventStore, data_dir: Path, db_path: Path | None, events: list[dict],
) -> dict:
    """Ingest remote events, refold affected keys, detect conflicts and write
    the changed views. Call inside ``store.transaction()``."""
    data_dir = Path(data_dir)
    before = store.heads()
    rejected: list[str] = []
    inserted = ingest(store, events, rejected)
    by_key: dict[str, list[dict]] = {}
    for ev in inserted:
        by_key.setdefault(ev["key"], []).append(ev)

    changes: dict[str, Any] = {}
    conflicts: list[dict] = []

    # Redactions from other machines: swap in the scrubbed payloads (pure
    # removals only) before anything is written out.
    redactions = [e for e in inserted if e["type"] == "msg.redact"]
    redacted_messages = 0
    text_pairs: list[tuple[str, str]] = []
    if redactions:
        from .redact import add_fingerprints, apply_message_changes, replace_event_payloads

        for ev in redactions:
            value = ev["payload"].get("value") or {}
            if isinstance(value.get("fingerprints"), list):
                add_fingerprints(store, value["fingerprints"])
                store.conn.execute("DELETE FROM meta WHERE k = 'redact_scan_id'")  # rescan everything
            for key in replace_event_payloads(store, value.get("events") or {}, text_pairs):
                old_raw = before.get(key, (None, None))[1]
                new_value = store.head_value(key)
                if _canon(json.loads(old_raw) if old_raw else None) != _canon(new_value):
                    changes[key] = new_value
            if db_path is not None and Path(db_path).exists():
                from .db import get_connection

                conn = get_connection(db_path)
                try:
                    redacted_messages += apply_message_changes(conn, value.get("messages") or [])
                    conn.commit()
                finally:
                    conn.close()

    for key, evs in sorted(by_key.items()):
        old_id, old_raw = before.get(key, (None, None))
        old_value = json.loads(old_raw) if old_raw is not None else None
        new_id, new_value = refold(store, key)
        if key.startswith(_CONFLICT_PREFIXES) and old_id is not None:
            newest_remote = max(e["id"] for e in evs)
            conflict = _check_conflict(store, key, old_id, old_value, new_id, new_value, newest_remote)
            if conflict:
                conflicts.append(conflict)
        if _canon(old_value) != _canon(new_value):
            changes[key] = new_value

    host_id = store.host["host_id"] if store.host else None
    written = materialize(store, data_dir, db_path, changes, host_id)
    if text_pairs:
        from .redact import scrub_side_copies

        scrub_side_copies(data_dir, text_pairs)  # after materialize, which may have made a .bak
    if redactions and db_path is not None and Path(db_path).exists():
        # The same secret may also be in this machine's own transcripts.
        from .db import get_connection
        from .redact import scrub_new_messages

        conn = get_connection(db_path)
        try:
            redacted_messages += scrub_new_messages(store, conn)
            conn.commit()
        finally:
            conn.close()
    conflict_files = [_write_conflict(store, data_dir, c) for c in conflicts]
    return {
        "received": len(events),
        "inserted": len(inserted),
        "redactions": len(redactions),
        "redacted_messages": redacted_messages,
        "changed_keys": sorted(changes),
        "conflicts": [c for c in conflict_files if c],
        "errors": written["errors"] + (
            [f"ignored {len(rejected)} malformed or future-dated events (check that machine's clock)"]
            if rejected else []
        ),
    }


def _check_conflict(
    store: EventStore, key: str, old_id: str, old_value: Any,
    new_id: str | None, new_value: Any, newest_remote: str,
) -> dict | None:
    if new_id == old_id:
        # The local version is still newest. Conflict only if the remote edit
        # wasn't something the local version was already based on.
        if is_ancestor(store, newest_remote, old_id):
            return None
        loser_id, loser_value = newest_remote, _event_value(store, newest_remote)
    else:
        if new_id is None or is_ancestor(store, old_id, new_id):
            return None  # fast-forward
        loser_id, loser_value = old_id, old_value
    winner_value = new_value
    if not isinstance(loser_value, str) or not loser_value.strip() or loser_value == winner_value:
        return None
    return {"key": key, "event_id": loser_id, "value": loser_value}


def _host_label(store: EventStore, host_id: str) -> str:
    info = store.head_value(f"host/{host_id}") or {}
    name = (info.get("nickname") or info.get("hostname") or host_id) if isinstance(info, dict) else host_id
    label = re.sub(r"[^a-z0-9-]+", "-", str(name).lower()).strip("-")
    return label or host_id


def _write_conflict(store: EventStore, data_dir: Path, conflict: dict) -> str | None:
    key = conflict["key"]
    if key.startswith("state/"):
        ws, fname = key[len("state/"):], "state.md"
    else:
        ws, _, fname = key[len("file/"):].partition("/")
    if not WS_NAME_RE.match(ws) or fname not in ("state.md", *TRACKED_FILES):
        return None
    eid = conflict["event_id"]
    host_id = eid.rsplit("-", 1)[1]
    when = datetime.fromtimestamp(int(eid[:13]) / 1000, timezone.utc)
    stem = fname[: -len(".md")]
    path = data_dir / "workstreams" / ws / f"{stem}.conflict-{_host_label(store, host_id)}-{when:%Y%m%dT%H%M%SZ}.md"
    header = (
        f"<!-- relay: conflicting version of {fname} from {_host_label(store, host_id)} "
        f"({when:%Y-%m-%d %H:%M} UTC, event {eid}). Both machines edited {fname} from the same version; "
        f"the newer edit is in {fname}. Merge anything you need from here into {fname}, then delete this file. -->\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, header + conflict["value"])
    return str(path)


# ---------------------------------------------------------------------------
# Materialize views
# ---------------------------------------------------------------------------


def materialize(
    store: EventStore, data_dir: Path, db_path: Path | None, changes: dict[str, Any], host_id: str | None,
) -> dict:
    """Write changed keys to the registry, files, ideas.json and index.db."""
    errors: list[str] = []
    ws_changes = {k[3:]: v for k, v in changes.items() if k.startswith("ws/")}
    idea_changes = {k[5:]: v for k, v in changes.items() if k.startswith("idea/")}
    db_changes = {k: v for k, v in changes.items() if k.startswith(("msgb/", "hint/", "stag/", "mtag/"))}

    if ws_changes:
        errors += _write_registry(store, data_dir, ws_changes, host_id)
    for key, value in sorted(changes.items()):
        if key.startswith("state/"):
            errors += _write_ws_file(data_dir, key[len("state/"):], "state.md", value)
        elif key.startswith("file/"):
            ws, _, fname = key[len("file/"):].partition("/")
            errors += _write_ws_file(data_dir, ws, fname, value)
    if idea_changes:
        errors += _write_ideas(data_dir, idea_changes)
    if db_changes and db_path is not None and Path(db_path).exists():
        from .db import get_connection

        heads = store.heads()
        conn = get_connection(db_path)
        try:
            # Message batches first, so hints and tags find their sessions.
            for key, value in sorted(db_changes.items(), key=lambda kv: (not kv[0].startswith("msgb/"), kv[0])):
                if value is None:
                    continue
                origin = heads.get(key, ("",))[0].rsplit("-", 1)[-1]
                if host_id and origin == host_id:
                    continue  # our own items, echoed back in someone's history batch
                restore_item(conn, key, value, create_session=True, origin_host=origin)
            conn.commit()
        except sqlite3.Error as exc:
            errors.append(f"index.db: {exc}")
        finally:
            conn.close()
    return {"errors": errors}


def _write_registry(store: EventStore, data_dir: Path, ws_changes: dict[str, Any], host_id: str | None) -> list[str]:
    path = data_dir / "workstreams.json"
    ok, registry = _read_json(path)
    if not ok or (registry is not None and not isinstance((registry or {}).get("workstreams"), dict)):
        return [f"{path} can't be parsed; workstream changes from other machines not applied"]
    registry = registry or {"version": 1, "workstreams": {}}
    entries = registry["workstreams"]
    for name, shared in sorted(ws_changes.items()):
        if not WS_NAME_RE.match(name):
            continue
        if shared is None:
            entries.pop(name, None)
            continue
        if not isinstance(shared, dict):
            continue
        shared, _ = split_ws_entry(shared)  # events from before v0.17 included host-local fields
        current = entries.get(name)
        if isinstance(current, dict):
            _, local = split_ws_entry(current)
        else:
            local = _borrow_project_dir(store, name, host_id)
        entries[name] = join_ws_entry(shared, local)
    atomic_write(path, json.dumps(registry, indent=2) + "\n")
    return []


def _borrow_project_dir(store: EventStore, name: str, host_id: str | None) -> dict:
    """A workstream new to this machine: use another machine's project_dir if
    the same path exists here (same layout on both machines is common)."""
    for key, (_eid, raw) in sorted(store.heads().items()):
        if not key.startswith("wsdir/") or not key.endswith(f"/{name}") or raw is None:
            continue
        if host_id and key == f"wsdir/{host_id}/{name}":
            continue
        project_dir = json.loads(raw).get("project_dir")
        if isinstance(project_dir, str) and project_dir and Path(project_dir).is_dir():
            return {"project_dir": project_dir}
    return {}


def _write_ws_file(data_dir: Path, ws: str, fname: str, value: Any) -> list[str]:
    if not WS_NAME_RE.match(ws) or fname not in ("state.md", *TRACKED_FILES):
        return [f"ignored change to unexpected path {ws}/{fname}"]
    path = data_dir / "workstreams" / ws / fname
    if value is None:
        if path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = data_dir / "backups" / f"sync-deleted-{stamp}" / ws / fname
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), dest)
        return []
    if not isinstance(value, str):
        return [f"ignored non-text value for {ws}/{fname}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = path.read_text(errors="replace")
        if old == value:
            return []
        if fname == "state.md":
            atomic_write(path.with_name("state.md.bak"), old)
    atomic_write(path, value)
    return []


def _write_ideas(data_dir: Path, idea_changes: dict[str, Any]) -> list[str]:
    path = data_dir / "ideas.json"
    ok, ideas = _read_json(path)
    if not ok or (ideas is not None and not isinstance(ideas, list)):
        return [f"{path} can't be parsed; ideas from other machines not applied"]
    ideas = [i for i in (ideas or []) if isinstance(i, dict)]
    by_uid = {i.get("uid"): i for i in ideas if i.get("uid")}
    next_id = max((i["id"] for i in ideas if isinstance(i.get("id"), int)), default=0) + 1
    removed = set()
    for uid, value in sorted(idea_changes.items()):
        if value is None:
            removed.add(uid)
            continue
        if not isinstance(value, dict):
            continue
        value = {k: v for k, v in value.items() if k != "id"}  # numbers are per machine
        if uid in by_uid:
            local_id = by_uid[uid].get("id")
            by_uid[uid].clear()
            by_uid[uid].update({"id": local_id, **value, "uid": uid})
        else:
            idea = {"id": next_id, **value, "uid": uid}
            next_id += 1
            ideas.append(idea)
            by_uid[uid] = idea
    ideas = [i for i in ideas if i.get("uid") not in removed]
    atomic_write(path, json.dumps(ideas, indent=2) + "\n")
    return []
