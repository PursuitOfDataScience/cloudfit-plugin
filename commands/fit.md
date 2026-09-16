---
description: Fit a job or sbatch script to what it actually used, with the sample size stated.
argument-hint: <job-id | path/to/script.sbatch>
---

Fit `$ARGUMENTS` with the `cloudfit` MCP tools.

- A number that looks like a job id: `measure` it, then `fit` it.
- A path: `fit` it from its past runs, then `check` it.
- Neither: run `capabilities` and say which telemetry sources this machine has.

If a refusal turns on a partition or account name, run `site`. It reports what this cluster
actually calls things. Correct it there (`site(default_partition=..., account=..., save=True)`)
rather than editing a number to get past the guard.

Report the corrected `#SBATCH` block, the reason per axis, and the confidence with its `n`.
If `fit` refuses, relay the refusal as-is. Do not supply a number of your own.
