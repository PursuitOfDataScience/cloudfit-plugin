<div align="center">

# 🎯 cloudfit

**Asked for 16 cores and 96 GB, used 5 and 9, and left two-thirds of the GPU empty.
cloudfit cuts the over-ask and fills the card, in one pass.**

</div>

```
              allocated       used          cloudfit says
 cores        16  ██████████  5.4 ███▍      8    █████      ↓ cut
 RAM          96G ██████████  9.3G █        13G  █▎         ↓ cut
 walltime     2h  ██████████  47m ███▉      1h   █████      ↓ cut
 GPU HBM      80G ██████████  32G ████      72G  █████████  ↑ raise batch / seq / KV
```

🔁 Three over-asks and one idle card, corrected in one pass: never below a measured peak,
never above a live partition limit, never at `n=0`. One run is a guess and says so; four
agreeing within 10% is a recommendation.

## 🚀 Setup

**1.** Point Claude Code at the catalog, which is just this repo:

```bash
claude plugin marketplace add PursuitOfDataScience/cloudfit-plugin
```

**2.** Install the plugin from it:

```bash
claude plugin install cloudfit@cloudfit-plugin
```

**3.** Fit something: `/fit <job-id>` or `/fit job.sbatch`.

No `pip install` step. The server installs what it needs, into its own venv, on first start.

## 🧰 What you get

| tool | what it does |
| --- | --- |
| 🗺️ `site` | what **this** cluster calls things, and where to correct it |
| 🔍 `capabilities` | which telemetry sources exist here, and how to fix the gaps |
| 📟 `measure` | the four axes right now (hops to the node for GPU numbers) |
| 📜 `history` | `slurmpast` → `sacct` → its own record, saying which answered |
| 🎯 `fit` | corrected `#SBATCH` block, per-axis reasoning, confidence with its `n` |
| 🚦 `check` | pre-submit lint against the live partition |
| 📮 `submit` | keeps CPU jobs off GPU nodes, then verifies placement |
| ☁️ `doctor` | read-only GCP readiness, each gap with the command that fixes it |

A `PreToolUse` hook runs the same checks on `sbatch` and `gcloud` commands you type yourself.

## 🧭 Any cluster, no config

cloudfit ships **zero** partition or account names. It reads the default partition from the
one `sinfo` marks `*`, and your accounts from `sacctmgr`. When your site has opinions:

```
site(default_partition="bigmem", discouraged_partitions=["gpu-preempt"], save=True)
```

Env vars work too: `CLOUDFIT_DEFAULT_PARTITION`, `CLOUDFIT_DEFAULT_ACCOUNT`,
`CLOUDFIT_DISCOURAGED_PARTITIONS`. Precedence: cluster → saved profile → env → this call.

## ⚠️ Gotchas

- **The hook will stop you.** An `sbatch` that would squat a GPU node, or a `gcloud` VM with
  no `--max-run-duration`, gets denied before it runs.
- **No `slurmdbd`, no history.** Without Slurm accounting, cloudfit only knows the runs it
  measured itself, so the first `/fit` of a workload will say `n=0` and refuse.

<div align="center">

MIT · built on **slurmwatch** ([code](https://github.com/PursuitOfDataScience/slurmwatch) ·
[pypi](https://pypi.org/project/slurmwatch/)) and **slurmpast**
([code](https://github.com/PursuitOfDataScience/slurmpast) ·
[pypi](https://pypi.org/project/slurmpast/))

</div>
