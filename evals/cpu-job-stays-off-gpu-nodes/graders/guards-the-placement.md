---
type: llm
weight: 2
---

The reply catches that this job requests no GPU while its partition holds GPU nodes, and says
what to do about it:

- Submit with an `--exclude` list of the partition's GPU nodes, generated at submit time from
  the live partition rather than from a hardcoded list of node names.
- Verify where the job actually landed afterwards, because an empty `--exclude` is silently a
  no-op.

A reply that calls the script ready to submit with no mention of placement fails.
