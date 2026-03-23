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


def test_save_with_unknown_session_id():
    """save_workstream should auto-create a session placeholder for FK satisfaction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import save_workstream
            unknown_sid = "deadbeef-0000-0000-0000-000000000000"
            result = save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# State",
                session_id=unknown_sid,
                hint_summary=["First hint for new session"],
            )
            assert result["status"] == "saved"
            assert result["hint_written"] is True

            # Session placeholder was created
            session = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (unknown_sid,)
            ).fetchone()
            assert session is not None
            assert session["message_count"] == 0

            # Hint was written
            hint = conn.execute(
                "SELECT * FROM session_hints WHERE session_id = ?", (unknown_sid,)
            ).fetchone()
            assert hint is not None
            assert "First hint" in hint["summary"]
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


def test_save_stores_stash_ref():
    """save_workstream stores stash_ref in registry git block."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        try:
            # Add a git block to the test workstream's registry entry
            registry = json.loads((data_dir / "workstreams.json").read_text())
            registry["workstreams"]["test-ws"]["git"] = {"strategy": "branch", "branch": "main"}
            (data_dir / "workstreams.json").write_text(json.dumps(registry))

            from relay_server.workstreams import save_workstream
            result = save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# State",
                stash_ref="abc123def456",
            )
            assert result["status"] == "saved"

            reg = read_registry(data_dir)
            git = reg["workstreams"]["test-ws"]["git"]
            assert git["stash_ref"] == "abc123def456"
            assert "stash_message" in git
        finally:
            conn.close()


def test_save_clear_stash():
    """save_workstream with clear_stash=True removes stash_ref from registry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup(tmpdir)
        try:
            # Pre-populate git block with stash info
            registry = json.loads((data_dir / "workstreams.json").read_text())
            registry["workstreams"]["test-ws"]["git"] = {
                "strategy": "branch",
                "branch": "main",
                "stash_ref": "oldstashref",
                "stash_message": "relay: test-ws at 2026-01-01T00:00:00Z",
            }
            (data_dir / "workstreams.json").write_text(json.dumps(registry))

            from relay_server.workstreams import save_workstream
            result = save_workstream(
                data_dir=data_dir,
                conn=conn,
                name="test-ws",
                state_content="# State",
                clear_stash=True,
            )
            assert result["status"] == "saved"

            reg = read_registry(data_dir)
            git = reg["workstreams"]["test-ws"]["git"]
            assert "stash_ref" not in git
            assert "stash_message" not in git
            # git block itself still exists with strategy/branch preserved
            assert git["strategy"] == "branch"
            assert git["branch"] == "main"
        finally:
            conn.close()
