"""Workstream data directory management.

Handles registry reads/writes, atomic file operations, and data dir resolution.
The registry (workstreams.json) stays on disk as JSON so bash hooks can read it
with jq. This module is the primary writer.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def get_data_dir() -> Path:
    """Return the relay data directory, respecting XDG_CONFIG_HOME."""
    return Path(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    ) / "relay"


def read_registry(data_dir: Path | None = None) -> dict:
    """Read workstreams.json. Returns empty registry if missing."""
    if data_dir is None:
        data_dir = get_data_dir()
    registry_path = data_dir / "workstreams.json"
    if not registry_path.exists():
        return {"version": 1, "workstreams": {}}
    try:
        return json.loads(registry_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "workstreams": {}}


def write_registry_entry(
    data_dir: Path, name: str, entry: dict
) -> None:
    """Update a single workstream entry in the registry (atomic)."""
    registry = read_registry(data_dir)
    registry["workstreams"][name] = entry
    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")


def atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via tempfile + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        fd = -1  # Mark as closed so cleanup doesn't double-close
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def today() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_timestamp() -> str:
    """Return current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_workstream(
    *,
    data_dir: Path,
    conn: sqlite3.Connection,
    name: str,
    state_content: str,
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
) -> dict:
    """Save workstream state atomically.

    1. Write state.md.new
    2. Rotate state.md -> state.md.bak (if exists)
    3. Rename state.md.new -> state.md
    4. Update registry last_touched
    5. Write session hint to DB (if session_id provided)
    6. Write/update session marker in DB (if session_id provided)
    """
    ws_dir = data_dir / "workstreams" / name
    ws_dir.mkdir(parents=True, exist_ok=True)

    state_path = ws_dir / "state.md"
    new_path = ws_dir / "state.md.new"
    bak_path = ws_dir / "state.md.bak"

    # Step 1: Write new state to temp file
    atomic_write(new_path, state_content)

    # Step 2-3: Rotate (backup old, move new into place)
    if state_path.exists():
        os.replace(state_path, bak_path)
    os.replace(new_path, state_path)

    # Step 4: Update registry
    registry = read_registry(data_dir)
    if name in registry["workstreams"]:
        registry["workstreams"][name]["last_touched"] = today()
        atomic_write(
            data_dir / "workstreams.json",
            json.dumps(registry, indent=2) + "\n",
        )

    # Step 5: Write hint to DB
    if session_id and hint_summary:
        ts = utc_timestamp()
        conn.execute(
            """INSERT OR REPLACE INTO session_hints
               (session_id, timestamp, source_file, workstream, summary, decisions)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                ts,
                f"mcp-{ts}-{session_id[:8]}",
                name,
                json.dumps(hint_summary),
                json.dumps(hint_decisions) if hint_decisions else None,
            ),
        )

    # Step 6: Write/update session marker in DB
    if session_id:
        conn.execute(
            """INSERT OR REPLACE INTO session_markers
               (session_id, workstream, attached_at)
               VALUES (?, ?, ?)""",
            (session_id, name, utc_timestamp()),
        )

    conn.commit()

    return {
        "status": "saved",
        "workstream": name,
        "state_file": str(state_path),
        "backup": str(bak_path) if bak_path.exists() else None,
        "hint_written": bool(session_id and hint_summary),
    }
