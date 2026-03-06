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
    """Sessions without hints or markers go to 'other'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(conn, "2026-03-04", markers_dir=markers_dir)

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


def test_other_sorts_last():
    """'other' workstream appears after named workstreams."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, conn = _setup_db(tmpdir)
        markers_dir = Path(tmpdir) / "markers"
        markers_dir.mkdir()
        try:
            from relay_server.server import _summarize_activity_impl
            result = _summarize_activity_impl(conn, "2026-03-04", markers_dir=markers_dir)

            # Find positions of workstream headers
            sq_pos = result.index("### squadkeeper")
            other_pos = result.index("### other")
            assert other_pos > sq_pos
        finally:
            conn.close()
