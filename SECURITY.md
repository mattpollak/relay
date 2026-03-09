# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in relay, please report it responsibly.

**Email:** Open a [GitHub issue](https://github.com/mattpollak/relay/issues) with the label `security`, or contact the maintainer directly through GitHub.

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

I'll respond as quickly as I can and will credit you in the fix (unless you prefer otherwise).

## Scope

relay runs locally on your machine. The main security-relevant areas are:

- **SQLite database** — stores indexed conversation content
- **State files** — contain workstream state (may include project details)
- **Hook scripts** — execute automatically during Claude Code events
- **MCP server** — handles tool calls from Claude Code

## Design Decisions

- Database directories are created with `0o700` permissions
- UUID validation at all trust boundaries (hooks + indexer)
- Workstream names restricted to `[a-z0-9-]`
- Atomic file writes via tempfile + `os.replace`
- Input bounds enforced (max limits, tag count/length limits)
- FTS5 query errors caught with user-friendly messages
