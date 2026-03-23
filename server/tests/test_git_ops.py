# server/tests/test_git_ops.py
"""Tests for git subprocess helpers."""

import os
import subprocess
import tempfile
from pathlib import Path

from relay_server.git_ops import (
    get_current_branch,
    derive_worktree_path,
    is_dirty,
    is_git_repo,
    sanitize_branch_for_path,
    validate_stash_ref,
    create_worktree,
    remove_worktree,
    list_worktrees,
    get_worktree_branch,
)


def _init_repo(path):
    """Create a git repo with one commit."""
    subprocess.run(["git", "init", str(path)], capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True)
    (path / "README.md").write_text("# Test")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True)


def test_is_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        assert is_git_repo(repo) is False
        _init_repo(repo)
        assert is_git_repo(repo) is True


def test_get_current_branch():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        branch = get_current_branch(repo)
        assert branch in ("main", "master")


def test_get_current_branch_not_a_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert get_current_branch(Path(tmpdir)) is None


def test_is_dirty_clean():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        assert is_dirty(repo) is False


def test_is_dirty_with_changes():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        (repo / "new.txt").write_text("dirty")
        assert is_dirty(repo) is True


def test_sanitize_branch_for_path():
    assert sanitize_branch_for_path("feat/payments-api") == "feat-payments-api"
    assert sanitize_branch_for_path("feat/foo#bar") == "feat-foo-bar"
    assert sanitize_branch_for_path("feat//double") == "feat-double"
    assert sanitize_branch_for_path("main") == "main"


def test_derive_worktree_path():
    assert derive_worktree_path("/home/user/src/monorepo", "feat/payments") == "/home/user/src/monorepo-feat-payments"
    assert derive_worktree_path("/home/user/src/relay", "main") == "/home/user/src/relay-main"


def test_validate_stash_ref_valid():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        sha = result.stdout.strip()
        assert validate_stash_ref(repo, sha) is True


def test_validate_stash_ref_invalid():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        assert validate_stash_ref(repo, "deadbeef" * 5) is False


def test_create_and_list_worktree():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        wt_path = Path(tmpdir) / "worktree"
        result = create_worktree(repo, wt_path, "feat-test")
        assert result["status"] == "created"
        assert wt_path.exists()

        wts = list_worktrees(repo)
        paths = [w["path"] for w in wts]
        assert str(wt_path) in paths


def test_create_worktree_existing_branch():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        subprocess.run(["git", "-C", str(repo), "branch", "feat-existing"], capture_output=True)
        wt_path = Path(tmpdir) / "worktree"
        result = create_worktree(repo, wt_path, "feat-existing")
        assert result["status"] == "created"


def test_get_worktree_branch():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        wt_path = Path(tmpdir) / "worktree"
        create_worktree(repo, wt_path, "feat-branch")
        assert get_worktree_branch(wt_path) == "feat-branch"


def test_remove_worktree_clean():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        wt_path = Path(tmpdir) / "worktree"
        create_worktree(repo, wt_path, "feat-remove")
        result = remove_worktree(repo, wt_path)
        assert result["status"] == "removed"
        assert not wt_path.exists()


def test_remove_worktree_dirty():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        _init_repo(repo)
        wt_path = Path(tmpdir) / "worktree"
        create_worktree(repo, wt_path, "feat-dirty")
        (wt_path / "dirty.txt").write_text("uncommitted")
        result = remove_worktree(repo, wt_path)
        assert result["status"] == "error"
        assert "dirty" in result["message"].lower() or "uncommitted" in result["message"].lower()
