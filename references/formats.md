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

## relay.json sync (optional)

Absent means sync is off and relay is local-only. Written by `/relay:sync setup` (`manage_sync`); invalid values are treated as off.

```json
"sync": {
  "enabled": true,
  "backend": "folder",
  "folder": "/abs/path/to/shared/dir",
  "scope": "state",
  "configured_at": "2026-11-20T12:00:00Z"
}
```

- **enabled** — `true`/`false`. `/relay:sync disable` sets `false` and keeps the rest; `disable --forget` removes the block.
- **backend** — `folder` (a shared or synced directory; uses `folder`) or `http` (self-hosted mailbox; uses `url`, https only except localhost — not available yet).
- **scope** — `state` (workstreams, state files, ideas, hints, tags, git snapshots) or `state+messages` (also conversation messages, for cross-machine search).
- Hooks check `.sync.enabled == true` with `jq` and only then run `uv run --no-dev --extra sync … python -m relay_server.sync pull|push` (pull in the foreground at session start, capped by `RELAY_SYNC_PULL_TIMEOUT`, default 5 s; pushes in the background). Output goes to `sync.log`.

## Sync keys and files

- **`sync.key`** — this machine's Ed25519 private key (hex seed, mode 0600). Signs everything the machine writes; its X25519 form receives batches. Never leaves the machine.
- **`sync-notices.json`** — written after each pull for the SessionStart hook: `{"workstreams": {name: [lines]}, "requests": [lines], "role", "error"}`.
- `msgb/<host_id>/<session_id>/<first-message-key>` (`msg.batch`, scope `state+messages`): `{"session": {session_id, project_dir, slug, git_branch, cwd}, "messages": [{key, role, content, timestamp, model}]}`, at most 400 messages / 512 KB each. Receivers insert them into `index.db` as sessions with `sessions.origin_host` = the sender (NULL = this machine); `reindex` restores them. `events.db` table `sync_msg_sent` lists message keys already shared; `meta.sync_msg_cursor` is the last `messages.id` packed.
- `redact/<id>` (`msg.redact`): `{"events": {event_id: scrubbed payload}, "messages": [{session_id, role, timestamp, old_key, content}]}`. Also `"fingerprints": [{"salt", "len", "h"}]` — a keyed BLAKE2b hash of the redacted text (never the text). Every machine stores fingerprints in `events.db` `meta.redaction_fingerprints` and scrubs messages indexed after `meta.redact_scan_id` (at startup, after `reindex`, before packing messages, after receiving a redaction). Receivers apply a scrubbed payload only if it equals the old one with parts replaced by `[redacted]`, then rewrite their own batch files (`-r<time>-<n>` suffix) and `.bak`/conflict copies. Re-applied to local messages after `reindex`.
- Extra event-log keys with sync: `wsdir/<host_id>/<name>` (`ws.local`: per-machine `project_dir`, worktree path, stash ref — kept out of the shared `ws/` value) and `git/<host_id>/<workstream>` (`git.snapshot`). Idea values don't include the per-machine `id`.
- Local sync state lives in `events.db`: `meta` keys `sync_pin` (the founder this machine created or the user confirmed by fingerprint), `sync_records` (local copies of every membership record this machine accepted — re-applied even if deleted from the folder), `sync_accepted`, `sync_pushed`, `sync_recipients`, `sync_last_push|pull|error`, and table `sync_seen` (batches already processed).

## Shared sync folder

One flat directory — no subdirectories. Everything shared is an encrypted, signed batch of events; workstream, data type and IDs are inside the ciphertext. Only the records a machine needs before it can decrypt are plaintext (signed):

```
relay-sync.json                                              {"format": 2, "app": "relay"}
<by>.member.<ms>-<founder|approve|revoke>-<subject>.json     signed membership records
<host>.request.json                                          self-signed join request (removed by its owner once approved)
<host>.batch.<last-event-id>[-h<id>|-r<time>][-<n>].json     encrypted batches (-h: history for a new member; -r: rewritten after a redaction; -<n>: continuation, max 1000 events / ~4 MB per file)
```

Every name starts with the machine that writes it, so file-syncing services never see two machines edit one file. The folder is treated as untrusted input:

- **Create, never overwrite:** written under a temporary dot-name with `O_EXCL|O_NOFOLLOW`, renamed into place only if the name is free.
- **Delete only own, unchanged files:** `events.db` table `sync_written` records each file this machine wrote (name, SHA-256); a file is removed only if listed and unchanged. Other machines' files are never deleted.
- **Read only regular files** (`O_NOFOLLOW|O_NONBLOCK`, `fstat`), at most 64 MB. Symlinks, FIFOs and devices are ignored; the folder itself must not be a symlink.

- **Membership:** the founder record is self-signed; each approve/revoke must be signed by a current member. A joining machine pins the founder only after the user confirms its fingerprint (`trust_fingerprint`), and sends nothing until it's approved. Records seen for the first time are checked against the *current* membership (a revoked machine can't back-date an approval), and accepted records are kept locally (deleting a revoke record from the folder doesn't undo it).
- **Batch:** `{"v": 1, "from", "created", "first", "last", "count", "wraps": {host_id: SealedBox(file_key)}, "ct": SecretBox(file_key, body), "sig"}`. `body` is one format byte (`0` raw, `1` zlib) + JSON list of events. `sig` is Ed25519 over the canonical JSON of the other fields; receivers verify it before decrypting.
- **Merge:** a key's value is the fold of all its events in event-ID order. Concurrent edits of `state.md` / `plan.md` / `architecture.md` keep the newer one and save the other as `<file>.conflict-<machine>-<time>.md`. Events more than a day in the future are refused.
