#!/usr/bin/env bash
# pre-compact-save.sh — PreCompact hook: instruct Claude to save state before context compression.
# MUST exit 0 to avoid blocking compaction.
set -euo pipefail
trap 'exit 0' ERR
source "$(dirname "$0")/common.sh"

INPUT=$(cat)

if ! command -v jq &>/dev/null; then
  echo "IMPORTANT: Context compaction imminent. Save your workstream state now with /relay:save."
  exit 0
fi

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)

# Find workstream from session marker
ACTIVE_NAME=""
if [ -n "$SESSION_ID" ]; then
  MARKER_FILE="$DATA_DIR/session-markers/${SESSION_ID}.json"
  if [ -f "$MARKER_FILE" ]; then
    ACTIVE_NAME=$(jq -r '.workstream // empty' "$MARKER_FILE" 2>/dev/null || true)
  fi
fi

if [ -n "$ACTIVE_NAME" ] && [[ "$ACTIVE_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "IMPORTANT: Context compaction imminent. Save workstream '${ACTIVE_NAME}' now — call save_workstream or use /relay:save."
else
  echo "IMPORTANT: Context compaction imminent. Save your workstream state now with /relay:save."
fi

# Optional multi-machine sync: push in the background (no-op when sync is off)
relay_sync_background push

exit 0
