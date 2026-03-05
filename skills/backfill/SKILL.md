---
name: backfill
description: >
  Generate session hints for older sessions that don't have them. Interactive — Claude reads conversations and writes structured summaries.
  Trigger phrases: "backfill hints", "generate summaries", "backfill sessions".
argument-hint: "<time-range: 7d, 30d, all>"
---

# Backfill Session Hints

Generate session hint files for sessions that don't have them yet. This reads conversation content and writes structured summaries — a one-time cost that makes future `/relay:summarize` calls nearly free.

**Argument:** `$ARGUMENTS` is a time range. If empty, default to 7d.

## Steps

1. **Parse time argument.** Same as `/relay:summarize` — convert `$ARGUMENTS` to an ISO date for `DATE_FROM`.

2. **Fetch sessions.** Use the `list_sessions` MCP tool:
   ```
   list_sessions(date_from=DATE_FROM, limit=100)
   ```
   If no sessions found, tell the user and stop.

3. **Check which sessions need hints.** Call:
   ```
   get_session_summaries(session_ids=[...])
   ```
   Filter to sessions where `hints_available` is `false`. If all sessions have hints, tell the user "All sessions in this range already have hints" and stop.

   Report: "Found N sessions without hints (M already have hints). Proceeding with backfill."

4. **For each session without hints:**

   a. **Read the session marker** to find the workstream:
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/read-data-file.sh" "session-markers/<session_id>.json"
      ```
      If no marker, use "other" as the workstream.

   b. **Read the conversation:**
      ```
      get_conversation(session_id, roles=["user", "assistant"], limit=50, format="markdown")
      ```

   c. **Write the hint file.** Based on the conversation content, write a hint:
      ```bash
      bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "session-hints/<timestamp>-<session_id>.json" << 'EOF'
      {
        "session_id": "<session_id>",
        "workstream": "<workstream from marker>",
        "summary": [
          "<3-6 bullets describing what was accomplished>",
          "<focus on capabilities, features, decisions — not counts>"
        ],
        "decisions": [
          "<key decisions, if any>"
        ]
      }
      EOF
      ```
      Use the session's `last_timestamp` (converted to filename format) as the timestamp.

   d. **Report progress:** "Wrote hint for session `<slug>` (<date>)"

   e. If the session clearly spans multiple workstreams (e.g., user switched workstreams mid-session), write separate hint files for each segment — use different timestamps in the filenames.

5. **Summary.** Report: "Backfill complete. Generated hints for N sessions."

## Notes

- This is intentionally interactive — Claude reads and synthesizes. It costs tokens but produces high-quality hints.
- Skip sessions with very few messages (< 5) — they're usually session starts or quick checks, not worth summarizing.
- For sessions that had context compaction, the conversation may be incomplete — do your best with what's available.
- If a session is an execution session (user messages are mostly "continue", "yes", "1"), focus on the assistant's outcome messages for the summary bullets.
