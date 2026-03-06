"""FastMCP server with conversation history search tools."""

import json
import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from .db import ensure_schema, get_connection, get_db_path
from .formatter import format_conversation
from .indexer import index_all, reindex as do_reindex

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    db_path: Path


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Initialize database and run incremental indexing on startup."""
    db_path = get_db_path()
    ensure_schema(db_path)
    stats = index_all(db_path)
    logger.info(
        f"Startup indexing: {stats['files']} files, "
        f"{stats['messages']} new messages, "
        f"{stats['skipped']} unchanged ({stats['duration_seconds']}s)"
    )
    yield AppContext(db_path=db_path)


mcp = FastMCP(
    "relay-search",
    instructions="Search Claude Code conversation history",
    lifespan=app_lifespan,
)


MAX_LIMIT = 500
MAX_TAGS = 50
MAX_TAG_LENGTH = 200


def _get_db_path(ctx: Context[ServerSession, AppContext]) -> Path:
    return ctx.request_context.lifespan_context.db_path


def _clamp_limit(limit: int, default: int = 10) -> int:
    """Clamp limit to a reasonable range."""
    return min(max(limit, 1), MAX_LIMIT)


def _parse_session_range(session_str: str, total: int) -> list[int]:
    """Parse a session range string into sorted 0-based indices.

    Accepts: "4", "4-5", "1,3,5", "1,3-5" (1-based, inclusive).
    Returns sorted list of unique 0-based indices.
    Raises ValueError on invalid input or out-of-range values.
    """
    if not session_str or not session_str.strip():
        raise ValueError("Empty session range")

    indices: set[int] = set()
    for part in session_str.split(","):
        part = part.strip()
        if "-" in part:
            pieces = part.split("-", 1)
            try:
                start, end = int(pieces[0]), int(pieces[1])
            except ValueError:
                raise ValueError(f"Invalid range: {part}")
            if start > end:
                raise ValueError(f"Invalid range: {part} (start > end)")
            if start < 1 or end > total:
                raise ValueError(f"Session {start}-{end} out of range (1-{total})")
            indices.update(range(start - 1, end))
        else:
            try:
                n = int(part)
            except ValueError:
                raise ValueError(f"Invalid session number: {part}")
            if n < 1 or n > total:
                raise ValueError(f"Session {n} out of range (1-{total})")
            indices.add(n - 1)

    return sorted(indices)


def _validate_tags(tags: list[str]) -> str | None:
    """Validate tag list. Returns error string or None."""
    if len(tags) > MAX_TAGS:
        return f"Too many tags (max {MAX_TAGS})"
    for tag in tags:
        if len(tag) > MAX_TAG_LENGTH:
            return f"Tag too long (max {MAX_TAG_LENGTH} chars): {tag[:50]}..."
    return None


@mcp.tool()
def search_history(
    query: str,
    ctx: Context[ServerSession, AppContext],
    limit: int = 10,
    project: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tags: list[str] | None = None,
) -> list[dict]:
    """Search across all indexed Claude Code conversations.

    Args:
        query: Full-text search query (supports FTS5 syntax: AND, OR, NOT, "phrases")
        limit: Maximum number of results (default 10)
        project: Filter by project directory path (substring match)
        date_from: Filter messages from this date (ISO format, e.g. "2026-01-15")
        date_to: Filter messages up to this date (ISO format)
        tags: Filter to messages with ALL of these tags (e.g. ["review:ux", "insight"])
    """
    limit = _clamp_limit(limit)
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)

    try:
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        if project:
            where_clauses.append("s.project_dir LIKE ?")
            params.append(f"%{project}%")
        if date_from:
            where_clauses.append("m.timestamp >= ?")
            params.append(date_from)
        if date_to:
            where_clauses.append("m.timestamp <= ?")
            params.append(date_to + "T23:59:59Z" if "T" not in date_to else date_to)

        # Tag filter: require ALL specified tags via repeated EXISTS subqueries
        if tags:
            for tag in tags:
                where_clauses.append(
                    "EXISTS (SELECT 1 FROM message_tags mt WHERE mt.message_id = m.id AND mt.tag = ?)"
                )
                params.append(tag)

        where = " AND ".join(where_clauses)
        params.append(limit)

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                s.slug,
                s.project_dir,
                s.git_branch,
                m.role,
                m.timestamp,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.session_id = m.session_id
            WHERE {where}
            ORDER BY rank
            LIMIT ?
        """

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            return [{"error": f"Invalid search query: {e}. FTS5 syntax: use AND, OR, NOT, \"quoted phrases\"."}]

        results = [dict(row) for row in rows]

        # Annotate with session_number within slug chain
        if results:
            slugs = {r["slug"] for r in results if r.get("slug")}
            session_number_lookup: dict[str, int] = {}
            if slugs:
                slug_placeholders = ",".join("?" * len(slugs))
                chain_rows = conn.execute(
                    f"""SELECT session_id, slug FROM sessions
                        WHERE slug IN ({slug_placeholders})
                        ORDER BY slug, first_timestamp ASC""",
                    list(slugs),
                ).fetchall()

                # Build {session_id: session_number} lookup
                current_slug = None
                counter = 0
                for r in chain_rows:
                    if r["slug"] != current_slug:
                        current_slug = r["slug"]
                        counter = 0
                    counter += 1
                    session_number_lookup[r["session_id"]] = counter

            for r in results:
                r["session_number"] = session_number_lookup.get(r["session_id"])

        return results
    finally:
        conn.close()


@mcp.tool()
def get_conversation(
    session_id_or_slug: str,
    ctx: Context[ServerSession, AppContext],
    around_timestamp: str | None = None,
    roles: list[str] | None = None,
    limit: int = 200,
    format: str = "markdown",
    session: str | None = None,
) -> dict | str:
    """Retrieve messages from a specific conversation session.

    If the identifier is a slug that spans multiple sessions (via "continue"),
    all sessions in the chain are combined into one chronological stream.

    Args:
        session_id_or_slug: Session UUID or slug (e.g. "sorted-humming-fox")
        around_timestamp: If provided, return ~20 messages centered on this timestamp
        roles: Filter by message roles (e.g. ["user", "assistant"]). Options: user, assistant, tool_summary, plan
        limit: Maximum messages to return (default 200)
        format: Output format - "markdown" (human-readable, default) or "json" (structured data)
        session: Filter to specific sessions in a multi-session slug (e.g. "4", "2-3", "1,4"). 1-based. Ignored for exact session ID lookups.
    """
    if format not in ("markdown", "json"):
        return {"error": f"Invalid format: {format}. Must be 'markdown' or 'json'."}
    limit = _clamp_limit(limit, default=200)
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)

    try:
        session_found_by_id = False

        # First, try exact session_id match
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id_or_slug,)
        ).fetchone()

        if row:
            session_found_by_id = True
            sessions = [dict(row)]
            session_ids = [row["session_id"]]
        else:
            # Slug lookup — find ALL sessions in the chain
            rows = conn.execute(
                """SELECT * FROM sessions WHERE slug = ?
                   ORDER BY first_timestamp ASC""",
                (session_id_or_slug,)
            ).fetchall()

            if not rows:
                return {"error": f"Session not found: {session_id_or_slug}"}

            sessions = [dict(r) for r in rows]
            session_ids = [r["session_id"] for r in rows]

        # Apply session filter (only for slug lookups, not exact session_id)
        if session and not session_found_by_id:
            try:
                indices = _parse_session_range(session, len(sessions))
            except ValueError as e:
                return {"error": str(e)}
            sessions = [sessions[i] for i in indices]
            session_ids = [s["session_id"] for s in sessions]

        # Build message query across all sessions in the chain
        placeholders = ",".join("?" * len(session_ids))
        where_clauses = [f"session_id IN ({placeholders})"]
        params: list = list(session_ids)

        if roles:
            role_placeholders = ",".join("?" * len(roles))
            where_clauses.append(f"role IN ({role_placeholders})")
            params.extend(roles)

        where = " AND ".join(where_clauses)

        if around_timestamp:
            before = conn.execute(
                f"""SELECT id, session_id, role, content, timestamp, model
                    FROM messages WHERE {where} AND timestamp <= ?
                    ORDER BY timestamp DESC LIMIT 10""",
                params + [around_timestamp]
            ).fetchall()

            after = conn.execute(
                f"""SELECT id, session_id, role, content, timestamp, model
                    FROM messages WHERE {where} AND timestamp > ?
                    ORDER BY timestamp ASC LIMIT 10""",
                params + [around_timestamp]
            ).fetchall()

            messages = [dict(r) for r in reversed(before)] + [dict(r) for r in after]
        else:
            params.append(limit)
            messages = [
                dict(r) for r in conn.execute(
                    f"""SELECT id, session_id, role, content, timestamp, model
                        FROM messages WHERE {where}
                        ORDER BY timestamp ASC LIMIT ?""",
                    params
                ).fetchall()
            ]

        if format == "markdown":
            return format_conversation(sessions, messages)

        return {
            "sessions": sessions,
            "messages": messages,
            "message_count": len(messages),
        }
    finally:
        conn.close()


@mcp.tool()
def list_sessions(
    ctx: Context[ServerSession, AppContext],
    limit: int = 20,
    project: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tags: list[str] | None = None,
    slug: str | None = None,
) -> list[dict]:
    """List recent Claude Code sessions with metadata.

    Args:
        limit: Maximum sessions to return (default 20)
        project: Filter by project directory path (substring match)
        date_from: Filter sessions starting from this date (ISO format)
        date_to: Filter sessions up to this date (ISO format)
        tags: Filter to sessions with ALL of these tags (e.g. ["workstream:game-tracking", "has:tests"])
        slug: Filter to sessions sharing this slug. Adds session_number field and orders chronologically.
    """
    limit = _clamp_limit(limit, default=20)
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)

    try:
        where_clauses = []
        params: list = []

        session_number_lookup: dict[str, int] = {}

        if slug:
            # Get full chain first for stable session numbering
            all_slug_sessions = conn.execute(
                "SELECT session_id FROM sessions WHERE slug = ? ORDER BY first_timestamp ASC",
                (slug,),
            ).fetchall()
            session_number_lookup = {
                r["session_id"]: i + 1 for i, r in enumerate(all_slug_sessions)
            }

            where_clauses.append("s.slug = ?")
            params.append(slug)
        elif project:
            where_clauses.append("s.project_dir LIKE ?")
            params.append(f"%{project}%")

        if date_from:
            where_clauses.append("s.last_timestamp >= ?")
            params.append(date_from)
        if date_to:
            end = date_to + "T23:59:59Z" if "T" not in date_to else date_to
            where_clauses.append("s.first_timestamp <= ?")
            params.append(end)

        if tags:
            for tag in tags:
                where_clauses.append(
                    "EXISTS (SELECT 1 FROM session_tags st WHERE st.session_id = s.session_id AND st.tag = ?)"
                )
                params.append(tag)

        where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        order = "s.first_timestamp ASC" if slug else "s.last_timestamp DESC"
        params.append(limit)

        rows = conn.execute(
            f"""SELECT s.session_id, s.project_dir, s.slug, s.first_timestamp,
                       s.last_timestamp, s.message_count, s.git_branch, s.cwd
                FROM sessions s {where}
                ORDER BY {order}
                LIMIT ?""",
            params
        ).fetchall()

        results = [dict(row) for row in rows]
        if slug:
            for r in results:
                r["session_number"] = session_number_lookup.get(r["session_id"])
        return results
    finally:
        conn.close()


@mcp.tool()
def tag_message(
    message_id: int,
    tags: list[str],
    ctx: Context[ServerSession, AppContext],
) -> dict:
    """Manually tag a message for future discoverability.

    Args:
        message_id: Integer message ID (from search_history or get_conversation results)
        tags: List of tag strings to apply (e.g. ["review:ux", "important"])
    """
    if err := _validate_tags(tags):
        return {"error": err}
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)

    try:
        # Verify message exists
        msg = conn.execute(
            "SELECT id, session_id, role, timestamp FROM messages WHERE id = ?",
            (message_id,)
        ).fetchone()
        if not msg:
            return {"error": f"Message not found: {message_id}"}

        conn.executemany(
            "INSERT OR IGNORE INTO message_tags (message_id, tag, source) VALUES (?, ?, ?)",
            [(message_id, tag, "manual") for tag in tags],
        )
        conn.commit()

        # Return updated tag list
        tag_rows = conn.execute(
            "SELECT tag, source FROM message_tags WHERE message_id = ?",
            (message_id,)
        ).fetchall()

        return {
            "message_id": message_id,
            "session_id": msg["session_id"],
            "role": msg["role"],
            "timestamp": msg["timestamp"],
            "tags": [{"tag": r["tag"], "source": r["source"]} for r in tag_rows],
        }
    finally:
        conn.close()


@mcp.tool()
def tag_session(
    session_id: str,
    tags: list[str],
    ctx: Context[ServerSession, AppContext],
) -> dict:
    """Manually tag a session (e.g. associate with a workstream).

    Args:
        session_id: Session UUID
        tags: List of tag strings (e.g. ["workstream:game-tracking", "important"])
    """
    if err := _validate_tags(tags):
        return {"error": err}
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)

    try:
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()
        if not session:
            return {"error": f"Session not found: {session_id}"}

        conn.executemany(
            "INSERT OR IGNORE INTO session_tags (session_id, tag, source) VALUES (?, ?, ?)",
            [(session_id, tag, "manual") for tag in tags],
        )
        conn.commit()

        tag_rows = conn.execute(
            "SELECT tag, source FROM session_tags WHERE session_id = ?",
            (session_id,)
        ).fetchall()

        return {
            "session_id": session_id,
            "slug": session["slug"],
            "tags": [{"tag": r["tag"], "source": r["source"]} for r in tag_rows],
        }
    finally:
        conn.close()


@mcp.tool()
def list_tags(
    ctx: Context[ServerSession, AppContext],
    scope: str = "all",
) -> list[dict]:
    """List all tags with counts for discoverability.

    Args:
        scope: "all", "message", or "session"
    """
    if scope not in ("all", "message", "session"):
        return [{"error": f"Invalid scope: {scope}. Must be 'all', 'message', or 'session'."}]
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)

    try:
        results: dict[str, dict] = {}

        if scope in ("all", "message"):
            rows = conn.execute(
                """SELECT tag, source, COUNT(*) as cnt
                   FROM message_tags GROUP BY tag, source
                   ORDER BY cnt DESC"""
            ).fetchall()
            for r in rows:
                tag = r["tag"]
                if tag not in results:
                    results[tag] = {"tag": tag, "scope": "message", "auto": 0, "manual": 0, "total": 0}
                results[tag][r["source"]] += r["cnt"]
                results[tag]["total"] += r["cnt"]

        if scope in ("all", "session"):
            rows = conn.execute(
                """SELECT tag, source, COUNT(*) as cnt
                   FROM session_tags GROUP BY tag, source
                   ORDER BY cnt DESC"""
            ).fetchall()
            for r in rows:
                tag = r["tag"]
                if tag not in results:
                    results[tag] = {"tag": tag, "scope": "session", "auto": 0, "manual": 0, "total": 0}
                elif results[tag]["scope"] == "message":
                    results[tag]["scope"] = "both"
                else:
                    results[tag]["scope"] = "session"
                results[tag][r["source"]] += r["cnt"]
                results[tag]["total"] += r["cnt"]

        return sorted(results.values(), key=lambda x: x["total"], reverse=True)
    finally:
        conn.close()


def _get_session_summaries_from_db(
    conn: sqlite3.Connection,
    session_ids: list[str],
) -> list[dict]:
    """Query session hints from DB. Extracted for testability."""
    if not session_ids:
        return []

    placeholders = ",".join("?" * len(session_ids))
    rows = conn.execute(
        f"""SELECT session_id, timestamp, workstream, summary, decisions
            FROM session_hints
            WHERE session_id IN ({placeholders})
            ORDER BY session_id, timestamp ASC""",
        session_ids,
    ).fetchall()

    # Group by session_id
    hints_by_session: dict[str, list[dict]] = {}
    for row in rows:
        sid = row["session_id"]
        if sid not in hints_by_session:
            hints_by_session[sid] = []
        segment = {
            "workstream": row["workstream"],
            "timestamp": row["timestamp"],
            "summary": json.loads(row["summary"]),
        }
        if row["decisions"]:
            segment["decisions"] = json.loads(row["decisions"])
        hints_by_session[sid].append(segment)

    # Build results for all requested session_ids
    results = []
    for sid in session_ids:
        segments = hints_by_session.get(sid, [])
        results.append({
            "session_id": sid,
            "hints_available": len(segments) > 0,
            "segments": segments,
        })
    return results


@mcp.tool()
def get_session_summaries(
    session_ids: list[str],
    ctx: Context[ServerSession, AppContext],
) -> list[dict]:
    """Get pre-written session summaries for efficient summarization.

    Returns all hint segments for the given sessions, ordered by timestamp.
    Sessions without hints return an entry with hints_available: false.

    Args:
        session_ids: List of session UUIDs to fetch summaries for
    """
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)
    try:
        return _get_session_summaries_from_db(conn, session_ids)
    finally:
        conn.close()


def _get_markers_dir() -> Path:
    """Return the session markers directory path."""
    return Path(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    ) / "relay" / "session-markers"


def _read_marker_workstream(session_id: str, markers_dir: Path | None = None) -> str | None:
    """Read workstream name from a session marker file. Returns None if not found."""
    if markers_dir is None:
        markers_dir = _get_markers_dir()
    marker_path = markers_dir / f"{session_id}.json"
    if not marker_path.exists():
        return None
    try:
        with open(marker_path) as f:
            data = json.load(f)
        return data.get("workstream")
    except (json.JSONDecodeError, OSError):
        return None


def _summarize_activity_impl(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str | None = None,
    workstream: str | None = None,
    markers_dir: Path | None = None,
) -> str:
    """Build a markdown activity summary. Extracted for testability."""
    # 1. Query sessions in date range
    where_clauses = ["s.last_timestamp >= ?"]
    params: list = [date_from]
    if date_to:
        end = date_to + "T23:59:59Z" if "T" not in date_to else date_to
        where_clauses.append("s.first_timestamp <= ?")
        params.append(end)

    where = "WHERE " + " AND ".join(where_clauses)
    rows = conn.execute(
        f"""SELECT s.session_id, s.slug, s.project_dir, s.first_timestamp,
                   s.last_timestamp, s.message_count
            FROM sessions s {where}
            ORDER BY s.first_timestamp ASC""",
        params,
    ).fetchall()

    sessions = [dict(r) for r in rows]
    if not sessions:
        return f"## Activity Summary: {date_from} – {date_to or 'now'}\n\nNo sessions found in this range."

    session_ids = [s["session_id"] for s in sessions]

    # 2. Fetch all hints for these sessions
    placeholders = ",".join("?" * len(session_ids))
    hint_rows = conn.execute(
        f"""SELECT session_id, workstream, summary, decisions
            FROM session_hints
            WHERE session_id IN ({placeholders})
            ORDER BY session_id, timestamp ASC""",
        session_ids,
    ).fetchall()

    # Group hints by session_id
    hints_by_session: dict[str, list[dict]] = {}
    for row in hint_rows:
        sid = row["session_id"]
        hints_by_session.setdefault(sid, [])
        segment = {
            "workstream": row["workstream"],
            "summary": json.loads(row["summary"]),
        }
        if row["decisions"]:
            segment["decisions"] = json.loads(row["decisions"])
        hints_by_session[sid].append(segment)

    # 3. Build session-to-workstream mapping
    #    Each session may have multiple hint segments (different workstreams).
    #    Sessions without hints fall back to marker files.
    #    Structure: workstream -> [{session, slug, date, bullets, decisions}]
    ws_groups: dict[str, list[dict]] = {}
    no_hint_sessions = []

    session_lookup = {s["session_id"]: s for s in sessions}

    for s in sessions:
        sid = s["session_id"]
        segments = hints_by_session.get(sid)

        if segments:
            for seg in segments:
                ws = seg["workstream"]
                ws_groups.setdefault(ws, [])
                ws_groups[ws].append({
                    "session_id": sid,
                    "slug": s.get("slug"),
                    "date": (s["first_timestamp"] or "")[:10],
                    "message_count": s.get("message_count", 0),
                    "summary": seg["summary"],
                    "decisions": seg.get("decisions"),
                })
        else:
            # Try session marker
            marker_ws = _read_marker_workstream(sid, markers_dir)
            ws = marker_ws or "other"
            ws_groups.setdefault(ws, [])
            ws_groups[ws].append({
                "session_id": sid,
                "slug": s.get("slug"),
                "date": (s["first_timestamp"] or "")[:10],
                "message_count": s.get("message_count", 0),
                "summary": None,
                "decisions": None,
            })
            no_hint_sessions.append(sid)

    # 4. Filter by workstream if requested
    if workstream:
        ws_groups = {k: v for k, v in ws_groups.items() if k == workstream}

    # 5. Format as markdown
    end_label = date_to or "now"
    lines = [f"## Activity Summary: {date_from} – {end_label}\n"]

    # Sort workstreams by session count (descending), "other" last
    sorted_ws = sorted(
        ws_groups.items(),
        key=lambda x: (x[0] == "other", -len(x[1])),
    )

    for ws_name, entries in sorted_ws:
        lines.append(f"### {ws_name} ({len(entries)} session{'s' if len(entries) != 1 else ''})\n")

        # Group entries by slug for compact display
        slug_groups: dict[str | None, list[dict]] = {}
        for entry in entries:
            slug_groups.setdefault(entry["slug"], []).append(entry)

        for slug, slug_entries in slug_groups.items():
            # Header: slug (or session ID if no slug) with date range
            dates = sorted(set(e["date"] for e in slug_entries if e["date"]))
            date_str = dates[0] if len(dates) == 1 else f"{dates[0]} – {dates[-1]}" if dates else ""

            if slug:
                lines.append(f"**`{slug}`** ({date_str})")
            else:
                # No slug — show as individual brief entries
                for e in slug_entries:
                    lines.append(f"- {e['date']}: {e['message_count']} messages (no slug)")
                continue

            # Collect all bullets and decisions across entries in this slug group
            all_bullets: list[str] = []
            all_decisions: list[str] = []
            has_hints = False
            for e in slug_entries:
                if e["summary"]:
                    has_hints = True
                    all_bullets.extend(e["summary"])
                if e.get("decisions"):
                    all_decisions.extend(e["decisions"])

            if has_hints:
                # Deduplicate bullets (hints from multiple segments may overlap)
                seen: set[str] = set()
                for bullet in all_bullets:
                    if bullet not in seen:
                        seen.add(bullet)
                        lines.append(f"- {bullet}")
                if all_decisions:
                    seen_d: set[str] = set()
                    for d in all_decisions:
                        if d not in seen_d:
                            seen_d.add(d)
                            lines.append(f"  - *Decision: {d}*")
            else:
                total_msgs = sum(e["message_count"] for e in slug_entries)
                lines.append(f"- {total_msgs} messages (no hints — run `/relay:backfill`)")

            lines.append("")  # blank line between slug groups

    # Footer
    if no_hint_sessions:
        lines.append(f"---\n*{len(no_hint_sessions)} session(s) without hints. Run `/relay:backfill` to generate summaries.*")

    lines.append(f"\n*{len(sessions)} sessions total. Use `get_conversation(\"<slug>\")` to drill into any session.*")

    return "\n".join(lines)


@mcp.tool()
def summarize_activity(
    date_from: str,
    ctx: Context[ServerSession, AppContext],
    date_to: str | None = None,
    workstream: str | None = None,
) -> str:
    """Summarize recent activity grouped by workstream.

    Returns a pre-formatted markdown summary with session bullets and decisions.
    Uses session hints for summarized sessions, falls back to session markers
    for workstream attribution, and shows metadata-only for uncovered sessions.

    Args:
        date_from: Start date (ISO format, e.g. "2026-02-19")
        date_to: End date (ISO format). Defaults to now.
        workstream: Filter to a single workstream name
    """
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)
    try:
        return _summarize_activity_impl(conn, date_from, date_to, workstream)
    finally:
        conn.close()


@mcp.tool()
def reindex(ctx: Context[ServerSession, AppContext]) -> dict:
    """Force a complete re-index of all conversation transcripts.

    Clears the existing index and rebuilds from scratch. Use when
    the index seems corrupted or out of sync.
    """
    db_path = _get_db_path(ctx)
    stats = do_reindex(db_path)
    return {
        "status": "complete",
        "files_indexed": stats["files"],
        "messages_indexed": stats["messages"],
        "sessions_found": stats["sessions"],
        "duration_seconds": stats["duration_seconds"],
    }
