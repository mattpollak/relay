# relay Data Formats

## workstreams.json

Central registry of all workstreams. Parsed with `jq` by hook scripts.

```json
{
  "workstreams": {
    "<name>": {
      "status": "active" | "parked" | "completed",
      "description": "Short description",
      "created": "YYYY-MM-DD",
      "last_touched": "YYYY-MM-DD",
      "project_dir": "/absolute/path/to/project"  // optional
    }
  }
}
```

### Status values
- **active**: Currently being worked on. At most one workstream should be active.
- **parked**: Paused, can be resumed later.
- **completed**: Done. Kept for reference.

### Fields
- **status** (required): One of active/parked/completed
- **description** (required): Human-readable summary
- **created** (required): ISO date when created
- **last_touched** (required): ISO date of last activity
- **project_dir** (optional): Absolute path to the project's code directory

## state.md

Per-workstream state file. Target: ~80 lines max. Atomic overwrite with `.bak` backup.

### Required sections
```markdown
# <Workstream Name>

## Metadata
- **Description:** ...
- **Created:** YYYY-MM-DD
- **Project dir:** /path/to/project (if applicable)

## Current Status
What's happening right now. 2-3 sentences.

## Key Decisions
Bullet list of important choices made.

## Next Steps
Numbered list of what to do next.
```

### Optional sections
- **Architecture Notes** — inline notes (or pointer to architecture.md)
- **Blockers** — anything preventing progress
- **Recent Sessions** — last 2-3 sessions in condensed form (one line each)

## parking-lot.md

Cross-project ideas and future work. Simple bullet list.

```markdown
# Parking Lot

- **Project: Feature name** — description
- Standalone idea — description
- ~~Completed item~~ — DONE (when/where)
```

## Companion files (optional, per-workstream)

- **plan.md** — Implementation plan. Not auto-loaded; read on explicit `/relay:switch`.
- **architecture.md** — Architecture notes. Not auto-loaded; read on explicit `/relay:switch`.

## events.db

SQLite event log of authored data (`~/.config/relay/events.db`). Written by the MCP server only.

### Tables
- **events** — `id` (HLC: `<unix-ms 13 digits>-<counter 4 digits>-<host_id>`), `host_id`, `type`, `schema` (event format version, currently 1), `key`, `parent` (previous event ID for the same key), `wall_ts`, `source` (tool name, `external`, or `import`), `payload` (JSON)
- **heads** — current value per key, folded from events (`value` NULL = deleted)
- **meta** — `hlc` clock state, `migrated_at`, `migration_backup`

### Keys and event types

| Key | Created / changed | Deleted | Payload |
|---|---|---|---|
| `ws/<name>` | `ws.create`, `ws.update` | `ws.delete` | `{"value": entry}` or `{"set": {...}, "unset": [...]}` |
| `state/<name>` | `state.save` | `state.delete` | `{"value": "<markdown>"}` |
| `file/<name>/<plan.md\|architecture.md>` | `ws.file` | `ws.file.delete` | `{"value": "<markdown>"}` |
| `idea/<uid>` | `idea.add`, `idea.update` | `idea.delete` | idea object or field patch |
| `hint/<source_file>` | `hint.write` | — | session_id, workstream, timestamp, summary, decisions |
| `stag/<session_id>/<tag>` | `tag.session` | — | manual session tag |
| `mtag/<message_key>/<tag>` | `tag.message` | — | manual message tag; `message_key` is a hash of session, timestamp, role, content (local message IDs differ per machine) |
| `host/<host_id>` | `host.info` | — | hostname, nickname, platform, relay version |

Hints and tags are never deleted by reconciling, because `reindex` empties them from the search index.

## ideas.json

List of ideas: `{"id": 1, "uid": "3f9c0a7d12be4e05", "text": "...", "added": "YYYY-MM-DD"}`.
- **id** — small number shown to users (`/relay:idea promote <id>`), assigned per machine.
- **uid** — stable random identifier used by the event log. Added automatically to ideas that don't have one.

## relay.json host identity

- **host_id** — 6 random base32 characters, created on first run, never changes. Part of every event ID.
- **host_id_hostname** — hostname when the ID was created.
- **nickname** — optional display name; defaults to the hostname.
