---
name: summarize
description: >
  Summarize recent activity grouped by workstream. Use for standup prep, brag books, or catching up after time away.
  Trigger phrases: "summarize activity", "what did I work on", "standup summary", "brag book".
argument-hint: "<time-range: 48h, 7d, since Monday, 2026-03-01>"
---

# Summarize Activity

Generate a summary of recent Claude Code sessions grouped by workstream.

**Argument:** `$ARGUMENTS` is a natural-language time expression. If empty, default to 24h.

## Steps

1. **Parse time argument.** Interpret `$ARGUMENTS` as a time range and convert to an ISO date (`YYYY-MM-DD`). Examples:
   - `48h` → 2 days before today
   - `7d` or `1w` → 7 days before today
   - `since Monday` → last Monday's date
   - `2026-03-01` → literal date
   - Empty → 1 day before today (24h default)

   Store the computed date as `DATE_FROM` for the next step.

2. **Fetch summary.** Use the `summarize_activity` MCP tool:
   ```
   summarize_activity(date_from=DATE_FROM)
   ```
   This returns a pre-formatted markdown summary grouped by workstream, with session bullets, decisions, and metadata.

3. **Present the result.** Output the markdown directly to the user. No further formatting needed — the server handles grouping, deduplication, and layout.
