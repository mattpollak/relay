#!/usr/bin/env bash
# pre-compact-save.sh — PreCompact hook: instruct Claude to save state before context compression.
# MUST exit 0 to avoid blocking compaction.
set -euo pipefail
trap 'exit 0' ERR
source "$(dirname "$0")/common.sh"

REGISTRY="$DATA_DIR/workstreams.json"

# Check jq and registry exist
if ! command -v jq &>/dev/null || [ ! -f "$REGISTRY" ]; then
  echo "IMPORTANT: Context compaction is about to occur. If you are tracking work in a workstream, save your state now."
  exit 0
fi

# Find active workstream
ACTIVE_NAME=$(jq -r '[.workstreams | to_entries[] | select(.value.status == "active")] | first | .key // empty' "$REGISTRY" 2>/dev/null || true)

# Validate workstream name format (lowercase alphanum + dashes)
if [ -z "$ACTIVE_NAME" ] || ! [[ "$ACTIVE_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
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

Then write a session hint file for efficient summarization:
bash "\${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "session-hints/\$(date -u +%Y-%m-%dT%H%M%SZ)-\${CLAUDE_SESSION_ID}.json" << 'HINTEOF'
{
  "session_id": "\${CLAUDE_SESSION_ID}",
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
