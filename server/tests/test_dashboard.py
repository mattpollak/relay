"""Test HTML dashboard generation."""
from relay_server.dashboard import render_dashboard_html


def test_render_empty():
    html = render_dashboard_html(workstreams={}, ideas=[])
    assert "<table" in html
    assert "No workstreams" in html


def test_render_single_active():
    workstreams = {
        "relay": {
            "status": "active",
            "description": "Claude Code plugin",
            "last_touched": "2026-03-28",
            "project_dir": "/home/user/relay",
            "color": "#0d1a2d",
            "git": {"branch": "main"},
        }
    }
    html = render_dashboard_html(workstreams=workstreams, ideas=[])
    assert "relay" in html
    assert "active" in html.lower()
    assert "#0d1a2d" in html
    assert "main" in html
    assert "<table" in html


def test_render_groups_by_status():
    workstreams = {
        "ws-active": {"status": "active", "description": "A", "last_touched": "2026-03-28"},
        "ws-parked": {"status": "parked", "description": "B", "last_touched": "2026-03-20"},
        "ws-done": {"status": "completed", "description": "C", "last_touched": "2026-03-10"},
    }
    html = render_dashboard_html(workstreams=workstreams, ideas=[])
    active_pos = html.index("Active")
    parked_pos = html.index("Parked")
    completed_pos = html.index("Completed")
    assert active_pos < parked_pos < completed_pos


def test_render_with_ideas():
    html = render_dashboard_html(
        workstreams={},
        ideas=[
            {"id": 1, "text": "MCP rate limiter"},
            {"id": 2, "text": "Voice memo transcriber"},
        ],
    )
    assert "MCP rate limiter" in html
    assert "Voice memo transcriber" in html
    assert "Ideas" in html


def test_render_terminal_preview_uses_workstream_color():
    workstreams = {
        "relay": {
            "status": "active",
            "description": "Plugin",
            "last_touched": "2026-03-28",
            "color": "#0d1a2d",
        }
    }
    html = render_dashboard_html(workstreams=workstreams, ideas=[])
    assert html.count("#0d1a2d") >= 2  # stripe + terminal preview


def test_render_is_self_contained_html():
    html = render_dashboard_html(workstreams={}, ideas=[])
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html
    assert "</html>" in html


def test_render_no_external_dependencies():
    workstreams = {
        "relay": {"status": "active", "description": "Test", "last_touched": "2026-03-28"},
    }
    html = render_dashboard_html(workstreams=workstreams, ideas=[])
    assert "http://" not in html
    assert "https://" not in html
