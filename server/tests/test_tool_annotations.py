"""Verify every MCP tool has correct ToolAnnotations."""
from mcp.types import ToolAnnotations

from relay_server.server import mcp


READ_ONLY_TOOLS = {
    "search_history", "get_conversation", "list_sessions", "list_tags",
    "get_session_summaries", "get_status", "list_workstreams",
    "summarize_activity",
}

WRITE_IDEMPOTENT_TOOLS = {
    "reindex", "fix_other_hints", "write_session_hint", "tag_message",
    "tag_session", "save_workstream", "update_workstream",
}

WRITE_NON_IDEMPOTENT_TOOLS = {
    "create_workstream", "park_workstream", "switch_workstream",
    "manage_idea", "manage_worktree",
}

ALL_TOOLS = READ_ONLY_TOOLS | WRITE_IDEMPOTENT_TOOLS | WRITE_NON_IDEMPOTENT_TOOLS


def _get_tool_annotations() -> dict[str, ToolAnnotations | None]:
    """Extract annotations from all registered MCP tools."""
    result = {}
    for tool in mcp._tool_manager.list_tools():
        result[tool.name] = tool.annotations
    return result


def test_all_tools_have_annotations():
    annotations = _get_tool_annotations()
    for name in ALL_TOOLS:
        assert name in annotations, f"Tool {name} not found in registered tools"
        assert annotations[name] is not None, f"Tool {name} has no annotations"


def test_read_only_tools():
    annotations = _get_tool_annotations()
    for name in READ_ONLY_TOOLS:
        ann = annotations[name]
        assert ann.readOnlyHint is True, f"{name} should be readOnly"
        assert ann.destructiveHint is False, f"{name} should not be destructive"
        assert ann.idempotentHint is True, f"{name} should be idempotent"
        assert ann.openWorldHint is False, f"{name} should not be openWorld"


def test_write_idempotent_tools():
    annotations = _get_tool_annotations()
    for name in WRITE_IDEMPOTENT_TOOLS:
        ann = annotations[name]
        assert ann.readOnlyHint is False, f"{name} should not be readOnly"
        assert ann.destructiveHint is False, f"{name} should not be destructive"
        assert ann.idempotentHint is True, f"{name} should be idempotent"
        assert ann.openWorldHint is False, f"{name} should not be openWorld"


def test_write_non_idempotent_tools():
    annotations = _get_tool_annotations()
    for name in WRITE_NON_IDEMPOTENT_TOOLS:
        ann = annotations[name]
        assert ann.readOnlyHint is False, f"{name} should not be readOnly"
        assert ann.destructiveHint is False, f"{name} should not be destructive"
        assert ann.idempotentHint is False, f"{name} should not be idempotent"
        assert ann.openWorldHint is False, f"{name} should not be openWorld"


def test_no_tools_are_destructive():
    annotations = _get_tool_annotations()
    for name, ann in annotations.items():
        if ann is not None:
            assert ann.destructiveHint is False, f"{name} should not be destructive"


def test_no_tools_are_open_world():
    annotations = _get_tool_annotations()
    for name, ann in annotations.items():
        if ann is not None:
            assert ann.openWorldHint is False, f"{name} should not be openWorld"
