#!/usr/bin/env bash
# pre-compact-save.sh — PreCompact hook: instruct Claude to save state before context compression.
# MUST exit 0 to avoid blocking compaction.
set -euo pipefail
trap 'exit 0' ERR
source "$(dirname "$0")/common.sh"

# Read stdin for session_id
INPUT=$(cat)

# Check jq is available
if ! command -v jq &>/dev/null; then
  echo "IMPORTANT: Context compaction is about to occur. If you are tracking work in a workstream, save your state now."
  exit 0
fi

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)

# Find workstream from session marker (not active status — multiple may be active)
ACTIVE_NAME=""
if [ -n "$SESSION_ID" ]; then
  MARKER_FILE="$DATA_DIR/session-markers/${SESSION_ID}.json"
  if [ -f "$MARKER_FILE" ]; then
    ACTIVE_NAME=$(jq -r '.workstream // empty' "$MARKER_FILE" 2>/dev/null || true)
  fi
fi

# Validate workstream name format (lowercase alphanum + dashes)
if [ -z "$ACTIVE_NAME" ] || ! [[ "$ACTIVE_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "IMPORTANT: Context compaction is about to occur. If you are tracking work in a workstream, save your state now with /relay:save."
  exit 0
fi

STATE_FILE="$DATA_DIR/workstreams/$ACTIVE_NAME/state.md"

cat <<EOF
IMPORTANT: Context compaction is imminent. You MUST save the active workstream '${ACTIVE_NAME}' state NOW.

Call the save_workstream MCP tool:

save_workstream(
  name="${ACTIVE_NAME}",
  state_content="<state markdown, under 80 lines>",
  session_id="<from relay-session-id in your session context>",
  hint_summary=["<3-6 bullets: what was accomplished>"],
  hint_decisions=["<key decisions, if any — omit if none>"]
)

The state content must include:
- Current status (what was being worked on)
- Key decisions made
- Next steps
- Any blockers or important context that would be lost

This single call handles: atomic state file write + backup, registry update, session hint to DB, and session marker.
EOF

exit 0
