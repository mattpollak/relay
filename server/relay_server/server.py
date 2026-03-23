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

_DEFAULT_SUMMARY_DIR = "~/.local/share/relay/summaries"


def _get_config() -> dict:
    """Read ~/.config/relay/relay.json. Returns empty dict on any error."""
    config_path = Path(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    ) / "relay" / "relay.json"
    try:
        return json.loads(config_path.read_text()) if config_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


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


def _read_marker_workstream(session_id: str, conn: sqlite3.Connection | None = None, markers_dir: Path | None = None) -> str | None:
    """Read workstream name from DB marker (preferred) or file fallback."""
    # Try DB first
    if conn:
        try:
            row = conn.execute(
                "SELECT workstream FROM session_markers WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                return row["workstream"]
        except sqlite3.OperationalError:
            pass  # Table doesn't exist yet (pre-v0.10.0 DB)
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


def _build_project_dir_mapping(data_dir: Path | None = None) -> dict[str, list[tuple[str, str]]]:
    """Build a project_dir -> [(workstream_name, status)] mapping from the registry.

    Used for inferring workstream from session project_dir when no hint or marker exists.
    """
    from .workstreams import get_data_dir, read_registry
    project_dir_ws: dict[str, list[tuple[str, str]]] = {}
    try:
        reg = read_registry(data_dir=data_dir or get_data_dir())
        for ws_name, ws_data in reg.get("workstreams", {}).items():
            pdir = ws_data.get("project_dir", "")
            if pdir:
                status = ws_data.get("status", "active")
                project_dir_ws.setdefault(pdir, []).append((ws_name, status))
    except Exception:
        pass  # Registry unreadable — skip inference
    return project_dir_ws


def _infer_workstream_from_project(
    project_dir: str | None,
    project_dir_ws: dict[str, list[tuple[str, str]]],
) -> str | None:
    """Infer workstream from project_dir using longest-prefix match.

    When multiple workstreams share the same project_dir, prefers
    non-completed workstreams. Returns None if still ambiguous.
    """
    if not project_dir or not project_dir_ws:
        return None
    # Find all workstream project_dirs that are a prefix of this session's project_dir
    matches: list[tuple[str, list[tuple[str, str]]]] = []
    for ws_pdir, ws_entries in project_dir_ws.items():
        if project_dir == ws_pdir or project_dir.startswith(ws_pdir + "/"):
            matches.append((ws_pdir, ws_entries))
    if not matches:
        return None
    # Pick longest prefix (most specific match)
    max_len = max(len(m[0]) for m in matches)
    best = [entry for pdir, entries in matches if len(pdir) == max_len for entry in entries]
    if len(best) == 1:
        return best[0][0]
    # Multiple matches — prefer non-completed workstreams
    non_completed = [name for name, status in best if status != "completed"]
    return non_completed[0] if len(non_completed) == 1 else None


def _fix_other_hints_impl(
    conn: sqlite3.Connection,
    data_dir: Path | None = None,
) -> dict:
    """Re-attribute session_hints with workstream='other' using two strategies:

    1. **Project-dir inference** — match session project_dir to workstream registry
    2. **Slug chain propagation** — if any session in a slug chain has a known workstream,
       apply it to all 'other' sessions in the same chain

    Idempotent — safe to run multiple times. Only updates unambiguous matches.
    """
    fixed_by_project = 0
    fixed_by_slug = 0

    # Pass 1: Project-dir inference
    project_dir_ws = _build_project_dir_mapping(data_dir=data_dir)
    if project_dir_ws:
        rows = conn.execute("""
            SELECT h.id, h.session_id, s.project_dir
            FROM session_hints h
            JOIN sessions s ON s.session_id = h.session_id
            WHERE h.workstream = 'other'
        """).fetchall()

        for row in rows:
            inferred = _infer_workstream_from_project(row["project_dir"], project_dir_ws)
            if inferred:
                conn.execute(
                    "UPDATE session_hints SET workstream = ? WHERE id = ?",
                    (inferred, row["id"]),
                )
                fixed_by_project += 1

        conn.commit()

    # Pass 2: Slug chain propagation
    # Find slugs that have both 'other' and non-'other' hints
    slug_rows = conn.execute("""
        SELECT s.slug, h.workstream
        FROM session_hints h
        JOIN sessions s ON s.session_id = h.session_id
        WHERE s.slug IS NOT NULL
    """).fetchall()

    # Build slug -> set of known workstreams (from hints, markers, and session tags)
    slug_workstreams: dict[str, set[str]] = {}
    slugs_with_other: set[str] = set()
    for row in slug_rows:
        slug = row["slug"]
        ws = row["workstream"]
        if ws == "other":
            slugs_with_other.add(slug)
        else:
            slug_workstreams.setdefault(slug, set()).add(ws)

    # Add workstream info from session markers
    if slugs_with_other:
        marker_slugs = conn.execute("""
            SELECT DISTINCT s.slug, sm.workstream
            FROM session_markers sm
            JOIN sessions s ON s.session_id = sm.session_id
            WHERE s.slug IS NOT NULL
        """).fetchall()
        for row in marker_slugs:
            slug_workstreams.setdefault(row["slug"], set()).add(row["workstream"])

    # Add workstream info from session tags (workstream:*)
    if slugs_with_other:
        tag_rows = conn.execute("""
            SELECT DISTINCT s.slug, st.tag
            FROM session_tags st
            JOIN sessions s ON s.session_id = st.session_id
            WHERE s.slug IS NOT NULL
            AND st.tag LIKE 'workstream:%'
        """).fetchall()
        for row in tag_rows:
            ws_from_tag = row["tag"].split(":", 1)[1]
            slug_workstreams.setdefault(row["slug"], set()).add(ws_from_tag)

    # For slugs that have 'other' hints AND exactly one known workstream, propagate
    for slug in slugs_with_other:
        known = slug_workstreams.get(slug, set())
        if len(known) == 1:
            ws_name = next(iter(known))
            result = conn.execute(
                """UPDATE session_hints SET workstream = ?
                   WHERE workstream = 'other'
                   AND session_id IN (SELECT session_id FROM sessions WHERE slug = ?)""",
                (ws_name, slug),
            )
            fixed_by_slug += result.rowcount

    conn.commit()

    remaining = conn.execute(
        "SELECT COUNT(*) FROM session_hints WHERE workstream = 'other'"
    ).fetchone()[0]

    total_fixed = fixed_by_project + fixed_by_slug
    parts = []
    if fixed_by_project:
        parts.append(f"{fixed_by_project} by project-dir")
    if fixed_by_slug:
        parts.append(f"{fixed_by_slug} by slug chain")
    detail = f" ({', '.join(parts)})" if parts else ""

    return {
        "fixed": total_fixed,
        "fixed_by_project": fixed_by_project,
        "fixed_by_slug": fixed_by_slug,
        "remaining_other": remaining,
        "message": f"Re-attributed {total_fixed} hints{detail}. {remaining} remain as 'other'.",
    }


def _summarize_activity_impl(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str | None = None,
    workstream: str | None = None,
    markers_dir: Path | None = None,
    data_dir: Path | None = None,
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

    # 3. Build project_dir -> workstream mapping for fallback inference.
    from .workstreams import get_data_dir, read_registry
    project_dir_ws = _build_project_dir_mapping(data_dir=data_dir)

    # 4. Build slug -> known workstream mapping for slug chain propagation.
    #    Sources: non-'other' hints, session markers, and workstream:* session tags.
    slug_known_ws: dict[str, set[str]] = {}
    for row in hint_rows:
        ws = row["workstream"]
        if ws != "other":
            sid = row["session_id"]
            s = next((s for s in sessions if s["session_id"] == sid), None)
            if s and s.get("slug"):
                slug_known_ws.setdefault(s["slug"], set()).add(ws)

    # Also check session tags (workstream:*) for slug chain anchors
    session_id_placeholders = ",".join("?" * len(session_ids))
    tag_rows = conn.execute(
        f"""SELECT st.session_id, st.tag
            FROM session_tags st
            WHERE st.session_id IN ({session_id_placeholders})
            AND st.tag LIKE 'workstream:%'""",
        session_ids,
    ).fetchall()
    for row in tag_rows:
        ws_from_tag = row["tag"].split(":", 1)[1]
        s = next((s for s in sessions if s["session_id"] == row["session_id"]), None)
        if s and s.get("slug"):
            slug_known_ws.setdefault(s["slug"], set()).add(ws_from_tag)

    def _resolve_other_ws(session: dict) -> str:
        """Resolve workstream for a session/hint tagged 'other'."""
        sid = session["session_id"]
        slug = session.get("slug")
        # 1. Slug chain propagation (high confidence)
        if slug and slug in slug_known_ws:
            known = slug_known_ws[slug]
            if len(known) == 1:
                return next(iter(known))
        # 2. Session marker
        marker_ws = _read_marker_workstream(sid, conn=conn, markers_dir=markers_dir)
        if marker_ws:
            return marker_ws
        # 3. Project-dir inference
        inferred = _infer_workstream_from_project(session.get("project_dir"), project_dir_ws)
        if inferred:
            return inferred
        return "other"

    # 5. Build session-to-workstream mapping
    #    Each session may have multiple hint segments (different workstreams).
    #    Hints with workstream='other' are re-resolved using the fallback chain.
    #    Structure: workstream -> [{session, slug, date, bullets, decisions}]
    ws_groups: dict[str, list[dict]] = {}
    no_hint_sessions = []

    for s in sessions:
        sid = s["session_id"]
        segments = hints_by_session.get(sid)

        if segments:
            for seg in segments:
                ws = seg["workstream"]
                if ws == "other":
                    ws = _resolve_other_ws(s)
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
            ws = _resolve_other_ws(s)
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


_SUMMARY_INLINE_THRESHOLD = 200  # lines; summaries longer than this get overview only


@mcp.tool()
def summarize_activity(
    date_from: str,
    ctx: Context[ServerSession, AppContext],
    date_to: str | None = None,
    workstream: str | None = None,
    output_dir: str | None = None,
    format: str = "markdown",
) -> dict | str:
    """Summarize recent activity grouped by workstream.

    Always writes full markdown summary to a file. Returns pre-formatted
    markdown by default (full inline if short, overview + file path if long).
    Pass format="json" for structured data.

    Output directory precedence: output_dir param > config file > default.

    Args:
        date_from: Start date (ISO format, e.g. "2026-02-19")
        date_to: End date (ISO format). Defaults to now.
        workstream: Filter to a single workstream name
        output_dir: Directory to write summary file (overrides config)
        format: "markdown" (default) or "json" for structured data
    """
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)
    try:
        markdown = _summarize_activity_impl(conn, date_from, date_to, workstream)
    finally:
        conn.close()

    # Resolve output directory: param > config > default
    if not output_dir:
        config = _get_config()
        output_dir = config.get("summary_dir")
    if not output_dir:
        output_dir = _DEFAULT_SUMMARY_DIR

    out_dir = Path(os.path.expanduser(output_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    suffix = f"-{workstream}" if workstream else ""
    out_path = out_dir / f"relay-summary-{date_from}{suffix}.md"
    out_path.write_text(markdown, encoding="utf-8")

    # Build overview from section headers
    overview = []
    for line in markdown.split("\n"):
        if line.startswith("### "):
            overview.append(line.removeprefix("### "))

    if format == "json":
        return {
            "file": str(out_path),
            "overview": overview,
            "date_range": f"{date_from} – {date_to or 'now'}",
        }

    # Markdown format: full inline if short, overview + file path if long
    line_count = markdown.count("\n") + 1
    file_note = f"*Full summary saved to `{out_path}`*"

    if line_count <= _SUMMARY_INLINE_THRESHOLD:
        return f"{file_note}\n\n{markdown}"

    # Long summary: overview only
    lines = [file_note, ""]
    lines.append(f"## Activity Summary: {date_from} – {date_to or 'now'}")
    lines.append(f"*{line_count} lines — open the file for full details.*\n")
    for item in overview:
        lines.append(f"- {item}")
    return "\n".join(lines)


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


@mcp.tool()
def create_workstream(
    name: str,
    description: str,
    ctx: Context[ServerSession, AppContext],
    project_dir: str = "",
    color: str = "",
    git_strategy: str | None = None,
    git_branch: str | None = None,
    worktree_path: str | None = None,
) -> dict:
    """Create a new workstream with initial state file.

    Args:
        name: Workstream name (lowercase, hyphens, e.g. "api-refactor")
        description: Brief description of the workstream
        project_dir: Project directory path (optional)
        color: Background color hex (e.g. "#0d1a2d") for terminal decoration (optional)
        git_strategy: Git strategy: "branch" (track branch) or "worktree" (create worktree). Omit for no git tracking.
        git_branch: Primary branch name. Auto-detected from project_dir if omitted.
        worktree_path: Absolute path for worktree. Auto-derived if omitted (sibling dir pattern).
    """
    from .workstreams import get_data_dir
    from .workstreams import create_workstream as _create
    return _create(
        data_dir=get_data_dir(), name=name, description=description,
        project_dir=project_dir, color=color,
        git_strategy=git_strategy, git_branch=git_branch, worktree_path=worktree_path,
    )


@mcp.tool()
def update_workstream(
    name: str,
    ctx: Context[ServerSession, AppContext],
    description: str | None = None,
    project_dir: str | None = None,
    color: str | None = None,
    git_strategy: str | None = None,
    git_branch: str | None = None,
    worktree_path: str | None = None,
) -> dict:
    """Update fields on an existing workstream.

    Only provided fields are changed. Pass empty string for color to remove it.

    Args:
        name: Workstream name
        description: New description (optional)
        project_dir: New project directory path (optional)
        color: Background color hex for terminal decoration, e.g. "#0d1a2d" (optional, empty string removes)
        git_strategy: Git strategy: "branch" or "worktree". Empty string removes git config.
        git_branch: Primary branch name. Auto-detected if omitted.
        worktree_path: Absolute path for worktree. Auto-derived if omitted.
    """
    from .workstreams import get_data_dir
    from .workstreams import update_workstream as _update
    return _update(
        data_dir=get_data_dir(), name=name, description=description,
        project_dir=project_dir, color=color,
        git_strategy=git_strategy, git_branch=git_branch, worktree_path=worktree_path,
    )


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
def get_status(
    ctx: Context[ServerSession, AppContext],
    attached: str | None = None,
    format: str = "markdown",
) -> dict | str:
    """Get the current session's workstream status.

    Returns pre-formatted markdown by default with the attached workstream's details
    (description, project, current status, next steps from state file), other
    workstreams summary, and available commands. Pass format="json" for structured data.

    Args:
        attached: Name of the attached workstream (from session context). Omit if none.
        format: "markdown" (default) or "json" for structured data.
    """
    from .workstreams import get_data_dir
    from .workstreams import get_status as _get_status
    return _get_status(data_dir=get_data_dir(), attached=attached, format=format)


@mcp.tool()
def list_workstreams(
    ctx: Context[ServerSession, AppContext],
    format: str = "markdown",
) -> dict | str:
    """List all workstreams grouped by status (active, parked, completed) plus ideas.

    Returns pre-formatted markdown by default. Pass format="json" for structured data.
    """
    from .workstreams import get_data_dir
    from .workstreams import list_workstreams as _list
    return _list(data_dir=get_data_dir(), format=format)


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


@mcp.tool()
def fix_other_hints(ctx: Context[ServerSession, AppContext]) -> dict:
    """Re-attribute session hints tagged as 'other' to the correct workstream.

    Uses project_dir matching to infer workstream for hints that were
    created with workstream='other' (e.g. from backfill before markers existed).
    Idempotent — safe to run multiple times. Only updates hints where the
    match is unambiguous.
    """
    db_path = _get_db_path(ctx)
    conn = get_connection(db_path)
    try:
        return _fix_other_hints_impl(conn)
    finally:
        conn.close()
