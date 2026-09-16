# Changelog

Every entry corresponds to a `version` in `.claude-plugin/plugin.json` and a `v<version>`
git tag. That field is a **pin**: Claude Code serves installed users whatever content
carried the version string they already have, so an unbumped release is an invisible one.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semver](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-09-16

### Fixed

- Pin `mcp>=1.28,<2`. `mcp` 2.x renamed `FastMCP` to `MCPServer`, so `cloudfit/server.py`
  raised `ModuleNotFoundError` at import and the only thing Claude Code reported was
  `cloudfit (CONNECTION_CLOSED): "Connection closed"` — with no indication the cause was a
  resolved dependency. Reproduced on a Debian 12 host where pip resolved `mcp` 2.x; midway3
  was unaffected only because its env happens to hold 1.28.1.

- Declare the MCP server inline in `plugin.json` and delete `.mcp.json`. That file had two
  identities at once: the plugin's server definition, where `${CLAUDE_PLUGIN_ROOT}` is set,
  and — because it sat at the repo root — an auto-discovered *project-scope* server for
  anyone whose cwd is this repo, where that variable is not set and the path expands to
  `/cloudfit/server.py`. `/mcp` showed `cloudfit  ✘ failed` beside a working copy of the
  same name, and the project-scope one additionally sat behind an approval gate it could
  never usefully pass. Nothing is left at the root to discover.

### Added

- `test_the_mcp_dependency_excludes_the_two_line`, which fails if the bound is ever widened
  back past the v1 line while `server.py` still imports `mcp.server.fastmcp`.
- `test_no_mcp_json_sits_at_the_repo_root`, which fails if the dual-identity file returns.
- `test_the_declared_versions_agree`, which fails if `plugin.json`, `pyproject.toml`, and
  the newest released `CHANGELOG.md` heading drift apart.

### Known

- `guard.DEFAULT_PARTITION = "amd"` and `guard.DEFAULT_ACCOUNT = "rcc-staff"` are RCC site
  policy applied unconditionally. On any other cluster `check` substitutes `amd`, finds it
  absent, and refuses a script the scheduler would have accepted — as does the unconditional
  `--account` refusal. Verified against a Slurm 22.05 single-node cluster reporting
  `ClusterName=lab`, where `sbatch` accepted the same script `check` rejected.
- `capabilities()` reads `AccountingStorageType` from `scontrol` and reports `sacct` as
  available on that basis, without running it. On the same 22.05 cluster it reported
  `remedy: null` while `history()` hit `sacct failed (rc=1) Slurm accounting storage is
  disabled`.

## [0.1.0] - 2026-09-15

Initial release: `capabilities`, `measure`, `history`, `fit`, `check`, `submit`, `doctor`,
the `PreToolUse` guard on `sbatch` and `gcloud`, and the `cloudfit` skill and agent.

[Unreleased]: https://github.com/PursuitOfDataScience/cloudfit-plugin/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/PursuitOfDataScience/cloudfit-plugin/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/PursuitOfDataScience/cloudfit-plugin/releases/tag/v0.1.0
