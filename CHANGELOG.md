# Changelog

## [0.11.6] - 2026-03-12

### Changed
- **Skills → commands migration** — Moved `list`, `status`, `summarize`, `switch`, `new`, `backfill`, and `setup-relay` from skills to commands. These are user-initiated actions that don't benefit from auto-invocation. `save`, `park`, and `idea` remain as skills since Claude can proactively recognize when to use them.
- **Updated README** — Natural language section reflects which operations are auto-invocable skills vs explicit commands. MCP tool descriptions updated for server-side markdown formatting.

## [0.11.5] - 2026-03-12

### Changed
- **Server-side markdown formatting** — `list_workstreams` and `summarize_activity` MCP tools now return pre-formatted markdown by default, eliminating LLM token spend on JSON→markdown conversion. Pass `format="json"` for structured data.
- **`summarize_activity` inline display** — Short summaries (≤200 lines) are returned inline; longer summaries return an overview with file path. File is always written regardless of length.
- **Simplified list and summarize skills** — Skills now display server output directly instead of containing rendering instructions.

## [0.11.4] - 2026-03-11

### Changed
- **Save and park skills** — No longer call `list_workstreams` to find the active workstream. The name is already in the session context, saving a round trip. Fixes unnecessary MCP call that was especially wasteful near context compaction.

## [0.11.3] - 2026-03-09

### Fixed
- **Reindex foreign key failure** — `reindex` now stashes manual session tags before clearing tables and restores them after rebuilding. Previously, deleting sessions with `PRAGMA foreign_keys=ON` failed because manual `session_tags` entries still referenced them.

## [0.11.2] - 2026-03-09

### Fixed
- **`CLAUDE_PLUGIN_ROOT` not available in Claude's shell** — When multiple workstreams are active, `session-start.sh` now resolves `CLAUDE_PLUGIN_ROOT` at hook execution time and embeds the actual path in the context message, instead of leaving the variable for Claude's shell (where it's not set). Fixes `No such file or directory` error when attaching to a workstream.

## [0.11.1] - 2026-03-06

### Fixed
- **Statusline with multiple active workstreams** — Uses session-specific mapping files (`session-workstreams/<session_id>`) to track which workstream each session is attached to, instead of scanning the registry for a single active workstream. Fixes blank statusline/title when multiple workstreams are active.
- **Statusline config efficiency** — Combined two `jq` calls for reading `terminal_color`/`terminal_title` into one.

### Changed
- **`attach-workstream.sh`** — Writes session-workstream mapping on attach.
- **`switch_workstream` MCP tool** — Writes session-workstream mapping on switch.
- **`session-end.sh`** — Cleans up session-workstream mapping on exit.

## [0.11.0] - 2026-03-06

### Added
- **Terminal decoration** — Per-workstream background color and tab title via Claude Code's `statusLine` setting. Colors auto-generated from workstream name hash, with optional manual override via `"color"` field in registry. Supports Windows Terminal (OSC 0/11), iTerm2 (AppleScript + OSC 11), Ghostty, and other xterm-compatible terminals.
- **`update_workstream` MCP tool** — Update description, project_dir, and/or color on an existing workstream. Pass empty string for color to remove override.
- **`/relay:setup-relay` skill** — Interactive guided setup for terminal decoration preferences and summary report directory. Detects terminal and shell, handles existing statusLine configs, saves preferences to `relay.json`.
- **`scripts/statusline.sh`** — Bundled statusline script. Reads `terminal_color` and `terminal_title` preferences from `relay.json`. Shortens `$HOME` paths to `~`.
- **`create_workstream`** — Now accepts optional `color` parameter for background color.
- **`/relay:new` skill** — Asks about custom color when terminal decoration is configured.

## [0.10.3] - 2026-03-06

### Changed
- **`summarize_activity` MCP tool** — Writes full markdown to a file and returns `{file, overview, date_range}` instead of raw markdown. Prevents 50KB+ responses from flooding the context window.
- **Summary output directory** — 3-tier config: `output_dir` param > `summary_dir` in `~/.config/relay/relay.json` > default `~/.local/share/relay/summaries/`.

### Added
- **`~/.config/relay/relay.json`** — Server-level config file. Gracefully handles missing or malformed files.

## [0.10.2] - 2026-03-06

### Changed
- **`/relay:summarize` skill** — Writes full summary to `/tmp/relay-summary-<date>.md` instead of displaying inline. Large summaries (50KB+) were being truncated. Shows brief overview (workstream names + session counts) inline with file path.

### Added
- **`CLAUDE.md`** — Project instructions including versioning rules (which files to bump, semver guidelines).

## [0.10.1] - 2026-03-06

### Added
- **`fix_other_hints` MCP tool** — Re-attributes session hints tagged `workstream='other'` using project-dir inference and slug chain propagation. Idempotent — safe to run repeatedly. Workflow: run `fix_other_hints`, tag remaining unknowns with `tag_session(id, ["workstream:X"])`, run again.
- **Project-dir inference** — `summarize_activity` now infers workstream from session `project_dir` using longest-prefix match against the registry. Prefers non-completed workstreams for disambiguation.
- **Slug chain propagation** — If any session in a slug chain has a known workstream (from hints, markers, or `workstream:*` session tags), all `other` sessions in the chain inherit it — both in `fix_other_hints` (data fix) and `summarize_activity` (query-time).

### Fixed
- **`_read_marker_workstream`** — Catches `OperationalError` when `session_markers` table doesn't exist (pre-v0.10.0 databases), falls back to file markers.

## [0.10.0] - 2026-03-05

### Added
- **`save_workstream` MCP tool** — Atomically saves state file (with backup), updates registry, writes session hint and marker to DB. Replaces 5 bash script calls with 1 MCP call.
- **`create_workstream` MCP tool** — Creates new workstream: adds registry entry, writes initial state file.
- **`park_workstream` MCP tool** — Saves state then sets workstream to parked.
- **`switch_workstream` MCP tool** — Saves current workstream, activates target, writes session marker, returns target state. Replaces 7+ bash calls with 1.
- **`list_workstreams` MCP tool** — Lists all workstreams grouped by status, plus ideas.
- **`manage_idea` MCP tool** — Add, remove, or list ideas for future work.
- **`workstreams` module** (`server/relay_server/workstreams.py`) — Shared infrastructure: registry reads/writes, atomic file operations, data directory resolution.
- **`session_markers` SQLite table** — MCP tools write markers directly to DB. Reads check DB first, fall back to JSON files.

### Changed
- **All skills** (`save`, `new`, `park`, `switch`, `list`, `status`, `idea`) — Rewritten to use MCP tool calls instead of bash scripts. ~30 tool invocations per workflow cycle reduced to ~8.
- **`pre-compact-save.sh`** — Now instructs Claude to use `save_workstream` MCP tool instead of bash scripts.
- **`_read_marker_workstream`** — Now checks DB `session_markers` table first, falls back to JSON file.
- **Session hints** — MCP tools write hints directly to `session_hints` table (no intermediate JSON files).

## [0.9.2] - 2026-03-05

### Added
- **`summarize_activity` MCP tool** — Server-side activity summarization. Joins sessions, hints, and session markers, groups by workstream, returns pre-formatted markdown. Replaces multi-call client-side assembly in `/relay:summarize`.

### Changed
- **`/relay:summarize` skill** — Simplified to a single `summarize_activity` MCP call + pass-through. No more manual session listing, hint fetching, marker reading, or client-side grouping.

## [0.9.1] - 2026-03-05

### Fixed
- **Empty registry crash** — `session-start.sh` incorrectly hit the "multiple active workstreams" path when no workstreams existed. Caused by `grep -c . || echo "0"` producing a multiline value (`"0\n0"`) that failed integer comparison.

## [0.9.0] - 2026-03-05

### Added
- **Multi-instance support** — Multiple workstreams can be active simultaneously. Run separate Claude Code instances (e.g., one per git worktree) each attached to a different workstream without conflicts.
- **`attach-workstream.sh`** — New script that binds a session to a workstream (writes marker + loads state) in one step. Warns if another live session is using the same workstream.

### Changed
- **`session-start.sh`** — Detects 0, 1, or 2+ active workstreams. Single active auto-attaches (unchanged). Multiple active lists them and asks the user to pick.
- **`session-end.sh`** — Uses session marker (not `status == "active"`) to find this session's workstream.
- **`pre-compact-save.sh`** — Uses session marker (not `status == "active"`) to find this session's workstream.
- **`switch-registry.sh`** — Only activates the target workstream. No longer parks the old one.
- **`/relay:new`** — No longer auto-parks other active workstreams. New workstream created alongside existing active ones.
- **`/relay:switch`** — Detaches from current workstream and attaches to target. Both stay active. Use `/relay:park` to explicitly deactivate.
- **`/relay:status`** — Shows attached workstream and lists other active workstreams.

## [0.8.1] - 2026-03-05

### Fixed
- **Session ID availability** — `session-start.sh` now outputs the session ID as `relay-session-id:` in context, so skills can reliably reference it for hint file naming. Previously referenced a nonexistent `CLAUDE_SESSION_ID` environment variable.
- **Hint UUID validation** — Indexer now rejects hint files with truncated session IDs (must be full 36-char UUIDs). Prevents silent data loss from malformed backfill output.

### Changed
- **Skills** — Save, park, switch, and pre-compact instructions updated to read session ID from context instead of env var.

## [0.8.0] - 2026-03-05

### Added
- **Session hints** — Write-time metadata for efficient summarization. Claude writes small JSON hint files at save/park/switch/compact time. The MCP server indexes them into a `session_hints` table for fast assembly. Reduces summarize token cost by 10x+.
- **`get_session_summaries` MCP tool** — Fetches pre-written session summaries from the database in one query. Returns structured segments with summary bullets and decisions.
- **`/relay:backfill` skill** — Generate session hints for older sessions interactively. Reads conversations and writes structured summaries as a one-time cost.

### Changed
- **`/relay:summarize`** — Now reads pre-written hints instead of fetching full conversation content. Sessions without hints degrade gracefully to metadata-only entries.
- **`/relay:save`, `/relay:park`, `/relay:switch`** — Now write session hint files after saving state.
- **PreCompact hook** — Now instructs Claude to write a session hint before context compression.
- **Indexer** — Scans `session-hints/` directory on startup and indexes hint files into `session_hints` table.

## [0.7.0] - 2026-03-05

### Added
- **`/relay:idea` skill** — Capture ideas for future work (`/relay:idea <text>`), displayed as a separate section in `/relay:list`. Promote an idea to a full workstream with `/relay:idea promote <id>`. Ideas stored in `ideas.json`.
- **`/relay:summarize` skill** — Summarize recent activity grouped by workstream. Supports natural-language time ranges (`48h`, `7d`, `since Monday`, ISO dates). Useful for standup prep, brag books, and catching up after time away.
- **Cross-project indexing docs** — README now clarifies that the search index covers all projects globally, with `project` parameter for narrowing results.

### Changed
- **`/relay:list`** — Now displays ideas as a separate section between workstreams and parking lot.
- **`/relay:new`** — Auto-detects matching ideas and offers to remove them when creating a workstream.

## [0.6.0] - 2026-03-01

### Added
- **Session-level addressing** — Three tool enhancements for navigating multi-session conversations:
  - `get_conversation` accepts `session` parameter to filter to specific sessions (e.g. `"4"`, `"2-3"`, `"1,4"`)
  - `list_sessions` accepts `slug` parameter, returns session index with `session_number` fields
  - `search_history` results include `session_number` showing position in slug chain

## [0.5.0] - 2026-02-28

### Changed
- **Renamed project from context-flow to relay** — avoids autocomplete collision with Claude Code's built-in `/context` command
- **Centralized app name** — new `scripts/common.sh` with shared `APP_NAME`, `DATA_DIR`, and `COUNTER_PREFIX` constants sourced by all 13 scripts
- **Python package renamed** — `context_flow_server` → `relay_server`, `context-flow-server` → `relay-server`
- **MCP server renamed** — `context-flow-search` → `relay-search`
- **Data paths updated** — `~/.config/context-flow/` → `~/.config/relay/`, `~/.local/share/context-flow/` → `~/.local/share/relay/`
- **All slash commands** — `/context-flow:*` → `/relay:*`

### Added
- **`scripts/migrate-data.sh`** — one-time migration script to move data from old `context-flow` paths to `relay`
- **Old directory detection** — `session-start.sh` detects `~/.config/context-flow/` and prompts user to run migration

## [0.4.0] - 2026-02-28

### Added
- **`write-data-file.sh`** — generic helper script to write files to the data directory from stdin, eliminating `$DATA_DIR` and `${XDG_CONFIG_HOME:-...}` permission prompts on writes

### Changed
- **All skills** (switch, save, park, new) — removed `## Data directory` section; all file reads use `read-data-file.sh`, all writes use `write-data-file.sh`. Claude no longer constructs `$DATA_DIR` paths inline, eliminating "Command contains ${} parameter substitution" and "Shell expansion syntax in paths" permission prompts.

## [0.3.0] - 2026-02-27

### Added
- **`read-data-file.sh`** — generic helper script to read files from the data directory, eliminating `${XDG_CONFIG_HOME:-...}` parameter substitution prompts

### Changed
- **List skill** — uses `read-data-file.sh` instead of inline `$DATA_DIR` with `${}`-based default, removing permission prompts
- **README copyediting** — clarified wording in intro, tag filtering example, and migration section

## [0.2.0] - 2026-02-26

Security hardening, architecture improvements, and workflow polish based on security + architecture reviews.

### Added
- **Test suite** — 49 tests across 5 files (db, tagger, formatter, indexer, server)
- **`complete-save.sh`** — single script for state file rotation, registry update, and counter reset
- **Helper scripts** — `update-registry.sh`, `switch-registry.sh`, `park-registry.sh`, `new-registry.sh`, `reset-counter.sh` to avoid inline jq/for in skills (which triggered permission prompts)
- **Permission pattern docs** in README — recommended `Bash(bash */context-flow/*/scripts/*:*)` for frictionless saves
- **Update instructions** in README
- **Schema versioning** — `workstreams.json` includes `"version": 1` for future migrations
- **Dev dependencies** — `pytest>=8.0` as optional dependency

### Changed
- **Context monitor resets on save/switch/park** — counter goes to 0 after saving, stopping repeated warnings
- **`command mv` everywhere** — bypasses shell `mv -i` aliases without the aggressive `-f` flag
- **`auto_tag_session` optimization** — only fetches `tool_summary` + `plan` roles instead of all messages
- **MCP dependency pinned** — `mcp[cli]>=1.0.0,<2.0.0` to prevent breaking changes
- **Skills use script calls** — all registry operations go through helper scripts instead of inline shell commands

### Security
- **DB directory permissions** — created with `mode=0o700`
- **`PRAGMA foreign_keys=ON`** — enforced on every connection
- **UUID validation** — regex check at all trust boundaries (hooks + indexer) to prevent path traversal
- **Workstream name validation** — `[a-z0-9-]` enforced in shell scripts
- **FTS5 error handling** — `sqlite3.OperationalError` caught with helpful syntax hint
- **Input bounds** — limit clamping (`MAX_LIMIT=500`), tag count/length limits, format/scope parameter validation
- **Type hints** — `callable` → `Callable` from `collections.abc`

## [0.1.0] - 2026-02-26

### Added
- Initial release
- 5 core skills: `/context-flow:new`, `/context-flow:switch`, `/context-flow:park`, `/context-flow:list`, `/context-flow:save`
- MCP conversation search server — indexes Claude Code JSONL transcripts into SQLite FTS5
- Slug chain support — `get_conversation` by slug returns all sessions chronologically
- Auto-tagging — messages tagged by content type (reviews, plans, decisions), sessions by activity (tests, deploy, browser)
- Manual tagging — `tag_message`, `tag_session`, `list_tags` tools
- Tag filtering on `search_history` and `list_sessions`
- Markdown formatter — `get_conversation` returns readable markdown by default
- SessionStart hook: auto-loads active workstream state, writes session markers
- PostToolUse hook: context exhaustion monitor with graduated warnings
- PreCompact hook: prompts Claude to save state before context compression
- SessionEnd hook: cleanup and timestamp update
- Migration script from manual workstream system
- JSON registry (`workstreams.json`) with `jq` parsing
- Atomic state saves with one-deep `.bak` backup
