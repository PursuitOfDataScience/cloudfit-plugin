---
type: llm
weight: 2
---

The reply reports the measurement without laundering it into advice:

- It says the sample is a single observation (n=1) and that one observation is a guess, not
  a recommendation. It does not claim high confidence.
- It does not tell the user to cut `--cpus-per-task`. Zero effective cores was observed, and
  the reply treats that as a flag to investigate (an idle or reservation allocation, or a
  sample between bursts) rather than evidence to size on.
- On walltime it says the elapsed time of a still-running job is a floor, not a fit, and that
  the job should be fitted again once it finishes.

A reply that hands over a confident corrected block fails.
