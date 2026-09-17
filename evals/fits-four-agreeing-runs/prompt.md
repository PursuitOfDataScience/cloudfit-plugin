---
description: Four agreeing runs is the bar, so the corrected block is a recommendation.
tags: [sizing]
max_turns: 12
timeout_seconds: 420
allowed_tools: [Skill]
---

Right-size this job. It has run four times already.

```bash
#!/bin/bash
#SBATCH --job-name=sft_run
#SBATCH --partition=compute
#SBATCH --cpus-per-task=16
#SBATCH --mem=96G
#SBATCH --time=02:00:00
python train.py --config sft.yaml
```
