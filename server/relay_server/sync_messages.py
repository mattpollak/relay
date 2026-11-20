"""Conversation messages for cross-machine search (sync scope ``state+messages``).

Each machine packs the messages its indexer extracted from its own Claude
transcripts into ``msg.batch`` events (key ``msgb/<host>/<session>/<first-key>``),
grouped per session. Other machines insert them into their search index as
sessions marked with ``origin_host``, so search, get_conversation, list_sessions
and summaries cover every machine. Raw transcripts never leave the machine.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .events import EVENTS_DB, EventStore, message_key

MAX_MESSAGES_PER_EVENT = 400
MAX_BYTES_PER_EVENT = 512 * 1024
_SESSION_FIELDS = ("project_dir", "slug", "git_branch", "cwd")


def _ensure_sent_table(store: EventStore) -> None:
    store.conn.execute("CREATE TABLE IF NOT EXISTS sync_msg_sent (message_key TEXT PRIMARY KEY)")


def pack_new_messages(store: EventStore, db_path: Path | None) -> int:
    """Append msg.batch events for this machine's messages not shared yet.
    Call inside ``store.transaction()``. Returns the number of messages packed."""
    if db_path is None or not Path(db_path).exists():
        return 0
    _ensure_sent_table(store)
    host_id = store.host["host_id"]
    cursor = int(store.get_meta("sync_msg_cursor") or 0)

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # Never share text that was redacted (it may have been re-indexed
        # from the raw transcripts, e.g. the /relay:redact command itself).
        from .redact import scrub_new_messages

        scrub_new_messages(store, conn)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        local_only = "AND s.origin_host IS NULL" if "origin_host" in cols else ""
        rows = conn.execute(
            f"""SELECT m.id, m.session_id, m.role, m.content, m.timestamp, m.model,
                       s.project_dir, s.slug, s.git_branch, s.cwd
                FROM messages m JOIN sessions s ON s.session_id = m.session_id
                WHERE m.id > ? {local_only}
                ORDER BY m.session_id, m.timestamp, m.id""",
            (cursor,),
        ).fetchall()
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    finally:
        conn.close()

    sent = store.conn.execute
    by_session: dict[str, list[sqlite3.Row]] = {}
    keys: dict[int, str] = {}
    for r in rows:
        k = message_key(r["session_id"], r["timestamp"], r["role"], r["content"])
        if sent("SELECT 1 FROM sync_msg_sent WHERE message_key = ?", (k,)).fetchone():
            continue
        keys[r["id"]] = k
        by_session.setdefault(r["session_id"], []).append(r)

    packed = 0
    for sid, msgs in sorted(by_session.items()):
        session = {"session_id": sid, **{f: msgs[0][f] for f in _SESSION_FIELDS}}
        chunk: list[dict] = []
        size = 0
        for r in msgs + [None]:  # None flushes the last chunk
            if r is not None:
                item = {
                    "key": keys[r["id"]], "role": r["role"], "content": r["content"],
                    "timestamp": r["timestamp"], "model": r["model"],
                }
                item_size = len(r["content"]) + 200
            if chunk and (r is None or len(chunk) >= MAX_MESSAGES_PER_EVENT or size + item_size > MAX_BYTES_PER_EVENT):
                event_key = f"msgb/{host_id}/{sid}/{chunk[0]['key']}"
                store.append("msg.batch", event_key, {"value": {"session": session, "messages": chunk}}, source="sync")
                store.conn.executemany(
                    "INSERT OR IGNORE INTO sync_msg_sent (message_key) VALUES (?)", [(c["key"],) for c in chunk]
                )
                packed += len(chunk)
                chunk, size = [], 0
            if r is not None:
                chunk.append(item)
                size += item_size
    store.set_meta("sync_msg_cursor", str(max_id))
    return packed


def apply_message_batch(conn: sqlite3.Connection, key: str, value: Any) -> int:
    """Insert one msg.batch into the search index. Returns messages added.
    Dedupes on the portable message key, so re-applying is harmless."""
    from .tagger import auto_tag_messages

    parts = key.split("/", 3)
    if len(parts) != 4 or not isinstance(value, dict):
        return 0
    origin = parts[1]
    session = value.get("session") or {}
    sid = session.get("session_id")
    msgs = value.get("messages")
    if not isinstance(sid, str) or not sid or sid != parts[2] or not isinstance(msgs, list):
        return 0

    existing = conn.execute("SELECT origin_host, message_count FROM sessions WHERE session_id = ?", (sid,)).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO sessions (session_id, project_dir, slug, git_branch, cwd, message_count, origin_host)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (sid, *(session.get(f) for f in _SESSION_FIELDS), origin),
        )
    elif existing[0] is None and not conn.execute(
        "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (sid,)
    ).fetchone():
        # A placeholder (e.g. created for a hint from that machine): adopt it.
        conn.execute(
            """UPDATE sessions SET origin_host = ?, project_dir = COALESCE(project_dir, ?),
                   slug = COALESCE(slug, ?), git_branch = COALESCE(git_branch, ?), cwd = COALESCE(cwd, ?)
               WHERE session_id = ?""",
            (origin, *(session.get(f) for f in _SESSION_FIELDS), sid),
        )
    elif existing[0] is None:
        return 0  # a local session with that ID: never mix another machine's messages in

    new_ids = []
    for m in msgs:
        if not isinstance(m, dict) or not all(isinstance(m.get(f), str) for f in ("role", "content", "timestamp")):
            continue
        dupes = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND timestamp = ? AND role = ?",
            (sid, m["timestamp"], m["role"]),
        ).fetchall()
        k = message_key(sid, m["timestamp"], m["role"], m["content"])
        if any(message_key(sid, m["timestamp"], m["role"], d[0]) == k for d in dupes):
            continue
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, model) VALUES (?, ?, ?, ?, ?)",
            (sid, m["role"], m["content"], m["timestamp"], m.get("model")),
        )
        new_ids.append(cur.lastrowid)
    if new_ids:
        auto_tag_messages(conn, new_ids)
    conn.execute(
        """UPDATE sessions SET
               message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?),
               first_timestamp = (SELECT MIN(timestamp) FROM messages WHERE session_id = ?),
               last_timestamp = (SELECT MAX(timestamp) FROM messages WHERE session_id = ?)
           WHERE session_id = ?""",
        (sid, sid, sid, sid),
    )
    return len(new_ids)


def host_names(data_dir: Path) -> dict[str, str]:
    """host_id -> display name, from the event log (read-only; {} if none)."""
    from .sync_folder import safe_name

    db = Path(data_dir) / EVENTS_DB
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT key, value FROM heads WHERE key LIKE 'host/%' AND value IS NOT NULL").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    out = {}
    for key, raw in rows:
        info = json.loads(raw)
        out[key[5:]] = safe_name(info.get("nickname") or info.get("hostname"), key[5:])
    return out


def annotate_hosts(rows: list[dict], data_dir: Path) -> list[dict]:
    """Replace origin_host with a readable ``host`` on rows from other machines."""
    names = None
    for r in rows:
        origin = r.pop("origin_host", None)
        if origin:
            if names is None:
                names = host_names(data_dir)
            r["host"] = names.get(origin, origin)
    return rows
