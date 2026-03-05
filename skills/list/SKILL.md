---
name: list
description: >
  List all workstreams grouped by status.
  Trigger phrases: "list workstreams", "show workstreams".
---

# List Workstreams

Display all workstreams from the registry, grouped by status.

## Steps

1. **Read registry.** Run the helper script to read the registry:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "workstreams.json"
   ```
   If the output is `NOT_FOUND`, tell the user no workstreams have been created yet and suggest `/relay:new`.

2. **Display grouped by status.** Format the output as a table grouped by status. Show active workstreams first, then parked, then completed:

   ```
   ## Active
   | Workstream | Description | Last Touched |
   |---|---|---|
   | name | description | date |

   ## Parked
   | Workstream | Description | Last Touched |
   |---|---|---|
   | name | description | date |

   ## Completed
   | Workstream | Description | Completed |
   |---|---|---|
   | name | description | date |
   ```

   If a group has no entries, skip it entirely.

3. **Show ideas.** Read the ideas file:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "ideas.json"
   ```
   If the output is NOT `NOT_FOUND` and the array is non-empty, display under a "## Ideas" heading as a numbered list:
   ```
   ## Ideas
   1. use websockets for real-time updates *(Mar 5)*
   2. Retrosheet historical data for Baseball Classics *(Mar 3)*

   `/relay:idea promote <id>` to start working on one.
   ```

4. **Quick tips.** After the listing, show:
   ```
   **Commands:** `/relay:status` · `/relay:new` · `/relay:switch <name>` · `/relay:save` · `/relay:park` · `/relay:idea`
   ```
