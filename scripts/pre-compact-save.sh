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

Write an updated state file to: ${STATE_FILE}

Use atomic overwrite:
1. Write content to ${STATE_FILE}.new
2. command mv ${STATE_FILE} ${STATE_FILE}.bak (if exists)
3. command mv ${STATE_FILE}.new ${STATE_FILE}

The state file must be under 80 lines and include:
- Current status (what was being worked on)
- Key decisions made
- Next steps
- Any blockers or important context that would be lost

Then write a session hint file for efficient summarization.
Use the session ID from the relay-session-id: line in your session context.
bash "\${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "session-hints/\$(date -u +%Y-%m-%dT%H%M%SZ)-<session_id>.json" << 'HINTEOF'
{
  "session_id": "<session_id>",
  "workstream": "${ACTIVE_NAME}",
  "summary": ["<3-6 bullets: what was accomplished in this session segment>"],
  "decisions": ["<key decisions, if any — omit field if none>"]
}
HINTEOF

Then update the registry and reset the context monitor:
bash "\${CLAUDE_PLUGIN_ROOT}/scripts/update-registry.sh" "${ACTIVE_NAME}"
bash "\${CLAUDE_PLUGIN_ROOT}/scripts/reset-counter.sh"
EOF

exit 0
