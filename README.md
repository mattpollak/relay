<p align="center">
  <img src="logo.png" alt="relay logo" width="120">
</p>

<h1 align="center">relay</h1>

<p align="center">Pass context between Claude Code sessions like a baton.</p>

## The Problem

Claude Code sessions are isolated. Every time you start a new one:

- **You re-explain everything.** "We're building an auth system. We chose JWT over sessions because..." — for the third time this week.
- **You lose decisions.** Yesterday Claude helped you evaluate three caching strategies. You picked one. Today, neither of you remembers why.
- **Project switching is manual.** You're deep in a backend refactor, need to fix a quick frontend bug, and now you're copy-pasting context between windows.
- **Your history is gone.** Two weeks ago Claude wrote a brilliant database migration pattern. It's buried in a JSONL transcript file somewhere. Good luck finding it.

## What relay Does

relay gives Claude Code persistent state that survives across sessions, a way to switch between projects without losing your place, and full-text search across every conversation you've ever had.

**Your workstream loads automatically.** When you start a session, relay injects your active workstream's state — what you were working on, decisions made, next steps. Claude picks up where you left off without being told.

**Project switching takes one command.** `/relay:switch database-refactor` saves your current workstream, loads the other project's state, and you're working in seconds. Both workstreams stay active — run them in parallel across multiple Claude instances if you want.

**Nothing gets lost.** Every conversation is indexed and searchable. Find that auth pattern from last week, that deployment fix from two weeks ago, or every UX review you've ever done — across all your projects.

## See It in Action

**Starting a session** — your workstream state is already loaded:
```
relay: Active workstream 'api-refactor'
---
# api-refactor — REST API Modernization

## Current Status
Completed endpoint migration for /users and /teams. 47 tests passing.

## Key Decisions
- Using Express middleware for auth (not per-route)
- Pagination via cursor, not offset

## Next Steps
1. Migrate /projects endpoints
2. Add rate limiting middleware
---
```

**Switching projects mid-session:**
```
> /relay:switch frontend-redesign

Saved api-refactor state. Switched to frontend-redesign.

# frontend-redesign — Dashboard Overhaul
## Current Status
Navigation component complete. Working on data tables...
```

**Finding something from weeks ago:**
```
> "How did we handle the database connection pooling issue?"

[Claude searches your history and finds the exact session]
```

**Saving before you go:**
```
> /relay:save

Saved workstream state, backup created, session hint written.
```

## Concepts

relay introduces one new concept — **workstreams** — and builds on two that Claude Code already has:

| Concept | What it is | Lifespan |
|---|---|---|
| **Session** | A single Claude Code conversation (start to exit). Has a UUID and a slug. When you "continue" a conversation, that's a new session in the same slug chain. | One conversation |
| **Context window** | Everything Claude can currently see — system prompt, conversation history, tool results, injected state. Gets compressed (compacted) when it fills up. Gone when the session ends. | During a session |
| **Workstream** | A named project or task with a persistent state file (`state.md`). relay loads it into the context window at session start, and saves it back to disk when you're done. This is what survives across sessions. | Until you complete or delete it |

The relationship:

```
workstream (persistent — lives on disk)
  └── loaded into context window (ephemeral — lives during a session)
       └── inside a session (one conversation, has a UUID)
```

relay bridges the gap between the ephemeral context window and persistent workstream state. When a session ends or context gets compacted, your workstream state has already been saved to disk. The next session loads it back in automatically.

## Key Features

- **Auto-loaded workstream** — every session starts with your workstream state already in the context window
- **One-command switching** — save current workstream, load another, keep both active
- **Multi-instance support** — run parallel Claude instances on different workstreams without conflicts
- **Compaction protection** — warnings as the context window fills up, with a prompt to save before compression
- **Full conversation search** — every transcript indexed into searchable SQLite FTS5 across all projects
- **Auto-tagging** — messages classified by type (UX reviews, architecture decisions, plans, debugging) for easy filtering
- **Activity summaries** — `/relay:summarize 7d` for standup prep, brag books, or catching up after time away
- **Idea capture** — `/relay:idea` to jot down future work without losing your flow

## Prerequisites

- **Claude Code** v2.1.0+ (plugin system support)
- **`jq`** — JSON parser, required for hook scripts
  ```bash
  # Ubuntu/Debian
  sudo apt install jq

  # macOS
  brew install jq

  # Other: https://jqlang.github.io/jq/download/
  ```
- **Python 3.10+** and **`uv`** — Required for the MCP conversation search server

## Installation

```bash
# Add the marketplace
claude plugin marketplace add mattpollak/relay

# Install the plugin
claude plugin install relay@relay
```

To verify it's installed:
```bash
claude plugin list
```

Start a new Claude Code session — you should see the SessionStart hook fire. If no workstreams exist yet, it will prompt you to create one.

### Permissions

Core workstream operations (save, create, park, switch, list, ideas) are handled by MCP tools — **no bash permission prompts needed**. Hooks (session start, context monitor, pre-compact) still run as bash scripts and are automatically approved by a bundled `PreToolUse` hook (`scripts/approve-scripts.sh`).

### Updating

```bash
# Pull latest from the marketplace
claude plugin marketplace update relay

# Update the plugin
claude plugin update relay@relay
```

Restart Claude Code after updating to apply changes.

### Testing without installing

```bash
claude --plugin-dir /path/to/relay
```

## Usage

### Slash commands

| Command | What it does |
|---|---|
| `/relay:status` | Show active workstream, other workstreams, and available commands |
| `/relay:new api-refactor Modernizing the REST API` | Create a new workstream |
| `/relay:list` | List all workstreams grouped by status |
| `/relay:save` | Save current workstream state to disk |
| `/relay:switch database-refactor` | Save current, load a different workstream |
| `/relay:park` | Save and deactivate the current workstream |
| `/relay:idea use websockets for real-time` | Capture an idea for future work (shown in `/relay:list`) |
| `/relay:idea promote 2` | Promote an idea to a full workstream |
| `/relay:summarize 48h` | Summarize recent activity grouped by workstream (standup prep, brag books) |
| `/relay:backfill 7d` | Generate session hints for older sessions (one-time cost for efficient future summaries) |

### Natural language

The skills also respond to natural language:
- "new workstream", "start workstream", "create workstream"
- "switch to X", "resume workstream", "work on X"
- "save state", "save workstream", "save session"
- "park this", "park workstream", "pause workstream"
- "list workstreams", "show workstreams"
- "relay status", "workstream status", "what am I working on"
- "add idea", "jot down", "remember this idea"
- "summarize activity", "what did I work on", "standup summary", "brag book"
- "backfill hints", "generate summaries", "backfill sessions"

### MCP tools

The MCP server provides tools that Claude uses directly during your session — both for conversation search and workstream management:

**Workstream management:**

| Tool | What it does |
|---|---|
| `save_workstream` | Atomically save state file (with backup), update registry, write session hint + marker to DB — all in one call |
| `create_workstream` | Create a new workstream: add to registry, write initial state file |
| `park_workstream` | Save state and set workstream status to parked |
| `switch_workstream` | Save current workstream, activate target, write session marker, return target state |
| `list_workstreams` | List all workstreams grouped by status (active, parked, completed) plus ideas |
| `manage_idea` | Add, remove, or list ideas for future work |
| `summarize_activity` | Summarize recent activity grouped by workstream — writes markdown to file, returns path + overview |

**Conversation search:**

| Tool | What it does |
|---|---|
| `search_history` | Full-text search across all conversations (FTS5: AND, OR, NOT, "phrases"). Results include `session_number` showing position in slug chain. |
| `get_conversation` | Retrieve messages from a session by UUID or slug. Slug chains (via "continue") return all sessions combined chronologically. Use `session` param to filter to specific sessions (e.g. `"4"`, `"2-3"`, `"1,4"`). |
| `list_sessions` | List recent sessions with metadata, filterable by project, date, and tags. Use `slug` param to get a session index with `session_number` fields. |
| `tag_message` | Manually tag a message for future discoverability |
| `tag_session` | Manually tag a session (e.g., associate with a workstream) |
| `list_tags` | List all tags with counts — see what's been auto-detected |
| `get_session_summaries` | Get pre-written session summaries (hint segments with bullets and decisions) |
| `reindex` | Force a complete re-index from scratch |

**Session-level addressing:** When a conversation spans multiple sessions (via "continue"), you can address specific sessions:
- `list_sessions(slug="my-conversation")` — returns a session index with `session_number` for each session
- `get_conversation("my-conversation", session="4-5")` — retrieves only sessions 4 and 5
- `search_history("ledger")` — results include `session_number` so you know which session each hit is in

**Tag filtering:** Both `search_history` and `list_sessions` accept an optional `tags` parameter to narrow results. For example, searching for "splash page" with `tags: ["review:ux"]` returns only UX review messages that mention splash page.

**Auto-tags applied during indexing:**

| Tag | What it detects |
|---|---|
| `review:ux` | Substantial UX/usability review content |
| `review:architecture` | Architecture or system design reviews |
| `review:code` | Code quality reviews |
| `review:security` | Security reviews or audits |
| `plan` | Implementation plans (plan-mode messages or structured phase/implementation docs) |
| `decision` | Architectural or approach decisions |
| `investigation` | Root cause analysis and debugging findings |
| `insight` | Messages with `★ Insight` markers |
| `has:browser` | Session used browser/Playwright tools |
| `has:tests` | Session ran tests (pytest, vitest, etc.) |
| `has:deploy` | Session involved deployment (ssh, docker, etc.) |
| `has:planning` | Session used Claude Code's plan mode |

## Data Storage

Configuration lives at `${XDG_CONFIG_HOME:-$HOME/.config}/relay/`:

```
~/.config/relay/
├── relay.json                    # Server-level config (optional — see below)
├── workstreams.json              # Central registry
├── ideas.json                    # Pre-workstream ideas (shown in /relay:list)
├── session-markers/              # Links session IDs to workstreams (written by hooks; also stored in DB by MCP tools)
│   └── <session-id>.json
├── session-hints/                # Pre-written session summaries (legacy file path; MCP tools write directly to DB)
│   └── <timestamp>-<session-id>.json
└── workstreams/
    ├── api-refactor/
    │   ├── state.md              # ~80 lines, auto-loaded on session start
    │   ├── state.md.bak          # One-deep backup (previous version)
    │   ├── plan.md               # Optional, loaded on /switch
    │   └── architecture.md       # Optional, loaded on /switch
    └── ...
```

The conversation search index lives at `~/.local/share/relay/index.db` (SQLite, WAL mode).

Activity summaries are written to `~/.local/share/relay/summaries/` by default (configurable — see below).

### Configuration

Optional server-level settings in `~/.config/relay/relay.json`:

```json
{
  "summary_dir": "~/Documents/relay-summaries"
}
```

| Key | What it does | Default |
|---|---|---|
| `summary_dir` | Directory for `/relay:summarize` output files | `~/.local/share/relay/summaries` |

The file is optional — relay works fine without it. Missing or malformed files are silently ignored. The `output_dir` parameter on `summarize_activity` overrides the config value.

## How It Works

### Hooks

| Hook | Event | What it does |
|---|---|---|
| `session-start.sh` | SessionStart | Reads registry, injects workstream state into the context window. Auto-attaches if one active workstream; prompts for choice if multiple are active. Writes session marker. |
| `context-monitor.sh` | PostToolUse | Counts tool calls, warns at 80 and 100 that the context window is filling up |
| `pre-compact-save.sh` | PreCompact | Prompts Claude to save workstream state before context compression |
| `session-end.sh` | SessionEnd | Cleans up temp files, updates `last_touched` timestamp |
| `approve-scripts.sh` | PreToolUse | Auto-approves Bash commands targeting plugin scripts (no user prompt) |

### State files

State files (`state.md`) are kept under 80 lines and contain:
- Current status
- Key decisions
- Next steps
- Recent session summaries (if space permits)

Saves are handled by the `save_workstream` MCP tool, which atomically writes the state file (with `.bak` backup), updates the registry, and writes session hints and markers to the database — all in one call.

### MCP server

The MCP server handles both workstream management and conversation search. Workstream operations (save, create, park, switch, list, ideas) use atomic file writes and SQLite transactions — each skill invocation is a single MCP call instead of multiple bash scripts.

On startup, the server scans `~/.claude/projects/` for JSONL transcript files and incrementally indexes them into SQLite FTS5. First run takes 3-5 seconds; subsequent runs process only new/modified files (~0.01s). Auto-tagging runs during indexing — keyword heuristics classify messages by content type (reviews, plans, decisions, etc.) and sessions by activity (testing, deployment, browser usage).

**Cross-project indexing** — The search index covers every Claude Code conversation across all your projects, not just the current one. Search for that auth pattern you figured out in project A while working in project B. Use the `project` parameter on `search_history` or `list_sessions` to narrow results to a specific project.

## Complementary Systems

relay handles **workstream state** (what you're working on, decisions made, next steps). It complements, not replaces, Claude Code's built-in systems:

| System | Purpose | Example |
|---|---|---|
| Auto-memory (`MEMORY.md`) | Learnings about the codebase | "Use TypeORM migrations for schema changes" |
| `CLAUDE.md` | Instructions for Claude | "Run tests with `npm test` before committing" |
| **relay** | Workstream state + project switching + history search | "Working on auth migration, next: add OAuth" |

## Migrating from Manual Workstreams

If you have - and you almost certainly don't, unless you're me - an existing manual workstream system with a `WORKSTREAMS.md` registry:

```bash
# Preview what the migration will do
bash /path/to/relay/scripts/migrate-from-workstreams.sh --dry-run

# Run the migration
bash /path/to/relay/scripts/migrate-from-workstreams.sh
```

The migration is non-destructive — it copies files to the new location without deleting originals.

## Inspired By

- [Episodic Memory](https://github.com/obra/episodic-memory) — conversation archival and search
- [Get Shit Done](https://github.com/gsd-build/get-shit-done) — context monitoring and lean state files
- [CASS Memory System](https://github.com/Dicklesworthstone/cass_memory_system) — structured knowledge accumulation

See [ATTRIBUTION.md](./ATTRIBUTION.md) for details.

## License

MIT — see [LICENSE](./LICENSE).
