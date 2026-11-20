#!/usr/bin/env bash
# session-start.sh — SessionStart hook: load active workstream context.
# Outputs JSON with additionalContext for Claude to see.
# MUST exit 0 to avoid blocking the session.
set -euo pipefail
trap 'exit 0' ERR
source "$(dirname "$0")/common.sh"

REGISTRY="$DATA_DIR/workstreams.json"

# Capture stdin (JSON with session_id from Claude Code)
INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)

# Validate session ID format (UUID hex + dashes only)
if [ -n "$SESSION_ID" ] && ! [[ "$SESSION_ID" =~ ^[a-f0-9-]+$ ]]; then
  SESSION_ID=""
fi

# Initialize data directory (idempotent)
bash "${CLAUDE_PLUGIN_ROOT}/scripts/init-data-dir.sh" 2>/dev/null || true

# Check jq is available
if ! command -v jq &>/dev/null; then
  cat <<'ENDJSON'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "relay: WARNING — jq is not installed. Install jq to enable workstream management. See: https://jqlang.github.io/jq/download/"
  }
}
ENDJSON
  exit 0
fi

# Check for old data directory that needs migration
OLD_DATA_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/context-flow"
if [ -d "$OLD_DATA_DIR" ] && [ ! -d "$DATA_DIR" ]; then
  MIGRATE_MSG="relay: Detected old context-flow data at $OLD_DATA_DIR. Run: bash \${CLAUDE_PLUGIN_ROOT}/scripts/migrate-data.sh"
  jq -n --arg ctx "$MIGRATE_MSG" '{
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: $ctx
    }
  }'
  exit 0
fi

# Optional multi-machine sync: pull first so the state below is current
# (no-op when sync is off)
relay_sync_pull_now

# Check registry exists
if [ ! -f "$REGISTRY" ]; then
  exit 0
fi

# Find all active workstreams
ACTIVE_NAMES=$(jq -r '[.workstreams | to_entries[] | select(.value.status == "active") | .key] | join(",")' "$REGISTRY" 2>/dev/null || true)
ACTIVE_COUNT=$(echo "$ACTIVE_NAMES" | tr ',' '\n' | grep -c . || true)

if [ "$ACTIVE_COUNT" -eq 0 ]; then
  # No active workstreams — list parked ones
  AVAILABLE=$(jq -r '[.workstreams | to_entries[] | select(.value.status == "parked") | .key] | join(", ")' "$REGISTRY" 2>/dev/null || true)
  if [ -n "$AVAILABLE" ]; then
    CONTEXT="relay: No active workstream. Parked workstreams: ${AVAILABLE}. Use /relay:switch to resume one, or /relay:new to create one."
  else
    CONTEXT="relay: No workstreams found. Use /relay:new to create one."
  fi
elif [ "$ACTIVE_COUNT" -eq 1 ]; then
  # Single active — auto-attach (existing behavior)
  ACTIVE_NAME="$ACTIVE_NAMES"
  # Validate workstream name format
  if ! [[ "$ACTIVE_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    ACTIVE_NAME=""
  fi
  if [ -n "$ACTIVE_NAME" ]; then
    # Write session marker + mapping for statusline
    if [ -n "$SESSION_ID" ]; then
      MARKER_DIR="$DATA_DIR/session-markers"
      mkdir -p "$MARKER_DIR"
      jq -n --arg ws "$ACTIVE_NAME" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '{workstream: $ws, timestamp: $ts}' > "$MARKER_DIR/${SESSION_ID}.json"
      SW_DIR="$DATA_DIR/session-workstreams"
      mkdir -p "$SW_DIR"
      printf '%s' "$ACTIVE_NAME" > "$SW_DIR/${SESSION_ID}"
    fi

    # Read and output state
    STATE_FILE="$DATA_DIR/workstreams/$ACTIVE_NAME/state.md"
    if [ -f "$STATE_FILE" ]; then
      STATE_CONTENT=$(cat "$STATE_FILE")
      CONTEXT=$(printf "relay: Active workstream '%s'\n---\n%s\n---" "$ACTIVE_NAME" "$STATE_CONTENT")
    else
      CONTEXT="relay: Active workstream '${ACTIVE_NAME}' (no state file found — use /relay:save to create one)"
    fi
  fi
else
  # Multiple active workstreams — list them and instruct Claude to ask
  ACTIVE_LIST=$(echo "$ACTIVE_NAMES" | tr ',' '\n' | while read -r ws; do
    DESC=$(jq -r --arg name "$ws" '.workstreams[$name].description // "(no description)"' "$REGISTRY" 2>/dev/null || true)
    echo "  - **$ws**: $DESC"
  done)
  CONTEXT=$(printf "relay: Multiple active workstreams detected. Ask the user which one to work on for this session.\n\n%s\n\nOnce the user picks, call switch_workstream(to_name=\"<name>\", session_id=\"<session_id>\") to attach to it.\nUse the session ID shown below." "$ACTIVE_LIST")
fi

# Sync notices: conflicts / work on other machines for the attached
# workstream, join requests, problems (nothing when sync is off)
SYNC_NOTES=$(relay_sync_notices "${ACTIVE_NAME:-}")
if [ -n "$SYNC_NOTES" ]; then
  CONTEXT=$(printf "%s\n\n%s" "${CONTEXT:-}" "$SYNC_NOTES")
fi

# Append session ID so skills can reference it for hint files
if [ -n "$SESSION_ID" ]; then
  CONTEXT=$(printf "%s\nrelay-session-id: %s" "$CONTEXT" "$SESSION_ID")
fi

# Output as JSON with additionalContext
# Use jq to safely encode the context string
jq -n --arg ctx "$CONTEXT" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $ctx
  }
}'

exit 0
