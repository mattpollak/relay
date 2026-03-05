---
name: idea
description: >
  Capture an idea for future work, or promote one to a workstream. Displayed by /relay:list.
  Trigger phrases: "add idea", "jot down", "remember this idea", "idea promote".
argument-hint: "<idea text> | promote <id>"
---

# Capture or Promote Ideas

Ideas are things you want to track but haven't started working on yet. They live in `ideas.json` and are displayed by `/relay:list`. When you're ready to work on one, promote it to a full workstream.

**Argument:** `$ARGUMENTS` is either idea text to capture, or `promote <id>` to turn an idea into a workstream.

## Subcommand: promote

If `$ARGUMENTS` starts with `promote`, extract the idea ID (the number after "promote").

1. **Read ideas.** Read the current ideas file:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "ideas.json"
   ```
   If `NOT_FOUND` or empty, tell the user there are no ideas to promote and stop.

2. **Find the idea.** Look for the idea with the matching `id` field. If not found, list all ideas with their IDs and stop.

3. **Remove the idea.** Remove the matching idea from the array and write the updated list:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "ideas.json" << 'EOF'
   <updated JSON array without the promoted idea>
   EOF
   ```

4. **Create workstream.** Tell the user the idea has been removed from the list, then invoke the `/relay:new` skill flow using the idea text as context. Ask the user for a workstream name and description (suggest based on the idea text).

## Subcommand: add (default)

If `$ARGUMENTS` does not start with `promote`, treat it as idea text to capture.

1. **Get the idea.** The idea text is `$ARGUMENTS`. If empty, ask the user what they want to capture and stop.

2. **Read existing ideas.** Read the current file:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "ideas.json"
   ```
   If the output is `NOT_FOUND`, start with an empty array `[]`.

3. **Assign ID.** The new idea's `id` is one more than the highest existing `id`, or `1` if the list is empty.

4. **Append the idea.** Add a new entry to the array and write:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "ideas.json" << 'EOF'
   <updated JSON array with new idea appended>
   EOF
   ```
   Each idea has: `{"id": <number>, "text": "<idea text>", "added": "YYYY-MM-DD"}`

5. **Confirm.** Tell the user the idea was captured with its ID. Mention `/relay:list` to see all ideas and `/relay:idea promote <id>` when they're ready to start working on it.
