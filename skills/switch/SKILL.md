---
name: switch
description: >
  Switch to a different workstream, saving the current one first.
  Also handles "resume workstream", "load workstream", "work on <name>".
argument-hint: "<workstream-name>"
---

# Switch Workstream

Switch from the current active workstream to the one named `$ARGUMENTS`.

## Steps

1. **Parse arguments.** The target workstream name is `$ARGUMENTS`. If empty, read the registry and list available (non-active) workstreams, then ask the user which one to switch to:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams.json"
   ```

2. **Validate target exists.** Check that `$ARGUMENTS` exists in the registry output. If not, list available workstreams and stop.

3. **Save current workstream.** If there is an active workstream:
   a. Write updated state to `state.md.new` (under 80 lines: current status, key decisions, next steps):
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "workstreams/<active-name>/state.md.new" << 'STATEEOF'
      <content>
      STATEEOF
      ```
   b. Complete the save (rotate files, update registry, reset counter):
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/complete-save.sh" "<active-name>"
      ```

4. **Write session hint.** Write a session hint file for the workstream being switched away from (same format and guidelines as in `/relay:save` Step 5). Use `date -u +%Y-%m-%dT%H%M%SZ` for the timestamp and the session ID from the `relay-session-id:` line in your session context.

5. **Activate target workstream.** Park the old and activate the new in the registry:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/switch-registry.sh" "<old-name>" "<new-name>"
   ```

6. **Load target context.** Read and display the target workstream's files:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams/<name>/state.md"
   ```
   Also check for optional files (skip any that return `NOT_FOUND`):
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams/<name>/plan.md"
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams/<name>/architecture.md"
   ```

7. **Change directory.** If the target workstream has a `project_dir` set in the registry and that directory exists, tell the user: "This workstream's project directory is `<path>`. You may want to `cd` there."

8. **Summarize.** Tell the user what workstream is now active and give a brief summary of its current status from state.md.
