"""Workstream data directory management.

Handles registry reads/writes, atomic file operations, and data dir resolution.
The registry (workstreams.json) stays on disk as JSON so bash hooks can read it
with jq. This module is the primary writer.
"""

import json
import os
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
