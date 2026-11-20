"""Opt-in sync plumbing (rollout step 2).

The key guarantee: with sync unconfigured, relay makes no network calls,
launches nothing extra from hooks, and has no new dependencies.
"""

import json
import os
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from relay_server import sync
from relay_server.events import ensure_event_log, record_changes
from relay_server.workstreams import create_workstream, get_data_dir, manage_idea, save_workstream

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts"


@pytest.fixture
def data_dir():
    d = get_data_dir()  # XDG-isolated by conftest
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def shared(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    return d


def _config(data_dir: Path) -> dict:
    return json.loads((data_dir / "relay.json").read_text())


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_off_when_no_config(data_dir):
    assert sync.load_sync_config(data_dir) is None
    assert sync.sync_status(data_dir)["state"] == "off"


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"sync": "yes"}),
        json.dumps({"sync": {"enabled": "true", "backend": "folder", "folder": "/x"}}),
        json.dumps({"sync": {"enabled": True, "backend": "carrier-pigeon"}}),
        json.dumps({"sync": {"enabled": True, "backend": "folder"}}),
        json.dumps({"sync": {"enabled": True, "backend": "folder", "folder": "/x", "scope": "all"}}),
        json.dumps({"sync": {"enabled": False, "backend": "folder", "folder": "/x"}}),
    ],
)
def test_anything_but_a_valid_enabled_block_is_off(data_dir, content):
    (data_dir / "relay.json").write_text(content)
    assert sync.load_sync_config(data_dir) is None
    assert sync.run_sync("push", data_dir) == {"ran": False, "reason": "off"}


def test_status_reports_invalid_block(data_dir):
    (data_dir / "relay.json").write_text(json.dumps({"sync": {"enabled": True, "backend": "ftp"}}))
    status = sync.sync_status(data_dir)
    assert status["state"] == "invalid"
    assert "invalid" in sync.format_status(status)


def test_status_does_not_create_host_id(data_dir):
    sync.sync_status(data_dir)
    assert not (data_dir / "relay.json").exists()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def test_setup_folder(data_dir, shared):
    (data_dir / "relay.json").write_text(json.dumps({"summary_dir": "~/notes", "terminal_color": True}))
    result = sync.setup_sync(data_dir, backend="folder", folder=str(shared), nickname="laptop")
    cfg = _config(data_dir)
    assert cfg["summary_dir"] == "~/notes" and cfg["terminal_color"] is True  # other keys kept
    assert cfg["nickname"] == "laptop"
    assert cfg["host_id"]  # identity created
    assert cfg["sync"]["enabled"] is True
    assert cfg["sync"]["backend"] == "folder"
    assert cfg["sync"]["folder"] == str(shared.resolve())
    assert cfg["sync"]["scope"] == "state"
    assert result["transport_available"] is True
    assert result["join"]["founded"] is True and result["join"]["role"] == "member"
    assert sync.load_sync_config(data_dir)["folder"] == str(shared.resolve())
    assert "started a new sync group" in sync.format_setup_result(result)
    assert (data_dir / "sync.key").stat().st_mode & 0o777 == 0o600


def test_setup_expands_tilde(data_dir, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "Sync").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    sync.setup_sync(data_dir, backend="folder", folder="~/Sync")
    assert _config(data_dir)["sync"]["folder"] == str((home / "Sync").resolve())


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"backend": "ftp"}, "backend must be"),
        ({"backend": "folder"}, "needs a folder"),
        ({"backend": "folder", "folder": "relative/path"}, "absolute"),
        ({"backend": "folder", "folder": "/definitely/not/here"}, "doesn't exist"),  # parent missing too
        ({"backend": "http"}, "needs a mailbox url"),
        ({"backend": "http", "url": "http://nas.example.com"}, "https"),
        ({"backend": "http", "url": "https://u:p@nas.example.com"}, "credentials"),
        ({"backend": "http", "url": "nas.example.com"}, "https://host"),
        ({"backend": "http", "url": "https://nas", "scope": "everything"}, "scope must be"),
        ({"backend": "http", "url": "https://nas", "nickname": "x" * 40}, "at most"),
        ({"backend": "http", "url": "https://nas", "nickname": "bad\x1bname"}, "control"),
    ],
)
def test_setup_rejects_bad_settings(data_dir, kwargs, message):
    with pytest.raises(sync.SyncConfigError, match=message):
        sync.setup_sync(data_dir, **kwargs)
    assert not (data_dir / "relay.json").exists()  # nothing written


def test_setup_rejects_folder_inside_data_dir(data_dir):
    inner = data_dir / "sync-out"
    inner.mkdir()
    with pytest.raises(sync.SyncConfigError, match="overlaps"):
        sync.setup_sync(data_dir, backend="folder", folder=str(inner))
    with pytest.raises(sync.SyncConfigError, match="overlaps"):
        sync.setup_sync(data_dir, backend="folder", folder=str(data_dir.parent))


def test_setup_refuses_to_overwrite_corrupt_config(data_dir, shared):
    (data_dir / "relay.json").write_text("{oops")
    with pytest.raises(Exception):
        sync.setup_sync(data_dir, backend="folder", folder=str(shared))
    assert (data_dir / "relay.json").read_text() == "{oops"


def test_setup_http_localhost_allowed(data_dir):
    sync.setup_sync(data_dir, backend="http", url="http://localhost:8443/")
    assert _config(data_dir)["sync"]["url"] == "http://localhost:8443"


def test_rerun_setup_switches_backend_and_drops_stale_target(data_dir, shared):
    sync.setup_sync(data_dir, backend="folder", folder=str(shared))
    sync.setup_sync(data_dir, backend="http", url="https://nas.tailnet.ts.net:8443", scope="state+messages")
    block = _config(data_dir)["sync"]
    assert block["backend"] == "http" and "folder" not in block
    assert block["scope"] == "state+messages"


def test_nickname_none_keeps_empty_clears(data_dir, shared):
    sync.setup_sync(data_dir, backend="folder", folder=str(shared), nickname="desk")
    sync.setup_sync(data_dir, backend="folder", folder=str(shared))
    assert _config(data_dir)["nickname"] == "desk"
    sync.setup_sync(data_dir, backend="folder", folder=str(shared), nickname="")
    assert "nickname" not in _config(data_dir)


def test_nickname_change_is_recorded_as_host_event(data_dir, shared):
    ensure_event_log(data_dir, None)
    sync.setup_sync(data_dir, backend="folder", folder=str(shared), nickname="laptop")
    from relay_server.events import EventStore

    assert record_changes(data_dir, None, source="manage_sync")  # at least one event
    host_id = _config(data_dir)["host_id"]
    store = EventStore(data_dir)
    try:
        assert store.head_value(f"host/{host_id}")["nickname"] == "laptop"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Disable / status
# ---------------------------------------------------------------------------


def test_disable_when_not_set_up(data_dir):
    result = sync.disable_sync(data_dir)
    assert result["changed"] is False and "isn't set up" in result["message"]


def test_disable_keeps_settings_and_forget_removes_them(data_dir, shared):
    sync.setup_sync(data_dir, backend="folder", folder=str(shared), nickname="lap")
    assert sync.disable_sync(data_dir)["changed"] is True
    cfg = _config(data_dir)
    assert cfg["sync"]["enabled"] is False and cfg["sync"]["folder"]
    assert sync.load_sync_config(data_dir) is None
    assert sync.sync_status(data_dir)["state"] == "disabled"
    assert sync.disable_sync(data_dir)["changed"] is False  # already disabled

    assert sync.disable_sync(data_dir, forget=True)["changed"] is True
    cfg = _config(data_dir)
    assert "sync" not in cfg
    assert cfg["nickname"] == "lap" and cfg["host_id"]  # identity is not sync-specific


def test_status_on(data_dir, shared):
    ensure_event_log(data_dir, None)
    sync.setup_sync(data_dir, backend="folder", folder=str(shared), nickname="laptop")
    status = sync.sync_status(data_dir)
    assert status["state"] == "on"
    assert status["host"]["display_name"] == "laptop"
    total, others = status["events"]
    assert total >= 1 and others == 0
    text = sync.format_status(status)
    assert "**Status:** on" in text
    assert "not available in this version" not in text
    assert "approved member" in text and "Key fingerprint" in text
    assert str(shared.resolve()) in text


def test_status_off_text(data_dir):
    text = sync.format_status(sync.sync_status(data_dir))
    assert "off (local-only)" in text
    assert "/relay:sync setup" in text
    assert "not created yet" in text


def test_cli(data_dir, capsys):
    assert sync.main(["status"]) == 0
    assert "relay sync" in capsys.readouterr().out
    assert sync.main(["push"]) == 0
    assert sync.main(["pull"]) == 0
    assert capsys.readouterr().out == ""  # silent when off
    assert sync.main(["bogus"]) == 2


def test_run_sync_http_not_available_yet(data_dir):
    sync.setup_sync(data_dir, backend="http", url="https://nas.example.ts.net:8443")
    assert sync.run_sync("pull", data_dir) == {
        "ran": False, "reason": "transport-unavailable", "backend": "http",
    }
    text = sync.format_status(sync.sync_status(data_dir))
    assert "not available in this version" in text
    with pytest.raises(ValueError):
        sync.run_sync("sideways", data_dir)


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


def test_manage_sync_tool(data_dir, shared):
    from relay_server.server import manage_sync

    assert "off (local-only)" in manage_sync(action="status", ctx=None)
    assert manage_sync(action="setup", ctx=None) == {"error": "setup needs backend='folder' or backend='http'"}
    assert "error" in manage_sync(action="setup", ctx=None, backend="folder", folder="/nope/nope")
    out = manage_sync(action="setup", ctx=None, backend="folder", folder=str(shared), nickname="lap")
    assert "Sync settings saved" in out
    assert manage_sync(action="status", ctx=None, format="json")["state"] == "on"
    assert "Sync disabled" in manage_sync(action="disable", ctx=None)
    assert manage_sync(action="disable", ctx=None, forget=True, format="json")["changed"] is True
    assert "error" in manage_sync(action="explode", ctx=None)


# ---------------------------------------------------------------------------
# No network, no new deps, when sync is unconfigured
# ---------------------------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch):
    calls = []

    def blocked(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"network call attempted: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    return calls


def test_local_only_flow_makes_no_network_calls(data_dir, no_network, tmp_path):
    db_path = tmp_path / "index.db"
    from relay_server.db import ensure_schema

    ensure_schema(db_path)
    ensure_event_log(data_dir, db_path)
    from relay_server.db import get_connection

    create_workstream(data_dir=data_dir, name="demo", description="d")
    conn = get_connection(db_path)
    try:
        save_workstream(data_dir=data_dir, conn=conn, name="demo", state_content="# demo\n")
    finally:
        conn.close()
    manage_idea(data_dir=data_dir, action="add", text="an idea")
    record_changes(data_dir, db_path, source="test")
    sync.run_sync("push", data_dir)
    sync.run_sync("pull", data_dir)
    sync.format_status(sync.sync_status(data_dir))
    assert no_network == []


def test_sync_module_imports_no_network_clients():
    code = (
        "import sys, relay_server.sync, relay_server.events, relay_server.workstreams;"
        "bad=[m for m in ('http.client','urllib.request','httpx','requests','ssl') if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_no_new_runtime_dependencies():
    """Sync deps (encryption, http client) must go in an optional extra."""
    pyproject = tomllib.loads((REPO / "server" / "pyproject.toml").read_text())
    deps = pyproject["project"]["dependencies"]
    assert [d.split(">")[0].split("[")[0] for d in deps] == ["mcp"]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_uv(tmp_path):
    """A `uv` on PATH that records its arguments instead of running anything."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv-calls.log"
    uv = bin_dir / "uv"
    uv.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n')
    uv.chmod(0o755)
    return bin_dir, log


def _run_hook(script: str, fake_uv, data_dir: Path, stdin: dict) -> subprocess.CompletedProcess:
    bin_dir, _ = fake_uv
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["XDG_CONFIG_HOME"] = str(data_dir.parent)
    result = subprocess.run(
        ["bash", str(SCRIPTS / script)], input=json.dumps(stdin),
        capture_output=True, text=True, env=env, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result


HOOKS = [("session-start.sh", "pull"), ("session-end.sh", "push"), ("pre-compact-save.sh", "push")]
STDIN = {"session_id": "abc12345-1234-1234-1234-123456789abc"}


@pytest.mark.parametrize("script, _direction", HOOKS)
@pytest.mark.parametrize(
    "relay_json",
    [None, {}, {"sync": {"enabled": False, "backend": "folder", "folder": "/x"}}, {"sync": "on"}],
)
def test_hooks_do_not_launch_sync_when_off(data_dir, fake_uv, script, _direction, relay_json):
    if relay_json is not None:
        (data_dir / "relay.json").write_text(json.dumps(relay_json))
    _run_hook(script, fake_uv, data_dir, STDIN)
    time.sleep(0.2)
    assert not fake_uv[1].exists(), "hook launched uv although sync is off"
    assert not (data_dir / "sync.log").exists()


@pytest.mark.parametrize("script, direction", HOOKS)
def test_hooks_launch_sync_in_background_when_on(data_dir, fake_uv, shared, script, direction):
    sync.setup_sync(data_dir, backend="folder", folder=str(shared))
    _run_hook(script, fake_uv, data_dir, STDIN)
    log = fake_uv[1]
    for _ in range(50):
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.05)
    calls = log.read_text().strip().splitlines()
    assert len(calls) == 1
    assert calls[0].endswith(f"python -m relay_server.sync {direction}")
    assert f"--directory {REPO}/server" in calls[0]


def test_session_start_output_unchanged_by_sync(data_dir, fake_uv, shared):
    off = _run_hook("session-start.sh", fake_uv, data_dir, STDIN).stdout
    sync.setup_sync(data_dir, backend="folder", folder=str(shared))
    on = _run_hook("session-start.sh", fake_uv, data_dir, STDIN).stdout
    assert off == on


def test_plugin_runs_without_dev_dependencies():
    """Dev-only packages (pytest, PyNaCl for tests) must not land in users' plugin env."""
    mcp = json.loads((REPO / ".mcp.json").read_text())
    assert "--no-dev" in mcp["mcpServers"]["relay-search"]["args"]
    common = (SCRIPTS / "common.sh").read_text()
    assert common.count("uv run --quiet --no-dev --extra sync") == 2
