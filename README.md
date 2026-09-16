<div align="center">

# 🎯 cloudfit

**Your Slurm job asked for 16 cores and 96 GB. It used 5 and 9. cloudfit fixes that — both directions.**

</div>

```
            asked      used      cloudfit says
 cores      16 ████    5.4 █     8    ██      ↓ cut
 RAM        96G ████   9.3G █    13G  █       ↓ cut
 GPU HBM    80G ████   32G  █▌   72G  ███▌    ↑ raise batch / seq / KV-cache
 walltime   2h ████    47m  █▌   1h   ██      ↓ cut
```

CPU and RAM get over-asked; the GPU gets under-driven. 🔁 Opposite corrections, one pass —
never below a measured peak, never above a live partition limit, never at `n=0`.
One run is a guess and says so. Four agreeing within 10% is a recommendation.

## 🚀 Setup

```bash
pip install slurmwatch slurmpast                                  # 1. the measuring tools
claude plugin marketplace add PursuitOfDataScience/cloudfit-plugin # 2. add the marketplace
claude plugin install cloudfit@cloudfit-plugin                     # 3. install
```

4. Run `/fit <job-id>` or `/fit job.sbatch`. That's it.

## 🧰 What you get

| tool | what it does |
| --- | --- |
| 🗺️ `site` | what **this** cluster calls things — and where to correct it |
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
one `sinfo` marks `*` and your accounts from `sacctmgr`. Nothing to set up — but when your
site has opinions, say so and they stick:

```
site(default_partition="bigmem", discouraged_partitions=["gpu-preempt"], save=True)
```

Env vars work too: `CLOUDFIT_DEFAULT_PARTITION`, `CLOUDFIT_DEFAULT_ACCOUNT`,
`CLOUDFIT_DISCOURAGED_PARTITIONS`. Precedence: cluster → saved profile → env → this call.

## ⚠️ Gotchas

- Needs `mcp>=1.28,<2`. On `mcp` 2.x the import dies and Claude Code says only
  "Connection closed".
- `history` needs `slurmdbd`; without accounting, cloudfit still fits from its own record.
- Measuring GPU load requires the job to be running — a finished job has no HBM to read.

<div align="center">

MIT · built on **slurmwatch** ([code](https://github.com/PursuitOfDataScience/slurmwatch) ·
[pypi](https://pypi.org/project/slurmwatch/)) and **slurmpast**
([code](https://github.com/PursuitOfDataScience/slurmpast) ·
[pypi](https://pypi.org/project/slurmpast/))

</div>
