"""Tests for switch_workstream and list_workstreams."""

import json
import subprocess
import tempfile
from pathlib import Path

from relay_server.db import ensure_schema, get_connection
from relay_server.workstreams import atomic_write


def _setup(tmpdir):
    db_path = Path(tmpdir) / "test.db"
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()
    ensure_schema(db_path)

    registry = {
        "version": 1,
        "workstreams": {
            "alpha": {"status": "active", "description": "Alpha", "created": "2026-01-01", "last_touched": "2026-01-01", "project_dir": "/alpha"},
            "beta": {"status": "parked", "description": "Beta", "created": "2026-01-01", "last_touched": "2026-01-01", "project_dir": "/beta"},
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))
    for ws in ("alpha", "beta"):
        d = data_dir / "workstreams" / ws
        d.mkdir(parents=True)
        (d / "state.md").write_text(f"# {ws.title()} State")

    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO sessions (session_id, project_dir, first_timestamp, last_timestamp, message_count)
           VALUES ('aabbccdd-1122-3344-5566-778899aabbcc', '/test', '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z', 10)"""
    )
    conn.commit()
    return db_path, data_dir, conn


def _setup_git(tmpdir):
    """Setup with 'source' and 'target' workstreams for git-aware switch tests."""
    db_path = Path(tmpdir) / "test.db"
    data_dir = Path(tmpdir) / "data"
    data_dir.mkdir()
    ensure_schema(db_path)

    registry = {
        "version": 1,
        "workstreams": {
            "source": {"status": "active", "description": "Source", "created": "2026-01-01", "last_touched": "2026-01-01", "project_dir": ""},
            "target": {"status": "parked", "description": "Target", "created": "2026-01-01", "last_touched": "2026-01-01", "project_dir": ""},
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))
    for ws in ("source", "target"):
        d = data_dir / "workstreams" / ws
        d.mkdir(parents=True)
        (d / "state.md").write_text(f"# {ws.title()} State")

    conn = get_connection(db_path)
    return db_path, data_dir, conn


def test_switch_saves_current_and_loads_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import switch_workstream, read_registry
            result = switch_workstream(
                data_dir=data_dir,
                conn=conn,
                from_name="alpha",
                to_name="beta",
                state_content="# Alpha saved",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
                hint_summary=["Wrapped up alpha work"],
            )
            assert result["status"] == "switched"
            assert "Beta State" in result["target_state"]

            reg = read_registry(data_dir)
            assert reg["workstreams"]["alpha"]["status"] == "active"  # stays active
            assert reg["workstreams"]["beta"]["status"] == "active"  # now active

            # Marker points to beta
            row = conn.execute(
                "SELECT workstream FROM session_markers WHERE session_id = 'aabbccdd-1122-3344-5566-778899aabbcc'"
            ).fetchone()
            assert row["workstream"] == "beta"
        finally:
            conn.close()


def test_switch_without_from():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import switch_workstream
            result = switch_workstream(
                data_dir=data_dir,
                conn=conn,
                to_name="beta",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
            )
            assert result["status"] == "switched"
        finally:
            conn.close()


def test_switch_nonexistent_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import switch_workstream
            result = switch_workstream(
                data_dir=data_dir, conn=conn, to_name="nope",
                session_id="aabbccdd-1122-3344-5566-778899aabbcc",
            )
            assert result["status"] == "error"
        finally:
            conn.close()


def test_list_workstreams_markdown():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        ideas = [{"id": 1, "text": "try websockets", "added": "2026-03-01"}]
        (data_dir / "ideas.json").write_text(json.dumps(ideas))
        try:
            from relay_server.workstreams import list_workstreams
            result = list_workstreams(data_dir=data_dir)
            assert isinstance(result, str)
            assert "## Active" in result
            assert "## Parked" in result
            assert "| alpha |" in result
            assert "| beta |" in result
            assert "## Ideas" in result
            assert "try websockets" in result
            assert "/relay:idea promote" in result
            assert "**Commands:**" in result
        finally:
            conn.close()


def test_list_workstreams_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        ideas = [{"id": 1, "text": "try websockets", "added": "2026-03-01"}]
        (data_dir / "ideas.json").write_text(json.dumps(ideas))
        try:
            from relay_server.workstreams import list_workstreams
            result = list_workstreams(data_dir=data_dir, format="json")
            assert isinstance(result, dict)
            assert "alpha" in [w["name"] for w in result["active"]]
            assert "beta" in [w["name"] for w in result["parked"]]
            assert len(result["ideas"]) == 1
        finally:
            conn.close()


def test_list_workstreams_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        (data_dir / "workstreams.json").write_text('{"version": 1, "workstreams": {}}')
        from relay_server.workstreams import list_workstreams
        # Markdown: should still have commands line
        result = list_workstreams(data_dir=data_dir)
        assert isinstance(result, str)
        assert "**Commands:**" in result
        assert "## Active" not in result

        # JSON: empty arrays
        result_json = list_workstreams(data_dir=data_dir, format="json")
        assert result_json["active"] == []
        assert result_json["parked"] == []
        assert result_json["completed"] == []
        assert result_json["ideas"] == []


def test_get_status_attached():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        # Write a richer state file for alpha
        (data_dir / "workstreams" / "alpha" / "state.md").write_text(
            "# alpha\n\n## Current Status\nWorking on feature X.\n\n## Next Steps\n1. Finish X\n2. Start Y\n"
        )
        try:
            from relay_server.workstreams import get_status
            result = get_status(data_dir=data_dir, attached="alpha")
            assert isinstance(result, str)
            assert "## Attached: alpha" in result
            assert "**Description:** Alpha" in result
            assert "**Project:** /alpha" in result
            assert "### Current Status" in result
            assert "Working on feature X" in result
            assert "### Next Steps" in result
            assert "Finish X" in result
            # beta should be in "other" parked
            assert "beta" in result
            assert "**Parked:**" in result
            assert "**Commands:**" in result
        finally:
            conn.close()


def test_get_status_no_attached():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import get_status
            result = get_status(data_dir=data_dir)
            assert "No workstream attached" in result
            assert "alpha" in result  # should appear in other active
            assert "**Commands:**" in result
        finally:
            conn.close()


def test_get_status_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        (data_dir / "workstreams" / "alpha" / "state.md").write_text(
            "# alpha\n\n## Current Status\nWorking on X.\n\n## Next Steps\n1. Finish X\n"
        )
        try:
            from relay_server.workstreams import get_status
            result = get_status(data_dir=data_dir, attached="alpha", format="json")
            assert isinstance(result, dict)
            assert result["attached"]["name"] == "alpha"
            assert result["attached"]["description"] == "Alpha"
            assert "Working on X" in result["attached"]["current_status"]
            assert "Finish X" in result["attached"]["next_steps"]
            assert "beta" in result["others"]["parked"]
            assert "alpha" not in result["others"]["active"]
        finally:
            conn.close()


def test_get_status_excludes_attached_from_other():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, data_dir, conn = _setup(tmpdir)
        try:
            from relay_server.workstreams import get_status
            result = get_status(data_dir=data_dir, attached="alpha")
            # alpha should NOT appear in the "Other active" line
            other_active_line = [l for l in result.split("\n") if "**Other active:**" in l][0]
            assert "alpha" not in other_active_line
        finally:
            conn.close()


def test_switch_branch_mismatch_warning():
    """switch_workstream warns when current branch doesn't match target's git.branch."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup_git(tmpdir)
        from relay_server.workstreams import switch_workstream, read_registry
        registry = read_registry(data_dir)
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)
        registry["workstreams"]["target"]["project_dir"] = str(repo)
        registry["workstreams"]["target"]["git"] = {"strategy": "branch", "branch": "feat/test"}
        atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")
        try:
            result = switch_workstream(data_dir=data_dir, conn=conn, to_name="target")
            assert "git_warning" in result
            assert "feat/test" in result["git_warning"]
            assert "git_suggestion" in result
        finally:
            conn.close()


def test_switch_worktree_returns_path():
    """switch_workstream includes worktree_path for worktree-strategy targets."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup_git(tmpdir)
        from relay_server.workstreams import switch_workstream, read_registry
        wt_path = Path(tmpdir) / "worktree"
        wt_path.mkdir()
        registry = read_registry(data_dir)
        registry["workstreams"]["target"]["git"] = {
            "strategy": "worktree", "branch": "feat/wt",
            "worktree_path": str(wt_path),
        }
        atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")
        try:
            result = switch_workstream(data_dir=data_dir, conn=conn, to_name="target")
            assert result.get("worktree_path") == str(wt_path)
        finally:
            conn.close()


def test_switch_stash_reminder():
    """switch_workstream includes stash_reminder when target has stash_ref."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup_git(tmpdir)
        from relay_server.workstreams import switch_workstream, read_registry
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

        registry = read_registry(data_dir)
        registry["workstreams"]["target"]["project_dir"] = str(repo)
        registry["workstreams"]["target"]["git"] = {
            "strategy": "branch", "branch": "main",
            "stash_ref": sha, "stash_message": "relay: target at 2026-03-22",
        }
        atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")
        try:
            result = switch_workstream(data_dir=data_dir, conn=conn, to_name="target")
            assert "stash_reminder" in result
            assert sha[:7] in result["stash_reminder"]
        finally:
            conn.close()


def test_switch_stores_stash_ref_on_from():
    """switch_workstream stores stash_ref on from workstream when passed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup_git(tmpdir)
        from relay_server.workstreams import switch_workstream, read_registry
        registry = read_registry(data_dir)
        registry["workstreams"]["source"]["git"] = {"strategy": "branch", "branch": "main"}
        atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")
        try:
            switch_workstream(
                data_dir=data_dir, conn=conn, to_name="target",
                from_name="source", state_content="# State",
                stash_ref="abc123def456",
            )
            reg = read_registry(data_dir)
            assert reg["workstreams"]["source"]["git"]["stash_ref"] == "abc123def456"
        finally:
            conn.close()


def test_switch_stale_stash_cleared():
    """switch_workstream clears stash_ref if SHA no longer exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path, data_dir, conn = _setup_git(tmpdir)
        from relay_server.workstreams import switch_workstream, read_registry
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True)

        registry = read_registry(data_dir)
        registry["workstreams"]["target"]["project_dir"] = str(repo)
        registry["workstreams"]["target"]["git"] = {
            "strategy": "branch", "branch": "main",
            "stash_ref": "deadbeef" * 5,
            "stash_message": "stale stash",
        }
        atomic_write(data_dir / "workstreams.json", json.dumps(registry, indent=2) + "\n")
        try:
            result = switch_workstream(data_dir=data_dir, conn=conn, to_name="target")
            assert "stash_reminder" not in result
            reg = read_registry(data_dir)
            assert "stash_ref" not in reg["workstreams"]["target"]["git"]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Display tests: list_workstreams git info
# ---------------------------------------------------------------------------

def test_list_workstreams_shows_branch():
    """list_workstreams appends (branch-name) for branch-strategy workstreams."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "payments": {
                    "status": "active",
                    "description": "Payments feature",
                    "created": "2026-01-01",
                    "last_touched": "2026-01-01",
                    "project_dir": "/myapp",
                    "git": {"strategy": "branch", "branch": "feat/test"},
                }
            }
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))
        from relay_server.workstreams import list_workstreams
        result = list_workstreams(data_dir=data_dir)
        assert isinstance(result, str)
        assert "(feat/test)" in result


def test_list_workstreams_shows_worktree():
    """list_workstreams appends (worktree: path) for worktree-strategy workstreams."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "infra": {
                    "status": "active",
                    "description": "Infra work",
                    "created": "2026-01-01",
                    "last_touched": "2026-01-01",
                    "project_dir": "/myapp",
                    "git": {
                        "strategy": "worktree",
                        "branch": "feat/infra",
                        "worktree_path": "/some/path",
                    },
                }
            }
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))
        from relay_server.workstreams import list_workstreams
        result = list_workstreams(data_dir=data_dir)
        assert isinstance(result, str)
        assert "(worktree:" in result
        assert "/some/path" in result


def test_list_no_git_unchanged():
    """list_workstreams does not append git info when no git config is present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "plain": {
                    "status": "active",
                    "description": "Plain workstream",
                    "created": "2026-01-01",
                    "last_touched": "2026-01-01",
                    "project_dir": "",
                }
            }
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))
        from relay_server.workstreams import list_workstreams
        result = list_workstreams(data_dir=data_dir)
        assert isinstance(result, str)
        # No parenthetical git info should appear in description column
        assert "(feat/" not in result
        assert "(worktree:" not in result


# ---------------------------------------------------------------------------
# Display tests: get_status git section
# ---------------------------------------------------------------------------

def test_get_status_git_section():
    """get_status includes a ## Git section when attached workstream has git config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "myws": {
                    "status": "active",
                    "description": "My workstream",
                    "created": "2026-01-01",
                    "last_touched": "2026-01-01",
                    "project_dir": "",
                    "git": {"strategy": "branch", "branch": "feat/payments-api"},
                }
            }
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))
        ws_dir = data_dir / "workstreams" / "myws"
        ws_dir.mkdir(parents=True)
        (ws_dir / "state.md").write_text("# myws\n\n## Current Status\nIn progress.\n")
        from relay_server.workstreams import get_status
        result = get_status(data_dir=data_dir, attached="myws")
        assert isinstance(result, str)
        assert "## Git" in result
        assert "branch" in result
        assert "feat/payments-api" in result


def test_get_status_shows_stash():
    """get_status shows stashed changes info when stash_ref is set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "data"
        data_dir.mkdir()
        registry = {
            "version": 1,
            "workstreams": {
                "myws": {
                    "status": "active",
                    "description": "My workstream",
                    "created": "2026-01-01",
                    "last_touched": "2026-01-01",
                    "project_dir": "",
                    "git": {
                        "strategy": "branch",
                        "branch": "feat/payments-api",
                        "stash_ref": "a1b2c3d4e5f6",
                        "stash_message": "relay: myws at 2026-03-22T10:00:00Z",
                    },
                }
            }
        }
        (data_dir / "workstreams.json").write_text(json.dumps(registry))
        ws_dir = data_dir / "workstreams" / "myws"
        ws_dir.mkdir(parents=True)
        (ws_dir / "state.md").write_text("# myws\n\n## Current Status\nIn progress.\n")
        from relay_server.workstreams import get_status
        result = get_status(data_dir=data_dir, attached="myws")
        assert isinstance(result, str)
        assert "Stashed changes" in result
        assert "a1b2c3d" in result
