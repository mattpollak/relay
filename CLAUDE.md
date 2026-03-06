# CLAUDE.md — relay

## Versioning

Every commit must bump the version. Files to update:
- `.claude-plugin/plugin.json` → `"version"`
- `.claude-plugin/marketplace.json` → `"version"`
- `CHANGELOG.md` → add entry under new version header

Semver rules:
- **patch** — skill changes, docs, bug fixes
- **minor** — new MCP tools, new skills, new features
- **major** — breaking changes to MCP tool signatures or data formats
