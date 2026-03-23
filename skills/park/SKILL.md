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

1. **Determine target.** If `$ARGUMENTS` is provided, park that workstream. Otherwise, read the workstream name from the session context (injected by session-start or switch). Do NOT call `list_workstreams` — the name is already in context. If no workstream is attached and no name given, tell the user and stop.

2. **Park.** Call `park_workstream`:
   ```
   park_workstream(
     name="<name>",
     state_content="<80-line state markdown>",
     session_id="<from relay-session-id context>",
     hint_summary=["<3-6 bullets>"],
     hint_decisions=["<decisions if any>"]
   )
   ```
   See `/relay:save` for state file content guidelines and hint writing guidelines.

   If the workstream has a worktree (git strategy is "worktree"), ask the user if they want to remove it. If yes, include `remove_worktree=true` in the call. The tool will refuse if the worktree has uncommitted changes.

3. **Confirm.** Tell the user the workstream is parked. Mention they can resume with `/relay:switch <name>`.
