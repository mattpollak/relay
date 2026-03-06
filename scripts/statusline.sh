#!/usr/bin/env bash
# statusline.sh — Claude Code statusLine command for relay.
# Sets terminal tab title and background color based on the active workstream.
# Receives JSON on stdin with session context. Writes escape sequences to /dev/tty
# (side effect), echoes status line text to stdout.
#
# Respects config in ~/.config/relay/relay.json:
#   "terminal_color": true/false  (default: true)
#   "terminal_title": true/false  (default: true)

input=$(cat)

cwd=$(echo "$input" | jq -r '.workspace.current_dir // ""')
dirname=$(basename "$cwd")
branch=$(GIT_OPTIONAL_LOCKS=0 git -C "$cwd" symbolic-ref --short HEAD 2>/dev/null)

# Read relay config
RELAY_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/relay/relay.json"
enable_color=true
enable_title=true
if [ -f "$RELAY_CONFIG" ] && command -v jq &>/dev/null; then
  read -r cfg_color cfg_title <<< "$(jq -r '[.terminal_color // true, .terminal_title // true] | @tsv' "$RELAY_CONFIG" 2>/dev/null)"
  [ "$cfg_color" = "false" ] && enable_color=false
  [ "$cfg_title" = "false" ] && enable_title=false
fi

# Determine active workstream for this session
RELAY_DATA="${XDG_CONFIG_HOME:-$HOME/.config}/relay"
RELAY_REGISTRY="$RELAY_DATA/workstreams.json"
session_id=$(echo "$input" | jq -r '.session_id // ""')
workstream=""

if command -v jq &>/dev/null && [ -f "$RELAY_REGISTRY" ]; then
  # Strategy 1: session-specific mapping file (written by switch/attach)
  if [ -n "$session_id" ] && [ -f "$RELAY_DATA/session-workstreams/${session_id}" ]; then
    workstream=$(cat "$RELAY_DATA/session-workstreams/${session_id}" 2>/dev/null)
  fi

  # Strategy 2: if only one active workstream, use it
  if [ -z "$workstream" ]; then
    workstream=$(jq -r '
      [.workstreams | to_entries[] | select(.value.status == "active") | .key]
      | if length == 1 then .[0] else "" end
    ' "$RELAY_REGISTRY" 2>/dev/null || true)
  fi
fi

# Set background color
if [ "$enable_color" = "true" ] && [ -n "$workstream" ]; then
  # Check for explicit color in workstream definition
  bg_color=$(jq -r --arg ws "$workstream" \
    '.workstreams[$ws].color // ""' "$RELAY_REGISTRY" 2>/dev/null || true)

  # Fallback: hash workstream name to a hue, render as dark background
  if [ -z "$bg_color" ]; then
    hash=0
    for (( i=0; i<${#workstream}; i++ )); do
      byte=$(printf '%d' "'${workstream:$i:1}")
      hash=$(( (hash * 31 + byte) % 360 ))
    done
    bg_color=$(awk -v h="$hash" 'BEGIN {
      s = 0.40; l = 0.12;
      c = (1 - (2*l > 1 ? 2*l - 1 : 1 - 2*l)) * s;
      hp = h / 60.0;
      x = c * (1 - (hp % 2 - 1 > 0 ? hp % 2 - 1 : 1 - hp % 2));
      m = l - c/2;
      if      (hp < 1) { r=c; g=x; b=0 }
      else if (hp < 2) { r=x; g=c; b=0 }
      else if (hp < 3) { r=0; g=c; b=x }
      else if (hp < 4) { r=0; g=x; b=c }
      else if (hp < 5) { r=x; g=0; b=c }
      else             { r=c; g=0; b=x }
      printf "#%02x%02x%02x", (r+m)*255, (g+m)*255, (b+m)*255
    }')
  fi
  printf '\033]11;%s\007' "$bg_color" > /dev/tty 2>/dev/null
fi

# Shorten cwd: replace $HOME prefix with ~
display_cwd="${cwd}"
if [ -n "$HOME" ] && [[ "$cwd" == "$HOME"* ]]; then
  display_cwd="~${cwd#$HOME}"
fi

# Build status line: ~/path/to/dir (branch) │ workstream
statusline="${display_cwd}"
[ -n "$branch" ] && statusline="${statusline} (${branch})"
[ -n "$workstream" ] && statusline="${statusline} │ ${workstream}"

# Set terminal title: dirname (branch) │ workstream
if [ "$enable_title" = "true" ]; then
  title="${dirname}"
  [ -n "$branch" ] && title="${title} (${branch})"
  [ -n "$workstream" ] && title="${title} │ ${workstream}"

  if [ -n "$ITERM_SESSION_ID" ]; then
    # iTerm2: use AppleScript to set session name
    osascript -e "
      tell application \"iTerm2\"
        tell current session of current tab of current window
          set name to \"${title}\"
        end tell
      end tell
    " 2>/dev/null &
  else
    # Windows Terminal, xterm, and others: OSC 0
    printf '\033]0;%s\007' "$title" > /dev/tty 2>/dev/null
  fi
fi

echo "$statusline"
