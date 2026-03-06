#!/usr/bin/env bash
# session-end.sh — SessionEnd hook: cleanup temp files and update timestamp.
# Claude can't act on output from this hook (session is over).
# MUST exit 0 to avoid blocking session end.
set -euo pipefail
trap 'exit 0' ERR
source "$(dirname "$0")/common.sh"

# Read stdin for session_id
INPUT=$(cat)

# Check jq is available
if ! command -v jq &>/dev/null; then
  exit 0
fi

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)

# Validate session ID format (UUID hex + dashes only)
if [ -n "$SESSION_ID" ] && ! [[ "$SESSION_ID" =~ ^[a-f0-9-]+$ ]]; then
  SESSION_ID=""
fi

# Clean up counter file
if [ -n "$SESSION_ID" ]; then
  rm -f "${COUNTER_PREFIX}-${SESSION_ID}.count"
fi

# Update last_touched using session marker (not active status — multiple may be active)
REGISTRY="$DATA_DIR/workstreams.json"
MARKER_FILE="$DATA_DIR/session-markers/${SESSION_ID}.json"

if [ -f "$REGISTRY" ] && [ -n "$SESSION_ID" ] && [ -f "$MARKER_FILE" ]; then
  WS_NAME=$(jq -r '.workstream // empty' "$MARKER_FILE" 2>/dev/null || true)
  if [ -n "$WS_NAME" ]; then
    TODAY=$(date +%Y-%m-%d)
    jq --arg name "$WS_NAME" --arg date "$TODAY" \
      '.workstreams[$name].last_touched = $date' \
      "$REGISTRY" > "$REGISTRY.tmp" 2>/dev/null && \
    command mv "$REGISTRY.tmp" "$REGISTRY" 2>/dev/null || true
  fi
fi

exit 0
