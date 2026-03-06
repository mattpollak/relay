---
name: new
description: >
  Create a new workstream for tracking a project or task.
  Trigger phrases: "new workstream", "start workstream", "create workstream".
argument-hint: "<name> [description]"
---

# Create New Workstream

Create a new workstream with the name and description provided in `$ARGUMENTS`.

## Steps

1. **Parse arguments.** The first word of `$ARGUMENTS` is the workstream name. Everything after is the description. If no arguments were provided, ask the user for a name and description before proceeding.

2. **Validate name.** Must match `^[a-z0-9][a-z0-9-]*$` (lowercase, hyphens, no leading/trailing hyphens). If invalid, tell the user and ask for a corrected name.

3. **Initialize data directory.** Run:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/init-data-dir.sh"
   ```

4. **Check for duplicates.** Read the registry and check if a workstream with this name already exists. If it does, tell the user and stop.
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams.json"
   ```

5. **Create workstream directory and state file.** Read the template and write the initial state, replacing `{{NAME}}` with the name, `{{DESCRIPTION}}` with the description, `{{DATE}}` with today's date (YYYY-MM-DD), and `{{PROJECT_DIR}}` with the current working directory:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "workstreams/<name>/state.md" << 'STATEEOF'
   <expanded template content>
   STATEEOF
   ```
   The template is at `${CLAUDE_PLUGIN_ROOT}/templates/state.md` — read it with the Read tool, then expand the placeholders before writing.

6. **Update registry.** Add the new workstream to the registry:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/new-registry.sh" "<name>" "<description>" "$(pwd)"
   ```

7. **Check for matching ideas.** Read the ideas file:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "ideas.json"
   ```
   If any idea's text is a close match to the new workstream's name or description, ask the user if they'd like to remove it from the ideas list. If yes, remove it and write the updated array:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "ideas.json" << 'EOF'
   <updated JSON array without the matched idea>
   EOF
   ```

8. **Confirm.** Tell the user the workstream was created and is now active. Show the path to the state file.
