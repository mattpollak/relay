"""Integration tests for session-level addressing features."""

import sqlite3
import pytest
from unittest.mock import MagicMock
from pathlib import Path

from relay_server.db import ensure_schema, get_connection
from relay_server.server import get_conversation, list_sessions, search_history


@pytest.fixture
def db_path(tmp_path):
    """Create a test database with a 4-session slug."""
    path = tmp_path / "test.db"
    ensure_schema(path)
    conn = get_connection(path)

    # 4 sessions sharing slug "test-conversation"
    sessions = [
        ("s1", "/home/test/project", "test-conversation", "2026-01-01T10:00:00Z", "2026-01-01T10:30:00Z", 3, "main", "/home/test/project"),
        ("s2", "/home/test/project", "test-conversation", "2026-01-01T11:00:00Z", "2026-01-01T11:30:00Z", 3, "main", "/home/test/project"),
        ("s3", "/home/test/project", "test-conversation", "2026-01-01T12:00:00Z", "2026-01-01T12:30:00Z", 3, "main", "/home/test/project"),
        ("s4", "/home/test/project", "test-conversation", "2026-01-01T13:00:00Z", "2026-01-01T13:30:00Z", 3, "main", "/home/test/project"),
    ]
    conn.executemany(
        "INSERT INTO sessions (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count, git_branch, cwd) VALUES (?,?,?,?,?,?,?,?)", sessions
    )

    # 3 messages per session
    messages = []
    for i, (sid, _, _, start, _, _, _, _) in enumerate(sessions):
        hour = 10 + i
        messages.extend([
            (sid, "user", f"Session {i+1} user message", f"2026-01-01T{hour}:00:00Z", None),
            (sid, "assistant", f"Session {i+1} assistant message", f"2026-01-01T{hour}:10:00Z", None),
            (sid, "tool_summary", f"[Read] /path/to/file-{i+1}", f"2026-01-01T{hour}:05:00Z", None),
        ])
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, model) VALUES (?,?,?,?,?)",
        messages,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def ctx(db_path):
    """Mock MCP context that returns the test db_path."""
    mock_ctx = MagicMock()
    mock_ctx.request_context.lifespan_context.db_path = db_path
    return mock_ctx


class TestGetConversationSessionFilter:
    def test_single_session(self, ctx):
        result = get_conversation("test-conversation", ctx, session="2", format="json")
        assert result["message_count"] == 3
        assert all(m["session_id"] == "s2" for m in result["messages"])
        assert len(result["sessions"]) == 1

    def test_session_range(self, ctx):
        result = get_conversation("test-conversation", ctx, session="2-3", format="json")
        assert result["message_count"] == 6
        session_ids = {m["session_id"] for m in result["messages"]}
        assert session_ids == {"s2", "s3"}
        assert len(result["sessions"]) == 2

    def test_session_csv(self, ctx):
        result = get_conversation("test-conversation", ctx, session="1,4", format="json")
        assert result["message_count"] == 6
        session_ids = {m["session_id"] for m in result["messages"]}
        assert session_ids == {"s1", "s4"}

    def test_session_out_of_range(self, ctx):
        result = get_conversation("test-conversation", ctx, session="5", format="json")
        assert "error" in result

    def test_session_ignored_for_uuid(self, ctx):
        """session param is ignored when using exact session_id."""
        result = get_conversation("s2", ctx, session="1", format="json")
        # Should return s2's messages, not s1's — session param ignored
        assert all(m["session_id"] == "s2" for m in result["messages"])

    def test_session_with_around_timestamp(self, ctx):
        result = get_conversation(
            "test-conversation", ctx,
            session="2", around_timestamp="2026-01-01T11:05:00Z",
            format="json",
        )
        # Should only contain s2 messages, windowed around the timestamp
        assert all(m["session_id"] == "s2" for m in result["messages"])

    def test_session_with_markdown_format(self, ctx):
        result = get_conversation("test-conversation", ctx, session="2-3")
        assert isinstance(result, str)
        assert "Session" in result

    def test_no_session_param_returns_all(self, ctx):
        result = get_conversation("test-conversation", ctx, format="json")
        assert result["message_count"] == 12
        assert len(result["sessions"]) == 4


class TestListSessionsSlug:
    def test_slug_returns_all_sessions(self, ctx):
        result = list_sessions(ctx, slug="test-conversation")
        assert len(result) == 4
        assert all("session_number" in r for r in result)
        assert [r["session_number"] for r in result] == [1, 2, 3, 4]

    def test_slug_chronological_order(self, ctx):
        result = list_sessions(ctx, slug="test-conversation")
        timestamps = [r["first_timestamp"] for r in result]
        assert timestamps == sorted(timestamps)

    def test_slug_not_found(self, ctx):
        result = list_sessions(ctx, slug="nonexistent-slug")
        assert result == []

    def test_no_slug_no_session_number(self, ctx):
        result = list_sessions(ctx)
        assert len(result) >= 4
        assert all("session_number" not in r for r in result)

    def test_slug_with_date_filter(self, ctx):
        result = list_sessions(ctx, slug="test-conversation", date_from="2026-01-01T12:00:00Z")
        assert len(result) == 2  # sessions 3 and 4
        assert [r["session_number"] for r in result] == [3, 4]


class TestSearchHistorySessionNumber:
    def test_results_include_session_number(self, ctx):
        result = search_history("assistant message", ctx, limit=10)
        assert len(result) > 0
        assert all("session_number" in r for r in result)

    def test_session_numbers_are_correct(self, ctx):
        result = search_history("Session 3 assistant", ctx, limit=5)
        # Should find the message in session 3
        s3_results = [r for r in result if r["session_id"] == "s3"]
        assert len(s3_results) > 0
        assert all(r["session_number"] == 3 for r in s3_results)

    def test_session_number_none_for_single_session(self, db_path, ctx):
        """Sessions without a slug chain get session_number=1."""
        conn = get_connection(db_path)
        conn.execute(
            "INSERT INTO sessions (session_id, project_dir, slug, first_timestamp, last_timestamp, message_count, git_branch, cwd) VALUES (?,?,?,?,?,?,?,?)",
            ("solo", "/home/test/project", "solo-slug", "2026-02-01T10:00:00Z", "2026-02-01T10:30:00Z", 1, "main", "/"),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, model) VALUES (?,?,?,?,?)",
            ("solo", "user", "solo unique searchterm xyzzy", "2026-02-01T10:00:00Z", None),
        )
        conn.commit()
        conn.close()

        result = search_history("xyzzy", ctx, limit=5)
        assert len(result) == 1
        assert result[0]["session_number"] == 1
