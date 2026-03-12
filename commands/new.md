---
description: Create a new workstream for tracking a project or task.
argument-hint: "<name> [description]"
---

# Create New Workstream

Create a new workstream with the name and description provided in `$ARGUMENTS`.

## Steps

1. **Parse arguments.** The first word of `$ARGUMENTS` is the workstream name. Everything after is the description. If no arguments provided, ask the user.

2. **Validate name.** Must match `^[a-z0-9][a-z0-9-]*$`. If invalid, ask for correction.

3. **Ask about color (optional).** If terminal decoration is configured (check if `~/.claude/statusline-command.sh` exists), ask the user if they'd like to set a custom background color for this workstream. Offer:
   - **Auto** (default) — a color will be generated from the workstream name
   - **Custom** — let them provide a dark hex color (e.g., `#0d1a2d`)

   If terminal decoration isn't configured, skip this step.

4. **Create workstream.** Call `create_workstream`:
   ```
   create_workstream(name="<name>", description="<desc>", project_dir="<cwd>", color="<hex or empty>")
   ```
   If it returns an error (duplicate), tell the user.

5. **Check for matching ideas.** Call `manage_idea(action="list")`. If any idea's text closely matches the new workstream's name or description, ask the user if they'd like to remove it. If yes, call `manage_idea(action="remove", idea_id=<id>)`.

6. **Confirm.** Tell the user the workstream was created and is now active.
