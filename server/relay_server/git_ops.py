# server/relay_server/git_ops.py
"""Git subprocess helpers.

All git operations are isolated here to keep workstreams.py focused on
registry/state management. Functions return data; callers decide what to do.
"""

import re
import subprocess
from pathlib import Path


def is_git_repo(path: Path) -> bool:
    """Check if path is inside a git repository."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True,
    )
    return result.returncode == 0


def get_current_branch(path: Path) -> str | None:
    """Get the current branch name, or None if not a git repo / detached HEAD."""
    result = subprocess.run(
        ["git", "-C", str(path), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def is_dirty(path: Path) -> bool:
    """Check if the working tree has uncommitted changes."""
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def sanitize_branch_for_path(branch: str) -> str:
    """Sanitize a branch name for use in a filesystem path.

    Replaces /, #, spaces, and other non-alphanumeric/hyphen chars with -.
    Collapses consecutive hyphens.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", branch)
    sanitized = re.sub(r"-+", "-", sanitized)
    return sanitized.strip("-")


def validate_stash_ref(path: Path, sha: str) -> bool:
    """Check if a stash SHA still exists as a valid git object."""
    result = subprocess.run(
        ["git", "-C", str(path), "cat-file", "-t", sha],
        capture_output=True,
    )
    return result.returncode == 0


def derive_worktree_path(project_dir: str, branch: str) -> str:
    """Derive default worktree path from project dir and branch name.

    Uses sibling directory pattern: <project_dir>-<sanitized-branch>
    """
    return f"{project_dir}-{sanitize_branch_for_path(branch)}"


def create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> dict:
    """Create a git worktree.

    If branch already exists, checks it out. If not, creates it from HEAD.
    """
    check = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
    )
    if check.returncode == 0:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", str(worktree_path), branch],
            capture_output=True, text=True,
        )
    else:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", "-b", branch, str(worktree_path)],
            capture_output=True, text=True,
        )

    if result.returncode != 0:
        return {"status": "error", "message": result.stderr.strip()}
    return {"status": "created", "path": str(worktree_path), "branch": branch}


def remove_worktree(repo_path: Path, worktree_path: Path) -> dict:
    """Remove a git worktree. Refuses if working tree is dirty."""
    if is_dirty(worktree_path):
        return {"status": "error", "message": f"Worktree has uncommitted changes at {worktree_path}"}

    result = subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "remove", str(worktree_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"status": "error", "message": result.stderr.strip()}
    return {"status": "removed", "path": str(worktree_path)}


def list_worktrees(repo_path: Path) -> list[dict]:
    """List all worktrees for a repo. Returns list of {path, branch, bare}."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []

    worktrees = []
    current = {}
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"path": line[len("worktree "):]}
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "bare":
            current["bare"] = True
        elif line == "":
            if current:
                worktrees.append(current)
                current = {}
    if current:
        worktrees.append(current)
    return worktrees


def get_worktree_branch(worktree_path: Path) -> str | None:
    """Get the branch checked out in a worktree."""
    return get_current_branch(worktree_path)
