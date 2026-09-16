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

🔁 Three over-asks and one idle card, corrected in one pass. It will never tell you to ask
for less than the job actually used, never more than your partition allows, and never
anything at all off zero runs. From one run it says "this is a guess". From four that agree,
it calls it a recommendation.

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
| 🗺️ `site` | what your cluster calls its partitions and accounts |
| 🔍 `capabilities` | what can be measured here, and what is missing |
| 📟 `measure` | cores, RAM, GPU and time for a job running right now |
| 📜 `history` | what this job used the last times you ran it |
| 🎯 `fit` | the corrected `#SBATCH` block, with the reason for every line |
| 🚦 `check` | what the scheduler will reject, before you submit |
| 📮 `submit` | submits it, and keeps CPU jobs off GPU nodes |
| ☁️ `doctor` | whether your GCP project is ready, and the fix for each gap |

A `PreToolUse` hook runs the same checks on `sbatch` and `gcloud` commands you type yourself.

## 🧭 It works on your cluster, not mine

No partition names, no account names, no config file. cloudfit asks your cluster what it
uses and goes from there.

Guessed wrong? Say so in plain English and it remembers:

> *"default to the bigmem partition, and never send anything to gpu-preempt"*

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
