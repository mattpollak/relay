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

## Releases

After pushing a version bump, create a GitHub release:
```bash
gh release create v<version> --title "v<version>" --notes "<changelog notes>"
```
Tag the current commit. Mark only the latest version as `--latest`.

## Repository

- **Public OSS repo** — all commits are public
- **Direct to main** for maintainer work; PRs for external contributors
- **Issue templates** exist at `.github/ISSUE_TEMPLATE/` (bug report, feature request)
- **PR template** at `.github/PULL_REQUEST_TEMPLATE.md`
- Do not commit plans, drafts, or internal docs to the repo (keep in `docs/plans/` only if gitignored)
