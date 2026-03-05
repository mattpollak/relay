# Changelog

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
