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


def test_list_workstreams_markdown():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        ideas = [{"id": 1, "text": "try websockets", "added": "2026-03-01"}]
        (data_dir / "ideas.json").write_text(json.dumps(ideas))
        try:
            from relay_server.workstreams import list_workstreams
            result = list_workstreams(data_dir=data_dir)
            assert isinstance(result, str)
            assert "## Active" in result
            assert "## Parked" in result
            assert "| alpha |" in result
            assert "| beta |" in result
            assert "## Ideas" in result
            assert "try websockets" in result
            assert "/relay:idea promote" in result
            assert "**Commands:**" in result
        finally:
            conn.close()


def test_list_workstreams_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        ideas = [{"id": 1, "text": "try websockets", "added": "2026-03-01"}]
        (data_dir / "ideas.json").write_text(json.dumps(ideas))
        try:
            from relay_server.workstreams import list_workstreams
            result = list_workstreams(data_dir=data_dir, format="json")
            assert isinstance(result, dict)
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
        # Markdown: should still have commands line
        result = list_workstreams(data_dir=data_dir)
        assert isinstance(result, str)
        assert "**Commands:**" in result
        assert "## Active" not in result

        # JSON: empty arrays
        result_json = list_workstreams(data_dir=data_dir, format="json")
        assert result_json["active"] == []
        assert result_json["parked"] == []
        assert result_json["completed"] == []
        assert result_json["ideas"] == []


def test_get_status_attached():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        # Write a richer state file for alpha
        (data_dir / "workstreams" / "alpha" / "state.md").write_text(
            "# alpha\n\n## Current Status\nWorking on feature X.\n\n## Next Steps\n1. Finish X\n2. Start Y\n"
        )
        try:
            from relay_server.workstreams import get_status
            result = get_status(data_dir=data_dir, attached="alpha")
            assert isinstance(result, str)
            assert "## Attached: alpha" in result
            assert "**Description:** Alpha" in result
            assert "**Project:** /alpha" in result
            assert "### Current Status" in result
            assert "Working on feature X" in result
            assert "### Next Steps" in result
            assert "Finish X" in result
            # beta should be in "other" parked
            assert "beta" in result
            assert "**Parked:**" in result
            assert "**Commands:**" in result
        finally:
            conn.close()


def test_get_status_no_attached():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import get_status
            result = get_status(data_dir=data_dir)
            assert "No workstream attached" in result
            assert "alpha" in result  # should appear in other active
            assert "**Commands:**" in result
        finally:
            conn.close()


def test_get_status_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        (data_dir / "workstreams" / "alpha" / "state.md").write_text(
            "# alpha\n\n## Current Status\nWorking on X.\n\n## Next Steps\n1. Finish X\n"
        )
        try:
            from relay_server.workstreams import get_status
            result = get_status(data_dir=data_dir, attached="alpha", format="json")
            assert isinstance(result, dict)
            assert result["attached"]["name"] == "alpha"
            assert result["attached"]["description"] == "Alpha"
            assert "Working on X" in result["attached"]["current_status"]
            assert "Finish X" in result["attached"]["next_steps"]
            assert "beta" in result["others"]["parked"]
            assert "alpha" not in result["others"]["active"]
        finally:
            conn.close()


def test_get_status_excludes_attached_from_other():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import get_status
            result = get_status(data_dir=data_dir, attached="alpha")
            # alpha should NOT appear in the "Other active" line
            other_active_line = [l for l in result.split("\n") if "**Other active:**" in l][0]
            assert "alpha" not in other_active_line
        finally:
            conn.close()
