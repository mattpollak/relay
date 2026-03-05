---
name: save
description: >
  Save the current workstream's state to disk.
  Trigger phrases: "save context", "save state", "save session", "persist context", "update context".
---

# Save Workstream State

Save the current session's context to the active workstream's state file.

## Steps

1. **Find active workstream.** Read the registry and find the workstream with `"status": "active"`. If none is active, tell the user there's no active workstream to save and suggest `/relay:new` or `/relay:switch`.
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams.json"
   ```

2. **Write state file.** Write the updated state to a `.new` temp file:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "workstreams/<name>/state.md.new" << 'STATEEOF'
   <content>
   STATEEOF
   ```

3. **State file content.** The state file MUST stay under 80 lines. Include these sections:

   ```markdown
   # <Workstream Name>

   ## Metadata
   - **Description:** ...
   - **Created:** YYYY-MM-DD
   - **Project dir:** /path (if applicable)

   ## Current Status
   2-3 sentences on what's happening right now.

   ## Key Decisions
   - Bullet list of important choices made (accumulated across sessions)

   ## Next Steps
   1. Numbered list of what to do next

   ## Recent Sessions (optional, if space permits)
   - YYYY-MM-DD: One-line summary
   ```

   **Priority if space is tight:** Current Status > Next Steps > Key Decisions > Recent Sessions.

4. **Complete the save.** Rotate files, update registry, and reset the context monitor — all in one step:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/complete-save.sh" "<name>"
   ```

5. **Write session hint.** Write a session hint file summarizing what was accomplished in this session segment. The hint is a small JSON file that the relay indexer will pick up for efficient summarization later.

   Generate a UTC timestamp for the filename: `date -u +%Y-%m-%dT%H%M%SZ` (e.g. `2026-03-05T163000Z`).

   Get the current session ID from the `relay-session-id:` line in your session context (set at session start). If not available, skip this step silently.

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "session-hints/<timestamp>-<session_id>.json" << 'EOF'
   {
     "session_id": "<session_id>",
     "workstream": "<active workstream name>",
     "summary": [
       "<3-6 bullets describing what was accomplished>",
       "<focus on capabilities added, features built, decisions made>",
       "<not task counts or commit hashes>"
     ],
     "decisions": [
       "<key architectural or design decisions, if any>",
       "<omit this field if no notable decisions were made>"
     ]
   }
   EOF
   ```

   **Hint writing guidelines:**
   - Summary bullets should be **what changed**, not how much work happened. "Added broadcast messaging with recipient snapshots" not "completed 13 tasks"
   - Include specific outcomes: features, capabilities, fixes, design decisions
   - If the session spanned multiple workstreams, write one hint per workstream segment
   - Keep each bullet to one line, no sub-bullets
   - Omit the `decisions` field entirely if no notable decisions were made

6. **Confirm.** Tell the user the state was saved. Mention the backup file exists at `state.md.bak`.
