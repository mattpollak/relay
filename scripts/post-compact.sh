#!/usr/bin/env bash
# post-compact.sh — PostCompact hook: re-inject active workstream pointer after context compaction.
# Outputs lightweight additionalContext so Claude can call get_status if needed.
# MUST exit 0 to avoid blocking.
set -euo pipefail
trap 'exit 0' ERR
source "$(dirname "$0")/common.sh"

INPUT=$(cat)

# Need jq to parse JSON
if ! command -v jq &>/dev/null; then
  exit 0
fi

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
if [ -z "$SESSION_ID" ]; then
  exit 0
fi

# Validate session ID format
if ! [[ "$SESSION_ID" =~ ^[a-f0-9-]+$ ]]; then
  exit 0
fi

# Look up session marker
MARKER_FILE="$DATA_DIR/session-markers/${SESSION_ID}.json"
if [ ! -f "$MARKER_FILE" ]; then
  exit 0
fi

ACTIVE_NAME=$(jq -r '.workstream // empty' "$MARKER_FILE" 2>/dev/null || true)
if [ -z "$ACTIVE_NAME" ] || ! [[ "$ACTIVE_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  exit 0
fi

# Look up project_dir from registry
REGISTRY="$DATA_DIR/workstreams.json"
PROJECT_DIR=""
if [ -f "$REGISTRY" ]; then
  PROJECT_DIR=$(jq -r --arg name "$ACTIVE_NAME" '.workstreams[$name].project_dir // ""' "$REGISTRY" 2>/dev/null || true)
fi

if [ -n "$PROJECT_DIR" ]; then
  CONTEXT="relay: Active workstream is \"${ACTIVE_NAME}\" (project: ${PROJECT_DIR}). Call get_status(attached=\"${ACTIVE_NAME}\") for full context."
else
  CONTEXT="relay: Active workstream is \"${ACTIVE_NAME}\". Call get_status(attached=\"${ACTIVE_NAME}\") for full context."
fi

jq -n --arg ctx "$CONTEXT" '{
  hookSpecificOutput: {
    hookEventName: "PostCompact",
    additionalContext: $ctx
  }
}'

exit 0
