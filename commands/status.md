---
description: Show current workstream status and available commands.
---

# Workstream Status

Show this session's attached workstream status, plus a summary of other workstreams and available commands.

## Steps

1. **Fetch data.** Call `list_workstreams` to get all workstreams grouped by status.

2. **Identify attached workstream.** Check the `relay:` line in your session context for the workstream name. If none, check the active workstreams from the response.

3. **If an attached/active workstream exists:** The workstream state was loaded into session context at session start (the block between `---` markers after the `relay:` line). Display:
   ```
   ## Attached: <name>
   **Description:** <description>
   **Project:** <project_dir or "none">
   **Last touched:** <last_touched>

   ### Current Status
   <Current Status section from state in session context>

   ### Next Steps
   <Next Steps section from state in session context, if present>
   ```

4. **If no attached workstream:** Say "No workstream attached to this session."

5. **Show other workstreams.** From the `list_workstreams` response:
   ```
   **Other active:** name1, name2 (or "none")
   **Parked:** name1, name2 (or "none")
   **Completed:** name1, name2 (or "none")
   ```

6. **Show commands.** End with:
   ```
   **Commands:** `/relay:new` · `/relay:switch <name>` · `/relay:save` · `/relay:park` · `/relay:list`
   ```
