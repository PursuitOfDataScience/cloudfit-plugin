# cloudfit

**You asked for 96 GB. You used 9.** Meanwhile the GPU you paid for sat 40% full.

A Claude Code plugin that reads what your Slurm job *actually did* and tells you what you
should have asked for: cores, RAM and walltime come **down**, the GPU knobs go **up** until
the expensive card is the bottleneck. Two corrections, opposite directions, one pass — the
`fit`.

Every number arrives with its sample size. One run is a guess and says so; four runs agreeing
within 10% is a recommendation. It never returns a number below a peak it measured or above a
limit the scheduler would reject.

> **Not this:** SkyPilot picks *where* to run. cloudfit answers the question underneath.

## Install

```bash
git clone git@github.com:PursuitOfDataScience/cloudfit-plugin.git
claude plugin install ./cloudfit-plugin
```

Needs the `mcp` SDK on the interpreter in `.mcp.json`. Then `/fit <job-id>` or
`/fit job.sbatch`.

## Tools

| tool | what it does |
| --- | --- |
| `capabilities()` | which telemetry sources exist here, and how to fix the gaps |
| `measure(jobid)` | the four axes now, via `slurmwatch` (hops to the node for GPU) |
| `history(workload)` | `slurmpast` → `sacct` → own record, saying which answered |
| `fit(jobid \| script)` | corrected `#SBATCH` block, per-axis reasoning, confidence |
| `check(script)` | pre-submit lint against the live partition |
| `submit(script)` | generates `--exclude` for GPU nodes, then verifies it took |
| `doctor()` | read-only GCP readiness, each gap with the command that fixes it |

A `PreToolUse` hook checks your own `sbatch` and `gcloud` commands too.

Not built yet: `apply`, `bootstrap`, `launch`, `provision`, `teardown`, `sweep`, `where`.

## Checks

```bash
ruff check . && python -m pytest -q && claude plugin validate . --strict
```

158 tests, no cluster or cloud needed. In `fixtures/`, `_real` means captured from a live
command and `_synthetic` means built by hand from the real schema — never blurred.
