# relay-server

MCP server for searching Claude Code conversation history. Indexes JSONL transcript files and provides full-text search via SQLite FTS5.

## Tools

- **search_history** — Full-text search across all indexed conversations. Results include `session_number` within slug chains.
- **get_conversation** — Retrieve messages from a specific session. Supports `session` param for filtering multi-session slugs (e.g. `"4"`, `"2-3"`, `"1,4"`).
- **list_sessions** — List recent sessions with metadata. Use `slug` param to get a session index with numbered sessions.
- **tag_message** — Manually tag a message for future discoverability
- **tag_session** — Manually tag a session (e.g., associate with a workstream)
- **list_tags** — List all tags with counts, filterable by scope (message/session/all)
- **get_session_summaries** — Get pre-written session summaries (hint segments with summary bullets and decisions)
- **reindex** — Force a complete re-index from scratch

## Usage

```bash
# Run directly (starts stdio MCP server)
cd server && uv run relay-server

# Or via python -m
cd server && uv run python -m relay_server
```

## How it works

On startup, the server scans `~/.claude/projects/` for JSONL transcript files. It incrementally indexes new/modified files into a SQLite database at `~/.local/share/relay/index.db`. Subsequent startups only process new or grown files.

The index stores:
- **User messages** — actual human input (not tool results)
- **Assistant text** — Claude's responses (not thinking blocks)
- **Tool summaries** — what tools were called and with what arguments
- **Plans** — `planContent` from plan-mode entries

Messages are searchable via FTS5 full-text search with support for AND, OR, NOT, and phrase queries.
