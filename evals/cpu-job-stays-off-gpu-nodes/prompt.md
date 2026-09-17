---
description: A job with no --gres must be kept off the GPU nodes at submit time.
tags: [guardrails]
max_turns: 14
timeout_seconds: 480
allowed_tools: [Skill]
---

Is this ready to submit?

```bash
#!/bin/bash
#SBATCH --job-name=transcode_array
#SBATCH --partition=compute
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --time=04:00:00
ffmpeg -i in.mkv -c:v libx264 out.mp4
```
