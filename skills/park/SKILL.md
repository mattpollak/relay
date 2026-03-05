---
name: park
description: >
  Park the current workstream (save state and deactivate).
  Trigger phrases: "park this", "park workstream", "pause workstream", "shelve this".
argument-hint: "[workstream-name]"
---

# Park Workstream

Park the active workstream (or the one named `$ARGUMENTS`), saving its state first.

## Steps

1. **Determine target.** If `$ARGUMENTS` is provided, park that workstream. Otherwise, read the registry and park the currently active workstream. If no workstream is active and no name was given, tell the user and stop.
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams.json"
   ```

2. **Save state first.** Before parking, save the current state:
   a. Write updated state to `state.md.new` (under 80 lines: current status, key decisions, next steps):
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "workstreams/<name>/state.md.new" << 'STATEEOF'
      <content>
      STATEEOF
      ```
   b. Complete the save (rotate files, update registry, reset counter):
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/complete-save.sh" "<name>"
      ```

3. **Write session hint.** Write a session hint file for this session segment (same format and guidelines as in `/relay:save` Step 5). Use `date -u +%Y-%m-%dT%H%M%SZ` for the timestamp and the session ID from the `relay-session-id:` line in your session context. If the save step already wrote a hint in this session, skip this — don't write duplicate hints for the same segment.

4. **Park the workstream.** Set status to `"parked"` in the registry:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/park-registry.sh" "<name>"
   ```

5. **Confirm.** Tell the user the workstream has been parked. Mention they can resume it later with `/relay:switch <name>`.
