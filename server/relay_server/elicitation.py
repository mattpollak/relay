"""Pydantic schemas and helpers for MCP elicitation forms."""
from __future__ import annotations

import logging
from typing import TypeVar

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class WorkstreamCreateSchema(BaseModel):
    """Schema for workstream creation form."""
    name: str = Field(description="Lowercase with hyphens, e.g. api-refactor")
    description: str = Field(description="Brief description of the workstream")
    project_dir: str | None = Field(default=None, description="Absolute path to project root")
    git_strategy: str | None = Field(default=None, description="none, branch, or worktree")
    color: str | None = Field(default=None, description="Hex color for terminal background, e.g. #0d1a2d")


def build_picker_enum(workstreams: dict) -> list[str]:
    """Build the enum list for workstream picker from registry data."""
    choices = []
    for name, ws in sorted(workstreams.items()):
        status = ws.get("status", "unknown")
        choices.append(f"{name} ({status})")
    choices.append("+ Create new...")
    return choices


def build_picker_schema(workstreams: dict) -> type[BaseModel]:
    """Build a dynamic Pydantic model with enum-constrained workstream choices."""
    choices = build_picker_enum(workstreams)

    class WorkstreamPickerSchema(BaseModel):
        """Schema for workstream selection form."""
        workstream: str = Field(
            description="Pick a workstream to switch to",
            json_schema_extra={"enum": choices},
        )

    return WorkstreamPickerSchema


def parse_picker_choice(choice: str) -> str | None:
    """Extract workstream name from picker choice string like 'relay (active)'.

    Returns None if the choice is '+ Create new...'.
    """
    if choice == "+ Create new...":
        return None
    # Strip the " (status)" suffix
    paren_idx = choice.rfind(" (")
    if paren_idx > 0:
        return choice[:paren_idx]
    return choice


async def elicit_or_fallback(ctx, message: str, schema: type[T]) -> T | None:
    """Try to elicit structured input from the user. Returns None if unsupported.

    Catches all exceptions so callers can fall back to markdown-based flows.
    """
    try:
        result = await ctx.elicit(message, schema)
        if result.action == "accept":
            return result.data
        return None  # User declined or cancelled
    except Exception:
        logger.debug("Elicitation not supported or failed, falling back", exc_info=True)
        return None
