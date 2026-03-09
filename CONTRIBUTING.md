# Contributing to relay

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

1. **Clone the repo:**
   ```bash
   git clone https://github.com/mattpollak/relay.git
   cd relay
   ```

2. **Install dependencies:**
   ```bash
   cd server
   uv sync --dev
   ```

3. **Run tests:**
   ```bash
   cd server
   uv run pytest
   ```

4. **Test the plugin locally:**
   ```bash
   claude --plugin-dir /path/to/relay
   ```

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- [jq](https://jqlang.github.io/jq/) for hook scripts
- Claude Code v2.1.0+

## Making Changes

1. Fork the repo and create a branch from `main`.
2. Make your changes.
3. Add or update tests as needed — run `uv run pytest` from `server/`.
4. Update `CHANGELOG.md` with a description of your change.
5. Bump the version in `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` (see versioning rules in `CLAUDE.md`).
6. Open a pull request.

## What to Contribute

- Bug fixes
- Documentation improvements
- New MCP tools or skills
- Test coverage
- Terminal compatibility improvements (especially macOS/Linux variations)

## Reporting Bugs

Open an [issue](https://github.com/mattpollak/relay/issues) with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your OS and Claude Code version

## Code Style

- Python: follow existing patterns in `server/relay_server/`
- Shell scripts: use `set -euo pipefail`, quote variables, validate inputs
- Skills: follow the structure of existing skills in `skills/`

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
