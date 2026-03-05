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

2. **Fetch sessions.** Use the `list_sessions` MCP tool:
   ```
   list_sessions(date_from=DATE_FROM, limit=100)
   ```
   If no sessions are found, tell the user there's no activity in that range and stop.

3. **Map sessions to workstreams.** For each session, read its marker file to find the workstream:
   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "session-markers/<session_id>.json"
   ```
   The marker JSON contains a `workstream` field. Sessions without markers (output is `NOT_FOUND`) go into an "Other" group.

4. **Fetch content for each session.** Use the `get_conversation` MCP tool to get conversation content:
   ```
   get_conversation(session_id, roles=["user", "assistant"], limit=50, format="markdown")
   ```

5. **Determine detail level.** Based on the total time span from `DATE_FROM` to today:
   - **≤ 3 days → Detailed:** 3-6 bullets per session, include specific outcomes (commits, files changed, decisions made)
   - **4-14 days → Standard:** 2-4 bullets per session
   - **15+ days → Overview:** 1-2 bullets per session, focus on themes and milestones

6. **Generate summary.** Output grouped by workstream, ordered by most sessions first. Format:

   ```
   ## Activity Summary: <start date> – <end date>

   ### <workstream-name> (<N> sessions)

   **Session <number>** — `<slug>` (<date>)
   - <bullet>
   - <bullet>

   **Session <number>** — `<slug>` (<date>)
   - <bullet>

   ### <other-workstream> (<N> sessions)
   ...

   ---
   *Use `get_conversation("<slug>")` to drill into any session.*
   ```

   Include the slug in each session header so the user (or Claude) can easily drill in later.

## Notes

- Prioritize **breadth over depth** — mention every session, even if briefly. Users want to know what happened, not miss sessions.
- Session markers provide the workstream grouping — this is what makes this more useful than raw `list_sessions`.
- The `roles: ["user", "assistant"]` filter skips tool_summary noise, keeping context manageable.
- For very long ranges with many sessions, summarize the oldest sessions more briefly and give more detail to recent ones.
