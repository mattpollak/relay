"""Tests for summarize_activity tool logic."""

import json
import tempfile
from pathlib import Path

from relay_server.db import ensure_schema, get_connection


def _setup_db(tmpdir):
    """Create a test DB with sessions and hints."""
    db_path = Path(tmpdir) / "test.db"
    ensure_schema(db_path)
    conn = get_connection(db_path)

    # Three sessions across two days
    conn.execute(
        """INSERT INTO sessions
           (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
           VALUES ('s1', '/test', 'brave-coding-fox', '2026-03-04T10:00:00Z', '2026-03-04T12:00:00Z', 100)"""
    )
    conn.execute(
        """INSERT INTO sessions
           (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
           VALUES ('s2', '/test', 'brave-coding-fox', '2026-03-04T13:00:00Z', '2026-03-04T15:00:00Z', 80)"""
    )
    conn.execute(
        """INSERT INTO sessions
           (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
           VALUES ('s3', '/test', 'calm-testing-owl', '2026-03-05T08:00:00Z', '2026-03-05T10:00:00Z', 50)"""
    )

    # Hints: s1 has hints (squadkeeper), s2 has hints (relay), s3 has no hints
    conn.execute(
        """INSERT INTO session_hints
           (session_id, timestamp, source_file, workstream, summary, decisions)
           VALUES ('s1', '2026-03-04T12:00:00Z', 'h1.json', 'squadkeeper',
                   ?, ?)""",
        (json.dumps(["Built auth system", "Added login page"]), json.dumps(["Use JWT tokens"])),
    )
    conn.execute(
        """INSERT INTO session_hints
           (session_id, timestamp, source_file, workstream, summary)
           VALUES ('s2', '2026-03-04T15:00:00Z', 'h2.json', 'relay',
                   ?)""",
        (json.dumps(["Added search tool", "Fixed indexer bug"]),),
    )
    conn.commit()
    return db_path, conn


def test_basic_grouping():
    """Sessions group by workstream from hints."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(conn, "2026-03-04", markers_dir=markers_dir)

            assert "### squadkeeper" in result
            assert "### relay" in result
            assert "Built auth system" in result
            assert "Added search tool" in result
        finally:
            conn.close()


def test_marker_fallback():
    """Sessions without hints use marker files for workstream."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        # Write a marker for s3
        (markers_dir / "s3.json").write_text(json.dumps({"workstream": "squadkeeper"}))
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(conn, "2026-03-04", markers_dir=markers_dir)

            # s3 should appear under squadkeeper, not "other"
            assert "### other" not in result
            assert "calm-testing-owl" in result
            assert "no hints" in result
        finally:
            conn.close()


def test_no_marker_goes_to_other():
    """Sessions without hints, markers, or project_dir match go to 'other'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        # Empty data_dir so project_dir inference has no registry
        data_dir = Path(tmpdir) / "empty-data"
        data_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(
                conn, "2026-03-04", markers_dir=markers_dir, data_dir=data_dir,
            )

            assert "### other" in result
        finally:
            conn.close()


def test_workstream_filter():
    """Workstream param filters to single workstream."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(
                conn, "2026-03-04", workstream="relay", markers_dir=markers_dir,
            )

            assert "### relay" in result
            assert "### squadkeeper" not in result
        finally:
            conn.close()


def test_date_range_filtering():
    """date_to limits sessions returned."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            # Only March 4 — should exclude s3 (March 5)
            result = _summarize_activity_impl(
                conn, "2026-03-04", date_to="2026-03-04", markers_dir=markers_dir,
            )

            assert "brave-coding-fox" in result
            assert "calm-testing-owl" not in result
        finally:
            conn.close()


def test_no_sessions():
    """Empty date range returns no-sessions message."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(
                conn, "2099-01-01", markers_dir=markers_dir,
            )

            assert "No sessions found" in result
        finally:
            conn.close()


def test_slug_grouping():
    """Multiple sessions with same slug are grouped together."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()

        # Add another hint for s2 under squadkeeper (same slug as s1: brave-coding-fox)
        # Actually s1 and s2 share the slug. Let's add a hint for s1 that's also squadkeeper.
        # s1 already has squadkeeper hint. s2 has relay hint. They share slug brave-coding-fox.
        # They should appear as separate workstream entries.
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(conn, "2026-03-04", markers_dir=markers_dir)

            # brave-coding-fox should appear under both squadkeeper and relay
            assert result.count("brave-coding-fox") >= 2
        finally:
            conn.close()


def test_decisions_included():
    """Decisions from hints appear in output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(conn, "2026-03-04", markers_dir=markers_dir)

            assert "Use JWT tokens" in result
        finally:
            conn.close()


def test_deduplicates_bullets():
    """Duplicate hint bullets within a slug group are deduplicated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        # Add a second hint for s1 with overlapping bullets
        conn.execute(
            """INSERT INTO session_hints
               (session_id, timestamp, source_file, workstream, summary)
               VALUES ('s1', '2026-03-04T12:30:00Z', 'h1b.json', 'squadkeeper',
                       ?)""",
            (json.dumps(["Built auth system", "Wrote docs"]),),
        )
        conn.commit()
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(conn, "2026-03-04", markers_dir=markers_dir)

            # "Built auth system" should appear only once
            assert result.count("Built auth system") == 1
            assert "Wrote docs" in result
        finally:
            conn.close()


def test_backfill_hint_in_footer():
    """Footer mentions sessions without hints and suggests backfill."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(conn, "2026-03-04", markers_dir=markers_dir)

            assert "/relay:backfill" in result
            assert "3 sessions total" in result
        finally:
            conn.close()


def test_db_marker_preferred_over_file():
    """When a marker exists in both DB and file, DB wins."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            sid = conn.execute("SELECT session_id FROM sessions LIMIT 1").fetchone()["session_id"]

            # Write DB marker pointing to "db-ws"
            conn.execute(
                "INSERT INTO session_markers (session_id, workstream, attached_at) VALUES (?, ?, ?)",
                (sid, "db-ws", "2026-01-01T00:00:00Z"),
            )
            conn.commit()

            # Write file marker pointing to "file-ws"
            marker_path = markers_dir / f"{sid}.json"
            marker_path.write_text(json.dumps({"workstream": "file-ws"}))

            # Import and test directly
            from relay_server.server import _read_marker_workstream
            result = _read_marker_workstream(sid, conn=conn, markers_dir=markers_dir)
            assert result == "db-ws"
        finally:
            conn.close()


def test_project_dir_inference():
    """Sessions without hints or markers infer workstream from project_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()

        # Create a registry with workstream project_dirs
        data_dir = Path(tmpdir) / "relay-data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "my-app": {
                    "status": "active",
                    "description": "My app",
                    "created": "2026-01-01",
                    "last_touched": "2026-03-05",
                    "project_dir": "/test",
                },
            },
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))

        try:
            from relay_server.server import _summarize_activity_impl
            # s3 has project_dir="/test" and no hints/markers — should match "my-app"
            result = _summarize_activity_impl(
                conn, "2026-03-04", markers_dir=markers_dir, data_dir=data_dir,
            )

            # s3 should now appear under "my-app", not "other"
            assert "### my-app" in result
            assert "### other" not in result
        finally:
            conn.close()


def test_project_dir_inference_ambiguous():
    """Ambiguous project_dir match (multiple workstreams) falls back to other."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()

        # Two workstreams sharing the same project_dir
        data_dir = Path(tmpdir) / "relay-data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "ws-a": {"status": "active", "description": "A", "project_dir": "/test",
                         "created": "2026-01-01", "last_touched": "2026-03-05"},
                "ws-b": {"status": "parked", "description": "B", "project_dir": "/test",
                          "created": "2026-01-01", "last_touched": "2026-03-05"},
            },
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))

        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(
                conn, "2026-03-04", markers_dir=markers_dir, data_dir=data_dir,
            )

            # Ambiguous — should still be "other"
            assert "### other" in result
        finally:
            conn.close()


def test_project_dir_inference_longest_prefix():
    """Longest prefix match picks the most specific workstream."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        ensure_schema(db_path)
        conn = get_connection(db_path)

        # Session with specific project_dir
        conn.execute(
            """INSERT INTO sessions
               (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
               VALUES ('s1', '/home/matt/src/app', 'test-slug', '2026-03-04T10:00:00Z', '2026-03-04T12:00:00Z', 50)"""
        )
        conn.commit()

        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()

        data_dir = Path(tmpdir) / "relay-data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "broad": {"status": "active", "description": "Broad", "project_dir": "/home/matt/src",
                          "created": "2026-01-01", "last_touched": "2026-03-05"},
                "specific": {"status": "active", "description": "Specific", "project_dir": "/home/matt/src/app",
                             "created": "2026-01-01", "last_touched": "2026-03-05"},
            },
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))

        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(
                conn, "2026-03-04", markers_dir=markers_dir, data_dir=data_dir,
            )

            assert "### specific" in result
            assert "### broad" not in result
            assert "### other" not in result
        finally:
            conn.close()


def test_fix_other_hints():
    """fix_other_hints re-attributes 'other' hints using project_dir inference."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        ensure_schema(db_path)
        conn = get_connection(db_path)

        # Create sessions with different project_dirs
        conn.execute(
            """INSERT INTO sessions
               (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
               VALUES ('s1', '/home/matt/src/app', 'test-slug', '2026-03-04T10:00:00Z', '2026-03-04T12:00:00Z', 50)"""
        )
        conn.execute(
            """INSERT INTO sessions
               (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
               VALUES ('s2', '/home/matt/src', 'test-slug-2', '2026-03-04T10:00:00Z', '2026-03-04T12:00:00Z', 30)"""
        )

        # Create hints with workstream='other'
        conn.execute(
            """INSERT INTO session_hints
               (session_id, timestamp, source_file, workstream, summary)
               VALUES ('s1', '2026-03-04T12:00:00Z', 'h1.json', 'other', ?)""",
            (json.dumps(["Did some work"]),),
        )
        conn.execute(
            """INSERT INTO session_hints
               (session_id, timestamp, source_file, workstream, summary)
               VALUES ('s2', '2026-03-04T12:00:00Z', 'h2.json', 'other', ?)""",
            (json.dumps(["Did other work"]),),
        )
        conn.commit()

        # Create registry with workstream matching s1's project_dir
        data_dir = Path(tmpdir) / "relay-data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "my-app": {"status": "active", "description": "App", "project_dir": "/home/matt/src/app",
                           "created": "2026-01-01", "last_touched": "2026-03-05"},
                "ws-a": {"status": "active", "description": "A", "project_dir": "/home/matt/src",
                         "created": "2026-01-01", "last_touched": "2026-03-05"},
                "ws-b": {"status": "parked", "description": "B", "project_dir": "/home/matt/src",
                          "created": "2026-01-01", "last_touched": "2026-03-05"},
            },
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))

        try:
            from relay_server.server import _fix_other_hints_impl
            result = _fix_other_hints_impl(conn, data_dir=data_dir)

            # s1 should be fixed (matches my-app), s2 should remain other (ambiguous)
            assert result["fixed"] == 1
            assert result["remaining_other"] == 1

            # Verify s1's hint is now my-app
            row = conn.execute(
                "SELECT workstream FROM session_hints WHERE session_id = 's1'"
            ).fetchone()
            assert row["workstream"] == "my-app"

            # Run again — idempotent, nothing to fix
            result2 = _fix_other_hints_impl(conn, data_dir=data_dir)
            assert result2["fixed"] == 0
            assert result2["remaining_other"] == 1
        finally:
            conn.close()


def test_fix_other_hints_slug_propagation():
    """fix_other_hints propagates workstream through slug chains."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        ensure_schema(db_path)
        conn = get_connection(db_path)

        # Three sessions sharing a slug, one has a known workstream
        for i, sid in enumerate(["s1", "s2", "s3"]):
            conn.execute(
                """INSERT INTO sessions
                   (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
                   VALUES (?, '/ambiguous', 'shared-slug', ?, ?, 50)""",
                (sid, f"2026-03-04T{10+i}:00:00Z", f"2026-03-04T{11+i}:00:00Z"),
            )

        # s1 has a known workstream, s2 and s3 are 'other'
        conn.execute(
            """INSERT INTO session_hints
               (session_id, timestamp, source_file, workstream, summary)
               VALUES ('s1', '2026-03-04T11:00:00Z', 'h1.json', 'my-project', ?)""",
            (json.dumps(["Did known work"]),),
        )
        conn.execute(
            """INSERT INTO session_hints
               (session_id, timestamp, source_file, workstream, summary)
               VALUES ('s2', '2026-03-04T12:00:00Z', 'h2.json', 'other', ?)""",
            (json.dumps(["Did more work"]),),
        )
        conn.execute(
            """INSERT INTO session_hints
               (session_id, timestamp, source_file, workstream, summary)
               VALUES ('s3', '2026-03-04T13:00:00Z', 'h3.json', 'other', ?)""",
            (json.dumps(["Did even more work"]),),
        )
        conn.commit()

        # Empty data_dir (no registry) so project_dir inference does nothing
        data_dir = Path(tmpdir) / "relay-data"
        data_dir.mkdir()

        try:
            from relay_server.server import _fix_other_hints_impl
            result = _fix_other_hints_impl(conn, data_dir=data_dir)

            assert result["fixed"] == 2
            assert result["fixed_by_slug"] == 2
            assert result["remaining_other"] == 0

            # Verify both are now 'my-project'
            rows = conn.execute(
                "SELECT workstream FROM session_hints WHERE session_id IN ('s2', 's3')"
            ).fetchall()
            assert all(r["workstream"] == "my-project" for r in rows)

            # Idempotent
            result2 = _fix_other_hints_impl(conn, data_dir=data_dir)
            assert result2["fixed"] == 0
        finally:
            conn.close()


def test_other_sorts_last():
    """'other' workstream appears after named workstreams."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        # Empty data_dir so project_dir inference has no registry
        data_dir = Path(tmpdir) / "empty-data"
        data_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(
                conn, "2026-03-04", markers_dir=markers_dir, data_dir=data_dir,
            )

            # Find positions of workstream headers
            sq_pos = result.index("### squadkeeper")
            other_pos = result.index("### other")
            assert other_pos > sq_pos
        finally:
            conn.close()
