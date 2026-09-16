# cloudfit

**Fits Slurm jobs to what they actually used.** Not just smaller — *both* directions.

```
              you asked      it used        cloudfit says
  cores       16  ████████   5.4  ███       8    ████      ↓ cut
  RAM         96G ████████   9.3G █         13G  █▌        ↓ cut
  GPU HBM     80G ████████   32G  ███       72G  ███████   ↑ raise batch / seq / KV
  walltime    2h  ████████   47m  ███       1h   ████      ↓ cut
```

CPU and RAM get over-requested; the GPU gets under-driven. Opposite corrections, one pass.

```
  slurmwatch (live)  ┐
  slurmpast / sacct  ├─→  fit  ─→  #SBATCH block + why + confidence(n)
  cloudfit's record  ┘     │
                           └─ never below a measured peak, never above
                              a partition limit, never at n=0
```

One run is a guess and says so; four agreeing within 10% is a recommendation.

cloudfit measures nothing itself — it is the decision layer over two tools that do, both MIT
and by the same author:

| tool | what it gives cloudfit | |
| --- | --- | --- |
| `slurmwatch` | live CPU / RAM / GPU telemetry from cgroups, `/proc`, `nvidia-smi` — no accounting needed | [GitHub](https://github.com/PursuitOfDataScience/slurmwatch) · [PyPI](https://pypi.org/project/slurmwatch/) |
| `slurmpast` | finished-job history and per-workload sizing advice — needs `slurmdbd` | [GitHub](https://github.com/PursuitOfDataScience/slurmpast) · [PyPI](https://pypi.org/project/slurmpast/) |

> **Not this:** SkyPilot picks *where* to run. cloudfit answers the question underneath.

## Install

```bash
pip install slurmwatch slurmpast        # the tools that do the measuring
claude plugin marketplace add PursuitOfDataScience/cloudfit-plugin
claude plugin install cloudfit@cloudfit-plugin
```

Then `/fit <job-id>` or `/fit job.sbatch`. Needs `mcp>=1.28,<2` on the interpreter in
the plugin manifest — on `mcp` 2.x the import fails and Claude Code reports only "Connection
closed".

## Tools

| tool | what it does |
| --- | --- |
| `capabilities()` | which telemetry sources exist here, and how to fix the gaps |
| `measure(jobid)` | the four axes now, via `slurmwatch` (hops to the node for GPU) |
| `history(workload)` | `slurmpast` → `sacct` → own record, saying which answered |
| `fit(jobid \| script)` | corrected `#SBATCH` block, reasoning, confidence |
| `check(script)` | pre-submit lint against the live partition |
| `submit(script)` | generates `--exclude` for GPU nodes, then verifies it took |
| `doctor()` | read-only GCP readiness, each gap with the command that fixes it |

A `PreToolUse` hook checks your own `sbatch` and `gcloud` commands too.
Not built yet: `apply`, `bootstrap`, `launch`, `provision`, `teardown`, `sweep`, `where`.

## Checks

```bash
ruff check . && python -m pytest -q && claude plugin validate . --strict
```

171 tests, no cluster or cloud needed. In `fixtures/`, `_real` was captured from a live
command and `_synthetic` was built from the real schema — never blurred.
