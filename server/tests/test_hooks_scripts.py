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
        {"cwd": "/home/user/squadkeeper/src", "session_id": session_id},
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    parsed = json.loads(output)
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert "squadkeeper" in ctx
    assert "/relay:switch" in ctx


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
        {"cwd": "/home/user/relay/server", "session_id": session_id},
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
        {"cwd": "/home/user/unrelated", "session_id": "some-session"},
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
        {"cwd": "/home/user/projects/child/src", "session_id": session_id},
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path)},
    )
    parsed = json.loads(output)
    ctx = parsed["hookSpecificOutput"]["additionalContext"]
    assert '"child"' in ctx
