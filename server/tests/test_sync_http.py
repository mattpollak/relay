"""Sync through a relay mailbox (backend http), against a real local server."""

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("nacl")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mailbox"))

from relay_mailbox.server import Store, make_server  # noqa: E402

from relay_server import sync, sync_engine  # noqa: E402
from relay_server.workstreams import create_workstream  # noqa: E402

from .test_sync_multi import Host  # noqa: E402


@pytest.fixture
def mailbox(tmp_path):
    data = tmp_path / "mailbox"
    server = make_server(data, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    store = Store(data)
    yield SimpleBox(f"http://127.0.0.1:{server.server_address[1]}", store, data)
    server.shutdown()
    server.server_close()


class SimpleBox:
    def __init__(self, url, store, data):
        self.url, self.store, self.data = url, store, data

    def token(self, label):
        return self.store.add_token(label)

    def call(self, method, path, token=None, body=None):
        req = urllib.request.Request(f"{self.url}{path}", data=body, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


def _setup(host: Host, box: SimpleBox, token: str, trust: Host | None = None, **kw):
    return sync.setup_sync(
        host.data_dir, backend="http", url=box.url, token=token, nickname=host.name,
        trust_fingerprint=trust.fingerprint if trust else None, **kw,
    )


@pytest.fixture
def hpair(tmp_path, mailbox):
    a, b = Host(tmp_path, "laptop"), Host(tmp_path, "desk")
    ta, tb = mailbox.token("laptop"), mailbox.token("desk")
    create_workstream(data_dir=a.data_dir, name="proj", description="over http")
    a.write_state("proj", "# proj\n\nv1 from laptop\n")
    _setup(a, mailbox, ta)
    a.push()
    with pytest.raises(sync_engine.NeedsConfirmation):
        _setup(b, mailbox, tb)
    _setup(b, mailbox, tb, trust=a)
    sync_engine.approve(a.data_dir, a.db_path, "desk")
    b.pull()
    b.push()
    a.pull()
    return a, b, mailbox, ta, tb


def test_two_machines_sync_through_mailbox(hpair):
    a, b, box, _, _ = hpair
    assert b.state("proj") == "# proj\n\nv1 from laptop\n"
    b.write_state("proj", "# proj\n\nv2 from desk\n")
    b.push()
    assert a.pull()["conflicts"] == []
    assert a.state("proj") == "# proj\n\nv2 from desk\n"
    names = [f["name"] for f in box.store.list_files()]
    assert "relay-sync.json" in names
    assert not any(n.endswith(".request.json") for n in names)  # desk removed its own request
    status = sync.sync_status(b.data_dir)
    assert status["state"] == "on" and status["details"]["role"] == "member"
    assert "not available" not in sync.format_status(status)


def test_tokens_are_bound_and_stored_privately(hpair):
    a, b, box, ta, _ = hpair
    assert {t["label"]: t["host_id"] for t in box.store.list_tokens()} == {"laptop": a.host_id, "desk": b.host_id}
    token_file = a.data_dir / "sync.token"
    assert token_file.read_text().strip() == ta and token_file.stat().st_mode & 0o777 == 0o600
    assert ta not in (a.data_dir / "relay.json").read_text()
    assert ta not in (box.data / "tokens.json").read_text()  # server keeps hashes only


def test_token_command(tmp_path, mailbox):
    a = Host(tmp_path, "laptop")
    token = mailbox.token("laptop")
    _setup(a, mailbox, None, token_command=f"printf %s {token}")
    assert not (a.data_dir / "sync.token").exists()
    assert json.loads((a.data_dir / "relay.json").read_text())["sync"]["token_command"].startswith("printf")
    assert a.push()["ok"]


def test_one_token_per_machine(tmp_path, mailbox):
    a, b = Host(tmp_path, "laptop"), Host(tmp_path, "desk")
    ta = mailbox.token("laptop")
    _setup(a, mailbox, ta)
    with pytest.raises(sync.SyncConfigError, match="belongs to another machine"):
        _setup(b, mailbox, ta, trust=a)


def test_http_requires_a_token(tmp_path, mailbox):
    a = Host(tmp_path, "laptop")
    with pytest.raises(sync.SyncConfigError, match="token"):
        sync.setup_sync(a.data_dir, backend="http", url=mailbox.url)


def test_unreachable_mailbox_is_a_clean_error(tmp_path):
    a = Host(tmp_path, "laptop")
    with pytest.raises(sync.SyncConfigError, match="can't reach"):
        sync.setup_sync(a.data_dir, backend="http", url="http://127.0.0.1:9", token="x" * 32)


def test_revoked_token(hpair):
    a, b, box, _, _ = hpair
    box.store.revoke_token("desk")
    res = sync.run_sync("pull", b.data_dir, b.db_path)
    assert res["ran"] is False and "rejected this machine's token" in res["error"]


def test_redaction_reseal_over_http(hpair, tmp_path):
    a, b, box, _, _ = hpair
    from relay_server.redact import redact

    a.write_state("proj", "# proj\n\nleaked hunter2hunter2\n")
    a.push()
    b.pull()
    before = {f["name"] for f in box.store.list_files() if f["name"].startswith(a.host_id)}
    redact(a.data_dir, a.db_path, "hunter2hunter2", transcripts_root=tmp_path / "none")
    assert sync_engine.reseal_own_batches(a.data_dir, a.db_path) >= 1
    after = {f["name"] for f in box.store.list_files() if f["name"].startswith(a.host_id)}
    old_batches = {n for n in before if ".batch." in n}
    assert old_batches and not (old_batches & after)  # old ciphertext gone from the mailbox
    assert b.pull()["redactions"] == 1
    assert "hunter2hunter2" not in b.state("proj")


# ---------------------------------------------------------------------------
# What the mailbox itself enforces
# ---------------------------------------------------------------------------


def test_mailbox_requires_auth(hpair):
    _a, _b, box, _, _ = hpair
    assert box.call("GET", "/v1/files")[0] == 401
    assert box.call("GET", "/v1/files", token="not-a-token")[0] == 401
    assert box.call("GET", "/v1/files/relay-sync.json")[0] == 401


def test_member_cannot_write_or_delete_another_machines_files(hpair):
    a, b, box, _ta, tb = hpair
    forged = f"{a.host_id}.batch.9999999999999-0000-{a.host_id}.json"
    status, body = box.call("PUT", f"/v1/files/{forged}", token=tb, body=b"{}")
    assert status == 403 and b"own host ID" in body
    laptop_file = next(f["name"] for f in box.store.list_files() if f["name"].startswith(f"{a.host_id}.batch."))
    assert box.call("DELETE", f"/v1/files/{laptop_file}", token=tb)[0] == 403
    assert box.call("DELETE", "/v1/files/relay-sync.json", token=tb)[0] == 403
    assert any(f["name"] == laptop_file for f in box.store.list_files())


def test_files_are_create_only(hpair):
    a, b, box, _, tb = hpair
    own = next(f["name"] for f in box.store.list_files() if f["name"].startswith(f"{b.host_id}.batch."))
    assert box.call("PUT", f"/v1/files/{own}", token=tb, body=b"replaced")[0] == 409
    assert box.call("PUT", "/v1/files/relay-sync.json", token=tb, body=b"{}")[0] == 409


def test_unbound_token_cannot_write_and_cannot_steal_a_host_id(hpair):
    a, b, box, _, _ = hpair
    outsider = box.token("outsider")
    name = f"zzzzzz.batch.9999999999999-0000-zzzzzz.json"
    assert box.call("PUT", f"/v1/files/{name}", token=outsider, body=b"{}")[0] == 403
    status, body = box.call("POST", "/v1/bind", token=outsider, body=json.dumps({"host_id": a.host_id}).encode())
    assert status == 409 and b"already bound" in body


def test_only_relay_file_names_are_accepted(hpair):
    _a, b, box, _, tb = hpair
    for path in ("/v1/files/../tokens.json", "/v1/files/..%2Ftokens.json", "/v1/files/tokens.json",
                 f"/v1/files/{b.host_id}.batch.x.json", "/v1/files/.hidden"):
        status, body = box.call("GET", path, token=tb)
        assert status in (400, 404), path
        assert b"label" not in body
    assert box.call("PUT", f"/v1/files/{b.host_id}.notes.txt", token=tb, body=b"x")[0] == 400


def test_mailbox_never_sees_plaintext(hpair):
    a, _b, box, _, _ = hpair
    a.write_state("proj", "# proj\n\nTOP-SECRET-MARKER\n")
    a.push()
    for p in (box.data / "files").iterdir():
        assert b"TOP-SECRET-MARKER" not in p.read_bytes()


def test_client_never_follows_redirects():
    """urllib would carry the Authorization header to the redirect target."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from relay_server.sync_folder import FolderError
    from relay_server.sync_http import HttpStorage

    seen = []

    class Catcher(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"files": []}')

        def log_message(self, *a):
            pass

    catcher = HTTPServer(("127.0.0.1", 0), Catcher)
    target = f"http://127.0.0.1:{catcher.server_address[1]}/stolen"

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *a):
            pass

    redirector = HTTPServer(("127.0.0.1", 0), Redirector)
    for srv in (catcher, redirector):
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        storage = HttpStorage(f"http://127.0.0.1:{redirector.server_address[1]}", "SECRET-TOKEN-123")
        with pytest.raises(FolderError, match="redirect"):
            storage.names()
        assert seen == []  # the token never reached the other server
    finally:
        for srv in (catcher, redirector):
            srv.shutdown()
            srv.server_close()


def test_revoked_host_id_stays_reserved(hpair):
    a, b, box, _, _ = hpair
    box.store.revoke_token("desk")
    fresh = box.token("someone")
    status, body = box.call("POST", "/v1/bind", token=fresh, body=json.dumps({"host_id": b.host_id}).encode())
    assert status == 409  # can't take over desk's files
    tokens = {t["label"]: t for t in box.store.list_tokens()}
    assert tokens["desk"]["revoked"] is True and tokens["desk"]["host_id"] == b.host_id

    # The admin re-issues access for desk explicitly:
    reissued = box.store.add_token("desk2", b.host_id)
    from relay_server.sync_http import save_token

    save_token(b.data_dir, reissued)
    assert b.pull()["ok"]
    with pytest.raises(ValueError, match="already has an active token"):
        box.store.add_token("desk3", b.host_id)


def test_lost_mailbox_is_rebuilt_from_the_machines(hpair):
    a, b, box, _, _ = hpair
    import shutil as _sh

    from relay_server.sync_http import save_token

    _sh.rmtree(box.data / "files")
    (box.data / "tokens.json").unlink()
    (box.data / "files").mkdir()
    # Admin re-issues each machine's token for its host ID (docs: add-token --host)
    save_token(a.data_dir, box.store.add_token("laptop", a.host_id))
    save_token(b.data_dir, box.store.add_token("desk", b.host_id))
    a.write_state("proj", "# proj\n\nafter mailbox loss\n")
    res = a.push()
    assert res["ok"] and res["snapshot"] == "repair" and res["repaired"]["marker"]
    assert b.push()["ok"]
    assert b.pull()["ok"] and b.state("proj") == "# proj\n\nafter mailbox loss\n"
    names = {f["name"] for f in box.store.list_files()}
    assert "relay-sync.json" in names and any(".member." in n for n in names)


def test_reissued_unbound_token_binds_on_next_sync(hpair):
    a, b, box, _, _ = hpair
    from relay_server.sync_http import save_token

    box.store.revoke_token("desk")
    save_token(b.data_dir, box.store.add_token("desk2", b.host_id))
    assert b.pull()["ok"] and b.push()["ok"]
