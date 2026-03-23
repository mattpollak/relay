# server/tests/test_manage_worktree.py
"""Tests for manage_worktree tool."""

import json
import subprocess
import tempfile
from pathlib import Path

from relay_server.workstreams import manage_worktree, create_workstream, read_registry, atomic_write


def _init_repo(path):
    subprocess.run(["git", "init", str(path)], capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"], capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], capture_output=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True)


def _setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    create_workstream(
        data_dir=data_dir, name="test-ws", description="Test",
        project_dir=str(repo), git_strategy="branch", git_branch="main",
    )
    return data_dir, repo


def test_attach_worktree():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir, repo = _setup(Path(tmpdir))
        wt_path = Path(tmpdir) / "wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "feat-attach", str(wt_path)], capture_output=True)
        result = manage_worktree(data_dir=data_dir, action="attach", name="test-ws", path=str(wt_path))
        assert result["status"] == "attached"
        reg = read_registry(data_dir)
        git = reg["workstreams"]["test-ws"]["git"]
        assert git["strategy"] == "worktree"
        assert git["worktree_path"] == str(wt_path)
        assert git["branch"] == "feat-attach"


def test_detach_worktree():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir, repo = _setup(Path(tmpdir))
        wt_path = Path(tmpdir) / "wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "feat-detach", str(wt_path)], capture_output=True)
        manage_worktree(data_dir=data_dir, action="attach", name="test-ws", path=str(wt_path))
        result = manage_worktree(data_dir=data_dir, action="detach", name="test-ws")
        assert result["status"] == "detached"
        reg = read_registry(data_dir)
        git = reg["workstreams"]["test-ws"]["git"]
        assert git["strategy"] == "branch"
        assert "worktree_path" not in git
        assert git["branch"] == "feat-detach"  # preserved


def test_remove_worktree():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir, repo = _setup(Path(tmpdir))
        wt_path = Path(tmpdir) / "wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "feat-rm", str(wt_path)], capture_output=True)
        manage_worktree(data_dir=data_dir, action="attach", name="test-ws", path=str(wt_path))
        result = manage_worktree(data_dir=data_dir, action="remove", name="test-ws")
        assert result["status"] == "removed"
        assert not wt_path.exists()
        reg = read_registry(data_dir)
        assert reg["workstreams"]["test-ws"].get("git") is None


def test_list_worktrees():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir, repo = _setup(Path(tmpdir))
        wt_path = Path(tmpdir) / "wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "feat-list", str(wt_path)], capture_output=True)
        manage_worktree(data_dir=data_dir, action="attach", name="test-ws", path=str(wt_path))
        result = manage_worktree(data_dir=data_dir, action="list")
        assert result["status"] == "ok"
        assert len(result["worktrees"]) == 1
        assert result["worktrees"][0]["workstream"] == "test-ws"
