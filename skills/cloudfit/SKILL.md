---
name: cloudfit
description: Rules for sizing Slurm jobs from measured telemetry - cutting over-requested cores, RAM and walltime while driving an under-used GPU up. Use when asked to right-size, fit, or fix the resources of a job or sbatch script, when a job is wasting a GPU, or when submitting to a billed partition.
---

# Sizing rules

The `cloudfit` MCP tools enforce all of this. These are the rules, not the enforcement.

## Learn the cluster before judging a script
- cloudfit knows no partition or account names. `site` reports what this one uses: the default
  partition `sinfo` marks with `*`, every partition, and the user's associations.
- Wrong or missing? Correct it in place: `site(default_partition=…, account=…,
  discouraged_partitions=[…])`, plus `save=True` to pin it for later sessions and the hook.
  Never work around a guard by editing the number it complained about.

## Fit both directions in one pass
- Cut `--cpus-per-task` to `ceil(1.3 x peak effective cores)`.
- Cut `--mem` to `ceil(1.4 x peak)`. Never pad to a round number.
- Cut `--time` to `1.25 x longest completed run`.
- Raise the GPU knobs (batch size, K, sequence length, KV-cache) until HBM sits near 90%.
- Flag GPU compute below 50%. Full HBM plus an idle card is a data-pipeline stall; raising
  batch size will not fix it.

## State the sample every time
- `n=0`: refuse. Say "submit it once first."
- `n=1`: a guess. Never call it a recommendation.
- `n>=4` agreeing within 10%: a recommendation.
- Count `n` per axis. Four runs where one carried GPU numbers is `n=1` on the GPU axes.

## Trust the right memory number
- Prefer the cgroup anonymous working set over `memory.peak` / `sstat` MaxRSS, which count
  reclaimable page cache. Arrow-backed datasets inflate it tenfold. Report the disagreement;
  never silently size to the larger figure.
- `sacct` gives CPU-seconds, so its core figure is a run average, not a peak. Say so, and
  confirm against `slurmwatch` before cutting cores.
- Accounting existing and being allowed to read it are separate. Scope every history query to
  the caller: an `Operator` account sees the whole cluster, and a shared workload name would
  otherwise fit your job from someone else's telemetry.

## Never emit something the scheduler rejects
- Never recommend below an observed peak.
- Never exceed the live partition limits. Read them, do not assume them.
- Never assume a partition or account name. They are site-specific, and a script checked
  against a partition that does not exist here gets refused for nothing. Read the cluster's
  own default from `sinfo` (the one marked `*`), or the site's `CLOUDFIT_DEFAULT_PARTITION` /
  `CLOUDFIT_DEFAULT_ACCOUNT` / `CLOUDFIT_DISCOURAGED_PARTITIONS`.
- A missing `--account` is usually fine: Slurm fills it from the user's default association.
  Refuse only when `sacctmgr` answers that they have none. That is when submission fails
  with "Account is not specified", which names no cause.
- A job with no `--gres` must not land on a GPU node. Generate `--exclude` at submit time from
  the live partition, then verify placement. An empty `--exclude` is silently a no-op.
- Every VM gets `--max-run-duration`.
