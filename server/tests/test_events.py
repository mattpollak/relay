"""Tests for the local event log (sync rollout step 1)."""

import json
import multiprocessing
import re
from pathlib import Path

import pytest

from relay_server import events as ev
from relay_server.db import ensure_schema, get_connection
from relay_server.events import (
    EventStore,
    collect_current,
    ensure_event_log,
    ensure_host_identity,
    record_changes,
    restore_authored_rows,
    split_ws_entry,
)

ID_RE = re.compile(r"^\d{13}-\d{4}-[a-z2-7]{6}$")
SID = "aabbccdd-1122-3344-5566-778899aabbcc"


def _make_data_dir(tmp_path: Path) -> Path:
    data_dir = tmp_path / "relay"
    (data_dir / "workstreams" / "alpha").mkdir(parents=True)
    (data_dir / "workstreams" / "beta").mkdir(parents=True)
    registry = {
        "version": 1,
        "workstreams": {
            "alpha": {
                "status": "active", "description": "Alpha", "created": "2026-01-01",
                "last_touched": "2026-01-02", "project_dir": "/src/alpha",
            },
            "beta": {
                "status": "parked", "description": "Beta", "created": "2026-01-01",
                "last_touched": "2026-01-01", "project_dir": "",
                "git": {"strategy": "branch", "branch": "main"},
            },
        },
    }
    (data_dir / "workstreams.json").write_text(json.dumps(registry, indent=2))
    (data_dir / "workstreams" / "alpha" / "state.md").write_text("# alpha\n\nstate A\n")
    (data_dir / "workstreams" / "alpha" / "plan.md").write_text("# plan\n")
    (data_dir / "workstreams" / "beta" / "state.md").write_text("# beta\n")
    (data_dir / "ideas.json").write_text(json.dumps([{"id": 1, "text": "idea one", "added": "2026-01-01"}]))
    (data_dir / "relay.json").write_text(json.dumps({"terminal_color": True}))
    hints = data_dir / "session-hints"
    hints.mkdir()
    (hints / f"2026-03-05T043956Z-{SID}.json").write_text(
        json.dumps({"session_id": SID, "workstream": "alpha", "summary": ["did a thing"]})
    )
    return data_dir


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "index.db"
    ensure_schema(db_path)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO sessions (session_id, project_dir, message_count) VALUES (?, '/src/alpha', 1)", (SID,)
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hello there', '2026-03-05T04:00:00Z')",
        (SID,),
    )
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    conn.execute("INSERT INTO message_tags (message_id, tag, source) VALUES (?, 'important', 'manual')", (mid,))
    conn.execute("INSERT INTO message_tags (message_id, tag, source) VALUES (?, 'auto-tag', 'auto')", (mid,))
    conn.execute("INSERT INTO session_tags (session_id, tag, source) VALUES (?, 'starred', 'manual')", (SID,))
    conn.execute(
        """INSERT INTO session_hints (session_id, timestamp, source_file, workstream, summary, decisions)
           VALUES (?, '2026-03-06T00:00:00Z', 'mcp-2026-03-06T00:00:00Z-aabbccdd', 'alpha', ?, ?)""",
        (SID, json.dumps(["mcp hint"]), json.dumps(["chose X"])),
    )
    conn.commit()
    conn.close()
    return db_path


def _types(data_dir):
    with EventStore(data_dir) as store:
        return [(e["type"], e["key"]) for e in store.events()]


# --- host identity --------------------------------------------------------


def test_host_identity_created_once_and_preserves_config(tmp_path):
    data_dir = tmp_path / "relay"
    data_dir.mkdir()
    (data_dir / "relay.json").write_text(json.dumps({"summary_dir": "/x", "nickname": "laptop"}))
    first = ensure_host_identity(data_dir)
    second = ensure_host_identity(data_dir)
    assert first["host_id"] == second["host_id"]
    assert re.match(r"^[a-z2-7]{6}$", first["host_id"])
    assert first["display_name"] == "laptop"
    cfg = json.loads((data_dir / "relay.json").read_text())
    assert cfg["summary_dir"] == "/x"
    assert cfg["host_id"] == first["host_id"]
    assert cfg["host_id_hostname"] == ev.get_hostname()


def test_host_display_name_falls_back_to_hostname(tmp_path):
    data_dir = tmp_path / "relay"
    data_dir.mkdir()
    assert ensure_host_identity(data_dir)["display_name"] == ev.get_hostname()


def test_corrupt_relay_json_is_not_overwritten(tmp_path):
    data_dir = tmp_path / "relay"
    data_dir.mkdir()
    (data_dir / "relay.json").write_text("{not json")
    with pytest.raises(RuntimeError):
        ensure_host_identity(data_dir)
    assert (data_dir / "relay.json").read_text() == "{not json"


# --- HLC --------------------------------------------------------------------


def test_hlc_ids_monotonic_even_when_clock_goes_back(tmp_path):
    with EventStore(tmp_path) as store, store.transaction():
        a = store.next_id(wall_ms=2_000_000_000_000)
        b = store.next_id(wall_ms=1_000_000_000_000)  # clock stepped back
        c = store.next_id(wall_ms=2_000_000_000_000)
        d = store.next_id(wall_ms=2_000_000_000_001)
    assert all(ID_RE.match(x) for x in (a, b, c, d))
    assert a < b < c < d
    assert d.startswith("2000000000001-0000-")


def test_hlc_counter_overflow_rolls_into_next_ms(tmp_path):
    with EventStore(tmp_path) as store, store.transaction():
        store.set_meta("hlc", f"5000000000000:{ev._MAX_COUNTER}")
        nxt = store.next_id(wall_ms=5_000_000_000_000)
    assert nxt.startswith("5000000000001-0000-")


def test_observe_advances_clock_past_remote_ids(tmp_path):
    with EventStore(tmp_path) as store, store.transaction():
        store.observe("9000000000000-0005-zzzzzz")
        nxt = store.next_id(wall_ms=1_000_000_000_000)
    assert nxt.startswith("9000000000000-0006-")


def _append_many(data_dir, n, out):
    ids = []
    with EventStore(Path(data_dir)) as store:
        for i in range(n):
            with store.transaction():
                ids.append(store.append("idea.add", f"idea/x{i}", {"value": i}))
    out.extend(ids)


def test_concurrent_processes_get_unique_ordered_ids(tmp_path):
    data_dir = tmp_path / "relay"
    data_dir.mkdir()
    ensure_host_identity(data_dir)  # both processes share one host_id
    with multiprocessing.Manager() as mgr:
        out = mgr.list()
        procs = [multiprocessing.Process(target=_append_many, args=(str(data_dir), 50, out)) for _ in range(3)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        ids = list(out)
    assert len(ids) == 150
    assert len(set(ids)) == 150
    with EventStore(data_dir) as store:
        stored = [e["id"] for e in store.events()]
    assert stored == sorted(stored) and len(stored) == 150


# --- migration / import -------------------------------------------------------


def test_first_run_imports_everything_and_backs_up(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    db_path = _make_db(tmp_path)
    result = ensure_event_log(data_dir, db_path)

    assert result["status"] == "migrated"
    keys = {k for _t, k in _types(data_dir)}
    assert {
        "ws/alpha", "ws/beta", "state/alpha", "state/beta", "file/alpha/plan.md",
        f"hint/2026-03-05T043956Z-{SID}.json", "hint/mcp-2026-03-06T00:00:00Z-aabbccdd",
        f"stag/{SID}/starred", f"host/{result['host']['host_id']}",
    } <= keys
    assert any(k.startswith("mtag/") and k.endswith("/important") for k in keys)
    idea = json.loads((data_dir / "ideas.json").read_text())[0]
    assert re.match(r"^[0-9a-f]{16}$", idea["uid"])
    assert f"idea/{idea['uid']}" in keys
    assert not any(k.endswith("/auto-tag") for k in keys), "auto tags are derived, not authored"

    backup = Path(result["backup"])
    assert (backup / "workstreams.json").exists()
    assert (backup / "workstreams" / "alpha" / "state.md").read_text() == "# alpha\n\nstate A\n"
    authored = json.loads((backup / "index-authored-rows.json").read_text())
    assert "hint/mcp-2026-03-06T00:00:00Z-aabbccdd" in authored

    with EventStore(data_dir) as store:
        sources = {e["source"] for e in store.events()}
    assert sources == {"import"}


def test_import_preserves_existing_files_byte_for_byte(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    skip = {"relay.json", "ideas.json"}  # host_id / idea uid are added
    before = {p: p.read_bytes() for p in data_dir.rglob("*") if p.is_file() and p.name not in skip}
    ensure_event_log(data_dir, _make_db(tmp_path))
    after = {p: p.read_bytes() for p in before}
    assert before == after


def test_second_run_is_noop(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    db_path = _make_db(tmp_path)
    ensure_event_log(data_dir, db_path)
    again = ensure_event_log(data_dir, db_path)
    assert again["status"] == "ok"
    assert again["new_events"] == 0
    assert again["backup"] is None


def test_empty_data_dir_migrates_without_backup(tmp_path):
    result = ensure_event_log(tmp_path / "relay")
    assert result["status"] == "migrated"
    assert result["backup"] is None


# --- recording changes --------------------------------------------------------


def test_external_state_edit_recorded_with_parent(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    ensure_event_log(data_dir)
    with EventStore(data_dir) as store:
        first = store.heads()["state/alpha"][0]

    (data_dir / "workstreams" / "alpha" / "state.md").write_text("# alpha\n\nedited by sed\n")
    ids = record_changes(data_dir, source="external")
    assert len(ids) == 1
    with EventStore(data_dir) as store:
        e = store.events()[-1]
        assert (e["type"], e["key"], e["parent"], e["source"]) == ("state.save", "state/alpha", first, "external")
        assert json.loads(e["payload"]) == {"value": "# alpha\n\nedited by sed\n"}


def test_registry_change_records_only_changed_fields(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    ensure_event_log(data_dir)
    reg = json.loads((data_dir / "workstreams.json").read_text())
    reg["workstreams"]["beta"]["status"] = "active"
    reg["workstreams"]["beta"]["color"] = "#112233"
    del reg["workstreams"]["beta"]["git"]
    (data_dir / "workstreams.json").write_text(json.dumps(reg))
    record_changes(data_dir)
    with EventStore(data_dir) as store:
        e = store.events()[-1]
        assert e["type"] == "ws.update"
        assert json.loads(e["payload"]) == {"set": {"status": "active", "color": "#112233"}, "unset": ["git"]}
        shared, _local = split_ws_entry(reg["workstreams"]["beta"])
        assert "project_dir" not in shared  # host-local, recorded under wsdir/
        assert store.head_value("ws/beta") == shared


def test_deletions_become_tombstones(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    ensure_event_log(data_dir)
    reg = json.loads((data_dir / "workstreams.json").read_text())
    del reg["workstreams"]["beta"]
    (data_dir / "workstreams.json").write_text(json.dumps(reg))
    (data_dir / "workstreams" / "alpha" / "plan.md").unlink()
    uid = json.loads((data_dir / "ideas.json").read_text())[0]["uid"]
    (data_dir / "ideas.json").write_text("[]")
    record_changes(data_dir)
    types = _types(data_dir)
    assert ("ws.delete", "ws/beta") in types
    assert ("ws.file.delete", "file/alpha/plan.md") in types
    assert ("idea.delete", f"idea/{uid}") in types
    with EventStore(data_dir) as store:
        assert store.heads()["ws/beta"][1] is None


def test_corrupt_registry_never_causes_deletions(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    ensure_event_log(data_dir)
    (data_dir / "workstreams.json").write_text("{truncated")
    (data_dir / "ideas.json").write_text("[oops")
    record_changes(data_dir)
    assert not any(t.endswith(".delete") for t, _k in _types(data_dir))


def test_hints_and_tags_missing_from_db_are_not_deleted(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    db_path = _make_db(tmp_path)
    ensure_event_log(data_dir, db_path)
    conn = get_connection(db_path)
    conn.execute("DELETE FROM session_hints")
    conn.execute("DELETE FROM message_tags")
    conn.execute("DELETE FROM session_tags")
    conn.commit()
    conn.close()
    assert record_changes(data_dir, db_path) == []


def test_fold_of_log_matches_heads(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    ensure_event_log(data_dir, _make_db(tmp_path))
    (data_dir / "workstreams" / "alpha" / "state.md").write_text("v2")
    record_changes(data_dir)
    reg = json.loads((data_dir / "workstreams.json").read_text())
    reg["workstreams"]["alpha"]["status"] = "parked"
    (data_dir / "workstreams.json").write_text(json.dumps(reg))
    record_changes(data_dir)
    with EventStore(data_dir) as store:
        assert store.fold() == store.heads()


def test_heads_match_disk_after_recording(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    ensure_event_log(data_dir)
    current, _ = collect_current(data_dir)
    with EventStore(data_dir) as store:
        heads = store.heads()
    for key, value in current.items():
        assert heads[key][1] == ev._canon(value), key


def test_relay_workstream_ops_are_recorded(tmp_path):
    """End to end through the real workstream functions."""
    from relay_server.workstreams import create_workstream, manage_idea, save_workstream

    data_dir = tmp_path / "relay"
    db_path = tmp_path / "index.db"
    ensure_schema(db_path)
    ensure_event_log(data_dir, db_path)

    create_workstream(data_dir=data_dir, name="gamma", description="G")
    record_changes(data_dir, db_path, source="create_workstream")
    conn = get_connection(db_path)
    save_workstream(
        data_dir=data_dir, conn=conn, name="gamma", state_content="# gamma v2",
        session_id=SID, hint_summary=["saved"],
    )
    conn.close()
    record_changes(data_dir, db_path, source="save_workstream")
    manage_idea(data_dir=data_dir, action="add", text="new idea")
    record_changes(data_dir, db_path, source="manage_idea")

    with EventStore(data_dir) as store:
        evs = [(e["type"], e["key"], e["source"]) for e in store.events() if not e["key"].startswith("host/")]
    assert ("ws.create", "ws/gamma", "create_workstream") in evs
    assert ("state.save", "state/gamma", "create_workstream") in evs
    assert ("state.save", "state/gamma", "save_workstream") in evs
    assert any(t == "hint.write" and s == "save_workstream" for t, _k, s in evs)
    uid = json.loads((data_dir / "ideas.json").read_text())[0]["uid"]
    assert ("idea.add", f"idea/{uid}", "manage_idea") in evs


# --- idea uids ------------------------------------------------------------------


def test_new_ideas_get_uid_and_keep_numeric_id(tmp_path):
    from relay_server.workstreams import list_workstreams, manage_idea

    data_dir = tmp_path / "relay"
    data_dir.mkdir()
    a = manage_idea(data_dir=data_dir, action="add", text="first")
    b = manage_idea(data_dir=data_dir, action="add", text="second")
    assert (a["id"], b["id"]) == (1, 2)
    ideas = json.loads((data_dir / "ideas.json").read_text())
    assert len({i["uid"] for i in ideas}) == 2
    md = list_workstreams(data_dir=data_dir)
    assert "1. first" in md and "uid" not in md  # display unchanged
    manage_idea(data_dir=data_dir, action="remove", idea_id=1)
    assert [i["text"] for i in json.loads((data_dir / "ideas.json").read_text())] == ["second"]


def test_ideas_added_by_hand_get_uid_once(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    ensure_event_log(data_dir)
    ideas = json.loads((data_dir / "ideas.json").read_text())
    ideas.append({"id": 2, "text": "typed in an editor", "added": "2026-11-20"})
    (data_dir / "ideas.json").write_text(json.dumps(ideas))
    record_changes(data_dir)
    after = json.loads((data_dir / "ideas.json").read_text())
    assert after[0]["uid"] == ideas[0]["uid"], "existing uid must not change"
    assert after[1]["uid"]
    assert record_changes(data_dir) == []


def test_same_numeric_id_on_two_machines_does_not_collide(tmp_path):
    """Two hosts each create 'idea 1'; keys differ because they use uids."""
    from relay_server.workstreams import manage_idea

    keys = []
    for host in ("a", "b"):
        data_dir = tmp_path / host
        data_dir.mkdir()
        manage_idea(data_dir=data_dir, action="add", text=f"idea on {host}")
        record_changes(data_dir)
        keys += [k for _t, k in _types(data_dir) if k.startswith("idea/")]
    assert len(keys) == 2 and len(set(keys)) == 2


def test_backup_keeps_original_ideas_json(tmp_path):
    data_dir = _make_data_dir(tmp_path)
    original = (data_dir / "ideas.json").read_text()
    result = ensure_event_log(data_dir)
    assert (Path(result["backup"]) / "ideas.json").read_text() == original


# --- reindex restore (regression: reindex wiped MCP-written hints) ------------


def test_reindex_restores_hints_and_manual_tags(tmp_path):
    from relay_server.indexer import reindex

    data_dir = _make_data_dir(tmp_path)
    db_path = _make_db(tmp_path)
    ensure_event_log(data_dir, db_path)

    transcripts = tmp_path / "projects" / "-src-alpha"
    transcripts.mkdir(parents=True)
    (transcripts / f"{SID}.jsonl").write_text(
        json.dumps({
            "type": "user", "sessionId": SID, "timestamp": "2026-03-05T04:00:00Z",
            "cwd": "/src/alpha", "message": {"role": "user", "content": "hello there"},
        }) + "\n"
    )
    reindex(db_path, transcript_root=tmp_path / "projects")

    conn = get_connection(db_path)
    assert conn.execute("SELECT count(*) FROM session_hints WHERE source_file LIKE 'mcp-%'").fetchone()[0] == 0
    counts = restore_authored_rows(data_dir, conn)
    assert counts["hints"] >= 1
    hint = conn.execute(
        "SELECT workstream, summary, decisions FROM session_hints WHERE source_file = 'mcp-2026-03-06T00:00:00Z-aabbccdd'"
    ).fetchone()
    assert hint is not None
    assert json.loads(hint["summary"]) == ["mcp hint"]
    assert json.loads(hint["decisions"]) == ["chose X"]
    tags = {r[0] for r in conn.execute("SELECT tag FROM message_tags WHERE source = 'manual'")}
    assert tags == {"important"}
    conn.close()


# --- server wiring ------------------------------------------------------------


def test_record_events_never_raises(monkeypatch):
    from relay_server import server

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ev, "record_changes", boom)
    server._record_events("save_workstream")  # must not raise


def test_server_tools_record_into_isolated_xdg(tmp_path, monkeypatch):
    """Server tools write events under XDG_CONFIG_HOME (isolated by conftest)."""
    import asyncio
    import os
    from unittest.mock import MagicMock

    from relay_server import server

    db_path = tmp_path / "index.db"
    ensure_schema(db_path)
    ctx = MagicMock()
    ctx.request_context.lifespan_context.db_path = db_path

    asyncio.run(server.create_workstream(ctx, name="delta", description="D"))
    data_dir = Path(os.environ["XDG_CONFIG_HOME"]) / "relay"
    assert ("ws.create", "ws/delta") in _types(data_dir)
    assert str(data_dir).startswith(str(Path(os.environ["XDG_CONFIG_HOME"])))
