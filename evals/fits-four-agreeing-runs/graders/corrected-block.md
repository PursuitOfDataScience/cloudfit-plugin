---
type: llm
weight: 2
---

The reply corrects the script from the measured numbers:

- `--mem` comes down to `14G`, and the reply does not round it to 16G, 20G or any other
  tidier figure.
- `--time` comes down to `01:02:00`.
- `--cpus-per-task` stays at `16`, and the reply says the cores were already right rather
  than cutting them.
- It gives the reason per axis (the observed peak behind each number).
