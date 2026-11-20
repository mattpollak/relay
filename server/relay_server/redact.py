"""Redact text (e.g. a leaked secret) from relay's stores — and, with sync on,
from every machine and the shared folder.

The redacting machine replaces the text with ``[redacted]`` in its search index
(messages, hints) and its event log, then records a ``msg.redact`` event that
carries only the *scrubbed* versions: the secret itself is never sent. Other
machines accept a scrubbed version only if it's a pure removal of the old one
(see :func:`is_redaction`), apply it, and rewrite their own folder batches so
old ciphertext containing the text is deleted.

Claude Code's raw transcripts (``~/.claude/projects``) are not relay's files;
the result lists the ones on this machine that still contain the text.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from .events import EventStore, _canon, message_key

MARKER = "[redacted]"
MIN_LENGTH = 6


class RedactError(ValueError):
    pass


def is_redaction(old: str, new: str) -> bool:
    """True if ``new`` is ``old`` with one or more non-empty parts replaced by
    MARKER, and nothing added."""
    if new == old:
        return True
    pieces = new.split(MARKER)
    if len(pieces) < 2 or not old.startswith(pieces[0]) or not old.endswith(pieces[-1]):
        return False
    pos = len(pieces[0])
    end = len(old) - len(pieces[-1])
    if end < pos:
        return False
    for piece in pieces[1:-1]:
        idx = old.find(piece, pos + 1)  # each marker stands for at least one character
        if idx < 0:
            return False
        pos = idx + len(piece)
    return end >= pos + 1


def is_payload_redaction(old: Any, new: Any) -> bool:
    """True if ``new`` has exactly ``old``'s JSON shape — same keys, same
    types, equal non-string values — and every string is a pure removal
    (:func:`is_redaction`). Checking decoded values, not serialized JSON, so a
    "removal" can't cross string boundaries, change escapes or drop keys."""
    if isinstance(old, str):
        return isinstance(new, str) and is_redaction(old, new)
    if isinstance(old, dict):
        return (
            isinstance(new, dict) and old.keys() == new.keys()
            and all(is_payload_redaction(old[k], new[k]) for k in old)
        )
    if isinstance(old, list):
        return isinstance(new, list) and len(old) == len(new) and all(map(is_payload_redaction, old, new))
    return type(old) is type(new) and old == new


def _scrub_obj(obj: Any, text: str) -> Any:
    """Replace text inside every string value (keys are left alone)."""
    if isinstance(obj, str):
        return obj.replace(text, MARKER)
    if isinstance(obj, dict):
        return {k: _scrub_obj(v, text) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_obj(v, text) for v in obj]
    return obj


def _escaped(text: str) -> str:
    """How ``text`` appears inside canonical JSON (events.payload)."""
    return json.dumps(text, ensure_ascii=False)[1:-1]


# ---------------------------------------------------------------------------
# Index (index.db)
# ---------------------------------------------------------------------------


def _update_message(conn: sqlite3.Connection, msg_id: int, old: str, new: str) -> None:
    # messages_fts is an external-content table with no UPDATE trigger.
    conn.execute("INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', ?, ?)", (msg_id, old))
    conn.execute("UPDATE messages SET content = ? WHERE id = ?", (new, msg_id))
    conn.execute("INSERT INTO messages_fts(rowid, content) VALUES (?, ?)", (msg_id, new))


def _scrub_index(conn: sqlite3.Connection, text: str, session_id: str | None) -> tuple[list[dict], int]:
    """Replace text in messages and hints. Returns (message changes, hints changed)."""
    where, params = "instr(content, ?) > 0", [text]
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)
    changes = []
    for r in conn.execute(f"SELECT id, session_id, role, timestamp, content FROM messages WHERE {where}", params).fetchall():
        new = r[4].replace(text, MARKER)
        _update_message(conn, r[0], r[4], new)
        changes.append({
            "session_id": r[1], "role": r[2], "timestamp": r[3],
            "old_key": message_key(r[1], r[3], r[2], r[4]), "content": new,
        })
    hints = 0
    hwhere, hparams = "(instr(summary, ?) > 0 OR instr(COALESCE(decisions, ''), ?) > 0)", [_escaped(text), _escaped(text)]
    if session_id:
        hwhere += " AND session_id = ?"
        hparams.append(session_id)
    for h in conn.execute(f"SELECT id, summary, decisions FROM session_hints WHERE {hwhere}", hparams).fetchall():
        conn.execute(
            "UPDATE session_hints SET summary = ?, decisions = ? WHERE id = ?",
            (h[1].replace(_escaped(text), MARKER), h[2].replace(_escaped(text), MARKER) if h[2] else h[2], h[0]),
        )
        hints += 1
    return changes, hints


def apply_message_changes(conn: sqlite3.Connection, changes: list[dict]) -> int:
    """Apply scrubbed message versions (from a redact event) to the index."""
    applied = 0
    for c in changes:
        if not isinstance(c, dict) or not all(isinstance(c.get(f), str) for f in ("session_id", "role", "timestamp", "old_key", "content")):
            continue
        for mid, content in conn.execute(
            "SELECT id, content FROM messages WHERE session_id = ? AND timestamp = ? AND role = ?",
            (c["session_id"], c["timestamp"], c["role"]),
        ).fetchall():
            if message_key(c["session_id"], c["timestamp"], c["role"], content) != c["old_key"]:
                continue
            if content != c["content"] and is_redaction(content, c["content"]):
                _update_message(conn, mid, content, c["content"])
                applied += 1
    return applied


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def _scrub_events(store: EventStore, text: str, session_id: str | None) -> dict[str, dict]:
    """Rewrite every event payload containing text. Returns {event_id: new payload}."""
    needle = _escaped(text)
    replaced: dict[str, dict] = {}
    for r in store.conn.execute(
        "SELECT id, key, type, payload FROM events WHERE instr(payload, ?) > 0", (needle,)
    ).fetchall():
        if session_id and session_id not in r["key"] and session_id not in r["payload"]:
            continue
        old_payload = json.loads(r["payload"])
        new_payload = _scrub_obj(old_payload, text)
        if new_payload != old_payload:
            replaced[r["id"]] = new_payload
    return replaced


def replace_event_payloads(
    store: EventStore, replacements: dict[str, Any], text_pairs: list[tuple[str, str]] | None = None,
) -> set[str]:
    """Overwrite event payloads with scrubbed versions; refold their keys.
    Only pure removals are accepted. Returns the keys whose heads changed.
    text_pairs collects (old, new) document texts, for scrubbing side copies."""
    from .sync_apply import refold

    keys = set()
    for eid, payload in replacements.items():
        row = store.conn.execute("SELECT key, payload FROM events WHERE id = ?", (eid,)).fetchone()
        if row is None or not isinstance(payload, dict):
            continue
        new_raw = _canon(payload)
        if new_raw == row["payload"] or not is_payload_redaction(json.loads(row["payload"]), payload):
            continue
        store.conn.execute("UPDATE events SET payload = ? WHERE id = ?", (new_raw, eid))
        keys.add(row["key"])
        old_value = json.loads(row["payload"]).get("value")
        if text_pairs is not None and isinstance(old_value, str) and isinstance(payload.get("value"), str):
            text_pairs.append((old_value, payload["value"]))
    for key in keys:
        refold(store, key)
    return keys


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def find_transcripts_containing(text: str, root: Path | None = None, limit: int = 20) -> list[str]:
    root = root or Path(os.path.expanduser("~/.claude/projects"))
    found = []
    if not root.is_dir():
        return found
    needle = text.encode()
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if needle in path.read_bytes():
                found.append(str(path))
        except OSError:
            continue
        if len(found) >= limit:
            break
    return found


def redact(
    data_dir: Path, db_path: Path | None, text: str, *, session_id: str | None = None,
    dry_run: bool = False, transcripts_root: Path | None = None,
) -> dict:
    """Redact ``text`` everywhere relay stores it. dry_run only counts."""
    if not isinstance(text, str) or len(text.strip()) < MIN_LENGTH:
        raise RedactError(f"text to redact must be at least {MIN_LENGTH} characters (to avoid wiping common words)")
    if MARKER in text:
        raise RedactError(f"text can't contain {MARKER!r}")

    result: dict[str, Any] = {"messages": 0, "hints": 0, "events": 0, "dry_run": dry_run}
    conn = sqlite3.connect(str(db_path), timeout=10) if db_path and Path(db_path).exists() else None
    try:
        with EventStore(data_dir) as store, store.transaction():
            if dry_run:
                if conn is not None:
                    q, p = "instr(content, ?) > 0", [text]
                    if session_id:
                        q += " AND session_id = ?"
                        p.append(session_id)
                    result["messages"] = conn.execute(f"SELECT COUNT(*) FROM messages WHERE {q}", p).fetchone()[0]
                result["events"] = len(_scrub_events(store, text, session_id))
            else:
                message_changes: list[dict] = []
                if conn is not None:
                    message_changes, result["hints"] = _scrub_index(conn, text, session_id)
                    result["messages"] = len(message_changes)
                _mark_already_shared(store, message_changes)
                replaced = _scrub_events(store, text, session_id)
                changed_keys = replace_event_payloads(store, replaced)
                result["events"] = len(replaced)
                fingerprint = make_fingerprint(text)
                add_fingerprints(store, [fingerprint])
                store.append(
                    "msg.redact", f"redact/{secrets.token_hex(8)}",
                    {"value": {"events": replaced, "messages": message_changes, "fingerprints": [fingerprint]}},
                    source="redact",
                )
                _rewrite_views(store, data_dir, changed_keys)
                result["files"] = _scrub_files(data_dir, text)
                if conn is not None:
                    conn.commit()
    finally:
        if conn is not None:
            conn.close()
    result["transcripts"] = find_transcripts_containing(text, transcripts_root)
    result["backups"] = _files_containing(Path(data_dir) / "backups", text)
    return result


def _files_containing(root: Path, text: str) -> list[str]:
    if not root.is_dir():
        return []
    needles = {text.encode(), _escaped(text).encode()}
    out = []
    for p in sorted(root.rglob("*")):
        try:
            if p.is_file() and any(n in p.read_bytes() for n in needles):
                out.append(str(p))
        except OSError:
            continue
    return out


def _scrub_files(data_dir: Path, text: str) -> list[str]:
    """relay's own files that keep copies outside the log: legacy hint files,
    .bak and conflict files. (Tracked files were rewritten from the log.)"""
    from .workstreams import atomic_write

    scrubbed = []
    candidates = list((data_dir / "session-hints").glob("*.json")) + list((data_dir / "workstreams").glob("*/*.md*"))
    for p in candidates:
        try:
            raw = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        needle = _escaped(text) if p.suffix == ".json" else text
        if needle in raw:
            atomic_write(p, raw.replace(needle, MARKER))
            scrubbed.append(str(p))
    return scrubbed


def scrub_side_copies(data_dir: Path, text_pairs: list[tuple[str, str]]) -> list[str]:
    """On a machine that received a redaction (and never learns the secret):
    rewrite .bak and conflict files holding an old, unscrubbed document."""
    from .workstreams import atomic_write

    pairs = [(o, n) for o, n in text_pairs if o and o != n]
    changed = []
    if not pairs:
        return changed
    for p in (Path(data_dir) / "workstreams").glob("*/*"):
        if not (p.name.endswith(".bak") or ".conflict-" in p.name):
            continue
        try:
            raw = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        new = raw
        for old_text, new_text in pairs:
            new = new.replace(old_text, new_text)
        if new != raw:
            atomic_write(p, new)
            changed.append(str(p))
    return changed


# ---------------------------------------------------------------------------
# Fingerprints: keep redacted text out of what gets indexed later
# ---------------------------------------------------------------------------
#
# The redacted text usually lives on in Claude Code's raw transcripts — this
# session's included (the /relay:redact command line, replies that repeat it).
# Those are indexed later as new messages. To keep them out of the index and
# out of sync, relay stores a salted hash of each redacted string (never the
# string) and scrubs any newly indexed message containing a match. The
# fingerprint travels with the redact event, so every machine guards its own
# transcripts too.

_FP_META = "redaction_fingerprints"


def make_fingerprint(text: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "len": len(text), "h": _fp_hash(salt, text)}


def _fp_hash(salt: bytes, text: str) -> str:
    return hashlib.blake2b(text.encode(), key=salt, digest_size=16).hexdigest()


def _valid_fp(fp: Any) -> bool:
    try:
        return (
            isinstance(fp, dict) and isinstance(fp.get("len"), int) and fp["len"] >= MIN_LENGTH
            and len(bytes.fromhex(fp["salt"])) == 16 and len(bytes.fromhex(fp["h"])) == 16
        )
    except (TypeError, ValueError, KeyError):
        return False


def load_fingerprints(store: EventStore) -> list[dict]:
    raw = store.get_meta(_FP_META)
    try:
        fps = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    return [fp for fp in fps if _valid_fp(fp)]


def add_fingerprints(store: EventStore, fps: list[dict]) -> None:
    current = load_fingerprints(store)
    seen = {fp["h"] for fp in current}
    current += [fp for fp in fps if _valid_fp(fp) and fp["h"] not in seen]
    store.set_meta(_FP_META, json.dumps(current))


def scrub_with_fingerprints(content: str, fps: list[dict]) -> str:
    """Replace every substring whose fingerprint matches with MARKER."""
    for fp in fps:
        n, salt, want = fp["len"], bytes.fromhex(fp["salt"]), fp["h"]
        if len(content) < n:
            continue
        out, i, hit = [], 0, False
        while i <= len(content) - n:
            if _fp_hash(salt, content[i : i + n]) == want:
                out.append(MARKER)
                i += n
                hit = True
            else:
                out.append(content[i])
                i += 1
        if hit:
            content = "".join(out) + content[i:]
    return content


def scrub_new_messages(store: EventStore, conn: sqlite3.Connection) -> int:
    """Scrub messages indexed since the last scan. Call inside
    ``store.transaction()``; commits nothing on conn."""
    fps = load_fingerprints(store)
    cursor = int(store.get_meta("redact_scan_id") or 0)
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    changed = 0
    if fps:
        shortest = min(fp["len"] for fp in fps)
        for mid, content in conn.execute(
            "SELECT id, content FROM messages WHERE id > ? AND length(content) >= ?", (cursor, shortest)
        ).fetchall():
            new = scrub_with_fingerprints(content, fps)
            if new != content:
                _update_message(conn, mid, content, new)
                changed += 1
    store.set_meta("redact_scan_id", str(max_id))
    return changed


def reapply_known_redactions(data_dir: Path, db_path: Path | None) -> int:
    """Run after indexing (server start, reindex). Returns messages scrubbed."""
    if db_path is None or not Path(db_path).exists():
        return 0
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        with EventStore(data_dir) as store, store.transaction():
            changed = scrub_new_messages(store, conn)
        conn.commit()
    finally:
        conn.close()
    return changed


def _mark_already_shared(store: EventStore, changes: list[dict]) -> None:
    """A message already shared reaches other machines in its scrubbed form via
    the redact event, so its new version mustn't be shared again as new."""
    store.conn.execute("CREATE TABLE IF NOT EXISTS sync_msg_sent (message_key TEXT PRIMARY KEY)")
    for c in changes:
        if store.conn.execute("SELECT 1 FROM sync_msg_sent WHERE message_key = ?", (c["old_key"],)).fetchone():
            new_key = message_key(c["session_id"], c["timestamp"], c["role"], c["content"])
            store.conn.execute("INSERT OR IGNORE INTO sync_msg_sent (message_key) VALUES (?)", (new_key,))


def _rewrite_views(store: EventStore, data_dir: Path, keys: set[str]) -> None:
    """Files on disk (state.md etc.) whose event history was scrubbed."""
    from .sync_apply import materialize

    changes = {k: store.head_value(k) for k in keys if k.startswith(("state/", "file/", "ws/", "idea/"))}
    if changes:
        materialize(store, data_dir, None, changes, store.host["host_id"] if store.host else None)
