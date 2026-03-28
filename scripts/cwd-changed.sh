#!/usr/bin/env bash
# cwd-changed.sh — CwdChanged hook: suggest workstream switch when cwd matches a different workstream.
# MUST exit 0 to avoid blocking.
set -euo pipefail
trap 'exit 0' ERR
source "$(dirname "$0")/common.sh"

INPUT=$(cat)

if ! command -v jq &>/dev/null; then
  exit 0
fi

NEW_CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null || true)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)

if [ -z "$NEW_CWD" ]; then
  exit 0
fi

REGISTRY="$DATA_DIR/workstreams.json"
if [ ! -f "$REGISTRY" ]; then
  exit 0
fi

# Find the current workstream from session marker
CURRENT_WS=""
if [ -n "$SESSION_ID" ] && [[ "$SESSION_ID" =~ ^[a-f0-9-]+$ ]]; then
  MARKER_FILE="$DATA_DIR/session-markers/${SESSION_ID}.json"
  if [ -f "$MARKER_FILE" ]; then
    CURRENT_WS=$(jq -r '.workstream // empty' "$MARKER_FILE" 2>/dev/null || true)
  fi
fi

# Find best matching workstream (deepest project_dir that is a prefix of cwd)
BEST_MATCH=""
BEST_DIR=""
BEST_LEN=0

while IFS=$'\t' read -r ws_name ws_dir; do
  [ -z "$ws_dir" ] && continue
  # Normalize: remove trailing slash
  ws_dir="${ws_dir%/}"
  # Check if cwd equals or is under ws_dir
  if [ "$NEW_CWD" = "$ws_dir" ] || [[ "$NEW_CWD" == "$ws_dir"/* ]]; then
    dir_len=${#ws_dir}
    if [ "$dir_len" -gt "$BEST_LEN" ]; then
      BEST_MATCH="$ws_name"
      BEST_DIR="$ws_dir"
      BEST_LEN="$dir_len"
    fi
  fi
done < <(jq -r '.workstreams | to_entries[] | select(.value.project_dir != null and .value.project_dir != "") | [.key, .value.project_dir] | @tsv' "$REGISTRY" 2>/dev/null || true)

# No match or same workstream — no output
if [ -z "$BEST_MATCH" ] || [ "$BEST_MATCH" = "$CURRENT_WS" ]; then
  exit 0
fi

if [ -n "$CURRENT_WS" ]; then
  CONTEXT="relay: The working directory matches workstream \"${BEST_MATCH}\" (project: ${BEST_DIR}). Current workstream is \"${CURRENT_WS}\". Ask the user if they'd like to switch: use /relay:switch ${BEST_MATCH}"
else
  CONTEXT="relay: The working directory matches workstream \"${BEST_MATCH}\" (project: ${BEST_DIR}). No workstream is currently attached. Ask the user if they'd like to attach: use /relay:switch ${BEST_MATCH}"
fi

jq -n --arg ctx "$CONTEXT" '{
  hookSpecificOutput: {
    hookEventName: "CwdChanged",
    additionalContext: $ctx
  }
}'

exit 0
