---
description: With no telemetry the tool refuses, and the agent must relay it rather than guess.
tags: [sizing, confidence]
max_turns: 12
timeout_seconds: 420
allowed_tools: [Skill]
---

I just wrote this and have not run it yet. What should the resources be?

```bash
#!/bin/bash
#SBATCH --job-name=train_tiny
#SBATCH --partition=compute
#SBATCH --cpus-per-task=32
#SBATCH --mem=128G
#SBATCH --time=1-00:00:00
python pretrain.py
```
