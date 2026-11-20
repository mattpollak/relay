"""Test hook script output given mock stdin JSON."""
import json
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"


def _run_hook_script(script_name: str, stdin_json: dict, env_overrides: dict | None = None) -> str:
    """Run a hook script with mock stdin and return stdout."""
    import os
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(SCRIPTS_DIR.parent)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        ["bash", str(SCRIPTS_DIR / script_name)],
        input=json.dumps(stdin_json),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    return result.stdout


def test_post_compact_with_active_workstream(tmp_path):
    """PostCompact should output additionalContext with workstream name."""
    data_dir = tmp_path / "relay"
    markers_dir = data_dir / "session-markers"
    markers_dir.mkdir(parents=True)
    session_id = "abc12345-1234-1234-1234-123456789abc"
    marker = {"workstream": "my-project", "timestamp": "2026-03-28T00:00:00Z"}
    (markers_dir / f"{session_id}.json").write_text(json.dumps(marker))

    registry = {
        "version": 1,
        "workstreams": {
            "my-project": {
                "status": "active",
                "description": "Test project",
                "created": "2026-01-01",
                "last_touched": "2026-03-28",
                "project_dir": "/home/user/project",
            }
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))

    output = _run_hook_script(
        "post-compact.sh",
        {"session_id": session_id},
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    parsed = json.loads(output)
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert "my-project" in ctx
    assert "get_status" in ctx


def test_post_compact_no_session_marker(tmp_path):
    """PostCompact with no marker should produce no output."""
    data_dir = tmp_path / "relay"
    data_dir.mkdir(parents=True)
    output = _run_hook_script(
        "post-compact.sh",
        {"session_id": "no-such-session"},
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    assert output.strip() == ""


def test_cwd_changed_matches_different_workstream(tmp_path):
    """CwdChanged should suggest switching when cwd matches another workstream."""
    data_dir = tmp_path / "relay"
    markers_dir = data_dir / "session-markers"
    markers_dir.mkdir(parents=True)
    session_id = "abc12345-1234-1234-1234-123456789abc"
    marker = {"workstream": "relay", "timestamp": "2026-03-28T00:00:00Z"}
    (markers_dir / f"{session_id}.json").write_text(json.dumps(marker))

    registry = {
        "version": 1,
        "workstreams": {
            "relay": {
                "status": "active",
                "description": "Plugin",
                "created": "2026-01-01",
                "last_touched": "2026-03-28",
                "project_dir": "/home/user/relay",
            },
            "squadkeeper": {
                "status": "active",
                "description": "PWA",
                "created": "2026-01-01",
                "last_touched": "2026-03-28",
                "project_dir": "/home/user/squadkeeper",
            },
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))

    output = _run_hook_script(
        "cwd-changed.sh",
        {"cwd": "/home/user/relay", "new_cwd": "/home/user/squadkeeper/src", "session_id": session_id},
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    parsed = json.loads(output)
    msg = parsed["systemMessage"]
    assert "squadkeeper" in msg
    assert "/relay:switch" in msg


def test_cwd_changed_same_workstream_no_output(tmp_path):
    """CwdChanged should produce no output when cwd matches current workstream."""
    data_dir = tmp_path / "relay"
    markers_dir = data_dir / "session-markers"
    markers_dir.mkdir(parents=True)
    session_id = "abc12345-1234-1234-1234-123456789abc"
    marker = {"workstream": "relay", "timestamp": "2026-03-28T00:00:00Z"}
    (markers_dir / f"{session_id}.json").write_text(json.dumps(marker))

    registry = {
        "version": 1,
        "workstreams": {
            "relay": {
                "status": "active",
                "description": "Plugin",
                "created": "2026-01-01",
                "last_touched": "2026-03-28",
                "project_dir": "/home/user/relay",
            },
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))

    output = _run_hook_script(
        "cwd-changed.sh",
        {"cwd": "/home/user/relay", "new_cwd": "/home/user/relay/server", "session_id": session_id},
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    assert output.strip() == ""


def test_cwd_changed_no_match_no_output(tmp_path):
    """CwdChanged should produce no output when cwd doesn't match any workstream."""
    data_dir = tmp_path / "relay"
    data_dir.mkdir(parents=True)
    registry = {
        "version": 1,
        "workstreams": {
            "relay": {
                "status": "active",
                "description": "Plugin",
                "created": "2026-01-01",
                "last_touched": "2026-03-28",
                "project_dir": "/home/user/relay",
            },
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))

    output = _run_hook_script(
        "cwd-changed.sh",
        {"cwd": "/home/user/relay", "new_cwd": "/home/user/unrelated", "session_id": "some-session"},
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    assert output.strip() == ""


def test_cwd_changed_deepest_match_wins(tmp_path):
    """CwdChanged should match the most specific (deepest) project_dir."""
    data_dir = tmp_path / "relay"
    markers_dir = data_dir / "session-markers"
    markers_dir.mkdir(parents=True)
    session_id = "abc12345-1234-1234-1234-123456789abc"
    marker = {"workstream": "unrelated", "timestamp": "2026-03-28T00:00:00Z"}
    (markers_dir / f"{session_id}.json").write_text(json.dumps(marker))

    registry = {
        "version": 1,
        "workstreams": {
            "parent": {
                "status": "active",
                "description": "Parent",
                "created": "2026-01-01",
                "last_touched": "2026-03-28",
                "project_dir": "/home/user/projects",
            },
            "child": {
                "status": "active",
                "description": "Child",
                "created": "2026-01-01",
                "last_touched": "2026-03-28",
                "project_dir": "/home/user/projects/child",
            },
        }
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry))

    output = _run_hook_script(
        "cwd-changed.sh",
        {"cwd": "/home/user/other", "new_cwd": "/home/user/projects/child/src", "session_id": session_id},
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    parsed = json.loads(output)
    msg = parsed["systemMessage"]
    assert '"child"' in msg


def _two_active(tmp_path):
    data_dir = tmp_path / "relay"
    (data_dir / "workstreams" / "alpha").mkdir(parents=True)
    (data_dir / "workstreams" / "beta").mkdir(parents=True)
    (data_dir / "workstreams" / "beta" / "state.md").write_text("# beta state\n")
    (data_dir / "workstreams.json").write_text(json.dumps({"version": 1, "workstreams": {
        "alpha": {"status": "active", "description": "A"}, "beta": {"status": "active", "description": "B"},
    }}))
    return data_dir


def test_session_start_resumed_session_reattaches(tmp_path):
    """/exit deletes the statusline mapping; on resume the marker re-attaches."""
    data_dir = _two_active(tmp_path)
    sid = "abc12345-1234-1234-1234-123456789abc"
    (data_dir / "session-markers").mkdir()
    (data_dir / "session-markers" / f"{sid}.json").write_text(json.dumps({"workstream": "beta"}))
    out = _run_hook_script("session-start.sh", {"session_id": sid, "source": "resume"},
                           {"XDG_CONFIG_HOME": str(tmp_path)})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Active workstream 'beta'" in ctx and "# beta state" in ctx
    assert (data_dir / "session-workstreams" / sid).read_text() == "beta"


def test_session_start_new_session_with_several_active_still_asks(tmp_path):
    data_dir = _two_active(tmp_path)
    out = _run_hook_script("session-start.sh", {"session_id": "def12345-1234-1234-1234-123456789abc"},
                           {"XDG_CONFIG_HOME": str(tmp_path)})
    assert "Multiple active workstreams" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert not (data_dir / "session-workstreams").exists()


def test_session_start_ignores_marker_for_unknown_workstream(tmp_path):
    data_dir = _two_active(tmp_path)
    sid = "abc12345-1234-1234-1234-123456789abc"
    (data_dir / "session-markers").mkdir()
    (data_dir / "session-markers" / f"{sid}.json").write_text(json.dumps({"workstream": "gone"}))
    out = _run_hook_script("session-start.sh", {"session_id": sid}, {"XDG_CONFIG_HOME": str(tmp_path)})
    assert "Multiple active workstreams" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
