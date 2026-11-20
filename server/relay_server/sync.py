"""Multi-machine sync: opt-in configuration and entry points.

Sync is OFF by default. Local-only is a fully supported mode. Nothing in this
module touches the network: it only reads and writes the ``sync`` block in
relay.json. Transport backends (folder, http) are imported lazily, and only
when sync is enabled, so local-only installs never load them.

Config (in ~/.config/relay/relay.json; absence of ``sync`` means off)::

    "sync": {
      "enabled": true,
      "backend": "folder" | "http",
      "folder": "/path/to/shared/dir",          # backend=folder
      "url": "https://nas.tailnet.ts.net:8443",  # backend=http
      "scope": "state" | "state+messages",
      "configured_at": "2026-11-20T12:00:00Z"
    }

CLI (used by the hooks; silent no-op when sync is off)::

    python -m relay_server.sync status|push|pull
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .workstreams import atomic_write, get_data_dir

BACKENDS = ("folder", "http")
SCOPES = ("state", "state+messages")
DEFAULT_SCOPE = "state"

# Backends that can actually move events in this version. Config for the
# others is accepted and validated, but nothing is sent.
IMPLEMENTED_BACKENDS: tuple[str, ...] = ("folder",)

_NOT_SET_UP = "Sync isn't set up; relay is running local-only. Run `/relay:sync setup` to share with other machines."
_MAX_NICKNAME = 32


class SyncConfigError(ValueError):
    """Invalid sync settings passed to setup."""


# ---------------------------------------------------------------------------
# relay.json access
# ---------------------------------------------------------------------------


def _config_path(data_dir: Path) -> Path:
    return data_dir / "relay.json"


def _read_config(data_dir: Path) -> dict:
    """Read relay.json for modification. Raises if it exists but is corrupt,
    so we never overwrite a file we couldn't parse."""
    path = _config_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise SyncConfigError(f"{path} is not valid JSON ({exc}); fix or remove it first") from exc
    if not isinstance(data, dict):
        raise SyncConfigError(f"{path} must contain a JSON object")
    return data


def _write_config(data_dir: Path, config: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(_config_path(data_dir), json.dumps(config, indent=2) + "\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_folder(folder: str | None, data_dir: Path) -> str:
    if not folder or not folder.strip():
        raise SyncConfigError("backend 'folder' needs a folder path")
    path = Path(os.path.expanduser(folder.strip()))
    if not path.is_absolute():
        raise SyncConfigError(f"folder must be an absolute path, got {folder!r}")
    path = path.resolve()
    if not path.is_dir():
        if path.parent.is_dir() and not path.exists():
            path.mkdir()
        else:
            raise SyncConfigError(f"folder {path} doesn't exist (create it, or mount the share, first)")
    if not os.access(path, os.W_OK):
        raise SyncConfigError(f"folder {path} isn't writable")
    data_resolved = data_dir.resolve()
    if path == data_resolved or data_resolved in path.parents or path in data_resolved.parents:
        raise SyncConfigError(
            f"folder {path} overlaps relay's data dir {data_resolved}; use a separate shared folder"
        )
    return str(path)


def _validate_url(url: str | None) -> str:
    if not url or not url.strip():
        raise SyncConfigError("backend 'http' needs a mailbox url")
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise SyncConfigError(f"url must look like https://host:port, got {url!r}")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise SyncConfigError("url must use https (plain http is only allowed for localhost testing)")
    if parsed.username or parsed.password:
        raise SyncConfigError("don't put credentials in the url; the mailbox token is stored separately")
    return url


def _validate_nickname(nickname: str) -> str:
    nickname = nickname.strip()
    if len(nickname) > _MAX_NICKNAME:
        raise SyncConfigError(f"nickname must be at most {_MAX_NICKNAME} characters")
    if any(not ch.isprintable() for ch in nickname):
        raise SyncConfigError("nickname can't contain control characters")
    return nickname


def check_sync_block(block: Any) -> list[str]:
    """Return problems with a ``sync`` block as found on disk (empty = valid).

    Used for status and for deciding whether sync is effectively on: an
    invalid block is treated as off.
    """
    if not isinstance(block, dict):
        return ["`sync` must be a JSON object"]
    problems = []
    if not isinstance(block.get("enabled"), bool):
        problems.append("`sync.enabled` must be true or false")
    backend = block.get("backend")
    if backend not in BACKENDS:
        problems.append(f"`sync.backend` must be one of {', '.join(BACKENDS)}")
    elif backend == "folder" and not isinstance(block.get("folder"), str):
        problems.append("`sync.folder` is missing")
    elif backend == "http" and not isinstance(block.get("url"), str):
        problems.append("`sync.url` is missing")
    if block.get("scope", DEFAULT_SCOPE) not in SCOPES:
        problems.append(f"`sync.scope` must be one of {', '.join(SCOPES)}")
    return problems


# ---------------------------------------------------------------------------
# Reading config
# ---------------------------------------------------------------------------


def load_sync_config(data_dir: Path | None = None) -> dict | None:
    """Return the sync block if sync is enabled and valid, else None.

    Never raises: any problem (missing file, corrupt JSON, invalid block)
    means sync is off.
    """
    data_dir = data_dir or get_data_dir()
    try:
        config = json.loads(_config_path(data_dir).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    block = config.get("sync")
    if block is None or check_sync_block(block) or block.get("enabled") is not True:
        return None
    return {**block, "scope": block.get("scope", DEFAULT_SCOPE)}


def is_sync_enabled(data_dir: Path | None = None) -> bool:
    return load_sync_config(data_dir) is not None


# ---------------------------------------------------------------------------
# Setup / disable
# ---------------------------------------------------------------------------


def setup_sync(
    data_dir: Path,
    *,
    backend: str,
    folder: str | None = None,
    url: str | None = None,
    scope: str = DEFAULT_SCOPE,
    nickname: str | None = None,
    trust_fingerprint: str | None = None,
) -> dict:
    """Enable sync with validated settings. Doesn't contact anything.

    Joining a folder that already has a sync group raises
    ``sync_engine.NeedsConfirmation`` (a SyncConfigError) unless
    ``trust_fingerprint`` matches the founder's fingerprint.

    nickname: None leaves it unchanged; "" clears it (display name falls
    back to the hostname).
    """
    if backend not in BACKENDS:
        raise SyncConfigError(f"backend must be one of {', '.join(BACKENDS)}")
    if scope not in SCOPES:
        raise SyncConfigError(f"scope must be one of {', '.join(SCOPES)}")

    block: dict[str, Any] = {"enabled": True, "backend": backend, "scope": scope}
    if backend == "folder":
        block["folder"] = _validate_folder(folder, data_dir)
    else:
        block["url"] = _validate_url(url)
    clean_nickname = _validate_nickname(nickname) if nickname is not None else None

    # Host identity must exist before anything can be synced.
    from .events import ensure_host_identity

    host = ensure_host_identity(data_dir)
    config = _read_config(data_dir)
    if clean_nickname is not None:
        display_name = clean_nickname or host["hostname"]
    else:
        display_name = host["display_name"]

    join = None
    if backend in IMPLEMENTED_BACKENDS:
        from .sync_engine import NeedsConfirmation, SyncError, join_folder

        try:
            join = join_folder(data_dir, block["folder"], display_name, trust_fingerprint)
        except NeedsConfirmation:
            raise
        except SyncError as exc:
            raise SyncConfigError(str(exc)) from exc
        except Exception as exc:
            from .sync_folder import FolderError

            if isinstance(exc, FolderError):
                raise SyncConfigError(str(exc)) from exc
            raise
        config = _read_config(data_dir)  # re-read: joining may have touched relay.json
    previous = config.get("sync") if isinstance(config.get("sync"), dict) else {}
    block["configured_at"] = _utc_now()
    # Keep fields later steps add (keys, cursors) when re-running setup.
    config["sync"] = {**previous, **block}
    for stale in ("folder", "url"):
        if stale not in block:
            config["sync"].pop(stale, None)
    if clean_nickname is not None:
        if clean_nickname:
            config["nickname"] = clean_nickname
        else:
            config.pop("nickname", None)
    _write_config(data_dir, config)

    return {
        "enabled": True,
        "sync": config["sync"],
        "nickname": config.get("nickname", ""),
        "transport_available": backend in IMPLEMENTED_BACKENDS,
        "join": join,
    }


def disable_sync(data_dir: Path, *, forget: bool = False) -> dict:
    """Turn sync off. Local data is always kept.

    forget=False keeps the settings (enabled=false) so setup can be re-run
    quickly; forget=True removes the sync block entirely.
    """
    config = _read_config(data_dir)
    if "sync" not in config:
        return {"changed": False, "message": _NOT_SET_UP}
    if forget:
        del config["sync"]
        message = "Sync disabled and its settings removed. All local data is kept; relay is local-only."
    else:
        if not isinstance(config["sync"], dict):
            config["sync"] = {}
        if config["sync"].get("enabled") is False:
            return {"changed": False, "message": "Sync is already disabled. Local data is kept."}
        config["sync"]["enabled"] = False
        message = (
            "Sync disabled. All local data is kept and nothing is sent. "
            "Settings were kept; run `/relay:sync setup` to turn it back on."
        )
    _write_config(data_dir, config)
    return {"changed": True, "message": message}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _event_counts(data_dir: Path, own_host_id: str | None) -> tuple[int, int] | None:
    """(total, from other hosts), read-only; None if there's no event log."""
    from .events import EVENTS_DB

    db = data_dir / EVENTS_DB
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            others = conn.execute(
                "SELECT COUNT(*) FROM events WHERE host_id != ?", (own_host_id or "",)
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return total, others


def sync_status(data_dir: Path | None = None) -> dict:
    """Describe sync state without changing anything (no host ID creation)."""
    from .events import get_hostname

    data_dir = data_dir or get_data_dir()
    try:
        config = json.loads(_config_path(data_dir).read_text())
        config_error = None if isinstance(config, dict) else "relay.json isn't a JSON object"
    except FileNotFoundError:
        config, config_error = {}, None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        config, config_error = {}, f"relay.json can't be read: {exc}"
    if not isinstance(config, dict):
        config = {}

    hostname = get_hostname()
    host_id = config.get("host_id") if isinstance(config.get("host_id"), str) else None
    nickname = config.get("nickname") if isinstance(config.get("nickname"), str) else ""
    block = config.get("sync")
    problems = check_sync_block(block) if block is not None else []
    if config_error:
        problems.insert(0, config_error)

    if block is None:
        state = "off"
    elif problems:
        state = "invalid"
    elif block.get("enabled"):
        state = "on"
    else:
        state = "disabled"

    return {
        "state": state,
        "problems": problems,
        "host": {
            "host_id": host_id,
            "hostname": hostname,
            "nickname": nickname,
            "display_name": nickname or hostname,
        },
        "sync": block if isinstance(block, dict) else None,
        "transport_available": state == "on" and block.get("backend") in IMPLEMENTED_BACKENDS,
        "events": _event_counts(data_dir, host_id),
        "details": _details(data_dir) if state == "on" and block.get("backend") in IMPLEMENTED_BACKENDS else None,
    }


def _details(data_dir: Path) -> dict:
    try:
        from .sync_engine import details

        return details(data_dir)
    except Exception as exc:
        return {"error": str(exc)}


def format_status(status: dict) -> str:
    host = status["host"]
    lines = ["## relay sync", ""]
    state = status["state"]
    if state == "off":
        lines.append("**Status:** off (local-only). Run `/relay:sync setup` to share with other machines.")
    elif state == "disabled":
        lines.append("**Status:** disabled (local-only; settings kept). Run `/relay:sync setup` to turn it back on.")
    elif state == "invalid":
        lines.append("**Status:** off — the `sync` settings in relay.json are invalid:")
        lines.extend(f"- {p}" for p in status["problems"])
        lines.append("Run `/relay:sync setup` to fix them, or `/relay:sync disable --forget` to remove them.")
    else:
        lines.append("**Status:** on")

    name = host["display_name"]
    detail = f"hostname `{host['hostname']}`" if host["nickname"] else "no nickname set"
    host_id = f"host ID `{host['host_id']}`" if host["host_id"] else "host ID not created yet"
    lines.append(f"**This machine:** {name} ({detail}, {host_id})")

    block = status["sync"]
    if state in ("on", "disabled") and block:
        target = block.get("folder") if block.get("backend") == "folder" else block.get("url")
        lines.append(f"**Backend:** {block.get('backend')} → `{target}`")
        scope = block.get("scope", DEFAULT_SCOPE)
        scope_text = (
            "workstreams, state, ideas, hints, tags, git snapshots"
            if scope == "state"
            else "workstreams, state, ideas, hints, tags, git snapshots + conversation messages"
        )
        lines.append(f"**Shares:** {scope_text}")
    if state == "on" and not status["transport_available"]:
        lines.append(
            "**Transport:** not available in this version of relay yet. "
            "Settings are saved, but nothing is sent or received."
        )

    d = status.get("details")
    if d:
        lines.extend(_format_details(d))

    events = status["events"]
    if events is None:
        lines.append("**Event log:** not created yet (it's built the next time relay's server starts)")
    else:
        total, others = events
        lines.append(f"**Event log:** {total} events ({others} from other machines)")
    return "\n".join(lines)


_ROLE_TEXT = {
    "member": "approved member",
    "pending": "waiting for approval from another machine",
    "revoked": "revoked (this machine can no longer sync)",
    "key-mismatch": "the folder knows this machine with a different key; set up again",
}


def _format_details(d: dict) -> list[str]:
    lines = []
    if d.get("role"):
        lines.append(f"**Membership:** {_ROLE_TEXT.get(d['role'], d['role'])}")
    if d.get("fingerprint"):
        lines.append(f"**Key fingerprint:** `{d['fingerprint']}`")
    if d.get("founder"):
        f = d["founder"]
        lines.append(f"**Founder:** {f['name']} (`{f['fingerprint']}`) — a machine joining this group must confirm this fingerprint")
    if d.get("members"):
        names = ", ".join(f"{m['name']} (`{m['fingerprint']}`)" for m in d["members"])
        lines.append(f"**Machines:** {names}")
    for r in d.get("requests") or []:
        lines.append(
            f"**Waiting to join:** {r['name']} (hostname `{r.get('hostname')}`, fingerprint `{r['fingerprint']}`). "
            f"If it's yours and the fingerprint matches what that machine shows, run `/relay:sync approve {r['name']}`."
        )
    lines.append(f"**Last push:** {d.get('last_push') or 'never'} · **Last pull:** {d.get('last_pull') or 'never'}")
    if d.get("error"):
        lines.append(f"**Problem:** {d['error']}")
    for path in d.get("conflicts") or []:
        lines.append(f"**Conflict:** `{path}` (merge it into the main file, then delete it)")
    notices = d.get("notices") or {}
    if notices:
        lines.append("**Work on other machines:**")
        fetch = False
        for ws, n in sorted(notices.items()):
            lines.append(f"- {ws}:")
            lines.extend(f"    {line}" for line in n["lines"])
            fetch = fetch or n["fetch"]
        if fetch:
            lines.append("A `git fetch` is needed to see some of it. relay never fetches on its own.")
    return lines


def format_needs_confirmation(info: dict) -> str:
    lines = [
        f"This folder already has a sync group, founded by **{info['founder_name']}** "
        f"with key fingerprint **`{info['founder_fingerprint']}`**.",
    ]
    if info.get("given"):
        lines.append(f"That doesn't match the fingerprint given (`{info['given']}`), so nothing was changed.")
    lines.append(
        "On the founding machine, run `/relay:sync status` and compare its **Founder** fingerprint. "
        "Only if it matches, run setup again with `trust_fingerprint` set to it. "
        "If it doesn't match, someone else created this folder: don't join it. "
        "Nothing has been written or sent."
    )
    return "\n".join(lines)


def format_setup_result(result: dict) -> str:
    block = result["sync"]
    target = block.get("folder") or block.get("url")
    lines = [
        f"Sync settings saved: backend **{block['backend']}** → `{target}`, scope **{block['scope']}**.",
    ]
    if result["nickname"]:
        lines.append(f"This machine's nickname: **{result['nickname']}**.")
    join = result.get("join")
    if join:
        fp = join["fingerprint"]
        if join["founded"]:
            lines.append(
                f"This machine started a new sync group in that folder (key fingerprint `{fp}`). "
                "On your other machine, run `/relay:sync setup` with the same folder, then approve it here "
                "with `/relay:sync approve <name>`."
            )
        elif join["role"] == "pending":
            members = ", ".join(f"{m['name']} (`{m['fingerprint']}`)" for m in join["members"])
            lines.append(
                f"Asked to join the existing sync group ({members}). Nothing is sent in either direction "
                f"until one of them approves this machine: run `/relay:sync approve <this machine's name>` there, "
                f"and check it shows this fingerprint: `{fp}`."
            )
        else:
            lines.append(f"This machine is already an approved member (fingerprint `{fp}`); syncing is on.")
    if not result["transport_available"]:
        lines.append(
            "Note: this version of relay can't send or receive yet, so nothing has been shared. "
            "Syncing starts automatically once the transport ships in a later version."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Push / pull entry points (hooks call these via the CLI)
# ---------------------------------------------------------------------------


def run_sync(direction: str, data_dir: Path | None = None, db_path: Path | None = None) -> dict:
    """push, pull or now (pull then push). A silent no-op unless sync is enabled."""
    if direction not in ("push", "pull", "now"):
        raise ValueError(f"direction must be push, pull or now, got {direction!r}")
    data_dir = data_dir or get_data_dir()
    config = load_sync_config(data_dir)
    if config is None:
        return {"ran": False, "reason": "off"}
    if config["backend"] not in IMPLEMENTED_BACKENDS:
        return {"ran": False, "reason": "transport-unavailable", "backend": config["backend"]}
    from . import sync_engine

    if db_path is None:
        from .db import get_db_path

        db_path = get_db_path()
    fn = {"push": sync_engine.push, "pull": sync_engine.pull, "now": sync_engine.sync_now}[direction]
    from .sync_folder import FolderError

    try:
        return {"ran": True, **fn(data_dir, db_path)}
    except (sync_engine.SyncError, FolderError) as exc:
        return {"ran": False, "reason": "error", "error": str(exc)}


def format_run_result(result: dict) -> str:
    if not result.get("ran"):
        if result.get("reason") == "off":
            return _NOT_SET_UP
        if result.get("reason") == "transport-unavailable":
            return f"The {result.get('backend')} backend isn't available in this version of relay yet."
        return f"Sync failed: {result.get('error')}"
    parts = []
    pulled = result.get("pull", result if "inserted" in result else None)
    pushed = result.get("push", result if "batches" in result and "inserted" not in result else None)
    if pulled:
        if not pulled.get("ok"):
            parts.append(f"Pull failed: {pulled.get('error')}")
        else:
            parts.append(
                f"Pulled {pulled.get('inserted', 0)} new events from {pulled.get('batches', 0)} batches"
                + (f"; {pulled['waiting']} batches from machines not approved yet" if pulled.get("waiting") else "")
                + (f"; {pulled['rejected']} batches rejected (bad signature)" if pulled.get("rejected") else "")
            )
            for c in pulled.get("conflicts", []):
                parts.append(f"Conflict: the other version was saved to `{c}`. Merge what you need, then delete it.")
            for e in pulled.get("errors", []):
                parts.append(f"Warning: {e}")
    if pushed:
        if pushed.get("skipped") is True:
            pass
        elif pushed.get("skipped"):
            parts.append(f"Nothing pushed: this machine is {pushed['skipped']}.")
        elif not pushed.get("ok"):
            parts.append(f"Push failed: {pushed.get('error')}")
        else:
            parts.append(
                f"Pushed {pushed.get('events', 0)} events in {pushed.get('batches', 0)} batches"
                + (f" (including full history for {', '.join(pushed['history_for'])})" if pushed.get("history_for") else "")
            )
    return "\n".join(parts) or "Nothing to do."


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else "status"
    if command == "status":
        print(format_status(sync_status()))
        return 0
    if command in ("push", "pull", "now"):
        result = run_sync(command)
        if command == "now" or result.get("ran") or result.get("reason") == "error":
            if result.get("reason") != "off":
                print(f"{_utc_now()} {command}: {format_run_result(result)}")
        return 0 if result.get("ran") or result.get("reason") == "off" else 1
    if command in ("approve", "revoke") and len(argv) == 2:
        from . import sync_engine
        from .db import get_db_path

        try:
            fn = sync_engine.approve if command == "approve" else sync_engine.revoke
            print(json.dumps(fn(get_data_dir(), get_db_path(), argv[1]), indent=1, default=str))
        except sync_engine.SyncError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    print("usage: python -m relay_server.sync [status|push|pull|now|approve <machine>|revoke <machine>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
