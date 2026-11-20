"""Local event log for relay's authored data.

Every change to data a person (or Claude) authored is recorded as an immutable
event in ``<data_dir>/events.db``: workstream registry entries, state.md,
plan.md / architecture.md, ideas, session hints, and manual tags. The files on
disk stay the source that hooks and tools read; the log is a record of how they
got there, and later the unit that multi-machine sync exchanges.

Event IDs are hybrid-logical-clock values ``<unix-ms>-<counter>-<host-id>``:
they sort by time, are monotonic per host even if the wall clock steps back,
and can't collide across hosts.

Changes are captured by *reconciling*: compare what's on disk (and the
authored rows in index.db) against the log's current value for each key, and
append an event for every difference. That covers relay's own tools and edits
made outside relay (an editor, ``sed``) with one code path.

Recording must never break the operation that triggered it, so callers wrap
:func:`record_changes` and log failures instead of raising.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workstreams import atomic_write, new_idea_uid

EVENTS_DB = "events.db"
SCHEMA_VERSION = 1

# Companion files tracked per workstream (alongside state.md).
TRACKED_FILES = ("plan.md", "architecture.md")

_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"
_HOST_ID_RE = re.compile(r"^[a-z2-7]{6}$")
_MAX_COUNTER = 9999

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    type TEXT NOT NULL,
    schema INTEGER NOT NULL,
    key TEXT NOT NULL,
    parent TEXT,
    wall_ts TEXT NOT NULL,
    source TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_key ON events(key, id);

-- Current value per key, folded from events. value NULL = deleted (tombstone).
CREATE TABLE IF NOT EXISTS heads (
    key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    value TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

# key prefix -> (event type when the key is new/changed, event type on delete)
_TYPES = {
    "ws": ("ws.create", "ws.delete"),
    "state": ("state.save", "state.delete"),
    "file": ("ws.file", "ws.file.delete"),
    "idea": ("idea.add", "idea.delete"),
    "hint": ("hint.write", None),
    "stag": ("tag.session", None),
    "mtag": ("tag.message", None),
    "host": ("host.info", None),
}
_UPDATE_TYPES = {"ws": "ws.update", "idea": "idea.update"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _canon(value: Any) -> str:
    """Canonical JSON so equal values compare equal as strings."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _relay_version() -> str:
    try:
        from importlib.metadata import version

        return version("relay-server")
    except Exception:
        return "unknown"


def get_hostname() -> str:
    """Short hostname of this machine (domain stripped)."""
    name = socket.gethostname() or platform.node() or "unknown"
    return name.split(".")[0] or "unknown"


def generate_host_id() -> str:
    return "".join(secrets.choice(_BASE32) for _ in range(6))


def _read_json(path: Path) -> tuple[bool, Any]:
    """Return (ok, data). ok=False if the file exists but can't be parsed.

    A missing file is ok=True, data=None, so callers can tell "absent" from
    "unreadable" and never infer deletions from a corrupt file.
    """
    if not path.exists():
        return True, None
    try:
        return True, json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False, None


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------


def ensure_host_identity(data_dir: Path) -> dict:
    """Return this host's identity, creating a stable host_id on first use.

    host_id is random and never changes; it's what goes into event IDs. The
    display name is the nickname from relay.json if set, else the hostname.
    Other keys in relay.json are preserved.
    """
    config_path = data_dir / "relay.json"
    ok, config = _read_json(config_path)
    if not ok:
        raise RuntimeError(f"{config_path} is not valid JSON; not adding host_id")
    if not isinstance(config, dict):
        config = {}

    hostname = get_hostname()
    host_id = config.get("host_id")
    if not isinstance(host_id, str) or not _HOST_ID_RE.match(host_id):
        host_id = generate_host_id()
        config["host_id"] = host_id
        config["host_id_hostname"] = hostname
        atomic_write(config_path, json.dumps(config, indent=2) + "\n")

    nickname = config.get("nickname") or ""
    return {
        "host_id": host_id,
        "hostname": hostname,
        "nickname": nickname,
        "display_name": nickname or hostname,
    }


# ---------------------------------------------------------------------------
# Event store
# ---------------------------------------------------------------------------


def apply_event(current: Any, event_type: str, payload: dict) -> Any:
    """Fold one event onto the current value of its key. None = deleted."""
    if event_type.endswith(".delete"):
        return None
    if event_type in _UPDATE_TYPES.values():
        base = dict(current) if isinstance(current, dict) else {}
        base.update(payload.get("set", {}))
        for field in payload.get("unset", []):
            base.pop(field, None)
        return base
    return payload.get("value")


class EventStore:
    """Append-only event log in ``<data_dir>/events.db``.

    Use :meth:`transaction` for writes: it takes SQLite's write lock
    (BEGIN IMMEDIATE), which serializes relay processes (one MCP server per
    Claude session) so HLC values and heads stay consistent.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / EVENTS_DB
        self.conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.executescript(_SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.host: dict | None = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator["EventStore"]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.host is None:
                # Under the write lock, so two processes can't mint two IDs.
                self.host = ensure_host_identity(self.data_dir)
            yield self
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # -- meta ---------------------------------------------------------------

    def get_meta(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, k: str, v: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )

    # -- HLC ----------------------------------------------------------------

    def next_id(self, wall_ms: int | None = None) -> str:
        """Next hybrid-logical-clock event ID. Call inside a transaction."""
        assert self.host is not None, "next_id() must be called inside transaction()"
        if wall_ms is None:
            wall_ms = int(time.time() * 1000)
        last = self.get_meta("hlc")
        last_ms, last_c = (int(x) for x in last.split(":")) if last else (0, -1)
        if wall_ms > last_ms:
            ms, c = wall_ms, 0
        else:
            ms, c = last_ms, last_c + 1
            if c > _MAX_COUNTER:
                ms, c = ms + 1, 0
        self.set_meta("hlc", f"{ms}:{c}")
        return f"{ms:013d}-{c:04d}-{self.host['host_id']}"

    def observe(self, event_id: str) -> None:
        """Advance the local clock past a remote event ID (for sync)."""
        ms_s, c_s, _host = event_id.split("-", 2)
        last = self.get_meta("hlc")
        last_ms, last_c = (int(x) for x in last.split(":")) if last else (0, -1)
        if (int(ms_s), int(c_s)) > (last_ms, last_c):
            self.set_meta("hlc", f"{int(ms_s)}:{int(c_s)}")

    # -- events -------------------------------------------------------------

    def heads(self) -> dict[str, tuple[str, str | None]]:
        rows = self.conn.execute("SELECT key, event_id, value FROM heads").fetchall()
        return {r["key"]: (r["event_id"], r["value"]) for r in rows}

    def head_value(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM heads WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row and row["value"] is not None else None

    def append(
        self,
        event_type: str,
        key: str,
        payload: dict,
        *,
        source: str | None = None,
    ) -> str:
        """Append an event and update the key's head. Call inside a transaction."""
        row = self.conn.execute(
            "SELECT event_id, value FROM heads WHERE key = ?", (key,)
        ).fetchone()
        parent = row["event_id"] if row else None
        current = json.loads(row["value"]) if row and row["value"] is not None else None
        new_value = apply_event(current, event_type, payload)

        event_id = self.next_id()
        self.conn.execute(
            """INSERT INTO events (id, host_id, type, schema, key, parent, wall_ts, source, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id, self.host["host_id"], event_type, SCHEMA_VERSION, key,
                parent, _utc_now(), source, _canon(payload),
            ),
        )
        self.conn.execute(
            """INSERT INTO heads (key, event_id, value) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET event_id = excluded.event_id, value = excluded.value""",
            (key, event_id, None if new_value is None else _canon(new_value)),
        )
        return event_id

    def events(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events ORDER BY id").fetchall()

    def fold(self) -> dict[str, tuple[str, str | None]]:
        """Rebuild heads from the full log (for verification)."""
        values: dict[str, Any] = {}
        last: dict[str, str] = {}
        for ev in self.events():
            values[ev["key"]] = apply_event(values.get(ev["key"]), ev["type"], json.loads(ev["payload"]))
            last[ev["key"]] = ev["id"]
        return {
            k: (last[k], None if v is None else _canon(v)) for k, v in values.items()
        }


# ---------------------------------------------------------------------------
# Collecting current authored data (disk + index.db)
# ---------------------------------------------------------------------------


def message_key(session_id: str, timestamp: str, role: str, content: str) -> str:
    """Portable identity for a message (local integer IDs differ per host)."""
    h = hashlib.sha256(f"{session_id}\0{timestamp}\0{role}\0{content}".encode()).hexdigest()
    return h[:20]


def collect_current(
    data_dir: Path, db_path: Path | None = None, host: dict | None = None,
) -> tuple[dict[str, Any], set[str]]:
    """Read every tracked item from disk and index.db.

    Returns (values by key, key prefixes whose absence means "deleted").
    A prefix is only authoritative when its source was read successfully, so a
    corrupt or missing registry never turns into mass deletions.
    """
    values: dict[str, Any] = {}
    authoritative: set[str] = set()

    ok, registry = _read_json(data_dir / "workstreams.json")
    registry_ok = ok and isinstance(registry, dict) and isinstance(registry.get("workstreams"), dict)
    if registry_ok:
        authoritative.update({"ws/", "state/", "file/"})
        for name, entry in registry["workstreams"].items():
            values[f"ws/{name}"] = entry

    ws_root = data_dir / "workstreams"
    if ws_root.is_dir():
        for ws_dir in sorted(p for p in ws_root.iterdir() if p.is_dir()):
            state = ws_dir / "state.md"
            if state.is_file():
                values[f"state/{ws_dir.name}"] = state.read_text(errors="replace")
            for fname in TRACKED_FILES:
                fpath = ws_dir / fname
                if fpath.is_file():
                    values[f"file/{ws_dir.name}/{fname}"] = fpath.read_text(errors="replace")

    ok, ideas = _read_json(data_dir / "ideas.json")
    if ok:
        authoritative.add("idea/")
        for idea in ideas or []:
            if isinstance(idea, dict) and idea.get("uid"):
                values[f"idea/{idea['uid']}"] = idea

    # Legacy hint files (pre-MCP) — indexing them may have failed (FK) so
    # read them directly.
    hints_dir = data_dir / "session-hints"
    if hints_dir.is_dir():
        for hint_path in sorted(hints_dir.glob("*.json")):
            ok, data = _read_json(hint_path)
            if not ok or not isinstance(data, dict):
                continue
            if not data.get("session_id") or not data.get("summary"):
                continue
            values[f"hint/{hint_path.name}"] = {
                "session_id": data["session_id"],
                "workstream": data.get("workstream", ""),
                "timestamp": _hint_file_timestamp(hint_path.name),
                "summary": data["summary"],
                "decisions": data.get("decisions") or None,
            }

    if db_path is not None and Path(db_path).exists():
        values.update(_collect_db(Path(db_path)))

    if host is not None:
        authoritative_host_key = f"host/{host['host_id']}"
        values[authoritative_host_key] = {
            "hostname": host["hostname"],
            "nickname": host["nickname"],
            "platform": platform.system().lower(),
            "wsl": "microsoft" in platform.release().lower(),
            "relay_version": _relay_version(),
        }

    return values, authoritative


def _hint_file_timestamp(filename: str) -> str:
    # 2026-03-05T043956Z-<session_id>.json -> 2026-03-05T04:39:56Z
    z = filename.find("Z-")
    t = filename[: z + 1] if z >= 0 else ""
    if len(t) >= 18 and "T" in t:
        return f"{t[:13]}:{t[13:15]}:{t[15:]}"
    return t


def _collect_db(db_path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "session_hints" in tables:
            for r in conn.execute(
                "SELECT session_id, timestamp, source_file, workstream, summary, decisions FROM session_hints"
            ):
                values[f"hint/{r['source_file']}"] = {
                    "session_id": r["session_id"],
                    "workstream": r["workstream"],
                    "timestamp": r["timestamp"],
                    "summary": _loads_list(r["summary"]),
                    "decisions": _loads_list(r["decisions"]),
                }
        if "session_tags" in tables:
            for r in conn.execute(
                "SELECT session_id, tag, source FROM session_tags WHERE source != 'auto'"
            ):
                values[f"stag/{r['session_id']}/{r['tag']}"] = {
                    "session_id": r["session_id"], "tag": r["tag"], "source": r["source"],
                }
        if "message_tags" in tables and "messages" in tables:
            for r in conn.execute(
                """SELECT m.session_id, m.timestamp, m.role, m.content, t.tag, t.source
                   FROM message_tags t JOIN messages m ON m.id = t.message_id
                   WHERE t.source != 'auto'"""
            ):
                mk = message_key(r["session_id"], r["timestamp"], r["role"], r["content"])
                values[f"mtag/{mk}/{r['tag']}"] = {
                    "session_id": r["session_id"], "timestamp": r["timestamp"],
                    "role": r["role"], "message_key": mk, "tag": r["tag"], "source": r["source"],
                }
    finally:
        conn.close()
    return values


def _loads_list(raw: str | None) -> list | None:
    if raw is None:
        return None
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    return val if isinstance(val, list) else [val]


# ---------------------------------------------------------------------------
# Reconcile, migrate, restore
# ---------------------------------------------------------------------------


def _event_for_change(key: str, old: Any, new: Any) -> tuple[str, dict]:
    prefix = key.split("/", 1)[0]
    create_type, _ = _TYPES[prefix]
    update_type = _UPDATE_TYPES.get(prefix)
    if update_type and isinstance(old, dict) and isinstance(new, dict):
        set_fields = {k: v for k, v in new.items() if k not in old or old[k] != v}
        unset = sorted(k for k in old if k not in new)
        return update_type, {"set": set_fields, "unset": unset}
    return create_type, {"value": new}


def record_changes(
    data_dir: Path, db_path: Path | None = None, source: str = "external",
) -> list[str]:
    """Append events for every difference between disk/index.db and the log.

    Returns the new event IDs (empty when nothing changed).
    """
    new_ids: list[str] = []
    with EventStore(data_dir) as store, store.transaction():
        new_ids = _reconcile(store, data_dir, db_path, source)
    return new_ids


def ensure_idea_uids(data_dir: Path) -> int:
    """Give every idea in ideas.json a stable ``uid`` (additive field).

    The numeric ``id`` is assigned per machine, so it can't identify an idea
    across machines. Ideas created by relay get a uid at creation; this covers
    older ones and ideas added by hand. Returns how many were assigned.
    """
    path = data_dir / "ideas.json"
    ok, ideas = _read_json(path)
    if not ok or not isinstance(ideas, list):
        return 0
    missing = [i for i in ideas if isinstance(i, dict) and not i.get("uid")]
    if not missing:
        return 0
    for idea in missing:
        idea["uid"] = new_idea_uid()
    atomic_write(path, json.dumps(ideas, indent=2) + "\n")
    return len(missing)


def _reconcile(store: EventStore, data_dir: Path, db_path: Path | None, source: str) -> list[str]:
    ensure_idea_uids(data_dir)
    current, authoritative = collect_current(data_dir, db_path, host=store.host)
    heads = store.heads()
    new_ids: list[str] = []

    for key in sorted(current):
        new_json = _canon(current[key])
        head = heads.get(key)
        if head is not None and head[1] == new_json:
            continue
        old = json.loads(head[1]) if head is not None and head[1] is not None else None
        event_type, payload = _event_for_change(key, old, current[key])
        new_ids.append(store.append(event_type, key, payload, source=source))

    for key, (_eid, value) in sorted(heads.items()):
        if value is None or key in current:
            continue
        if not any(key.startswith(p) for p in authoritative):
            continue  # hints/tags: absence in index.db (e.g. after reindex) isn't a delete
        _, delete_type = _TYPES[key.split("/", 1)[0]]
        if delete_type:
            new_ids.append(store.append(delete_type, key, {}, source=source))

    return new_ids


def ensure_event_log(data_dir: Path, db_path: Path | None = None) -> dict:
    """Create the log on first run (backup + import), then pick up any edits
    made since the last run. Safe to call on every startup."""
    data_dir = Path(data_dir)
    with EventStore(data_dir) as store:
        with store.transaction():
            migrated = store.get_meta("migrated_at")
            backup = None
            if migrated is None:
                backup = _backup(data_dir, db_path)
                new_ids = _reconcile(store, data_dir, db_path, source="import")
                store.set_meta("migrated_at", _utc_now())
                if backup:
                    store.set_meta("migration_backup", str(backup))
                status = "migrated"
            else:
                new_ids = _reconcile(store, data_dir, db_path, source="external")
                status = "ok"
        return {
            "status": status,
            "new_events": len(new_ids),
            "backup": str(backup) if backup else None,
            "host": store.host,
        }


def _backup(data_dir: Path, db_path: Path | None) -> Path | None:
    """Copy the data dir (and authored index.db rows) before the first import."""
    has_data = any(
        (data_dir / n).exists() for n in ("workstreams.json", "workstreams", "ideas.json", "session-hints")
    )
    if not has_data:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = data_dir / "backups" / f"pre-event-log-{stamp}"
    skip = {"backups", EVENTS_DB, f"{EVENTS_DB}-wal", f"{EVENTS_DB}-shm"}
    dest.mkdir(parents=True)
    for item in data_dir.iterdir():
        if item.name in skip:
            continue
        if item.is_dir():
            shutil.copytree(item, dest / item.name)
        else:
            shutil.copy2(item, dest / item.name)
    if db_path is not None and Path(db_path).exists():
        authored = _collect_db(Path(db_path))
        (dest / "index-authored-rows.json").write_text(json.dumps(authored, indent=2) + "\n")
    return dest


def restore_authored_rows(data_dir: Path, conn: sqlite3.Connection) -> dict:
    """Put hints and manual tags from the log back into index.db.

    reindex() wipes session_hints and manual message tags; rows written by MCP
    tools existed only in the DB, so they were lost. The log has them.
    """
    from .db import ensure_session

    store = EventStore(data_dir)
    try:
        heads = store.heads()
    finally:
        store.close()

    counts = {"hints": 0, "session_tags": 0, "message_tags": 0}

    def session_exists(sid: str) -> bool:
        return conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (sid,)).fetchone() is not None

    for key, (_eid, raw) in heads.items():
        if raw is None:
            continue
        value = json.loads(raw)
        if key.startswith("hint/"):
            source_file = key[len("hint/"):]
            sid = value["session_id"]
            if source_file.startswith("mcp-"):
                ensure_session(conn, sid)  # same as when the hint was first written
            elif not session_exists(sid):
                continue
            conn.execute(
                """INSERT INTO session_hints (session_id, timestamp, source_file, workstream, summary, decisions)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_file) DO UPDATE SET
                     workstream = excluded.workstream, summary = excluded.summary,
                     decisions = excluded.decisions""",
                (
                    sid, value.get("timestamp", ""), source_file, value.get("workstream", ""),
                    json.dumps(value.get("summary") or []),
                    json.dumps(value["decisions"]) if value.get("decisions") else None,
                ),
            )
            counts["hints"] += 1
        elif key.startswith("stag/"):
            if session_exists(value["session_id"]):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO session_tags (session_id, tag, source) VALUES (?, ?, ?)",
                    (value["session_id"], value["tag"], value.get("source", "manual")),
                )
                counts["session_tags"] += cur.rowcount
        elif key.startswith("mtag/"):
            for m in conn.execute(
                "SELECT id, content FROM messages WHERE session_id = ? AND timestamp = ? AND role = ?",
                (value["session_id"], value["timestamp"], value["role"]),
            ).fetchall():
                if message_key(value["session_id"], value["timestamp"], value["role"], m[1]) == value["message_key"]:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO message_tags (message_id, tag, source) VALUES (?, ?, ?)",
                        (m[0], value["tag"], value.get("source", "manual")),
                    )
                    counts["message_tags"] += cur.rowcount
    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# CLI: python -m relay_server.events [status|verify]
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    from .workstreams import get_data_dir

    data_dir = get_data_dir()
    if not (data_dir / EVENTS_DB).exists():
        print(f"No event log at {data_dir / EVENTS_DB} yet (created on next relay server start).")
        return 1
    with EventStore(data_dir) as store:
        evs = store.events()
        by_type: dict[str, int] = {}
        for ev in evs:
            by_type[ev["type"]] = by_type.get(ev["type"], 0) + 1
        heads = store.heads()
        live = sum(1 for _e, v in heads.values() if v is not None)
        print(f"event log: {store.path}")
        print(f"migrated:  {store.get_meta('migrated_at')}  backup: {store.get_meta('migration_backup')}")
        print(f"events:    {len(evs)}   keys: {len(heads)} ({live} live)")
        for t in sorted(by_type):
            print(f"  {t:<16} {by_type[t]}")
        if evs:
            print(f"first: {evs[0]['id']}   last: {evs[-1]['id']}")
        if "verify" in argv:
            ok = store.fold() == heads
            print(f"fold(log) == heads: {'OK' if ok else 'MISMATCH'}")
            current, _ = collect_current(data_dir, None)
            stale = [
                k for k, v in current.items()
                if not k.startswith(("hint/", "stag/", "mtag/"))
                and (k not in heads or heads[k][1] != _canon(v))
            ]
            print(f"disk matches log: {'OK' if not stale else 'NO, unrecorded: ' + ', '.join(stale[:10])}")
            return 0 if ok and not stale else 2
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
