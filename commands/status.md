---
description: Show current workstream status and available commands.
---

# Workstream Status

Show this session's attached workstream status, plus a summary of other workstreams and available commands.

## Steps

1. **Fetch data.** Call `get_status(attached="<workstream name from relay: line in session context>")`. If no workstream is attached, call `get_status()` with no arguments. It returns pre-formatted markdown.

2. **Display.** Output the result directly. Do not reformat or restructure it.
