#!/usr/bin/env bash
# attach-workstream.sh — Attach a session to a workstream.
# Writes session marker and outputs state file content.
# Usage: bash attach-workstream.sh <workstream-name> <session-id>
set -euo pipefail
source "$(dirname "$0")/common.sh"

NAME="${1:-}"
SESSION_ID="${2:-}"
if [ -z "$NAME" ] || [ -z "$SESSION_ID" ]; then
  echo "Usage: attach-workstream.sh <workstream-name> <session-id>" >&2
  exit 1
fi

# Validate workstream name format
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "Invalid workstream name: $NAME" >&2
  exit 1
fi

# Validate session ID format (UUID hex + dashes only)
if ! [[ "$SESSION_ID" =~ ^[a-f0-9-]+$ ]]; then
  echo "Invalid session ID format" >&2
  exit 1
fi

REGISTRY="$DATA_DIR/workstreams.json"
if [ ! -f "$REGISTRY" ]; then
  echo "Registry not found" >&2
  exit 1
fi

# Check workstream exists
WS_STATUS=$(jq -r --arg name "$NAME" '.workstreams[$name].status // empty' "$REGISTRY" 2>/dev/null || true)
if [ -z "$WS_STATUS" ]; then
  echo "Workstream '$NAME' not found" >&2
  exit 1
fi

# Write session marker
MARKER_DIR="$DATA_DIR/session-markers"
mkdir -p "$MARKER_DIR"
jq -n --arg ws "$NAME" --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{workstream: $ws, timestamp: $ts}' > "$MARKER_DIR/${SESSION_ID}.json"

# Write session-workstream mapping for statusline
SW_DIR="$DATA_DIR/session-workstreams"
mkdir -p "$SW_DIR"
printf '%s' "$NAME" > "$SW_DIR/${SESSION_ID}"

# Check for other live sessions on the same workstream
WARNING=""
for counter_file in "${COUNTER_PREFIX}"-*.count; do
  [ -f "$counter_file" ] || continue
  # Extract session ID from counter filename: /tmp/relay-<uuid>.count
  OTHER_ID=$(basename "$counter_file" .count)
  OTHER_ID="${OTHER_ID#relay-}"
  # Skip our own session
  [ "$OTHER_ID" = "$SESSION_ID" ] && continue
  # Check if the other session's marker points to the same workstream
  OTHER_MARKER="$MARKER_DIR/${OTHER_ID}.json"
  if [ -f "$OTHER_MARKER" ]; then
    OTHER_WS=$(jq -r '.workstream // empty' "$OTHER_MARKER" 2>/dev/null || true)
    if [ "$OTHER_WS" = "$NAME" ]; then
      WARNING="WARNING: Another Claude session appears to be using workstream '$NAME'. Concurrent edits to state files may conflict."
      break
    fi
  fi
done

# Output state file content
STATE_FILE="$DATA_DIR/workstreams/$NAME/state.md"
if [ -f "$STATE_FILE" ]; then
  if [ -n "$WARNING" ]; then
    echo "$WARNING"
    echo "---"
  fi
  cat "$STATE_FILE"
else
  echo "(no state file — use /relay:save to create one)"
fi
