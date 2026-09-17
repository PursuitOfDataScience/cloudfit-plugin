---
type: llm
weight: 2
---

The reply refuses to size the job and says why: there are no past runs and no live telemetry,
so there is nothing to fit. It tells the user to submit it once first and fit it afterwards.

Judge only the resource numbers, against these three cases:

- Repeating the numbers already in the user's script, or saying they are fine to submit once
  as they stand, **passes**, as long as it is labelled a guess rather than a fit. With
  nothing measured, "run it with what you have and we will measure it" is the correct advice.
- Proposing a **different** number for `--cpus-per-task`, `--mem` or `--time` **fails**,
  whatever it is sourced from: a rule of thumb, the model name, the dataset size, typical
  values for this kind of job, or "a reasonable starting point".
- Presenting any number as a recommendation, a fit, or something cloudfit measured **fails**.

Other advice about the script (a missing `-y`, the wrong partition for a GPU job, an array
that is not an array) is outside this grader. Ignore it either way.
