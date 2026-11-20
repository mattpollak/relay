"""Sharing conversation messages between machines (scope state+messages)."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("nacl")

from relay_server import sync, sync_engine  # noqa: E402
from relay_server.db import get_connection  # noqa: E402
from relay_server.events import EventStore, restore_authored_rows  # noqa: E402
from relay_server.workstreams import create_workstream  # noqa: E402

from .test_sync_multi import Host  # noqa: E402

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"


def _ctx(host: Host):
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(db_path=host.db_path)))


def _say(host: Host, sid: str, *texts: str, start: int = 0) -> None:
    """Simulate the indexer picking up messages from this machine's transcripts."""
    conn = get_connection(host.db_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO sessions (session_id, project_dir, slug, git_branch, cwd, message_count)
               VALUES (?, '/src/proj', 'happy-fox', 'main', '/src/proj', 0)""",
            (sid,),
        )
        for i, text in enumerate(texts, start):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, model) VALUES (?, ?, ?, ?, NULL)",
                (sid, "user" if i % 2 == 0 else "assistant", text, f"2026-11-20T10:{i:02d}:00Z"),
            )
        conn.execute("UPDATE sessions SET message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?) WHERE session_id = ?", (sid, sid))
        conn.commit()
    finally:
        conn.close()


def _messages(host: Host, sid: str) -> list[str]:
    conn = get_connection(host.db_path)
    try:
        return [r[0] for r in conn.execute("SELECT content FROM messages WHERE session_id = ? ORDER BY timestamp", (sid,))]
    finally:
        conn.close()


@pytest.fixture
def mpair(tmp_path):
    (tmp_path / "shared").mkdir()
    folder = tmp_path / "shared" / "relay-sync"
    a, b = Host(tmp_path, "laptop"), Host(tmp_path, "desk")
    create_workstream(data_dir=a.data_dir, name="proj", description="p")
    _say(a, SID_A, "how do I fix the flux capacitor", "replace the gigawatt coupling")
    for h, trust in ((a, None), (b, a)):
        sync.setup_sync(h.data_dir, backend="folder", folder=str(folder), nickname=h.name,
                        scope="state+messages", trust_fingerprint=trust.fingerprint if trust else None)
    sync_engine.approve(a.data_dir, a.db_path, "desk")
    b.pull()
    b.push()
    a.pull()
    return a, b, folder


def test_messages_reach_other_machine_and_are_searchable(mpair, monkeypatch):
    a, b, _ = mpair
    assert _messages(b, SID_A) == ["how do I fix the flux capacitor", "replace the gigawatt coupling"]

    from relay_server.server import get_conversation, list_sessions, search_history

    monkeypatch.setenv("XDG_CONFIG_HOME", str(b.data_dir.parent))
    hits = search_history("gigawatt", _ctx(b))
    assert len(hits) == 1 and hits[0]["host"] == "laptop" and hits[0]["slug"] == "happy-fox"
    sessions = list_sessions(_ctx(b))
    assert [s.get("host") for s in sessions if s["session_id"] == SID_A] == ["laptop"]
    assert "**Machine:** laptop" in get_conversation(SID_A, _ctx(b))
    # Local results don't grow a host field
    monkeypatch.setenv("XDG_CONFIG_HOME", str(a.data_dir.parent))
    assert "host" not in search_history("gigawatt", _ctx(a))[0]


def test_only_new_messages_are_sent_and_nothing_echoes(mpair):
    a, b, _ = mpair
    _say(a, SID_A, "one more thing", start=2)
    pushed = a.push()
    assert pushed["messages"] == 1
    b.pull()
    assert _messages(b, SID_A)[-1] == "one more thing"
    assert b.push()["messages"] == 0  # b never re-shares laptop's messages
    assert a.push()["messages"] == 0


def test_state_scope_sends_no_messages(tmp_path):
    (tmp_path / "shared").mkdir()
    a = Host(tmp_path, "laptop")
    _say(a, SID_A, "private chat")
    a.setup(tmp_path / "shared" / "relay-sync")  # default scope: state
    assert a.push()["messages"] == 0
    with EventStore(a.data_dir) as store:
        assert not [k for k in store.heads() if k.startswith("msgb/")]


def test_remote_messages_survive_reindex(mpair, tmp_path):
    a, b, _ = mpair
    from relay_server.indexer import reindex

    empty = tmp_path / "no-transcripts"
    empty.mkdir()
    reindex(b.db_path, transcript_root=empty)
    assert _messages(b, SID_A) == []
    conn = get_connection(b.db_path)
    try:
        counts = restore_authored_rows(b.data_dir, conn)
    finally:
        conn.close()
    assert counts["messages"] == 1
    assert len(_messages(b, SID_A)) == 2


def test_sender_reindex_does_not_resend(mpair, tmp_path):
    a, b, _ = mpair
    from relay_server.indexer import reindex

    empty = tmp_path / "no-transcripts"
    empty.mkdir()
    reindex(a.db_path, transcript_root=empty)
    _say(a, SID_A, "how do I fix the flux capacitor", "replace the gigawatt coupling")  # re-indexed, new IDs
    assert a.push()["messages"] == 0
    b.pull()
    assert len(_messages(b, SID_A)) == 2


def test_remote_batch_never_mixes_into_a_local_session(mpair):
    a, b, _ = mpair
    from relay_server.sync_messages import apply_message_batch

    _say(b, "bbbbbbbb-0000-0000-0000-000000000000", "desk's own session")
    conn = get_connection(b.db_path)
    try:
        added = apply_message_batch(conn, f"msgb/{a.host_id}/bbbbbbbb-0000-0000-0000-000000000000/x", {
            "session": {"session_id": "bbbbbbbb-0000-0000-0000-000000000000"},
            "messages": [{"role": "user", "content": "injected", "timestamp": "2026-01-01T00:00:00Z"}],
        })
        conn.commit()
    finally:
        conn.close()
    assert added == 0
    assert _messages(b, "bbbbbbbb-0000-0000-0000-000000000000") == ["desk's own session"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

SECRET = "sk-live-9f8e7d6c5b4a"


def _event_log_contains(host: Host, text: str) -> bool:
    with EventStore(host.data_dir) as store:
        return bool(
            store.conn.execute("SELECT 1 FROM events WHERE instr(payload, ?) > 0", (text,)).fetchone()
            or store.conn.execute("SELECT 1 FROM heads WHERE instr(value, ?) > 0", (text,)).fetchone()
        )


def _folder_plaintext_contains(folder, hosts, text: str) -> bool:
    from relay_server import sync_crypto as sc

    for path in folder.glob("*.batch.*.json"):
        env = json.loads(path.read_text())
        for h in hosts:
            events = sc.open_batch(env, sc.load_host_key(h.data_dir), h.host_id)
            if events is not None and text in json.dumps(events):
                return True
    return False


def _search(host: Host, text: str) -> int:
    conn = get_connection(host.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", (f'"{text}"',)).fetchone()[0]
    finally:
        conn.close()


def test_is_redaction():
    from relay_server.redact import MARKER, is_redaction

    assert is_redaction(f"key {SECRET} end", f"key {MARKER} end")
    assert is_redaction("same", "same")
    assert not is_redaction("hello world", f"hello {MARKER} world")  # nothing removed
    assert not is_redaction("hello world", f"hello injected {MARKER}")  # text added
    assert not is_redaction("hello", "goodbye")


def test_redact_local_only(tmp_path):
    from relay_server.redact import redact

    a = Host(tmp_path, "laptop")
    create_workstream(data_dir=a.data_dir, name="proj", description="p")
    a.write_state("proj", f"# proj\n\ntoken was {SECRET}\n")
    a.write_state("proj", f"# proj\n\nstill {SECRET} here\n")  # .bak + older event both contain it
    (a.data_dir / "session-hints").mkdir()
    (a.data_dir / "session-hints" / "2026-01-01T000000Z-abcd.json").write_text(json.dumps(
        {"session_id": SID_A, "workstream": "proj", "summary": [f"used {SECRET}"]}))
    _say(a, SID_A, f"my key is {SECRET}", "noted")
    a.record()
    assert _event_log_contains(a, SECRET) and _search(a, SECRET) == 1

    dry = redact(a.data_dir, a.db_path, SECRET, dry_run=True, transcripts_root=tmp_path / "none")
    assert dry["messages"] == 1 and dry["events"] >= 3
    assert _event_log_contains(a, SECRET)  # dry run changes nothing

    res = redact(a.data_dir, a.db_path, SECRET, transcripts_root=tmp_path / "none")
    assert res["messages"] == 1
    assert not _event_log_contains(a, SECRET)
    assert _search(a, SECRET) == 0 and _search(a, "redacted") == 1
    for p in a.data_dir.rglob("*"):
        if p.is_file() and "backups" not in p.parts and not p.name.startswith("events.db"):
            assert SECRET.encode() not in p.read_bytes(), p
    assert "[redacted]" in a.state("proj")
    assert a.record() == []  # nothing re-recorded from disk
    assert a.fold_ok()


def test_redact_reports_raw_transcripts(tmp_path):
    from relay_server.redact import redact

    a = Host(tmp_path, "laptop")
    proj = tmp_path / "claude-projects" / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(json.dumps({"text": SECRET}) + "\n")
    res = redact(a.data_dir, a.db_path, SECRET, dry_run=True, transcripts_root=tmp_path / "claude-projects")
    assert res["transcripts"] == [str(proj / "s.jsonl")]


def test_redact_too_short():
    from relay_server.redact import RedactError, redact

    with pytest.raises(RedactError):
        redact(None, None, "abc")


def test_redaction_reaches_other_machine_and_folder(mpair, tmp_path):
    a, b, folder = mpair
    from relay_server.redact import redact

    _say(a, SID_A, f"the token is {SECRET}", start=2)
    a.write_state("proj", f"# proj\n\nuse {SECRET}\n")
    a.push()
    b.pull()
    b.write_state("proj", "# proj\n\ndesk edit after seeing it\n")
    b.push()  # desk's own history now references the secret-bearing state as parent
    a.pull()
    assert _search(b, SECRET) == 1 and _event_log_contains(b, SECRET)
    assert _folder_plaintext_contains(folder, (a, b), SECRET)

    redact(a.data_dir, a.db_path, SECRET, transcripts_root=tmp_path / "none")
    sync_engine.reseal_own_batches(a.data_dir, a.db_path)
    res = b.pull()
    assert res["redactions"] == 1 and res["redacted_messages"] == 1 and res["resealed"] >= 1
    assert _search(b, SECRET) == 0
    assert not _event_log_contains(b, SECRET)
    assert not _folder_plaintext_contains(folder, (a, b), SECRET)
    for h in (a, b):  # incl. state.md.bak on desk, which only ever saw scrubbed text sent to it
        for p in h.data_dir.rglob("*"):
            if p.is_file() and not p.name.startswith("events.db") and "backups" not in p.parts:
                assert SECRET.encode() not in p.read_bytes(), p
    assert b.fold_ok() and a.fold_ok()
    # and everything still converges afterwards
    a.write_state("proj", "# proj\n\nafter redaction\n")
    a.push()
    b.pull()
    assert b.state("proj") == "# proj\n\nafter redaction\n"


def test_forged_redaction_cannot_edit_history(mpair):
    a, b, folder = mpair
    from relay_server import sync_crypto as sc

    with EventStore(b.data_dir) as store:
        target = next(r for r in store.events() if r["key"] == "state/proj")
    forged_payload = {"value": "# proj\n\nsomething else entirely [redacted]\n"}
    evt = {
        "id": f"{int(target['id'][:13]) + 10**7:013d}-0000-{a.host_id}", "host_id": a.host_id,
        "type": "msg.redact", "schema": 1, "key": "redact/forged", "parent": None, "wall_ts": "x",
        "source": "redact", "payload": {"value": {"events": {target["id"]: forged_payload}, "messages": []}},
    }
    keys = {h.host_id: sc.load_host_key(h.data_dir).public_b64 for h in (a, b)}
    env = sc.seal_batch(sc.load_host_key(a.data_dir), a.host_id, [evt], keys, created="x")
    (folder / f"{a.host_id}.batch.{evt['id']}.json").write_text(json.dumps(env))
    before = b.state("proj")
    b.pull()
    with EventStore(b.data_dir) as store:
        assert store.conn.execute("SELECT payload FROM events WHERE id = ?", (target["id"],)).fetchone()[0] == target["payload"]
    assert b.state("proj") == before


def test_origin_reindex_reapplies_redaction(mpair, tmp_path):
    a, b, _ = mpair
    from relay_server.indexer import reindex
    from relay_server.redact import redact

    _say(a, SID_A, f"oops {SECRET}", start=2)
    assert a.push()["messages"] == 1  # shared before anyone noticed
    redact(a.data_dir, a.db_path, SECRET, transcripts_root=tmp_path / "none")
    empty = tmp_path / "empty"
    empty.mkdir()
    reindex(a.db_path, transcript_root=empty)
    _say(a, SID_A, "how do I fix the flux capacitor", "replace the gigawatt coupling", f"oops {SECRET}")  # raw transcript still has it
    conn = get_connection(a.db_path)
    try:
        assert restore_authored_rows(a.data_dir, conn)["redactions"] == 1
    finally:
        conn.close()
    assert _search(a, SECRET) == 0
    assert a.push()["messages"] == 0  # and it isn't re-shared


def test_large_push_split_across_files_is_fully_received(mpair, monkeypatch):
    a, b, folder = mpair
    from relay_server import sync_messages

    monkeypatch.setattr(sync_engine, "BATCH_SIZE", 3)
    monkeypatch.setattr(sync_messages, "MAX_MESSAGES_PER_EVENT", 2)
    _say(a, SID_A, *[f"message number {i}" for i in range(20)], start=2)
    pushed = a.push()
    assert pushed["batches"] > 1
    b.pull()
    assert len(_messages(b, SID_A)) == 22


def test_unshared_message_is_shared_once_in_redacted_form(mpair, tmp_path):
    a, b, _ = mpair
    from relay_server.redact import redact

    _say(a, SID_A, f"oops {SECRET}", start=2)
    redact(a.data_dir, a.db_path, SECRET, transcripts_root=tmp_path / "none")
    assert a.push()["messages"] == 1
    b.pull()
    assert _messages(b, SID_A)[-1] == "oops [redacted]"
    assert a.push()["messages"] == 0


# ---------------------------------------------------------------------------
# Redacted text coming back through later indexing
# ---------------------------------------------------------------------------

SID_A2 = "aaaaaaaa-9999-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def test_redact_command_line_indexed_later_is_not_shared(mpair, tmp_path):
    a, b, folder = mpair
    from relay_server.redact import redact

    _say(a, SID_A, f"oops {SECRET}", start=2)
    a.push()
    b.pull()
    redact(a.data_dir, a.db_path, SECRET, transcripts_root=tmp_path / "none")
    sync_engine.reseal_own_batches(a.data_dir, a.db_path)
    # Next session: the indexer picks up this session's transcript, including
    # the /relay:redact command line and a reply that repeats the secret.
    _say(a, SID_A2, f"<command-name>/relay:redact</command-name>\n<command-args>{SECRET}</command-args>",
         f"Redacted {SECRET} from 1 message.")
    pushed = a.push()
    assert pushed["messages"] == 2  # shared, but scrubbed
    assert _search(a, SECRET) == 0
    b.pull()
    assert _search(b, SECRET) == 0
    assert "[redacted]" in _messages(b, SID_A2)[0]
    assert not _folder_plaintext_contains(folder, (a, b), SECRET)


def test_other_machine_scrubs_its_own_copies(mpair, tmp_path):
    a, b, folder = mpair
    from relay_server.redact import reapply_known_redactions, redact

    _say(b, SID_B, f"desk also pasted {SECRET}")  # in desk's own transcripts, never shared yet
    redact(a.data_dir, a.db_path, SECRET, transcripts_root=tmp_path / "none")
    a.push()
    b.pull()
    assert _messages(b, SID_B) == ["desk also pasted [redacted]"]
    # ...and desk keeps guarding later indexing (e.g. after its next startup)
    _say(b, SID_B, f"and again {SECRET}", start=1)
    assert reapply_known_redactions(b.data_dir, b.db_path) == 1
    assert _search(b, SECRET) == 0
    b.push()
    assert not _folder_plaintext_contains(folder, (a, b), SECRET)


def test_reapply_after_startup_indexing_local_only(tmp_path):
    from relay_server.redact import reapply_known_redactions, redact

    a = Host(tmp_path, "laptop")
    redact(a.data_dir, a.db_path, SECRET, transcripts_root=tmp_path / "none")  # nothing indexed yet
    _say(a, SID_A, f"the key {SECRET} again")
    assert reapply_known_redactions(a.data_dir, a.db_path) == 1
    assert _messages(a, SID_A) == ["the key [redacted] again"]
    assert reapply_known_redactions(a.data_dir, a.db_path) == 0  # only new messages are scanned


def test_fingerprint_does_not_contain_the_secret(tmp_path):
    from relay_server.redact import redact

    a = Host(tmp_path, "laptop")
    _say(a, SID_A, f"x {SECRET}")
    redact(a.data_dir, a.db_path, SECRET, transcripts_root=tmp_path / "none")
    assert not _event_log_contains(a, SECRET)
    with EventStore(a.data_dir) as store:
        assert SECRET not in json.dumps([dict(r) for r in store.conn.execute("SELECT * FROM meta")])


def test_fingerprint_scan_is_fast():
    import time

    from relay_server.redact import make_fingerprint, scrub_with_fingerprints

    fp = make_fingerprint(SECRET)
    text = ("lorem ipsum dolor sit amet " * 8000) + SECRET + " end"
    t = time.time()
    out = scrub_with_fingerprints(text, [fp])
    assert out.endswith("[redacted] end") and SECRET not in out
    assert time.time() - t < 2.0


def test_payload_redaction_must_keep_json_shape():
    from relay_server.redact import MARKER, is_payload_redaction

    assert is_payload_redaction({"value": f"key {SECRET}"}, {"value": f"key {MARKER}"})
    assert is_payload_redaction({"set": {"a": "x", "n": 3}, "unset": []}, {"set": {"a": "x", "n": 3}, "unset": []})
    # A removal spanning two strings promotes a nested key:
    assert not is_payload_redaction(
        {"value": {"a": "x", "b": {"c": "y", "d": True, "f": "z"}, "e": "w"}},
        {"value": {"a": f"x{MARKER}y", "d": True, "f": f"z{MARKER}w"}},
    )
    # Dropping the "value" key would fold to a delete:
    assert not is_payload_redaction({"value": {"v": "x"}, "z": "w"}, {"v" + MARKER: "x"})
    assert not is_payload_redaction({"value": "abc def"}, {"value": 123})
    assert not is_payload_redaction({"value": True}, {"value": 1})
    assert not is_payload_redaction({"value": ["a", "b"]}, {"value": ["a"]})


def test_escape_change_is_rejected():
    from relay_server.redact import MARKER, is_payload_redaction, is_redaction

    old_text = "path C:\\new"   # backslash, then the letters n-e-w
    new_text = "path C:" + MARKER + "\new"  # ...but now a real newline character
    old_raw = '{"value":"path C:\\\\new"}'
    new_raw = '{"value":"path C:' + MARKER + '\\new"}'
    assert is_redaction(old_raw, new_raw)  # the raw-JSON check would have accepted it
    assert json.loads(new_raw)["value"] == new_text
    assert not is_payload_redaction({"value": old_text}, {"value": new_text})


def test_forged_structural_redaction_is_ignored_end_to_end(mpair):
    a, b, folder = mpair
    from relay_server import sync_crypto as sc
    from relay_server.redact import MARKER

    with EventStore(b.data_dir) as store:
        target = next(r for r in store.events() if r["key"] == "state/proj")
    # Passes a raw-JSON-text removal check ('{"v' … '":"' … '"}' all appear in order in
    # the original) but drops the "value" key, which would fold state.md into a delete.
    forged = {"v" + MARKER: MARKER}
    from relay_server.events import _canon
    from relay_server.redact import is_redaction

    assert is_redaction(target["payload"], _canon(forged))
    evt = {
        "id": f"{int(target['id'][:13]) + 10**7:013d}-0000-{a.host_id}", "host_id": a.host_id,
        "type": "msg.redact", "schema": 1, "key": "redact/forged2", "parent": None, "wall_ts": "x",
        "source": "redact", "payload": {"value": {"events": {target["id"]: forged}, "messages": []}},
    }
    keys = {h.host_id: sc.load_host_key(h.data_dir).public_b64 for h in (a, b)}
    env = sc.seal_batch(sc.load_host_key(a.data_dir), a.host_id, [evt], keys, created="x")
    (folder / f"{a.host_id}.batch.{evt['id']}.json").write_text(json.dumps(env))
    before = b.state("proj")
    b.pull()
    assert b.state("proj") == before
