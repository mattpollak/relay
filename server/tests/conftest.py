"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_xdg(tmp_path_factory, monkeypatch):
    """Point XDG dirs at a temp dir so no test can touch the real ~/.config/relay
    (e.g. via get_data_dir() in server tools, which now record events)."""
    root = tmp_path_factory.mktemp("xdg")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
