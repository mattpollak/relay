---
description: Switch to a different workstream, saving the current one first.
argument-hint: "<workstream-name>"
---

# Switch Workstream

Switch this session from the current workstream to the one named `$ARGUMENTS`. Both workstreams stay active — use `/relay:park` to explicitly deactivate one.

## Steps

1. **Parse arguments.** The target workstream name is `$ARGUMENTS`. If empty, call `list_workstreams` to show available workstreams, then ask the user which one to switch to.

2. **Switch.** Call `switch_workstream`:
   ```
   switch_workstream(
     to_name="<target>",
     from_name="<current workstream from session context>",
     state_content="<state for current workstream>",
     session_id="<from relay-session-id context>",
     hint_summary=["<bullets for current workstream>"],
     hint_decisions=["<decisions if any>"]
   )
   ```
   If no current workstream is attached (no `relay:` line in context), omit `from_name` and `state_content`.

   The response includes `target_state` (the new workstream's state.md content), `supplementary` (plan.md, architecture.md if they exist), and `project_dir`.

   If the response includes git-related fields, handle them:
   - If `git_warning`: Display the warning to the user (e.g., "⚠️ Branch mismatch: on 'main', expected 'feat/payments-api'") and show the `git_suggestion` command
   - If `dirty_warning`: Suggest stashing changes before switching branches
   - If `worktree_path`: Note the working directory the user should work in
   - If `stash_reminder`: Show the reminder about stashed changes from a previous session

3. **Present.** Show the target workstream's current status from the returned state. If `project_dir` is set, mention it. If supplementary files were returned, note their presence.
