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
