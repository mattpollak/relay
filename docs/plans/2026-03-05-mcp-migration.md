# MCP Migration — Move Skill Logic to Server-Side Tools

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace multi-bash-call skill flows with single MCP tool calls to improve data fidelity (transactional writes) and reduce token cost (fewer tool invocations).

**Architecture:** The MCP server (`server.py`) gains a `workstreams` module that manages the registry, state files, session markers, and hints. Session markers and hints are written directly to SQLite instead of JSON files (with the indexer's file-based pipeline preserved as a fallback for backfill). Hooks remain as bash scripts but get thinner — session-start.sh still reads the registry and state files, but the heavy lifting (save, create, park, switch) moves to MCP tools. The registry stays on disk as JSON (hooks need jq access) but the MCP server becomes the primary writer.

**Tech Stack:** Python 3.10+, SQLite, FastMCP, existing relay server infrastructure

**Key constraint:** Atomicity — state file writes use tempfile + os.rename. Registry writes use write-to-tmp + os.rename. SQLite writes are transactional. A failed save must never corrupt existing state.

---

### Task 1: Add workstreams module with data directory helpers

**Files:**
- Create: `server/relay_server/workstreams.py`
- Test: `server/tests/test_workstreams.py`

This module provides the shared infrastructure all new MCP tools will use: reading/writing the registry, atomic file operations, and data directory resolution.

**Step 1: Write the tests**

```python
# server/tests/test_workstreams.py
"""Tests for workstreams module."""

import json
import tempfile
from pathlib import Path

from relay_server.workstreams import (
    get_data_dir,
    read_registry,
    write_registry_entry,
    atomic_write,
)


def test_get_data_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert get_data_dir() == tmp_path / "relay"


def test_read_registry_empty(tmp_path):
    registry = tmp_path / "workstreams.json"
    registry.write_text('{"version": 1, "workstreams": {}}')
    result = read_registry(tmp_path)
    assert result["workstreams"] == {}


def test_read_registry_missing(tmp_path):
    result = read_registry(tmp_path)
    assert result["workstreams"] == {}


def test_write_registry_entry_new(tmp_path):
    registry = tmp_path / "workstreams.json"
    registry.write_text('{"version": 1, "workstreams": {}}')
    write_registry_entry(tmp_path, "test-ws", {
        "status": "active",
        "description": "Test",
        "created": "2026-01-01",
        "last_touched": "2026-01-01",
    })
    data = json.loads(registry.read_text())
    assert data["workstreams"]["test-ws"]["status"] == "active"


def test_write_registry_entry_preserves_others(tmp_path):
    registry = tmp_path / "workstreams.json"
    registry.write_text(json.dumps({
        "version": 1,
        "workstreams": {"existing": {"status": "parked"}}
    }))
    write_registry_entry(tmp_path, "new-ws", {"status": "active"})
    data = json.loads(registry.read_text())
    assert "existing" in data["workstreams"]
    assert "new-ws" in data["workstreams"]


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "test.txt"
    atomic_write(target, "hello world")
    assert target.read_text() == "hello world"


def test_atomic_write_is_atomic(tmp_path):
    target = tmp_path / "test.txt"
    target.write_text("original")
    atomic_write(target, "updated")
    assert target.read_text() == "updated"
    # No .tmp file left behind
    assert not (tmp_path / "test.txt.tmp").exists()
```

**Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_workstreams.py -v`
Expected: FAIL (module doesn't exist)

**Step 3: Write the implementation**

```python
# server/relay_server/workstreams.py
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
        os.rename(tmp, path)
    except BaseException:
        os.close(fd) if not os.get_inheritable(fd) else None
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
```

**Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_workstreams.py -v`
Expected: PASS (all 7 tests)

**Step 5: Commit**

```bash
git add server/relay_server/workstreams.py server/tests/test_workstreams.py
git commit -m "feat: add workstreams module with registry helpers and atomic writes"
```

---

### Task 2: Add session_markers table to SQLite schema

**Files:**
- Modify: `server/relay_server/db.py` (add table to SCHEMA)
- Test: `server/tests/test_db.py`

Session markers currently live as JSON files. Adding a DB table means the MCP server can read/write markers without touching the filesystem. The JSON files remain written by hooks (session-start.sh) as a fallback.

**Step 1: Write the failing test**

Add to `server/tests/test_db.py`:

```python
def test_session_markers_table_exists():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        ensure_schema(db_path)
        conn = get_connection(db_path)
        try:
            # Table exists and accepts inserts
            conn.execute(
                """INSERT INTO session_markers (session_id, workstream, attached_at)
                   VALUES ('test-sid', 'test-ws', '2026-01-01T00:00:00Z')"""
            )
            row = conn.execute(
                "SELECT * FROM session_markers WHERE session_id = 'test-sid'"
            ).fetchone()
            assert row["workstream"] == "test-ws"
        finally:
            conn.close()
```

**Step 2: Run test to verify it fails**

Run: `cd server && uv run pytest tests/test_db.py::test_session_markers_table_exists -v`
Expected: FAIL (no such table: session_markers)

**Step 3: Add the table to the schema**

In `server/relay_server/db.py`, add after the `session_hints` table definition:

```sql
CREATE TABLE IF NOT EXISTS session_markers (
    session_id TEXT PRIMARY KEY,
    workstream TEXT NOT NULL,
    attached_at TEXT NOT NULL
);
```

**Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_db.py -v`
Expected: PASS (all tests including new one)

**Step 5: Commit**

```bash
git add server/relay_server/db.py server/tests/test_db.py
git commit -m "feat: add session_markers table to SQLite schema"
```

---

### Task 3: Implement save_workstream MCP tool

**Files:**
- Modify: `server/relay_server/server.py` (add tool)
- Modify: `server/relay_server/workstreams.py` (add save logic)
- Test: `server/tests/test_save_workstream.py`

This is the highest-impact tool — replaces 5 bash calls with 1 MCP call. Handles: write state.md.new, rotate state.md → state.md.bak, update registry last_touched, write session hint to DB, reset counter file.

**Step 1: Write the tests**

```python
# server/tests/test_save_workstream.py
"""Tests for save_workstream tool logic."""

import json
import tempfile
from pathlib import Path

from relay_server.db import ensure_schema, get_connection
from relay_server.workstreams import atomic_write, read_registry


def _setup(tmpdir):
    """Create test DB, data dir, and registry with one active workstream."""
    db_path = Path(tmpdir) / "test.db"
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()
    ensure_schema(db_path)

    # Create registry with active workstream
    registry = {
        "version": 1,
        "workstreams": {
            "test-ws": {
                "status": "active",
                "description": "Test workstream",
                "created": "2026-01-01",
                "last_touched": "2026-01-01",
                "project_dir": "/test",
            }
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))
    (data_dir / "workstreams" / "test-ws").mkdir(parents=True)

    # Create existing state file
    (data_dir / "workstreams" / "test-ws" / "state.md").write_text("# Old State")

    # Insert a session so hint FK is satisfied
    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO sessions (session_id, project_dir, first_timestamp, last_timestamp, message_count)
           VALUES ('aabbccdd-1122-3344-5566-778899aabbcc', '/test', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', 10)"""
    )
    conn.commit()
    return db_path, data_dir, conn


def test_save_writes_state_and_backup():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import save_workstream
            result = save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# New State\n\nUpdated.",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
                hint_summary=["Did something"],
            )
            assert result["status"] == "saved"

            # State file updated
            state = (data_dir / "workstreams" / "test-ws" / "state.md").read_text()
            assert "New State" in state

            # Backup exists
            bak = (data_dir / "workstreams" / "test-ws" / "state.md.bak").read_text()
            assert "Old State" in bak
        finally:
            conn.close()


def test_save_writes_hint_to_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import save_workstream
            save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# State",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
                hint_summary=["Added feature X", "Fixed bug Y"],
                hint_decisions=["Used pattern Z"],
            )
            row = conn.execute(
                "SELECT * FROM session_hints WHERE session_id = 'aabbccdd-1122-3344-5566-778899aabbcc'"
            ).fetchone()
            assert row is not None
            assert row["workstream"] == "test-ws"
            assert "Added feature X" in row["summary"]
            assert "Used pattern Z" in row["decisions"]
        finally:
            conn.close()


def test_save_writes_marker_to_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import save_workstream
            save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# State",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
                hint_summary=["Did things"],
            )
            row = conn.execute(
                "SELECT * FROM session_markers WHERE session_id = 'aabbccdd-1122-3344-5566-778899aabbcc'"
            ).fetchone()
            assert row is not None
            assert row["workstream"] == "test-ws"
        finally:
            conn.close()


def test_save_updates_registry_last_touched():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import save_workstream
            save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# State",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
                hint_summary=["Did things"],
            )
            reg = read_registry(data_dir)
            assert reg["workstreams"]["test-ws"]["last_touched"] != "2026-01-01"
        finally:
            conn.close()


def test_save_without_session_id_skips_hint():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import save_workstream
            result = save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# State",
            )
            assert result["status"] == "saved"
            # No hint written
            row = conn.execute("SELECT COUNT(*) as c FROM session_hints").fetchone()
            assert row["c"] == 0
        finally:
            conn.close()


def test_save_no_existing_state_no_backup():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        # Remove existing state
        (data_dir / "workstreams" / "test-ws" / "state.md").unlink()
        try:
            from relay_server.workstreams import save_workstream
            result = save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# Brand New",
            )
            assert result["status"] == "saved"
            assert (data_dir / "workstreams" / "test-ws" / "state.md").read_text() == "# Brand New"
            assert not (data_dir / "workstreams" / "test-ws" / "state.md.bak").exists()
        finally:
            conn.close()
```

**Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_save_workstream.py -v`
Expected: FAIL (save_workstream doesn't exist)

**Step 3: Write the implementation**

Add to `server/relay_server/workstreams.py`:

```python
import sqlite3

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
        # os.replace is atomic and cross-platform
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
```

Add the MCP tool to `server/relay_server/server.py` (after `summarize_activity`):

```python
@mcp.tool()
def save_workstream(
    name: str,
    state_content: str,
    ctx: Context[ServerSession, AppContext],
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
) -> dict:
    """Save workstream state to disk and write session hint.

    Atomically writes the state file (with backup), updates the registry
    last_touched timestamp, and writes a session hint to the database.

    Args:
        name: Workstream name (e.g. "relay")
        state_content: Full markdown content for state.md (keep under 80 lines)
        session_id: Current session UUID (from relay-session-id context). If omitted, no hint is written.
        hint_summary: 3-6 bullets describing what was accomplished this session
        hint_decisions: Key decisions made (omit if none)
    """
    from .workstreams import get_data_dir
    from .workstreams import save_workstream as _save

    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)
    try:
        return _save(
            data_dir=get_data_dir(),
            conn=conn,
            name=name,
            state_content=state_content,
            session_id=session_id,
            hint_summary=hint_summary,
            hint_decisions=hint_decisions,
        )
    finally:
        conn.close()
```

**Step 4: Run tests to verify they pass**

Run: `cd server && uv run pytest tests/test_save_workstream.py -v`
Expected: PASS (all 6 tests)

Run: `cd server && uv run pytest -q`
Expected: All tests pass (no regressions)

**Step 5: Commit**

```bash
git add server/relay_server/workstreams.py server/relay_server/server.py server/tests/test_save_workstream.py
git commit -m "feat: add save_workstream MCP tool — atomic state save + hint write in one call"
```

---

### Task 4: Implement create_workstream and park_workstream MCP tools

**Files:**
- Modify: `server/relay_server/workstreams.py` (add functions)
- Modify: `server/relay_server/server.py` (add tools)
- Test: `server/tests/test_create_park_workstream.py`

These two tools are simpler versions of the save flow. `create_workstream` adds a new workstream to the registry and writes the initial state file. `park_workstream` is save + set status to parked.

**Step 1: Write the tests**

```python
# server/tests/test_create_park_workstream.py
"""Tests for create_workstream and park_workstream."""

import json
import tempfile
from pathlib import Path

from relay_server.db import ensure_schema, get_connection
from relay_server.workstreams import read_registry


def _setup(tmpdir):
    db_path = Path(tmpdir) / "test.db"
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()
    ensure_schema(db_path)
    (data_dir / "workstreams.json").write_text('{"version": 1, "workstreams": {}}')
    conn = get_connection(db_path)
    return db_path, data_dir, conn


def test_create_workstream():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import create_workstream
            result = create_workstream(
                data_dir=data_dir,
                name="my-project",
                description="A cool project",
                project_dir="/home/test/my-project",
            )
            assert result["status"] == "created"

            reg = read_registry(data_dir)
            assert "my-project" in reg["workstreams"]
            assert reg["workstreams"]["my-project"]["status"] == "active"

            state = (data_dir / "workstreams" / "my-project" / "state.md").read_text()
            assert "my-project" in state
            assert "A cool project" in state
        finally:
            conn.close()


def test_create_duplicate_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import create_workstream
            create_workstream(data_dir=data_dir, name="ws1", description="First")
            result = create_workstream(data_dir=data_dir, name="ws1", description="Dupe")
            assert result["status"] == "error"
        finally:
            conn.close()


def test_park_workstream():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        # Create and set up a workstream
        from relay_server.workstreams import create_workstream, park_workstream
        create_workstream(data_dir=data_dir, name="ws1", description="Test")
        conn.execute(
            """INSERT INTO sessions (session_id, project_dir, first_timestamp, last_timestamp, message_count)
               VALUES ('aabbccdd-1122-3344-5566-778899aabbcc', '/test', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', 10)"""
        )
        conn.commit()
        try:
            result = park_workstream(
                data_dir=data_dir,
                conn=conn,
                name="ws1",
                state_content="# Parked State",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
                hint_summary=["Did stuff"],
            )
            assert result["status"] == "parked"

            reg = read_registry(data_dir)
            assert reg["workstreams"]["ws1"]["status"] == "parked"

            state = (data_dir / "workstreams" / "ws1" / "state.md").read_text()
            assert "Parked State" in state
        finally:
            conn.close()


def test_park_nonexistent_fails():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import park_workstream
            result = park_workstream(
                data_dir=data_dir, conn=conn, name="nope", state_content="x"
            )
            assert result["status"] == "error"
        finally:
            conn.close()
```

**Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_create_park_workstream.py -v`
Expected: FAIL

**Step 3: Write the implementation**

Add to `server/relay_server/workstreams.py`:

```python
def create_workstream(
    *,
    data_dir: Path,
    name: str,
    description: str = "",
    project_dir: str = "",
) -> dict:
    """Create a new workstream: add to registry, write initial state file."""
    registry = read_registry(data_dir)
    if name in registry["workstreams"]:
        return {"status": "error", "message": f"Workstream '{name}' already exists"}

    date = today()
    entry = {
        "status": "active",
        "description": description,
        "created": date,
        "last_touched": date,
        "project_dir": project_dir,
    }
    registry["workstreams"][name] = entry
    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")

    # Write initial state file
    state = f"""# {name}

## Metadata
- **Description:** {description}
- **Created:** {date}
- **Project dir:** {project_dir}

## Current Status
New workstream — no work done yet.

## Key Decisions
(none yet)

## Next Steps
- Define initial goals
"""
    ws_dir = data_dir / "workstreams" / name
    ws_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(ws_dir / "state.md", state)

    return {
        "status": "created",
        "workstream": name,
        "state_file": str(ws_dir / "state.md"),
    }


def park_workstream(
    *,
    data_dir: Path,
    conn: sqlite3.Connection,
    name: str,
    state_content: str,
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
) -> dict:
    """Save state then set workstream status to parked."""
    registry = read_registry(data_dir)
    if name not in registry["workstreams"]:
        return {"status": "error", "message": f"Workstream '{name}' not found"}

    # Save state first (reuse save logic)
    save_result = save_workstream(
        data_dir=data_dir,
        conn=conn,
        name=name,
        state_content=state_content,
        session_id=session_id,
        hint_summary=hint_summary,
        hint_decisions=hint_decisions,
    )

    # Set status to parked
    registry = read_registry(data_dir)  # re-read (save_workstream updated it)
    registry["workstreams"][name]["status"] = "parked"
    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")

    return {
        "status": "parked",
        "workstream": name,
        **{k: v for k, v in save_result.items() if k != "status"},
    }
```

Add MCP tools to `server/relay_server/server.py`:

```python
@mcp.tool()
def create_workstream(
    name: str,
    description: str,
    ctx: Context[ServerSession, AppContext],
    project_dir: str = "",
) -> dict:
    """Create a new workstream with initial state file.

    Args:
        name: Workstream name (lowercase, hyphens, e.g. "api-refactor")
        description: Brief description of the workstream
        project_dir: Project directory path (optional)
    """
    from .workstreams import get_data_dir
    from .workstreams import create_workstream as _create
    return _create(data_dir=get_data_dir(), name=name, description=description, project_dir=project_dir)


@mcp.tool()
def park_workstream(
    name: str,
    state_content: str,
    ctx: Context[ServerSession, AppContext],
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
) -> dict:
    """Save workstream state and set status to parked.

    Args:
        name: Workstream name to park
        state_content: Full markdown content for state.md (keep under 80 lines)
        session_id: Current session UUID (from relay-session-id context)
        hint_summary: 3-6 bullets describing what was accomplished
        hint_decisions: Key decisions made (omit if none)
    """
    from .workstreams import get_data_dir
    from .workstreams import park_workstream as _park

    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)
    try:
        return _park(
            data_dir=get_data_dir(), conn=conn, name=name, state_content=state_content,
            session_id=session_id, hint_summary=hint_summary, hint_decisions=hint_decisions,
        )
    finally:
        conn.close()
```

**Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_create_park_workstream.py -v`
Expected: PASS

Run: `cd server && uv run pytest -q`
Expected: All pass

**Step 5: Commit**

```bash
git add server/relay_server/workstreams.py server/relay_server/server.py server/tests/test_create_park_workstream.py
git commit -m "feat: add create_workstream and park_workstream MCP tools"
```

---

### Task 5: Implement switch_workstream and list_workstreams MCP tools

**Files:**
- Modify: `server/relay_server/workstreams.py`
- Modify: `server/relay_server/server.py`
- Test: `server/tests/test_switch_list_workstream.py`

`switch_workstream` saves the current workstream, activates the target, writes a session marker, and returns the target's state content. `list_workstreams` reads the registry and ideas file and returns structured data.

**Step 1: Write the tests**

```python
# server/tests/test_switch_list_workstream.py
"""Tests for switch_workstream and list_workstreams."""

import json
import tempfile
from pathlib import Path

from relay_server.db import ensure_schema, get_connection


def _setup(tmpdir):
    db_path = Path(tmpdir) / "test.db"
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()
    ensure_schema(db_path)

    registry = {
        "version": 1,
        "workstreams": {
            "alpha": {"status": "active", "description": "Alpha", "created": "2026-01-01", "last_touched": "2026-01-01", "project_dir": "/alpha"},
            "beta": {"status": "parked", "description": "Beta", "created": "2026-01-01", "last_touched": "2026-01-01", "project_dir": "/beta"},
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))
    for ws in ("alpha", "beta"):
        d = data_dir / "workstreams" / ws
        d.mkdir(parents=True)
        (d / "state.md").write_text(f"# {ws.title()} State")

    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO sessions (session_id, project_dir, first_timestamp, last_timestamp, message_count)
           VALUES ('aabbccdd-1122-3344-5566-778899aabbcc', '/test', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', 10)"""
    )
    conn.commit()
    return db_path, data_dir, conn


def test_switch_saves_current_and_loads_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import switch_workstream, read_registry
            result = switch_workstream(
                data_dir=data_dir,
                conn=conn,
                from_name="alpha",
                to_name="beta",
                state_content="# Alpha saved",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
                hint_summary=["Wrapped up alpha work"],
            )
            assert result["status"] == "switched"
            assert "Beta State" in result["target_state"]

            reg = read_registry(data_dir)
            assert reg["workstreams"]["alpha"]["status"] == "active"  # stays active
            assert reg["workstreams"]["beta"]["status"] == "active"  # now active

            # Marker points to beta
            row = conn.execute(
                "SELECT workstream FROM session_markers WHERE session_id = 'aabbccdd-1122-3344-5566-778899aabbcc'"
            ).fetchone()
            assert row["workstream"] == "beta"
        finally:
            conn.close()


def test_switch_without_from():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import switch_workstream
            result = switch_workstream(
                data_dir=data_dir,
                conn=conn,
                to_name="beta",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
            )
            assert result["status"] == "switched"
        finally:
            conn.close()


def test_switch_nonexistent_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import switch_workstream
            result = switch_workstream(
                data_dir=data_dir, conn=conn, to_name="nope",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
            )
            assert result["status"] == "error"
        finally:
            conn.close()


def test_list_workstreams():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        # Write ideas file
        ideas = [{"id": 1, "text": "try websockets", "added": "2026-03-01"}]
        (data_dir / "ideas.json").write_text(json.dumps(ideas))
        try:
            from relay_server.workstreams import list_workstreams
            result = list_workstreams(data_dir=data_dir)
            assert "alpha" in [w["name"] for w in result["active"]]
            assert "beta" in [w["name"] for w in result["parked"]]
            assert len(result["ideas"]) == 1
        finally:
            conn.close()


def test_list_workstreams_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        (data_dir / "workstreams.json").write_text('{"version": 1, "workstreams": {}}')
        from relay_server.workstreams import list_workstreams
        result = list_workstreams(data_dir=data_dir)
        assert result["active"] == []
        assert result["parked"] == []
        assert result["completed"] == []
        assert result["ideas"] == []
```

**Step 2: Run tests to verify they fail**

Run: `cd server && uv run pytest tests/test_switch_list_workstream.py -v`
Expected: FAIL

**Step 3: Write the implementation**

Add to `server/relay_server/workstreams.py`:

```python
def switch_workstream(
    *,
    data_dir: Path,
    conn: sqlite3.Connection,
    to_name: str,
    from_name: str | None = None,
    state_content: str | None = None,
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
) -> dict:
    """Switch session from one workstream to another.

    Saves current workstream (if from_name provided), activates target,
    writes session marker, returns target state content.
    """
    registry = read_registry(data_dir)
    if to_name not in registry["workstreams"]:
        return {"status": "error", "message": f"Workstream '{to_name}' not found"}

    # Save current workstream if provided
    if from_name and state_content:
        save_workstream(
            data_dir=data_dir,
            conn=conn,
            name=from_name,
            state_content=state_content,
            session_id=session_id,
            hint_summary=hint_summary,
            hint_decisions=hint_decisions,
        )

    # Activate target
    registry = read_registry(data_dir)  # re-read after save
    registry["workstreams"][to_name]["status"] = "active"
    registry["workstreams"][to_name]["last_touched"] = today()
    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")

    # Write session marker
    if session_id:
        conn.execute(
            """INSERT OR REPLACE INTO session_markers
               (session_id, workstream, attached_at)
               VALUES (?, ?, ?)""",
            (session_id, to_name, utc_timestamp()),
        )
        conn.commit()

    # Read target state
    target_state = ""
    state_path = data_dir / "workstreams" / to_name / "state.md"
    if state_path.exists():
        target_state = state_path.read_text()

    # Read supplementary files
    supplementary = {}
    for extra in ("plan.md", "architecture.md"):
        extra_path = data_dir / "workstreams" / to_name / extra
        if extra_path.exists():
            supplementary[extra] = extra_path.read_text()

    return {
        "status": "switched",
        "from": from_name,
        "to": to_name,
        "target_state": target_state,
        "supplementary": supplementary,
        "project_dir": registry["workstreams"][to_name].get("project_dir", ""),
    }


def list_workstreams(*, data_dir: Path) -> dict:
    """List all workstreams grouped by status, plus ideas."""
    registry = read_registry(data_dir)

    groups: dict[str, list] = {"active": [], "parked": [], "completed": []}
    for name, ws in registry.get("workstreams", {}).items():
        status = ws.get("status", "parked")
        bucket = groups.get(status, groups["parked"])
        bucket.append({
            "name": name,
            "description": ws.get("description", ""),
            "last_touched": ws.get("last_touched", ""),
            "project_dir": ws.get("project_dir", ""),
        })

    # Read ideas
    ideas = []
    ideas_path = data_dir / "ideas.json"
    if ideas_path.exists():
        try:
            ideas = json.loads(ideas_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    return {**groups, "ideas": ideas}
```

Add MCP tools to `server/relay_server/server.py`:

```python
@mcp.tool()
def switch_workstream(
    to_name: str,
    ctx: Context[ServerSession, AppContext],
    from_name: str | None = None,
    state_content: str | None = None,
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
) -> dict:
    """Switch session to a different workstream. Saves current first if provided.

    Both workstreams stay active. Returns the target workstream's state content
    and any supplementary files (plan.md, architecture.md).

    Args:
        to_name: Target workstream name
        from_name: Current workstream name (if saving before switch)
        state_content: State to save for current workstream (required if from_name set)
        session_id: Current session UUID (from relay-session-id context)
        hint_summary: Bullets for current workstream session hint
        hint_decisions: Decisions for current workstream session hint
    """
    from .workstreams import get_data_dir
    from .workstreams import switch_workstream as _switch

    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)
    try:
        return _switch(
            data_dir=get_data_dir(), conn=conn, to_name=to_name, from_name=from_name,
            state_content=state_content, session_id=session_id,
            hint_summary=hint_summary, hint_decisions=hint_decisions,
        )
    finally:
        conn.close()


@mcp.tool()
def list_workstreams(ctx: Context[ServerSession, AppContext]) -> dict:
    """List all workstreams grouped by status (active, parked, completed) plus ideas.

    Returns structured data with workstream names, descriptions, last_touched dates,
    and any captured ideas.
    """
    from .workstreams import get_data_dir
    from .workstreams import list_workstreams as _list
    return _list(data_dir=get_data_dir())
```

**Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_switch_list_workstream.py -v`
Expected: PASS

Run: `cd server && uv run pytest -q`
Expected: All pass

**Step 5: Commit**

```bash
git add server/relay_server/workstreams.py server/relay_server/server.py server/tests/test_switch_list_workstream.py
git commit -m "feat: add switch_workstream and list_workstreams MCP tools"
```

---

### Task 6: Implement manage_idea MCP tool

**Files:**
- Modify: `server/relay_server/workstreams.py`
- Modify: `server/relay_server/server.py`
- Test: `server/tests/test_manage_idea.py`

Consolidates idea add/remove into a single tool. Replaces 2 bash calls per idea operation.

**Step 1: Write the tests**

```python
# server/tests/test_manage_idea.py
"""Tests for manage_idea."""

import json
import tempfile
from pathlib import Path


def test_add_idea():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        from relay_server.workstreams import manage_idea
        result = manage_idea(data_dir=data_dir, action="add", text="try websockets")
        assert result["status"] == "added"
        assert result["id"] == 1

        ideas = json.loads((data_dir / "ideas.json").read_text())
        assert len(ideas) == 1
        assert ideas[0]["text"] == "try websockets"


def test_add_idea_increments_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        from relay_server.workstreams import manage_idea
        manage_idea(data_dir=data_dir, action="add", text="idea 1")
        result = manage_idea(data_dir=data_dir, action="add", text="idea 2")
        assert result["id"] == 2


def test_remove_idea():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        from relay_server.workstreams import manage_idea
        manage_idea(data_dir=data_dir, action="add", text="idea 1")
        manage_idea(data_dir=data_dir, action="add", text="idea 2")
        result = manage_idea(data_dir=data_dir, action="remove", idea_id=1)
        assert result["status"] == "removed"

        ideas = json.loads((data_dir / "ideas.json").read_text())
        assert len(ideas) == 1
        assert ideas[0]["id"] == 2


def test_remove_nonexistent():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        from relay_server.workstreams import manage_idea
        result = manage_idea(data_dir=data_dir, action="remove", idea_id=99)
        assert result["status"] == "error"


def test_list_ideas():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        from relay_server.workstreams import manage_idea
        manage_idea(data_dir=data_dir, action="add", text="idea 1")
        manage_idea(data_dir=data_dir, action="add", text="idea 2")
        result = manage_idea(data_dir=data_dir, action="list")
        assert len(result["ideas"]) == 2
```

**Step 2-3: Implement**

Add to `server/relay_server/workstreams.py`:

```python
def manage_idea(
    *,
    data_dir: Path,
    action: str,
    text: str | None = None,
    idea_id: int | None = None,
) -> dict:
    """Add, remove, or list ideas."""
    ideas_path = data_dir / "ideas.json"
    ideas = []
    if ideas_path.exists():
        try:
            ideas = json.loads(ideas_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if action == "list":
        return {"status": "ok", "ideas": ideas}

    if action == "add":
        if not text:
            return {"status": "error", "message": "Text is required"}
        new_id = max((i.get("id", 0) for i in ideas), default=0) + 1
        ideas.append({"id": new_id, "text": text, "added": today()})
        atomic_write(ideas_path, json.dumps(ideas, indent=2) + "\n")
        return {"status": "added", "id": new_id, "text": text}

    if action == "remove":
        if idea_id is None:
            return {"status": "error", "message": "idea_id is required"}
        original_len = len(ideas)
        ideas = [i for i in ideas if i.get("id") != idea_id]
        if len(ideas) == original_len:
            return {"status": "error", "message": f"Idea {idea_id} not found"}
        atomic_write(ideas_path, json.dumps(ideas, indent=2) + "\n")
        return {"status": "removed", "id": idea_id}

    return {"status": "error", "message": f"Unknown action: {action}"}
```

Add MCP tool to `server/relay_server/server.py`:

```python
@mcp.tool()
def manage_idea(
    action: str,
    ctx: Context[ServerSession, AppContext],
    text: str | None = None,
    idea_id: int | None = None,
) -> dict:
    """Add, remove, or list ideas for future work.

    Args:
        action: "add", "remove", or "list"
        text: Idea text (required for "add")
        idea_id: Idea ID number (required for "remove")
    """
    from .workstreams import get_data_dir
    from .workstreams import manage_idea as _manage
    return _manage(data_dir=get_data_dir(), action=action, text=text, idea_id=idea_id)
```

**Step 4: Run tests**

Run: `cd server && uv run pytest tests/test_manage_idea.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add server/relay_server/workstreams.py server/relay_server/server.py server/tests/test_manage_idea.py
git commit -m "feat: add manage_idea MCP tool — add/remove/list ideas"
```

---

### Task 7: Update all skills to use MCP tools

**Files:**
- Modify: `skills/save/SKILL.md`
- Modify: `skills/new/SKILL.md`
- Modify: `skills/park/SKILL.md`
- Modify: `skills/switch/SKILL.md`
- Modify: `skills/list/SKILL.md`
- Modify: `skills/status/SKILL.md`
- Modify: `skills/idea/SKILL.md`

Each skill shrinks to 1-2 MCP calls. The skills retain the domain knowledge (what to include in state content, how to format output) but delegate all file/registry operations to the server.

**Step 1: Rewrite each skill**

`skills/save/SKILL.md` — shrinks from 6 steps to 2:
```markdown
## Steps

1. **Find active workstream.** Use the `list_workstreams` MCP tool to find which workstream is active. If none, tell the user and suggest `/relay:new`.

2. **Save.** Call `save_workstream` with the state content and session hint:
   ```
   save_workstream(
     name="<active workstream>",
     state_content="<80-line state markdown>",
     session_id="<from relay-session-id context>",
     hint_summary=["<3-6 bullets>"],
     hint_decisions=["<decisions if any>"]
   )
   ```
   State content guidelines: [keep existing section about content format]

3. **Confirm.** Tell the user the state was saved.
```

`skills/new/SKILL.md` — shrinks from 8 steps to 3:
```markdown
## Steps

1. **Parse arguments.** [same as current]

2. **Create workstream.** Call `create_workstream`:
   ```
   create_workstream(name="<name>", description="<desc>", project_dir="<cwd>")
   ```
   If it returns an error (duplicate), tell the user.

3. **Check for matching ideas.** Call `manage_idea(action="list")`. If any match, ask user if they want to remove. If yes, call `manage_idea(action="remove", idea_id=<id>)`.

4. **Confirm.** Tell the user the workstream was created.
```

`skills/park/SKILL.md` — shrinks from 5 steps to 2:
```markdown
## Steps

1. **Determine target.** [same as current, but use list_workstreams to find active]

2. **Park.** Call `park_workstream`:
   ```
   park_workstream(
     name="<name>",
     state_content="<80-line state markdown>",
     session_id="<from context>",
     hint_summary=["<bullets>"],
     hint_decisions=["<decisions>"]
   )
   ```

3. **Confirm.** Tell the user the workstream is parked.
```

`skills/switch/SKILL.md` — shrinks from 9 steps to 3:
```markdown
## Steps

1. **Parse arguments.** [same] If empty, call `list_workstreams` and ask user.

2. **Switch.** Call `switch_workstream`:
   ```
   switch_workstream(
     to_name="<target>",
     from_name="<current from context>",
     state_content="<state for current>",
     session_id="<from context>",
     hint_summary=["<bullets>"],
     hint_decisions=["<decisions>"]
   )
   ```
   The response includes `target_state` and `supplementary` files.

3. **Present.** Show the target workstream's state. Mention project_dir if set.
```

`skills/list/SKILL.md` — shrinks from 4 steps to 2:
```markdown
## Steps

1. **Fetch data.** Call `list_workstreams`. Returns active, parked, completed arrays and ideas.

2. **Display.** Format as grouped tables. [keep existing format spec]
```

`skills/status/SKILL.md` — keep as-is for now (reads session context which MCP can't access). Could use `list_workstreams` instead of bash for the registry read.

`skills/idea/SKILL.md` — shrinks to `manage_idea` calls:
```markdown
## Subcommand: add
1. Call `manage_idea(action="add", text="<idea text>")`
2. Confirm with ID.

## Subcommand: promote
1. Call `manage_idea(action="list")` to find the idea.
2. Call `manage_idea(action="remove", idea_id=<id>)`.
3. Invoke `/relay:new` with the idea text.
```

**Step 2: Write each SKILL.md with full content** (preserving the domain knowledge sections about state file format, content priorities, etc.)

**Step 3: Test by reading each skill and verifying no bash calls remain** (except in status which still reads session context)

**Step 4: Commit**

```bash
git add skills/
git commit -m "refactor: update all skills to use MCP tools instead of bash scripts"
```

---

### Task 8: Update session-start.sh to write markers to DB via hook output

**Files:**
- Modify: `scripts/session-start.sh`
- Modify: `server/relay_server/server.py` (enhance `summarize_activity` to read markers from DB)
- Modify: `server/relay_server/indexer.py` (read markers from DB table as well as files)

Session-start.sh still runs as a bash hook and writes JSON marker files (hooks can't call MCP). But the MCP server now also writes markers to SQLite. The indexer should prefer DB markers over file markers.

**Step 1:** Update `_read_marker_workstream` in `server.py` to check DB first, file fallback:

```python
def _read_marker_workstream(session_id: str, conn: sqlite3.Connection | None = None, markers_dir: Path | None = None) -> str | None:
    # Try DB first
    if conn:
        row = conn.execute(
            "SELECT workstream FROM session_markers WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row:
            return row["workstream"]
    # Fall back to file
    if markers_dir is None:
        markers_dir = _get_markers_dir()
    marker_path = markers_dir / f"{session_id}.json"
    if not marker_path.exists():
        return None
    try:
        with open(marker_path) as f:
            return json.load(f).get("workstream")
    except (json.JSONDecodeError, OSError):
        return None
```

**Step 2:** Update `_summarize_activity_impl` to pass `conn` to `_read_marker_workstream`.

**Step 3:** Update indexer `_apply_session_markers` to also check DB table.

**Step 4:** Test and commit.

```bash
git commit -m "feat: read session markers from DB with file fallback"
```

---

### Task 9: Update READMEs, CHANGELOG, version bump, cleanup

**Files:**
- Modify: `README.md` (update tool table, remove bash script references from "How It Works")
- Modify: `server/README.md` (add new tools)
- Modify: `CHANGELOG.md` (new version entry)
- Modify: `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` (version bump)

**Step 1:** Add all new MCP tools to the README tool tables.

**Step 2:** Update "How It Works" section — hooks remain but skills now use MCP tools.

**Step 3:** Write CHANGELOG entry documenting the migration.

**Step 4:** Bump version to 0.10.0 (significant architectural change).

**Step 5:** Run full test suite: `cd server && uv run pytest -v`

**Step 6:** Run shell script e2e tests to verify hooks still work.

**Step 7:** Commit and push.

```bash
git add README.md server/README.md CHANGELOG.md .claude-plugin/
git commit -m "docs: update for MCP migration, bump to v0.10.0"
git push
```

---

### Scripts that can be removed after migration

Once all skills use MCP tools, these scripts become dead code (only referenced by old skill versions):
- `complete-save.sh` — replaced by `save_workstream` MCP tool
- `new-registry.sh` — replaced by `create_workstream`
- `park-registry.sh` — replaced by `park_workstream`
- `switch-registry.sh` — replaced by `switch_workstream`
- `update-registry.sh` — replaced by save/park/switch tools
- `reset-counter.sh` — counter reset can be done by save tool
- `read-data-file.sh` — replaced by `list_workstreams` / direct MCP reads
- `write-data-file.sh` — replaced by atomic writes in workstreams module

**Keep:**
- `common.sh` — still used by hooks
- `session-start.sh`, `session-end.sh`, `context-monitor.sh`, `pre-compact-save.sh` — hooks (must stay bash)
- `approve-scripts.sh` — still needed for any remaining bash commands
- `attach-workstream.sh` — still called by session-start.sh for multi-active prompt
- `init-data-dir.sh` — still called by session-start.sh
- `migrate-data.sh`, `migrate-from-workstreams.sh` — one-time migration tools

Don't remove scripts until the MCP tools are confirmed working in production for at least one session cycle. Remove in a follow-up commit.

---

### Summary: Before vs After

| Skill | Bash calls before | MCP calls after |
|---|---|---|
| `/relay:save` | 5 | 1 (`save_workstream`) |
| `/relay:new` | 4-5 | 1-2 (`create_workstream` + optional `manage_idea`) |
| `/relay:park` | 6 | 1 (`park_workstream`) |
| `/relay:switch` | 7+ | 1 (`switch_workstream`) |
| `/relay:list` | 2 | 1 (`list_workstreams`) |
| `/relay:status` | 2-3 | 1 (`list_workstreams`) |
| `/relay:idea` | 2 | 1 (`manage_idea`) |
| `/relay:summarize` | 3+ | 1 (`summarize_activity`) — already done |
| **Total per cycle** | **~30** | **~8** |
