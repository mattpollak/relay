"""Workstream data directory management.

Handles registry reads/writes, atomic file operations, and data dir resolution.
The registry (workstreams.json) stays on disk as JSON so bash hooks can read it
with jq. This module is the primary writer.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def get_data_dir() -> Path:
    """Return the relay data directory, respecting XDG_CONFIG_HOME."""
    return Path(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    ) / "relay"


def read_registry(data_dir: Path | None = None) -> dict:
    """Read workstreams.json. Returns empty registry if missing."""
    if data_dir is None:
        data_dir = get_data_dir()
    registry_path = data_dir / "workstreams.json"
    if not registry_path.exists():
        return {"version": 1, "workstreams": {}}
    try:
        return json.loads(registry_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "workstreams": {}}


def write_registry_entry(
    data_dir: Path, name: str, entry: dict
) -> None:
    """Update a single workstream entry in the registry (atomic)."""
    registry = read_registry(data_dir)
    registry["workstreams"][name] = entry
    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")


def atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via tempfile + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        fd = -1  # Mark as closed so cleanup doesn't double-close
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def today() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_timestamp() -> str:
    """Return current UTC timestamp as ISO string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_workstream(
    *,
    data_dir: Path,
    name: str,
    description: str = "",
    project_dir: str = "",
    color: str = "",
    git_strategy: str | None = None,
    git_branch: str | None = None,
    worktree_path: str | None = None,
) -> dict:
    """Create a new workstream: add to registry, write initial state file."""
    registry = read_registry(data_dir)
    if name in registry["workstreams"]:
        return {"status": "error", "message": f"Workstream '{name}' already exists"}

    date = today()
    entry = {
        "status": "active",
        "description": description,
        "created": date,
        "last_touched": date,
        "project_dir": project_dir,
    }
    if color:
        entry["color"] = color

    # Git strategy
    if git_strategy:
        from .git_ops import (
            create_worktree,
            derive_worktree_path,
            get_current_branch,
        )

        branch = git_branch
        if not branch and project_dir:
            branch = get_current_branch(Path(project_dir))
        if not branch:
            return {"status": "error", "message": "git_branch required (could not auto-detect)"}

        git_block: dict = {"strategy": git_strategy, "branch": branch}

        if git_strategy == "worktree":
            if not project_dir:
                return {"status": "error", "message": "project_dir required for worktree strategy"}
            wt_path = worktree_path or derive_worktree_path(project_dir, branch)
            if not Path(wt_path).exists():
                result = create_worktree(Path(project_dir), Path(wt_path), branch)
                if result["status"] == "error":
                    return result
            git_block["worktree_path"] = wt_path

        entry["git"] = git_block

    registry["workstreams"][name] = entry
    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")

    # Write initial state file
    state = f"""# {name}

## Metadata
- **Description:** {description}
- **Created:** {date}
- **Project dir:** {project_dir}

## Current Status
New workstream — no work done yet.

## Key Decisions
(none yet)

## Next Steps
- Define initial goals
"""
    ws_dir = data_dir / "workstreams" / name
    ws_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(ws_dir / "state.md", state)

    return {
        "status": "created",
        "workstream": name,
        "state_file": str(ws_dir / "state.md"),
    }


def park_workstream(
    *,
    data_dir: Path,
    conn: sqlite3.Connection,
    name: str,
    state_content: str,
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
    stash_ref: str | None = None,
    clear_stash: bool = False,
    remove_worktree: bool = False,
) -> dict:
    """Save state then set workstream status to parked."""
    registry = read_registry(data_dir)
    if name not in registry["workstreams"]:
        return {"status": "error", "message": f"Workstream '{name}' not found"}

    # Save state first (reuse save logic)
    save_result = save_workstream(
        data_dir=data_dir,
        conn=conn,
        name=name,
        state_content=state_content,
        session_id=session_id,
        hint_summary=hint_summary,
        hint_decisions=hint_decisions,
        stash_ref=stash_ref,
        clear_stash=clear_stash,
    )

    # Set status to parked
    registry = read_registry(data_dir)  # re-read (save_workstream updated it)
    registry["workstreams"][name]["status"] = "parked"
    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")

    result = {
        "status": "parked",
        "workstream": name,
        **{k: v for k, v in save_result.items() if k != "status"},
    }

    if remove_worktree:
        registry = read_registry(data_dir)
        git = registry["workstreams"].get(name, {}).get("git")
        if git and git.get("strategy") == "worktree" and git.get("worktree_path"):
            from .git_ops import remove_worktree as _remove_wt
            wt_result = _remove_wt(
                Path(registry["workstreams"][name]["project_dir"]),
                Path(git["worktree_path"]),
            )
            if wt_result["status"] == "error":
                result["worktree_warning"] = wt_result["message"]
            else:
                git.pop("worktree_path", None)
                git["strategy"] = "branch"  # downgrade
                atomic_write(
                    data_dir / "workstreams.json",
                    json.dumps(registry, indent=2) + "\n",
                )
                result["worktree_removed"] = True

    return result


def save_workstream(
    *,
    data_dir: Path,
    conn: sqlite3.Connection,
    name: str,
    state_content: str,
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
    stash_ref: str | None = None,
    clear_stash: bool = False,
) -> dict:
    """Save workstream state atomically.

    1. Write state.md.new
    2. Rotate state.md -> state.md.bak (if exists)
    3. Rename state.md.new -> state.md
    4. Update registry last_touched
    5. Write session hint to DB (if session_id provided)
    6. Write/update session marker in DB (if session_id provided)
    """
    ws_dir = data_dir / "workstreams" / name
    ws_dir.mkdir(parents=True, exist_ok=True)

    state_path = ws_dir / "state.md"
    new_path = ws_dir / "state.md.new"
    bak_path = ws_dir / "state.md.bak"

    # Step 1: Write new state to temp file
    atomic_write(new_path, state_content)

    # Step 2-3: Rotate (backup old, move new into place)
    if state_path.exists():
        os.replace(state_path, bak_path)
    os.replace(new_path, state_path)

    # Step 4: Update registry
    registry = read_registry(data_dir)
    if name in registry["workstreams"]:
        registry["workstreams"][name]["last_touched"] = today()
        atomic_write(
            data_dir / "workstreams.json",
            json.dumps(registry, indent=2) + "\n",
        )

    # Step 5: Ensure session row exists (so FK constraints are satisfied)
    if session_id:
        from relay_server.db import ensure_session
        ensure_session(conn, session_id)

    # Step 6: Write hint to DB
    if session_id and hint_summary:
        ts = utc_timestamp()
        conn.execute(
            """INSERT OR REPLACE INTO session_hints
               (session_id, timestamp, source_file, workstream, summary, decisions)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                ts,
                f"mcp-{ts}-{session_id[:8]}",
                name,
                json.dumps(hint_summary),
                json.dumps(hint_decisions) if hint_decisions else None,
            ),
        )

    # Step 7: Write/update session marker in DB
    if session_id:
        conn.execute(
            """INSERT OR REPLACE INTO session_markers
               (session_id, workstream, attached_at)
               VALUES (?, ?, ?)""",
            (session_id, name, utc_timestamp()),
        )

    # Manage stash ref in registry
    if stash_ref or clear_stash:
        registry = read_registry(data_dir)
        if name in registry["workstreams"]:
            entry = registry["workstreams"][name]
            git = entry.get("git")
            if git:
                if clear_stash:
                    git.pop("stash_ref", None)
                    git.pop("stash_message", None)
                elif stash_ref:
                    git["stash_ref"] = stash_ref
                    git["stash_message"] = f"relay: {name} at {utc_timestamp()}"
                entry["git"] = git
                atomic_write(
                    data_dir / "workstreams.json",
                    json.dumps(registry, indent=2) + "\n",
                )

    conn.commit()

    return {
        "status": "saved",
        "workstream": name,
        "state_file": str(state_path),
        "backup": str(bak_path) if bak_path.exists() else None,
        "hint_written": bool(session_id and hint_summary),
    }


def switch_workstream(
    *,
    data_dir: Path,
    conn: sqlite3.Connection,
    to_name: str,
    from_name: str | None = None,
    state_content: str | None = None,
    session_id: str | None = None,
    hint_summary: list[str] | None = None,
    hint_decisions: list[str] | None = None,
    stash_ref: str | None = None,
) -> dict:
    """Switch session from one workstream to another.

    Saves current workstream (if from_name provided), activates target,
    writes session marker, returns target state content.
    """
    registry = read_registry(data_dir)
    if to_name not in registry["workstreams"]:
        return {"status": "error", "message": f"Workstream '{to_name}' not found"}

    # Save current workstream if provided
    if from_name and state_content:
        save_workstream(
            data_dir=data_dir,
            conn=conn,
            name=from_name,
            state_content=state_content,
            session_id=session_id,
            hint_summary=hint_summary,
            hint_decisions=hint_decisions,
        )

    # Store stash_ref on from workstream if provided
    if stash_ref and from_name:
        registry = read_registry(data_dir)
        from_entry = registry["workstreams"].get(from_name)
        if from_entry and "git" in from_entry:
            from_entry["git"]["stash_ref"] = stash_ref
            from_entry["git"]["stash_message"] = f"relay: {from_name} at {utc_timestamp()}"
            atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")

    # Activate target
    registry = read_registry(data_dir)  # re-read after save
    registry["workstreams"][to_name]["status"] = "active"
    registry["workstreams"][to_name]["last_touched"] = today()
    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")

    # Write session marker
    if session_id:
        conn.execute(
            """INSERT OR REPLACE INTO session_markers
               (session_id, workstream, attached_at)
               VALUES (?, ?, ?)""",
            (session_id, to_name, utc_timestamp()),
        )
        conn.commit()

        # Write session-workstream mapping for statusline
        sw_dir = data_dir / "session-workstreams"
        sw_dir.mkdir(parents=True, exist_ok=True)
        (sw_dir / session_id).write_text(to_name)

    # Read target state
    target_state = ""
    state_path = data_dir / "workstreams" / to_name / "state.md"
    if state_path.exists():
        target_state = state_path.read_text()

    # Read supplementary files
    supplementary = {}
    for extra in ("plan.md", "architecture.md"):
        extra_path = data_dir / "workstreams" / to_name / extra
        if extra_path.exists():
            supplementary[extra] = extra_path.read_text()

    result: dict = {
        "status": "switched",
        "from": from_name,
        "to": to_name,
        "target_state": target_state,
        "supplementary": supplementary,
        "project_dir": registry["workstreams"][to_name].get("project_dir", ""),
    }

    # Git awareness for target workstream
    target_entry = registry["workstreams"][to_name]
    target_git = target_entry.get("git")
    if target_git:
        from . import git_ops
        strategy = target_git.get("strategy")
        project_dir = target_entry.get("project_dir", "")

        if strategy == "branch":
            expected_branch = target_git.get("branch")
            if project_dir and expected_branch:
                current_branch = git_ops.get_current_branch(Path(project_dir))
                if current_branch and current_branch != expected_branch:
                    result["git_warning"] = (
                        f"Current branch '{current_branch}' doesn't match expected '{expected_branch}'"
                    )
                    result["git_suggestion"] = f"git checkout {expected_branch}"
                elif git_ops.is_dirty(Path(project_dir)):
                    result["dirty_warning"] = "Working tree has uncommitted changes"

        elif strategy == "worktree":
            wt_path = target_git.get("worktree_path")
            if wt_path:
                if Path(wt_path).exists():
                    result["worktree_path"] = wt_path
                else:
                    result["git_warning"] = f"Worktree path does not exist: {wt_path}"

        # Check stash_ref on target
        target_stash_ref = target_git.get("stash_ref")
        if target_stash_ref and project_dir:
            if git_ops.validate_stash_ref(Path(project_dir), target_stash_ref):
                stash_message = target_git.get("stash_message", "")
                result["stash_reminder"] = (
                    f"Stashed changes at {target_stash_ref[:7]}: {stash_message}"
                ).strip(": ")
            else:
                # Stale stash — clear it from registry silently
                registry = read_registry(data_dir)
                target_git_entry = registry["workstreams"][to_name].get("git", {})
                target_git_entry.pop("stash_ref", None)
                target_git_entry.pop("stash_message", None)
                atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")

    return result


def update_workstream(
    *,
    data_dir: Path,
    name: str,
    description: str | None = None,
    project_dir: str | None = None,
    color: str | None = None,
    git_strategy: str | None = None,
    git_branch: str | None = None,
    worktree_path: str | None = None,
) -> dict:
    """Update mutable fields on an existing workstream."""
    registry = read_registry(data_dir)
    if name not in registry["workstreams"]:
        return {"status": "error", "message": f"Workstream '{name}' not found"}

    entry = registry["workstreams"][name]
    updated = []
    if description is not None:
        entry["description"] = description
        updated.append("description")
    if project_dir is not None:
        entry["project_dir"] = project_dir
        updated.append("project_dir")
    if color is not None:
        if color:
            entry["color"] = color
        else:
            entry.pop("color", None)
        updated.append("color")

    if git_strategy is not None:
        if git_strategy == "":
            entry.pop("git", None)
        else:
            branch = git_branch
            if not branch:
                existing_git = entry.get("git")
                if existing_git:
                    branch = existing_git.get("branch")
            if not branch:
                proj = entry.get("project_dir", "")
                if proj:
                    from .git_ops import get_current_branch
                    branch = get_current_branch(Path(proj))
            if not branch:
                return {"status": "error", "message": "git_branch required (could not auto-detect)"}

            git_block: dict = {"strategy": git_strategy, "branch": branch}
            if git_strategy == "worktree":
                wt = worktree_path
                if not wt:
                    existing_git = entry.get("git")
                    if existing_git:
                        wt = existing_git.get("worktree_path")
                if not wt:
                    from .git_ops import derive_worktree_path
                    proj = entry.get("project_dir", "")
                    if proj:
                        wt = derive_worktree_path(proj, branch)
                if wt:
                    git_block["worktree_path"] = wt
            entry["git"] = git_block
        updated.append("git")

    if not updated:
        return {"status": "noop", "message": "No fields to update"}

    atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")
    return {"status": "updated", "workstream": name, "fields": updated}


def manage_idea(
    *,
    data_dir: Path,
    action: str,
    text: str | None = None,
    idea_id: int | None = None,
) -> dict:
    """Add, remove, or list ideas."""
    ideas_path = data_dir / "ideas.json"
    ideas = []
    if ideas_path.exists():
        try:
            ideas = json.loads(ideas_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if action == "list":
        return {"status": "ok", "ideas": ideas}

    if action == "add":
        if not text:
            return {"status": "error", "message": "Text is required"}
        new_id = max((i.get("id", 0) for i in ideas), default=0) + 1
        ideas.append({"id": new_id, "text": text, "added": today()})
        atomic_write(ideas_path, json.dumps(ideas, indent=2) + "\n")
        return {"status": "added", "id": new_id, "text": text}

    if action == "remove":
        if idea_id is None:
            return {"status": "error", "message": "idea_id is required"}
        original_len = len(ideas)
        ideas = [i for i in ideas if i.get("id") != idea_id]
        if len(ideas) == original_len:
            return {"status": "error", "message": f"Idea {idea_id} not found"}
        atomic_write(ideas_path, json.dumps(ideas, indent=2) + "\n")
        return {"status": "removed", "id": idea_id}

    return {"status": "error", "message": f"Unknown action: {action}"}


def manage_worktree(
    *,
    data_dir: Path,
    action: str,
    name: str | None = None,
    path: str | None = None,
) -> dict:
    """Manage git worktree associations for workstreams."""
    if action == "list":
        registry = read_registry(data_dir)
        worktrees = []
        for ws_name, ws in registry.get("workstreams", {}).items():
            git = ws.get("git")
            if git and git.get("strategy") == "worktree":
                wt_path = git.get("worktree_path", "")
                worktrees.append({
                    "workstream": ws_name,
                    "worktree_path": wt_path,
                    "branch": git.get("branch", ""),
                    "exists": Path(wt_path).exists() if wt_path else False,
                })
        return {"status": "ok", "worktrees": worktrees}

    if action == "attach":
        if not name:
            return {"status": "error", "message": "name is required for attach"}
        if not path:
            return {"status": "error", "message": "path is required for attach"}
        registry = read_registry(data_dir)
        workstreams = registry.get("workstreams", {})
        if name not in workstreams:
            return {"status": "error", "message": f"Workstream '{name}' not found"}
        wt_path = Path(path)
        if not wt_path.exists() or not wt_path.is_dir():
            return {"status": "error", "message": f"Path does not exist or is not a directory: {path}"}
        from .git_ops import get_worktree_branch
        branch = get_worktree_branch(wt_path)
        if not branch:
            return {"status": "error", "message": f"Could not detect branch at {path}"}
        entry = workstreams[name]
        entry["git"] = {
            **(entry.get("git") or {}),
            "strategy": "worktree",
            "branch": branch,
            "worktree_path": str(path),
        }
        write_registry_entry(data_dir, name, entry)
        return {"status": "attached", "workstream": name, "worktree_path": str(path), "branch": branch}

    if action == "detach":
        if not name:
            return {"status": "error", "message": "name is required for detach"}
        registry = read_registry(data_dir)
        workstreams = registry.get("workstreams", {})
        if name not in workstreams:
            return {"status": "error", "message": f"Workstream '{name}' not found"}
        entry = workstreams[name]
        git = entry.get("git") or {}
        branch = git.get("branch", "")
        entry["git"] = {"strategy": "branch", "branch": branch}
        write_registry_entry(data_dir, name, entry)
        return {"status": "detached", "workstream": name, "branch": branch}

    if action == "remove":
        if not name:
            return {"status": "error", "message": "name is required for remove"}
        registry = read_registry(data_dir)
        workstreams = registry.get("workstreams", {})
        if name not in workstreams:
            return {"status": "error", "message": f"Workstream '{name}' not found"}
        entry = workstreams[name]
        git = entry.get("git") or {}
        wt_path_str = git.get("worktree_path")
        project_dir = entry.get("project_dir", "")
        if not wt_path_str:
            return {"status": "error", "message": "No worktree_path set for this workstream"}
        if not project_dir:
            return {"status": "error", "message": "No project_dir set for this workstream"}
        from .git_ops import remove_worktree
        result = remove_worktree(Path(project_dir), Path(wt_path_str))
        if result.get("status") == "error":
            return result
        entry.pop("git", None)
        write_registry_entry(data_dir, name, entry)
        return {"status": "removed", "workstream": name}

    return {"status": "error", "message": f"Unknown action: {action}"}


def list_workstreams(*, data_dir: Path, format: str = "markdown") -> dict | str:
    """List all workstreams grouped by status, plus ideas.

    Args:
        format: "markdown" (default) returns pre-formatted markdown string.
                "json" returns structured dict with active/parked/completed/ideas.
    """
    registry = read_registry(data_dir)

    groups: dict[str, list] = {"active": [], "parked": [], "completed": []}
    for name, ws in registry.get("workstreams", {}).items():
        status = ws.get("status", "parked")
        bucket = groups.get(status, groups["parked"])
        bucket.append({
            "name": name,
            "description": ws.get("description", ""),
            "last_touched": ws.get("last_touched", ""),
            "project_dir": ws.get("project_dir", ""),
            "git": ws.get("git"),
        })

    # Read ideas
    ideas = []
    ideas_path = data_dir / "ideas.json"
    if ideas_path.exists():
        try:
            ideas = json.loads(ideas_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    if format == "json":
        return {**groups, "ideas": ideas}

    # Build pre-formatted markdown
    lines = []
    for status in ("active", "parked", "completed"):
        items = groups[status]
        if not items:
            continue
        lines.append(f"## {status.title()}")
        lines.append("| Workstream | Description | Last Touched |")
        lines.append("|---|---|---|")
        for ws in items:
            desc = ws['description']
            git = ws.get("git")
            if git:
                strategy = git.get("strategy")
                if strategy == "branch":
                    branch = git.get("branch", "")
                    if branch:
                        desc = f"{desc} ({branch})"
                elif strategy == "worktree":
                    wt_path = git.get("worktree_path", "")
                    if wt_path:
                        # Shorten home dir paths with ~
                        home = os.path.expanduser("~")
                        if wt_path.startswith(home):
                            wt_path = "~" + wt_path[len(home):]
                        desc = f"{desc} (worktree: {wt_path})"
            lines.append(f"| {ws['name']} | {desc} | {ws['last_touched']} |")
        lines.append("")

    if ideas:
        lines.append("## Ideas")
        for idea in ideas:
            text = idea.get("text", "")
            added = idea.get("added", "")
            lines.append(f"{idea.get('id', '')}. {text} *({added})*")
        lines.append("")
        lines.append("`/relay:idea promote <id>` to start working on one.")
        lines.append("")

    lines.append(
        "**Commands:** `/relay:status` · `/relay:new` · `/relay:switch <name>` "
        "· `/relay:save` · `/relay:park` · `/relay:idea`"
    )

    return "\n".join(lines)


def get_status(
    *, data_dir: Path, attached: str | None = None, format: str = "markdown",
) -> dict | str:
    """Build a status view for the current session.

    Args:
        attached: Name of the attached workstream (if any).
        format: "markdown" (default) returns pre-formatted markdown string.
                "json" returns structured dict.
    """
    registry = read_registry(data_dir)
    workstreams = registry.get("workstreams", {})

    # Build attached workstream data
    attached_data = None
    if attached and attached in workstreams:
        ws = workstreams[attached]
        attached_data = {
            "name": attached,
            "description": ws.get("description", ""),
            "project_dir": ws.get("project_dir", ""),
            "last_touched": ws.get("last_touched", ""),
            "current_status": None,
            "next_steps": None,
            "git": ws.get("git"),
        }
        state_path = data_dir / "workstreams" / attached / "state.md"
        if state_path.exists():
            state = state_path.read_text()
            attached_data["current_status"] = _extract_section(state, "Current Status")
            attached_data["next_steps"] = _extract_section(state, "Next Steps")

    # Build other workstreams grouped by status
    others: dict[str, list[str]] = {"active": [], "parked": [], "completed": []}
    for name, ws in workstreams.items():
        if name == attached:
            continue
        status = ws.get("status", "parked")
        bucket = others.get(status, others["parked"])
        bucket.append(name)

    if format == "json":
        return {
            "attached": attached_data,
            "others": others,
        }

    # Build markdown
    lines = []
    if attached_data:
        lines.append(f"## Attached: {attached}")
        lines.append(f"**Description:** {attached_data['description']}")
        lines.append(f"**Project:** {attached_data['project_dir'] or 'none'}")
        lines.append(f"**Last touched:** {attached_data['last_touched']}")
        lines.append("")
        if attached_data["current_status"]:
            lines.append("### Current Status")
            lines.append(attached_data["current_status"])
            lines.append("")
        if attached_data["next_steps"]:
            lines.append("### Next Steps")
            lines.append(attached_data["next_steps"])
            lines.append("")
        # Git section
        git = attached_data.get("git")
        if git:
            strategy = git.get("strategy", "")
            branch = git.get("branch", "")
            lines.append("## Git")
            lines.append(f"- **Strategy:** {strategy}")
            if branch:
                lines.append(f"- **Branch:** {branch}")
            # Check current branch if project_dir is a real git repo
            project_dir = attached_data.get("project_dir", "")
            if project_dir and strategy == "branch" and branch:
                try:
                    from .git_ops import get_current_branch
                    current_branch = get_current_branch(Path(project_dir))
                    if current_branch:
                        if current_branch == branch:
                            lines.append(f"- **Current branch:** {current_branch} \u2713")
                        else:
                            lines.append(
                                f"- **Current branch:** {current_branch} \u26a0\ufe0f (expected {branch})"
                            )
                except Exception:
                    pass
            stash_ref = git.get("stash_ref")
            if stash_ref:
                stash_date = ""
                stash_msg = git.get("stash_message", "")
                # Try to extract date from stash message (e.g. "relay: name at 2026-03-22T...")
                if "at " in stash_msg:
                    ts_part = stash_msg.split("at ")[-1]
                    stash_date = ts_part[:10]  # YYYY-MM-DD
                stash_line = f"- **Stashed changes:** {stash_ref[:7]}"
                if stash_date:
                    stash_line += f" (from {stash_date})"
                lines.append(stash_line)
            lines.append("")
    elif attached:
        lines.append(f"No workstream '{attached}' found in registry.")
        lines.append("")
    else:
        lines.append("No workstream attached to this session.")
        lines.append("")

    lines.append(f"**Other active:** {', '.join(others['active']) or 'none'}")
    lines.append(f"**Parked:** {', '.join(others['parked']) or 'none'}")
    lines.append(f"**Completed:** {', '.join(others['completed']) or 'none'}")
    lines.append("")
    lines.append(
        "**Commands:** `/relay:new` · `/relay:switch <name>` "
        "· `/relay:save` · `/relay:park` · `/relay:list`"
    )

    return "\n".join(lines)


def _extract_section(markdown: str, heading: str) -> str | None:
    """Extract content under a ## heading from markdown, stopping at the next heading."""
    lines = markdown.split("\n")
    capturing = False
    content: list[str] = []
    for line in lines:
        if line.startswith("## ") and heading in line:
            capturing = True
            continue
        if capturing:
            if line.startswith("## "):
                break
            content.append(line)
    text = "\n".join(content).strip()
    return text if text else None
