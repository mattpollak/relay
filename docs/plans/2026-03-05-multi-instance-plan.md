# Multi-Instance Support Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow multiple Claude Code instances to run in parallel, each attached to a different active workstream, without data conflicts.

**Architecture:** Replace the single-active-workstream model with multi-active. Sessions attach to workstreams via session markers. A new `attach-workstream.sh` script handles marker writing + state loading in one step. `session-start.sh` detects 0/1/2+ active workstreams and branches accordingly. Scripts that assumed single-active (`session-end.sh`, `pre-compact-save.sh`) switch to session-marker-based workstream lookup.

**Tech Stack:** Bash (hook scripts), Markdown (skill files)

**Design doc:** `docs/plans/2026-03-05-multi-instance-design.md`

---

### Task 1: Create `attach-workstream.sh`

New script that writes a session marker and outputs the state file. Used by `session-start.sh` (auto-attach for single active) and by Claude (after user picks in multi-active).

**Files:**
- Create: `scripts/attach-workstream.sh`

**Step 1: Write the script**

```bash
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
```

**Step 2: Make it executable and test manually**

Run: `chmod +x scripts/attach-workstream.sh`
Then: `bash scripts/attach-workstream.sh relay test-session-id-123`
Expected: outputs the relay state.md content and creates a session marker at `~/.config/relay/session-markers/test-session-id-123.json`

Clean up: `rm ~/.config/relay/session-markers/test-session-id-123.json`

**Step 3: Commit**

```bash
git add scripts/attach-workstream.sh
git commit -m "feat: add attach-workstream.sh for session-to-workstream binding"
```

---

### Task 2: Update `session-start.sh` for multi-active

Replace the single-active lookup with multi-active detection. Branch on 0/1/2+ active workstreams.

**Files:**
- Modify: `scripts/session-start.sh:54-91`

**Step 1: Rewrite the active workstream detection**

Replace lines 54-91 (from `# Find active workstream` through the session marker block) with:

```bash
# Find all active workstreams
ACTIVE_NAMES=$(jq -r '[.workstreams | to_entries[] | select(.value.status == "active") | .key] | join(",")' "$REGISTRY" 2>/dev/null || true)
ACTIVE_COUNT=$(echo "$ACTIVE_NAMES" | tr ',' '\n' | grep -c . 2>/dev/null || echo "0")

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
  if [ -n "$ACTIVE_NAME" ] && [ -n "$SESSION_ID" ]; then
    # Attach and get state + warning
    ATTACH_OUTPUT=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/attach-workstream.sh" "$ACTIVE_NAME" "$SESSION_ID" 2>/dev/null || true)
    if [ -n "$ATTACH_OUTPUT" ]; then
      CONTEXT=$(printf "relay: Active workstream '%s'\n---\n%s\n---" "$ACTIVE_NAME" "$ATTACH_OUTPUT")
    else
      CONTEXT="relay: Active workstream '${ACTIVE_NAME}' (no state file found — use /relay:save to create one)"
    fi
  elif [ -n "$ACTIVE_NAME" ]; then
    # No session ID available — just read state directly
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
  CONTEXT=$(printf "relay: Multiple active workstreams detected. Ask the user which one to work on for this session.\n\n%s\n\nOnce the user picks, attach to it:\n\`\`\`bash\nbash \"\${CLAUDE_PLUGIN_ROOT}/scripts/attach-workstream.sh\" \"<name>\" \"<session_id>\"\n\`\`\`\nUse the session ID shown below." "$ACTIVE_LIST")
fi

# Append session ID so skills can reference it for hint files
if [ -n "$SESSION_ID" ]; then
  CONTEXT=$(printf "%s\nrelay-session-id: %s" "$CONTEXT" "$SESSION_ID")
fi
```

Note: The old session marker write (lines 85-91) is removed — `attach-workstream.sh` now handles that.

**Step 2: Test with current single-active setup**

Start a new Claude Code session. Verify:
- Session loads the active workstream state (unchanged behavior)
- Session marker gets written
- `relay-session-id:` appears in context

**Step 3: Commit**

```bash
git add scripts/session-start.sh
git commit -m "feat: session-start.sh supports multiple active workstreams"
```

---

### Task 3: Update `session-end.sh` to use session marker

Replace the `status == "active"` lookup with a session marker read to find the workstream for this session.

**Files:**
- Modify: `scripts/session-end.sh:29-44`

**Step 1: Replace the active workstream lookup**

Replace lines 29-44 (from `# Update last_touched` to the end before `exit 0`) with:

```bash
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
```

**Step 2: Verify**

End a session and check that `last_touched` still updates correctly in `workstreams.json`.

**Step 3: Commit**

```bash
git add scripts/session-end.sh
git commit -m "refactor: session-end.sh uses session marker instead of active status"
```

---

### Task 4: Update `pre-compact-save.sh` to use session marker

Replace the `status == "active"` lookup with a session marker read. The hook receives `session_id` on stdin just like other hooks.

**Files:**
- Modify: `scripts/pre-compact-save.sh:1-58` (full rewrite since the flow changes)

**Step 1: Rewrite the script**

```bash
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
```

Key change: reads `session_id` from stdin and looks up the session marker instead of finding the active workstream from the registry.

**Step 2: Commit**

```bash
git add scripts/pre-compact-save.sh
git commit -m "refactor: pre-compact-save.sh uses session marker instead of active status"
```

---

### Task 5: Update `switch-registry.sh` — don't park the old workstream

In the multi-active model, switching just activates the new workstream (if parked) without changing the old one's status. The old workstream stays active.

**Files:**
- Modify: `scripts/switch-registry.sh`

**Step 1: Update the jq command**

Replace lines 20-27 with:

```bash
TODAY=$(date +%Y-%m-%d)
jq --arg new "$NEW" --arg date "$TODAY" \
  '(.workstreams[$new].status = "active") |
   (.workstreams[$new].last_touched = $date)' \
  "$REGISTRY" > "$REGISTRY.tmp" && \
command mv "$REGISTRY.tmp" "$REGISTRY"
```

Also update the script header comment (line 2):

```bash
# switch-registry.sh — Activate a workstream in the registry (does not park the old one).
# Usage: bash switch-registry.sh <new-name>
```

And update the argument handling — it only needs one arg now:

```bash
NEW="${1:-}"
if [ -z "$NEW" ]; then
  echo "Usage: switch-registry.sh <new-name>" >&2
  exit 1
fi
```

Remove the `OLD` variable entirely.

**Step 2: Commit**

```bash
git add scripts/switch-registry.sh
git commit -m "refactor: switch-registry.sh only activates target, no longer parks old"
```

---

### Task 6: Update `/relay:new` skill — remove auto-park step

Remove step 5 (auto-park) so new workstreams are created alongside existing active ones.

**Files:**
- Modify: `skills/new/SKILL.md`

**Step 1: Remove step 5 and renumber**

Delete step 5 entirely:
```
5. **Auto-park active workstream.** If any workstream has `"status": "active"`, set it to `"parked"` and update its `last_touched` to today. Tell the user you're parking it.
```

Renumber steps 6→5, 7→6, 8→7, 9→8.

**Step 2: Commit**

```bash
git add skills/new/SKILL.md
git commit -m "feat: /relay:new no longer auto-parks other active workstreams"
```

---

### Task 7: Update `/relay:switch` skill — detach/attach without parking

Rewrite the switch skill to save the current workstream's state, write a session hint, then attach to the new workstream — without parking the old one.

**Files:**
- Modify: `skills/switch/SKILL.md`

**Step 1: Rewrite the skill**

```markdown
---
name: switch
description: >
  Switch to a different workstream, saving the current one first.
  Also handles "resume workstream", "load workstream", "work on <name>".
argument-hint: "<workstream-name>"
---

# Switch Workstream

Switch this session from the current workstream to the one named `$ARGUMENTS`. Both workstreams stay active — use `/relay:park` to explicitly deactivate one.

## Steps

1. **Parse arguments.** The target workstream name is `$ARGUMENTS`. If empty, read the registry and list available (non-active) workstreams, then ask the user which one to switch to:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams.json"
   ```

2. **Validate target exists.** Check that `$ARGUMENTS` exists in the registry output. If not, list available workstreams and stop.

3. **Save current workstream.** If this session is attached to a workstream (check the `relay:` line in your session context):
   a. Write updated state to `state.md.new` (under 80 lines: current status, key decisions, next steps):
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "workstreams/<current-name>/state.md.new" << 'STATEEOF'
      <content>
      STATEEOF
      ```
   b. Complete the save (rotate files, update registry, reset counter):
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/complete-save.sh" "<current-name>"
      ```

4. **Write session hint.** Write a session hint file for the workstream being switched away from (same format and guidelines as in `/relay:save` Step 5). Use `date -u +%Y-%m-%dT%H%M%SZ` for the timestamp and the session ID from the `relay-session-id:` line in your session context.

5. **Activate target workstream.** Ensure the target is active in the registry (handles the case where it's parked):
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/switch-registry.sh" "<new-name>"
   ```

6. **Attach to target.** Write session marker and load state:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/attach-workstream.sh" "<new-name>" "<session-id>"
   ```

7. **Load supplementary files.** Check for optional files (skip any that return `NOT_FOUND`):
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams/<name>/plan.md"
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams/<name>/architecture.md"
   ```

8. **Change directory.** If the target workstream has a `project_dir` set in the registry and that directory exists, tell the user: "This workstream's project directory is `<path>`. You may want to `cd` there."

9. **Summarize.** Tell the user what workstream is now active and give a brief summary of its current status from state.md.
```

**Step 2: Commit**

```bash
git add skills/switch/SKILL.md
git commit -m "feat: /relay:switch detaches without parking old workstream"
```

---

### Task 8: Update `/relay:status` skill — show attached workstream

Update to show the workstream this session is attached to (from context), and mention other active workstreams.

**Files:**
- Modify: `skills/status/SKILL.md`

**Step 1: Rewrite the skill**

```markdown
---
name: status
description: >
  Show current workstream status and available commands.
  Trigger phrases: "relay status", "workstream status", "what workstream", "what am I working on".
---

# Workstream Status

Show this session's attached workstream status, plus a summary of other workstreams and available commands.

## Steps

1. **Read registry.** Run the helper script to read the registry:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams.json"
   ```
   If the output is `NOT_FOUND`, tell the user no workstreams exist yet and suggest `/relay:new`.

2. **Identify attached workstream.** Check the `relay:` line in your session context for the workstream name. If none, check the registry for active workstreams.

3. **If an attached/active workstream exists:**
   a. Read its state file:
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams/<name>/state.md"
      ```
   b. Display a status summary:
      ```
      ## Attached: <name>
      **Description:** <description>
      **Project:** <project_dir or "none">
      **Last touched:** <last_touched>

      ### Current Status
      <Current Status section from state.md, or "(no state file)" if NOT_FOUND>

      ### Next Steps
      <Next Steps section from state.md, if present>
      ```

4. **If no attached workstream:** Say "No workstream attached to this session." and skip to step 5.

5. **Show other workstreams.** From the registry, list other active, parked, and completed workstreams:
   ```
   **Other active:** name1, name2 (or "none")
   **Parked:** name1, name2 (or "none")
   **Completed:** name1, name2 (or "none")
   ```

6. **Show commands.** End with:
   ```
   **Commands:** `/relay:new` · `/relay:switch <name>` · `/relay:save` · `/relay:park` · `/relay:list`
   ```
```

**Step 2: Commit**

```bash
git add skills/status/SKILL.md
git commit -m "feat: /relay:status shows attached workstream and other active ones"
```

---

### Task 9: Update README files

Update both `README.md` (plugin) and `server/README.md` (MCP) for consistency with multi-active behavior.

**Files:**
- Modify: `README.md`
- Modify: `server/README.md`

**Step 1: Update plugin README**

Key changes:
- Mention multi-instance support in "What You Get" section
- Update the switching description — mention that both workstreams stay active
- Add a brief "Multiple Instances" section explaining the multi-active model

**Step 2: Update MCP server README**

Add `get_session_summaries` tool to the tools list (it was added in v0.8.0 but not documented in server README).

**Step 3: Commit**

```bash
git add README.md server/README.md
git commit -m "docs: update READMEs for multi-instance support and get_session_summaries"
```

---

### Task 10: Version bump and changelog

**Files:**
- Modify: `.claude-plugin/plugin.json`
- Modify: `.claude-plugin/marketplace.json`
- Modify: `CHANGELOG.md`

**Step 1: Bump to 0.9.0**

Update version in both JSON files from `0.8.1` to `0.9.0`.

**Step 2: Add changelog entry**

```markdown
## [0.9.0] - 2026-03-05

### Added
- **Multi-instance support** — Multiple workstreams can be active simultaneously. Run separate Claude Code instances (e.g., one per git worktree) each attached to a different workstream without conflicts.
- **`attach-workstream.sh`** — New script that binds a session to a workstream (writes marker + loads state) in one step. Warns if another live session is using the same workstream.

### Changed
- **`session-start.sh`** — Detects 0, 1, or 2+ active workstreams. Single active auto-attaches (unchanged). Multiple active lists them and asks the user to pick.
- **`session-end.sh`** — Uses session marker (not `status == "active"`) to find this session's workstream.
- **`pre-compact-save.sh`** — Uses session marker (not `status == "active"`) to find this session's workstream.
- **`switch-registry.sh`** — Only activates the target workstream. No longer parks the old one.
- **`/relay:new`** — No longer auto-parks other active workstreams. New workstream created alongside existing active ones.
- **`/relay:switch`** — Detaches from current workstream and attaches to target. Both stay active. Use `/relay:park` to explicitly deactivate.
- **`/relay:status`** — Shows attached workstream and lists other active workstreams.
```

**Step 3: Commit**

```bash
git add .claude-plugin/plugin.json .claude-plugin/marketplace.json CHANGELOG.md
git commit -m "chore: bump to v0.9.0, changelog for multi-instance support"
```
