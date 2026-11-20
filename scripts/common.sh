#!/usr/bin/env bash
# common.sh — Shared constants for relay plugin scripts.
# Source this at the top of every script: source "$(dirname "$0")/common.sh"

APP_NAME="relay"
DATA_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_NAME"

# --- Optional multi-machine sync -------------------------------------------
# Sync is off unless relay.json has "sync": {"enabled": true, ...}. When it's
# off these are a single jq check: no Python, no uv, no network.

relay_sync_enabled() {
  local config="$DATA_DIR/relay.json"
  [ -f "$config" ] || return 1
  command -v jq &>/dev/null || return 1
  jq -e '(.sync | type) == "object" and .sync.enabled == true' "$config" &>/dev/null
}

# relay_sync_background <push|pull> — start a sync run detached from the hook
# so it never blocks Claude Code. Output goes to $DATA_DIR/sync.log.
relay_sync_background() {
  local direction="$1"
  relay_sync_enabled || return 0
  [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || return 0
  command -v uv &>/dev/null || return 0
  nohup uv run --quiet --no-dev --extra sync --directory "${CLAUDE_PLUGIN_ROOT}/server" \
    python -m relay_server.sync "$direction" \
    </dev/null >>"$DATA_DIR/sync.log" 2>&1 &
  return 0
}

# relay_sync_pull_now — pull before session start reads state, capped at a few
# seconds (falls back to a background pull where `timeout` isn't available).
relay_sync_pull_now() {
  relay_sync_enabled || return 0
  [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || return 0
  command -v uv &>/dev/null || return 0
  if command -v timeout &>/dev/null; then
    timeout "${RELAY_SYNC_PULL_TIMEOUT:-5}" uv run --quiet --no-dev --extra sync --directory "${CLAUDE_PLUGIN_ROOT}/server" \
      python -m relay_server.sync pull </dev/null >>"$DATA_DIR/sync.log" 2>&1 || true
  else
    relay_sync_background pull
  fi
  return 0
}

# relay_sync_notices [workstream] — lines for Claude: conflicts and work left
# on other machines (for the given workstream), join requests, sync problems.
relay_sync_notices() {
  local ws="${1:-}" notices="$DATA_DIR/sync-notices.json" f base
  relay_sync_enabled || return 0
  if [ -n "$ws" ]; then
    for f in "$DATA_DIR/workstreams/$ws"/*.conflict-*.md; do
      [ -e "$f" ] || continue
      base=$(basename "$f")
      echo "relay: CONFLICT — ${base%%.conflict-*}.md was edited on two machines from the same version. The newer edit is in place; the other version is in $f. Show the user the differences, merge what they want, then delete that file."
    done
    if [ -f "$notices" ]; then
      jq -r --arg ws "$ws" '(.workstreams[$ws] // [])[]' "$notices" 2>/dev/null || true
    fi
  fi
  if [ -f "$notices" ]; then
    jq -r '(.requests // [])[], (if .error then "relay: sync problem: \(.error)" else empty end)' "$notices" 2>/dev/null || true
  fi
  return 0
}
