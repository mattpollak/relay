"""Tests for get_session_summaries tool logic."""

import json
import tempfile
from pathlib import Path

from relay_server.db import ensure_schema, get_connection


def _setup_db_with_hints(tmpdir):
    """Create a test DB with sessions and hints."""
    db_path = Path(tmpdir) / "test.db"
    ensure_schema(db_path)
    conn = get_connection(db_path)

    # Insert test sessions
    conn.execute(
        """INSERT INTO sessions
           (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
           VALUES ('s1', '/test', 'slug-1', '2026-03-05T00:00:00Z', '2026-03-05T01:00:00Z', 50)"""
    )
    conn.execute(
        """INSERT INTO sessions
           (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
           VALUES ('s2', '/test', 'slug-2', '2026-03-05T02:00:00Z', '2026-03-05T03:00:00Z', 30)"""
    )
    conn.execute(
        """INSERT INTO sessions
           (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count)
           VALUES ('s3', '/test', 'slug-3', '2026-03-05T04:00:00Z', '2026-03-05T05:00:00Z', 10)"""
    )

    # Insert hints for s1 (two segments) and s2 (one segment). No hints for s3.
    conn.execute(
        """INSERT INTO session_hints
           (session_id, timestamp, source_file, workstream, summary, decisions)
           VALUES ('s1', '2026-03-05T01:00:00Z', 'h1.json', 'squadkeeper',
                   ?, ?)""",
        (json.dumps(["Built feature X", "Added tests"]), json.dumps(["Used pattern Y"])),
    )
    conn.execute(
        """INSERT INTO session_hints
           (session_id, timestamp, source_file, workstream, summary)
           VALUES ('s1', '2026-03-05T02:00:00Z', 'h2.json', 'relay',
                   ?)""",
        (json.dumps(["Switched to relay work"]),),
    )
    conn.execute(
        """INSERT INTO session_hints
           (session_id, timestamp, source_file, workstream, summary)
           VALUES ('s2', '2026-03-05T03:00:00Z', 'h3.json', 'squadkeeper',
                   ?)""",
        (json.dumps(["Fixed bug Z"]),),
    )
    conn.commit()
    return db_path, conn


def test_get_summaries_with_hints():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, conn = _setup_db_with_hints(tmpdir)
        try:
            from relay_server.server import _get_session_summaries_from_db
            results = _get_session_summaries_from_db(conn, ["s1", "s2"])
            assert len(results) == 2

            s1 = next(r for r in results if r["session_id"] == "s1")
            assert s1["hints_available"] is True
            assert len(s1["segments"]) == 2
            assert s1["segments"][0]["workstream"] == "squadkeeper"
            assert s1["segments"][1]["workstream"] == "relay"

            s2 = next(r for r in results if r["session_id"] == "s2")
            assert s2["hints_available"] is True
            assert len(s2["segments"]) == 1
        finally:
            conn.close()


def test_get_summaries_missing_hints():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, conn = _setup_db_with_hints(tmpdir)
        try:
            from relay_server.server import _get_session_summaries_from_db
            results = _get_session_summaries_from_db(conn, ["s3"])
            assert len(results) == 1
            assert results[0]["hints_available"] is False
            assert results[0]["segments"] == []
        finally:
            conn.close()


def test_get_summaries_mixed():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, conn = _setup_db_with_hints(tmpdir)
        try:
            from relay_server.server import _get_session_summaries_from_db
            results = _get_session_summaries_from_db(conn, ["s1", "s3"])
            s1 = next(r for r in results if r["session_id"] == "s1")
            s3 = next(r for r in results if r["session_id"] == "s3")
            assert s1["hints_available"] is True
            assert s3["hints_available"] is False
        finally:
            conn.close()


def test_get_summaries_empty_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, conn = _setup_db_with_hints(tmpdir)
        try:
            from relay_server.server import _get_session_summaries_from_db
            results = _get_session_summaries_from_db(conn, [])
            assert results == []
        finally:
            conn.close()
