"""Tests for workstreams module."""

import json
from pathlib import Path

from relay_server.workstreams import (
    get_data_dir,
    read_registry,
    write_registry_entry,
    atomic_write,
)


def test_get_data_dir_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert get_data_dir() == tmp_path / "relay"


def test_read_registry_empty(tmp_path):
    registry = tmp_path / "workstreams.json"
    registry.write_text('{"version": 1, "workstreams": {}}')
    result = read_registry(tmp_path)
    assert result["workstreams"] == {}


def test_read_registry_missing(tmp_path):
    result = read_registry(tmp_path)
    assert result["workstreams"] == {}


def test_write_registry_entry_new(tmp_path):
    registry = tmp_path / "workstreams.json"
    registry.write_text('{"version": 1, "workstreams": {}}')
    write_registry_entry(tmp_path, "test-ws", {
        "status": "active",
        "description": "Test",
        "created": "2026-01-01",
        "last_touched": "2026-01-01",
    })
    data = json.loads(registry.read_text())
    assert data["workstreams"]["test-ws"]["status"] == "active"


def test_write_registry_entry_preserves_others(tmp_path):
    registry = tmp_path / "workstreams.json"
    registry.write_text(json.dumps({
        "version": 1,
        "workstreams": {"existing": {"status": "parked"}}
    }))
    write_registry_entry(tmp_path, "new-ws", {"status": "active"})
    data = json.loads(registry.read_text())
    assert "existing" in data["workstreams"]
    assert "new-ws" in data["workstreams"]


def test_atomic_write_creates_file(tmp_path):
    target = tmp_path / "test.txt"
    atomic_write(target, "hello world")
    assert target.read_text() == "hello world"


def test_atomic_write_is_atomic(tmp_path):
    target = tmp_path / "test.txt"
    target.write_text("original")
    atomic_write(target, "updated")
    assert target.read_text() == "updated"
    # No .tmp file left behind
    assert not (tmp_path / "test.txt.tmp").exists()
