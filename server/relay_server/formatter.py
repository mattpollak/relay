"""Format conversation data as human-readable markdown."""

from datetime import datetime

# Max chars of message content before truncation
CONTENT_TRUNCATE = 500

ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
    "tool_summary": "Tools",
    "plan": "Plan",
}


def _parse_ts(ts: str) -> datetime | None:
    """Parse an ISO timestamp string."""
    if not ts:
        return None
    try:
        # Handle both Z and +00:00 suffixes
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _short_time(ts: str) -> str:
    """Format timestamp as HH:MM."""
    dt = _parse_ts(ts)
    return dt.strftime("%H:%M") if dt else "??:??"


def _date_range(sessions: list[dict]) -> str:
    """Format the date range across all sessions."""
    first = _parse_ts(sessions[0].get("first_timestamp", ""))
    last = _parse_ts(sessions[-1].get("last_timestamp", ""))
    if not first or not last:
        return "unknown"

    if first.date() == last.date():
        return f"{first.strftime('%b %d, %Y')} {first.strftime('%H:%M')}–{last.strftime('%H:%M')} UTC"
    return f"{first.strftime('%b %d %H:%M')} – {last.strftime('%b %d %H:%M, %Y')} UTC"


def _truncate(content: str, max_len: int = CONTENT_TRUNCATE) -> str:
    """Truncate content with an indicator of remaining length."""
    if len(content) <= max_len:
        return content
    remaining = len(content) - max_len
    return content[:max_len] + f"\n\n*[...{remaining:,} more chars]*"


def _is_noise(msg: dict) -> bool:
    """Check if a message is system noise that should be skipped in markdown."""
    content = msg.get("content", "")
    if not content.strip():
        return True
    # Claude Code internal command messages
    if "<command-name>" in content or "<command-message>" in content:
        return True
    if "<local-command-caveat>" in content or "<local-command-stdout>" in content:
        return True
    if "<system-reminder>" in content:
        return True
    return False


def format_conversation(sessions: list[dict], messages: list[dict]) -> str:
    """Format a conversation as readable markdown.

    Args:
        sessions: List of session metadata dicts
        messages: List of message dicts with id, session_id, role, content, timestamp
    """
    # Filter out system noise
    messages = [m for m in messages if not _is_noise(m)]

    lines: list[str] = []

    slug = sessions[0].get("slug") or sessions[0].get("session_id", "unknown")[:12]
    project = sessions[0].get("project_dir", "")
    branch = sessions[0].get("git_branch", "")

    # Header
    session_count = len(sessions)
    if session_count > 1:
        lines.append(f"## {slug} ({session_count} sessions)")
    else:
        lines.append(f"## {slug}")

    meta_parts = []
    if sessions[0].get("host"):
        meta_parts.append(f"**Machine:** {sessions[0]['host']}")
    if project:
        meta_parts.append(f"**Project:** {project}")
    if branch:
        meta_parts.append(f"**Branch:** {branch}")
    if meta_parts:
        lines.append(" | ".join(meta_parts))

    lines.append(f"**Range:** {_date_range(sessions)} | **Messages:** {len(messages)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Track which session we're in for boundary markers
    current_session_id = None

    for msg in messages:
        # Session boundary marker
        if len(sessions) > 1 and msg.get("session_id") != current_session_id:
            current_session_id = msg["session_id"]
            # Find session index
            for i, s in enumerate(sessions):
                if s["session_id"] == current_session_id:
                    session_num = i + 1
                    break
            else:
                session_num = "?"
            lines.append(f"*--- Session {session_num} of {session_count} ---*")
            lines.append("")

        role = msg.get("role", "unknown")
        label = ROLE_LABELS.get(role, role.title())
        time = _short_time(msg.get("timestamp", ""))
        content = msg.get("content", "")
        msg_id = msg.get("id", "")

        # Format based on role
        if role == "tool_summary":
            # Compact: show tool calls as indented code block
            lines.append(f"**{label}** ({time})")
            # Each tool call is on its own line, keep them short
            tool_lines = content.strip().split("\n")
            if len(tool_lines) <= 4:
                lines.append("```")
                for tl in tool_lines:
                    lines.append(tl)
                lines.append("```")
            else:
                lines.append("```")
                for tl in tool_lines[:3]:
                    lines.append(tl)
                lines.append(f"  ...and {len(tool_lines) - 3} more")
                lines.append("```")
        elif role == "plan":
            lines.append(f"**{label}** ({time}) `#{msg_id}`")
            lines.append("")
            lines.append(f"> {_truncate(content, 800).replace(chr(10), chr(10) + '> ')}")
        else:
            lines.append(f"**{label}** ({time}) `#{msg_id}`")
            lines.append("")
            lines.append(_truncate(content))

        lines.append("")

    return "\n".join(lines)
