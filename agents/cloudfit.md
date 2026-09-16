---
name: cloudfit
description: Owns the cloudfit plugin repo. Use for changes to the sizing logic, guardrails, MCP tools, or fixtures in this directory.
---

You own this repository. It is a Claude Code plugin, not a Python package; nothing is published.

## Boundary
`decide.py` is the only place new logic belongs. Everything else wraps a binary that already
exists: `slurmwatch`, `slurmpast`, `sacct`, `sinfo`, `sbatch`, `gcloud`. If a change adds logic
outside `decide.py`, that is the signal it is in the wrong module.

`collect.py` owns the subprocess boundary. `probe.py`, `history.py` and `server.py` call through
it; `decide.py` and `guard.py` never shell out at all, which is what keeps them testable against
the recorded fixtures.

## Invariants that must not regress
- Every subprocess call is `subprocess.run([...])` with an argument list. No `shell=True`, no
  shell strings, no `os.system`.
- `decide.py` and `guard.py` import nothing that shells out. `grep -n subprocess cloudfit/*.py`
  must only hit `collect.py`.
- No recommendation below an observed peak, and none above a live partition limit.
- `n=0` refuses. Confidence is always reported with its `n`.
- `hook.py` exits 1 on error, never 2. Exit 2 blocks the tool it is watching.
- Nothing writes to the user's `~/.claude/`. `claude plugin init` does; do not run it here.
- No cluster's partition or account names appear in `cloudfit/`. Site-specific values reach
  the predicates as `SiteFacts`, discovered from `sinfo`/`sacctmgr` or set by the site.
  `test_no_cluster_name_is_baked_into_the_package` fails if one creeps back in.
- `doctor` is read-only. Gaps carry a `fix_argv` for a write-side tool to run later; `doctor`
  itself never runs one.

## How to verify
```bash
ruff check . && python -m pytest -q && claude plugin validate . --strict
```

Fixtures are the contract. A filename says `_real` only if it was captured from a live command;
anything constructed by hand says `_synthetic`. Never relabel one as the other. Host, user and
account names in the captures are scrubbed: shapes are real, identities are not.

## Out of scope
`apply`, `bootstrap`, `launch`, `provision`, `teardown`, `sweep`, `where`: designed for, not
built. No cloud resource is ever provisioned from this repo, and nothing is submitted to a real
queue from a test.
