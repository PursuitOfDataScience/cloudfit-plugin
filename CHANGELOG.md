# Changelog

Every entry corresponds to a `version` in `.claude-plugin/plugin.json` and a `v<version>`
git tag. That field is a **pin**: Claude Code serves installed users whatever content
carried the version string they already have, so an unbumped release is an invisible one.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semver](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- The site-discovery tests read the real environment, so on a machine with the `CLOUDFIT_*`
  vars set they measured that cluster instead of the fixture's and three of them failed.
  Setting those vars is the documented way to tell cloudfit what a cluster is called, which
  made the expected case the broken one, and CI stayed green only because it never sets them.
  `conftest.py` now clears the four of them for every test; the one test that exercises the
  env layer sets them itself, as it always did.

### Changed

- The README says what the reader gets, not how it is built. The cluster section listed three
  env var names and a four-step precedence chain, which is design documentation and belongs
  in the skill; it now says there is nothing to configure and shows the sentence you type
  when it guesses wrong. The tool table said "the four axes", "telemetry sources" and
  "slurmpast to sacct to its own record", and now says what each tool answers. The line under
  the chart no longer leans on `n=0`. The warning box is gone too: it put a hazard sign over
  two things that are not hazards, in words ("squat a GPU node", "slurmdbd", "n=0 and
  refuse") that only mean something to whoever wrote them. The hook is a feature, so it sits
  with the tools now; needing one run before a fit is a setup step, so it sits in setup.

## [0.2.2] - 2026-09-16

### Changed

- No em-dashes anywhere in the repo: prose, docstrings, and the strings the tools print.
  Each one was recast with the punctuation the sentence actually wanted rather than swapped
  for a hyphen, and the ASCII `--` stand-ins went with them. Three of the recasts changed
  text a caller sees, so the assertions pinning the old wording were updated. One of those
  pinned `ask RCC` in a `capabilities` remedy, which now says "Changing this needs a Slurm
  admin; ask yours about ...": one more site name out of the plugin.
- The README hook claimed both directions while showing two cuts, and the chart's first
  column read `asked` above 80G of GPU HBM, which nobody requests. It now names the idle
  card, and the column is `allocated`, which is true of all four rows. The setup steps lost
  the bare `# 1.` comments left over from the dropped `pip` line.
- Both manifests published `youzhi@uchicago.edu` while every commit in the log is authored
  from `yuyouzhi666@icloud.com`, so the repo read as though it had two owners. The manifests
  now match the commits.

### Added

- Tags for every released version. `v0.1.1` was the only one that existed, so a reader had no
  way to fetch the tree any other version shipped.

## [0.2.1] - 2026-09-16

### Fixed

- **The plugin started on exactly one machine.** `plugin.json` and `hooks/hooks.json` named
  an absolute interpreter path from the author's cluster, so anywhere else the MCP server
  never started ("Connection closed", which names no cause) and the `PreToolUse` guard
  silently never ran. Both now point at `${CLAUDE_PLUGIN_ROOT}/bin/cloudfit-*` launchers that
  find a Python 3.10+ on the host. `test_nothing_in_the_manifests_points_outside_the_plugin`
  fails if a host path returns.

### Added

- `bin/cloudfit-server`: resolves a Python, and if that interpreter cannot import `mcp`,
  builds a private venv (`CLOUDFIT_VENV`, default `~/.cache/cloudfit/venv`) holding `mcp`,
  `slurmwatch` and `slurmpast`, installed `--no-cache-dir` so the plugin leaves no pip
  cache behind. An interpreter that already has `mcp` is used untouched; nothing is ever
  installed into the user's own environment. `CLOUDFIT_PYTHON` overrides the choice and
  `CLOUDFIT_NO_BOOTSTRAP=1` refuses to install and fails loudly instead. Diagnostics go to
  stderr only: stdout is the MCP channel.
- `bin/cloudfit-hook`: the same resolution for the guard, stdlib-only so it needs no venv.
  Every failure path exits 0 printing nothing: a hook that errors must not block the Bash
  call it was watching.
- Tests that both launchers are executable, keep stdout clean, and check the same Python
  floor `pyproject.toml` declares.

### Changed

- The README's install is two commands. The `pip install slurmwatch slurmpast` step is gone:
  the server does it, once, only if it has to.

## [0.2.0] - 2026-09-16

### Changed

- **cloudfit no longer ships any cluster's names.** `guard.DEFAULT_PARTITION` and
  `guard.DEFAULT_ACCOUNT` held one site's partition and account, applied to everyone: on any
  other cluster `check` substituted a partition that does not exist there, refused the script
  for it, and told the user to add an account they have never heard of. Both constants are
  gone. Everything site-specific now arrives as `SiteFacts`, discovered per cluster:
  the default partition is the one `sinfo` marks `*`, the accounts are whatever `sacctmgr`
  says this user is associated with.
- A missing `--account` is no longer refused on sight. Most clusters fill it from the user's
  default association, so the old rule rejected scripts `sbatch` would have accepted
  (reproduced on Slurm 22.05, `ClusterName=lab`). It now refuses only when the lookup
  succeeded and came back empty (the one case that really does fail with "Account is not
  specified"), and it names `sacctmgr` rather than an account of its own.
- `policy_warnings` no longer knows which partitions bill. A site says so with
  `CLOUDFIT_DISCOURAGED_PARTITIONS`; unset means cloudfit has no opinion.
- Host, user and account names are scrubbed from every fixture, including a GCP project id
  and its default service-account address in `gcloud_doctor_real.json`. Shapes are real,
  identities are not; the `_real` captures say so in their header line.
- The README is shorter and leads with the correction it makes, rather than with the
  repo's own test count and fixture-naming convention.

### Added

- `site`, the eighth MCP tool: reports what this cluster calls things, and takes
  `default_partition` / `account` / `discouraged_partitions` to correct it mid-session.
  `save=True` writes `<CLOUDFIT_HOME>/site.json` so later sessions and the submit hook start
  from it. Precedence: cluster discovery → saved profile → `CLOUDFIT_*` env → this call.
- `test_no_cluster_name_is_baked_into_the_package`, which fails if any partition or account
  name reappears in `cloudfit/`.
- Tests for site discovery, the precedence order, and a cluster that answers nothing.

## [0.1.2] - 2026-09-16

### Fixed

- `--mem` no longer sizes below a peak the job actually reached. `mem_peak_trusted_bytes`
  preferred `slurmwatch`'s `peak_working_set_bytes` whenever the page cache had been measured
  separately, but a `--once` snapshot has no history behind it: that field is the anonymous set
  at the instant of sampling, not a peak over the run. A phased job that frees one stage's
  arrays before the next reads far under its own high mark, so a late sample recommended a
  `--mem` the job would OOM against. Measured on GCP: a six-phase linear-algebra run held
  1.88 GiB during a 9000x9000 GEMM, then read 0.09 GiB three phases later while the cgroup
  watermark stayed at 1.90 GiB throughout, so the old rule would have said `--mem=1G`. The
  cgroup watermark is now the floor unless the cache reading genuinely accounts for the gap.
  The repo's own `slurmwatch_cpu_overask_real.json` fixture already carried the evidence:
  12.4 GiB watermark, 0.17 GiB working set, and only 0.46 GiB of cache to explain it.
- The page-cache exclusion now has to earn its name. `mem_disagrees` fired on any gap wider
  than 25%, then blamed reclaimable page cache for what was usually an earlier phase's freed
  anonymous memory: it reported "the difference is reclaimable page cache" for a 1.6 GiB gap
  measured alongside 16 MB of cache. Cache must now carry at least half the gap. The Arrow
  case the exclusion exists for is unchanged: 52 GiB of measured cache against a 52 GiB gap
  still comes off, and still sizes to the 9.3 GiB anonymous set.

### Added

- `mem_peak_basis`, `mem_peak_understates`, `mem_cache_bytes` and `mem_peak_is_lifetime` on
  `Observation`, so a `--mem` reason can say which figure it sized to and why.
- GitHub Actions: `ci` (ruff, pytest on 3.10-3.13, an import of the MCP server against the
  pinned `mcp` range, and `claude plugin validate --strict`) and `release`, which refuses a
  `v*` tag that disagrees with `plugin.json` and cuts notes from this file.
- `tests/test_manifest.py`: the install surface is checked by the suite rather than by CI
  yaml, so a missing skill path or a dropped `mcp` ceiling fails locally first.


## [0.1.1] - 2026-09-16

### Fixed

- Pin `mcp>=1.28,<2`. `mcp` 2.x renamed `FastMCP` to `MCPServer`, so `cloudfit/server.py`
  raised `ModuleNotFoundError` at import and the only thing Claude Code reported was
  `cloudfit (CONNECTION_CLOSED): "Connection closed"`, with no indication the cause was a
  resolved dependency. Reproduced on a Debian 12 host where pip resolved `mcp` 2.x; the cluster
  was unaffected only because its env happens to hold 1.28.1.

- Declare the MCP server inline in `plugin.json` and delete `.mcp.json`. That file had two
  identities at once: the plugin's server definition, where `${CLAUDE_PLUGIN_ROOT}` is set,
  and (because it sat at the repo root) an auto-discovered *project-scope* server for
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

- (Fixed in 0.2.0.) `guard.DEFAULT_PARTITION` and `guard.DEFAULT_ACCOUNT` were one site's
  policy applied unconditionally, so on any other cluster `check` refused a script the
  scheduler would have accepted.
- `capabilities()` reads `AccountingStorageType` from `scontrol` and reports `sacct` as
  available on that basis, without running it. On the same 22.05 cluster it reported
  `remedy: null` while `history()` hit `sacct failed (rc=1) Slurm accounting storage is
  disabled`.

## [0.1.0] - 2026-09-15

Initial release: `capabilities`, `measure`, `history`, `fit`, `check`, `submit`, `doctor`,
the `PreToolUse` guard on `sbatch` and `gcloud`, and the `cloudfit` skill and agent.

[Unreleased]: https://github.com/PursuitOfDataScience/cloudfit-plugin/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/PursuitOfDataScience/cloudfit-plugin/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/PursuitOfDataScience/cloudfit-plugin/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/PursuitOfDataScience/cloudfit-plugin/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/PursuitOfDataScience/cloudfit-plugin/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/PursuitOfDataScience/cloudfit-plugin/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/PursuitOfDataScience/cloudfit-plugin/releases/tag/v0.1.0
