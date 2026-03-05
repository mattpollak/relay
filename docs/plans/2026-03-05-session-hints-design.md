# Session Hints — Efficient Summarization via Write-Time Metadata

## Context

The `/relay:summarize` skill currently fetches full conversation content for every session in the time range (~50 messages x N sessions), feeding it all through the main model to produce summaries. For a single day with 7 sessions, this means ~175K characters of input tokens — expensive and slow.

### Root cause

Summarization happens at **read time** — every time you run `/relay:summarize`, Claude re-reads and re-synthesizes the same conversations. The insight: Claude already has full context about what happened at save/park/switch time. If it writes a structured summary then, the summarize skill becomes a pure data assembly operation.

### Design goals

1. Reduce summarize token cost by 10x+ (from full conversation reads to pre-written metadata reads)
2. Support multi-segment sessions (one Claude Code session can span multiple topics/workstreams)
3. Backfill existing sessions with hints generated from conversation history
4. Store hints in the database for efficient server-side assembly

## Architecture: "Summarize on Write"

### Two layers

1. **Write-time hints** (primary) — Claude writes a small JSON hint file when it saves/parks/switches. Written by the Claude that did the work, with full context. Near-zero marginal cost since the tokens are already in context.

2. **Backfill extraction** (one-time + rebuild) — A skill that reads existing conversations and generates hints for sessions that lack them. Also used to rebuild/correct individual hints.

### Data flow

```
Save/Park/Switch time:
  Claude writes hint file → session-hints/<timestamp>-<session_id>.json

MCP server startup:
  Indexer scans session-hints/ → INSERTs into session_hints table (idempotent)

Summarize time:
  list_sessions() → get session IDs
  get_session_summaries(session_ids) → one DB query, returns all hints
  Format and display (no get_conversation calls)
```

## Hint File Format

Written to `$DATA_DIR/session-hints/` as write-once files. Filename includes timestamp for natural ordering and uniqueness (supports multiple segments per session):

```
session-hints/
  2026-03-05T043956Z-b3135334.json    ← first segment
  2026-03-05T044500Z-b3135334.json    ← second segment, same session
  2026-03-05T163000Z-b995fce8.json
```

File contents:

```json
{
  "session_id": "b3135334-b8f0-4a4a-bd49-3119ac80e8c8",
  "workstream": "squadkeeper",
  "summary": [
    "Executed I1 broadcast messaging plan (13 tasks)",
    "Message + MessageRecipient models with recipient snapshot pattern",
    "6 API endpoints: create with push, list, detail with auto-read, unread-count, read-status, dismiss",
    "AppBar notification bell with unread count badge",
    "12 backend + 9 frontend tests"
  ],
  "decisions": [
    "Separated ownership (team_id) from delivery (MessageRecipient) for I2/I4 flexibility",
    "Added read_source tracking for push/in_app/email analytics"
  ]
}
```

Fields:
- `session_id` (required) — UUID of the Claude Code session
- `workstream` (required) — which workstream this segment belongs to
- `summary` (required) — array of 3-6 bullet strings describing what was accomplished
- `decisions` (optional) — array of key architectural/design decisions made

## Database Schema

```sql
CREATE TABLE session_hints (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,           -- from filename, used for ordering segments
    source_file TEXT NOT NULL UNIQUE,  -- filename for idempotent re-indexing
    workstream TEXT NOT NULL,
    summary TEXT NOT NULL,             -- JSON array of bullet strings
    decisions TEXT,                     -- JSON array, nullable
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX idx_session_hints_session ON session_hints(session_id);
CREATE INDEX idx_session_hints_workstream ON session_hints(workstream);
```

Multiple rows per `session_id` = multiple segments. The `source_file` UNIQUE constraint makes re-indexing idempotent — if a file has already been indexed, skip it.

## MCP Server Changes

### New tool: `get_session_summaries`

```python
@mcp.tool()
def get_session_summaries(
    session_ids: list[str],
    ctx: Context,
) -> list[dict]:
    """Get pre-written session summaries for efficient summarization.

    Returns all hint segments for the given sessions, ordered by timestamp.
    Sessions without hints return an entry with hints_available: false.
    """
```

Returns:
```json
[
  {
    "session_id": "b3135334-...",
    "hints_available": true,
    "segments": [
      {
        "workstream": "squadkeeper",
        "timestamp": "2026-03-05T04:39:56Z",
        "summary": ["bullet1", "bullet2"],
        "decisions": ["decision1"]
      }
    ]
  },
  {
    "session_id": "abc-...",
    "hints_available": false,
    "segments": []
  }
]
```

Sessions with `hints_available: false` tell the summarize skill it needs to fall back to reading the conversation (or prompt the user to run the backfill).

### Indexer changes

Add a `_index_session_hints()` function called during startup indexing (alongside conversation indexing). Scans `$DATA_DIR/session-hints/`, parses each JSON file, INSERTs into `session_hints` table. Skips files already indexed (by `source_file` unique constraint).

## Skill Changes

### Save/Park/Switch skills — add hint writing step

After writing `state.md`, add a new step:

> **Write session hint.** Write a session hint file summarizing what was accomplished in this session segment. Include 3-6 summary bullets (focus on capabilities added, features built, decisions made — not task counts or commit hashes) and any key decisions.
>
> ```bash
> bash "${CLAUDE_PLUGIN_ROOT}/scripts/write-data-file.sh" "session-hints/<timestamp>-<session_id>.json" << 'EOF'
> <JSON content>
> EOF
> ```
>
> Use the current session ID from the environment and the current UTC timestamp formatted as `YYYY-MM-DDTHHMMSSZ`.

### PreCompact hook instructions — add hint writing

The PreCompact hook already instructs Claude to save state. Add instructions to also write a session hint before context is compressed.

### Updated summarize skill

1. `list_sessions(date_from=...)` → get session IDs
2. `get_session_summaries(session_ids)` → one query, all hints
3. Read session markers for any sessions without hints (workstream mapping fallback)
4. For sessions with `hints_available: false` — note them as "no summary available" or use a brief fallback (session metadata only: project, duration, message count)
5. Group by workstream, format, display

### New skill: `/relay:backfill`

Interactive skill for generating hints from existing conversations:

1. `list_sessions(date_from=<range>, limit=100)` — find sessions in range
2. `get_session_summaries(session_ids)` — check which already have hints
3. For each session without hints:
   a. `get_conversation(session_id, roles=["user", "assistant"], limit=50)` — read conversation
   b. Write hint file with summary bullets and decisions
   c. Confirm to user
4. Report: "Generated hints for N sessions, M already had hints"

This is intentionally interactive (Claude reads and synthesizes) rather than automated. It costs tokens but produces high-quality hints. Run once for the backfill, then occasionally for rebuilds.

## Session Markers

Session markers (`session-markers/<session_id>.json`) remain separate from hints. They serve different purposes:

- **Markers**: Written at session start by bash hook. Map session_id → workstream. Written before any work happens.
- **Hints**: Written at save/park/switch by Claude. Contain summary of work done. Written after work happens.

The hint's `workstream` field provides redundancy — if a session has a hint, the marker lookup can be skipped. But markers are still needed for sessions that never get a hint (abandoned sessions, quick questions, etc.).

## Testing

- Indexer: test parsing hint files, idempotent re-indexing, malformed file handling
- Server: test `get_session_summaries` with hints present, absent, and multi-segment
- Schema: test migration creates table and indices
- Integration: verify hint files written by save skill are picked up by indexer

## Verification

After implementation:
1. Run `/relay:save` → verify hint file is written
2. Restart Claude Code → verify indexer picks up the hint
3. Run `/relay:summarize` → verify it uses hints instead of full conversation reads
4. Run `/relay:backfill 7d` → verify it generates hints for older sessions
