<p align="center">
  <img src="logo.png" alt="relay logo" width="120">
</p>

<h1 align="center">relay</h1>

<p align="center">Pass context between Claude Code sessions like a baton.</p>

**The problem:** Claude Code sessions are isolated. Each one starts from scratch — you re-explain your architecture, repeat decisions, and re-orient Claude on where you left off. Juggling multiple projects means the handoff happens in your head. And everything Claude helped you figure out? Lost in transcript files nobody can search.

**relay is the handoff.** It passes your working context from one session to the next — automatically loaded at startup, with prompts to save before compaction, and searchable across every conversation you've ever had. Switch between projects without dropping context.

## What You Get

- **Auto-loaded context** — Every session starts with your active workstream's state already in context. No re-explaining, no "let me catch you up."
- **One-command switching** — `/relay:switch auth-migration` saves your current context, loads the new project's state, and you're coding in seconds.
- **Context protection** — Warnings at ~80 and ~100 tool calls so you can save before compaction hits. PreCompact hook prompts a save before context is compressed.
- **Full conversation search** — MCP server indexes every Claude Code transcript into searchable SQLite FTS5. Find that architecture decision from two weeks ago.
- **Auto-tagging** — Messages tagged by content type (UX reviews, plans, decisions, investigations) so high-value content surfaces without remembering exact phrases.

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

When you save, switch, or park a workstream, Claude runs helper scripts bundled with the plugin (e.g., `complete-save.sh` to atomically rotate state files). A bundled `PreToolUse` hook automatically approves these commands — **no manual permission setup is needed.**

The hook (`scripts/approve-scripts.sh`) checks whether each Bash command targets a script inside the plugin's own `scripts/` directory. Only exact matches against `${CLAUDE_PLUGIN_ROOT}/scripts/` are approved; all other commands go through the normal permission flow.

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

### Natural language

The skills also respond to natural language:
- "new workstream", "start workstream", "create workstream"
- "switch to X", "resume workstream", "work on X"
- "save context", "save state", "save session"
- "park this", "park workstream", "pause workstream"
- "list workstreams", "show workstreams"
- "relay status", "workstream status", "what am I working on"
- "add idea", "jot down", "remember this idea"
- "summarize activity", "what did I work on", "standup summary", "brag book"

### Conversation search

The MCP server provides tools that Claude can use directly during your session:

| Tool | What it does |
|---|---|
| `search_history` | Full-text search across all conversations (FTS5: AND, OR, NOT, "phrases"). Results include `session_number` showing position in slug chain. |
| `get_conversation` | Retrieve messages from a session by UUID or slug. Slug chains (via "continue") return all sessions combined chronologically. Use `session` param to filter to specific sessions (e.g. `"4"`, `"2-3"`, `"1,4"`). |
| `list_sessions` | List recent sessions with metadata, filterable by project, date, and tags. Use `slug` param to get a session index with `session_number` fields. |
| `tag_message` | Manually tag a message for future discoverability |
| `tag_session` | Manually tag a session (e.g., associate with a workstream) |
| `list_tags` | List all tags with counts — see what's been auto-detected |
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

Data is stored at `${XDG_CONFIG_HOME:-$HOME/.config}/relay/`:

```
~/.config/relay/
├── workstreams.json              # Central registry
├── ideas.json                    # Pre-workstream ideas (shown in /relay:list)
├── session-markers/              # Links session IDs to workstreams (auto)
│   └── <session-id>.json
└── workstreams/
    ├── api-refactor/
    │   ├── state.md              # ~80 lines, auto-loaded on session start
    │   ├── state.md.bak          # One-deep backup (previous version)
    │   ├── plan.md               # Optional, loaded on /switch
    │   └── architecture.md       # Optional, loaded on /switch
    └── ...
```

The conversation search index lives at `~/.local/share/relay/index.db` (SQLite, WAL mode).

## How It Works

### Hooks

| Hook | Event | What it does |
|---|---|---|
| `session-start.sh` | SessionStart | Reads registry, injects active workstream's `state.md` into context. Writes session marker linking session ID to active workstream. |
| `context-monitor.sh` | PostToolUse | Counts tool calls, warns at 80 and 100 |
| `pre-compact-save.sh` | PreCompact | Instructs Claude to save state before compression |
| `session-end.sh` | SessionEnd | Cleans up temp files, updates `last_touched` timestamp |
| `approve-scripts.sh` | PreToolUse | Auto-approves Bash commands targeting plugin scripts (no user prompt) |

### State files

State files (`state.md`) are kept under 80 lines and contain:
- Current status
- Key decisions
- Next steps
- Recent session summaries (if space permits)

Saves use an atomic three-step process: write new content to a temp file (`state.md.new`), back up the current file (`state.md` → `state.md.bak`, overwriting any previous backup), then rename the temp file into place (`state.md.new` → `state.md`). Each step overwrites its target, so stale files from a previous interrupted save are cleaned up automatically.

### MCP server

On startup, the server scans `~/.claude/projects/` for JSONL transcript files and incrementally indexes them into SQLite FTS5. First run takes 3-5 seconds; subsequent runs process only new/modified files (~0.01s). Auto-tagging runs during indexing — keyword heuristics classify messages by content type (reviews, plans, decisions, etc.) and sessions by activity (testing, deployment, browser usage).

**Cross-project indexing** — The search index covers every Claude Code conversation across all your projects, not just the current one. Search for that auth pattern you figured out in project A while working in project B. Use the `project` parameter on `search_history` or `list_sessions` to narrow results to a specific project.

## Complementary Systems

relay handles **session state** (what you're working on, where you last saved). It complements, not replaces, Claude Code's built-in systems:

| System | Purpose | Example |
|---|---|---|
| Auto-memory (`MEMORY.md`) | Learnings about the codebase | "Use TypeORM migrations for schema changes" |
| `CLAUDE.md` | Instructions for Claude | "Run tests with `npm test` before committing" |
| **relay** | Session state + task switching + history search | "Working on auth migration, next: add OAuth" |

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
