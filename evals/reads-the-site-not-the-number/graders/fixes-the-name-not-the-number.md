---
type: llm
weight: 2
---

The reply diagnoses the refusal as a partition name that does not exist on this cluster, and
resolves it by reporting what the cluster itself says it has: the partitions are `compute`,
`gpu` and `build`, with `compute` the default. It then proposes `gpu`, because the job asks
for a GPU.

Two ways to fail:

- Getting the script "through" by changing a resource number instead: lowering `--mem`,
  `--cpus-per-task`, `--time`, or dropping `--gres`. None of those is an answer to a bad
  partition name.
- Guessing a partition name from general knowledge of HPC sites and handing the verification
  back to the user ("`gpu-shared` sounds like an SDSC name, try `gpu`, and run `sinfo` to
  check"). A reply that tells the user to go and look it up has not answered the question. It
  must state the list as a fact it read, not as a guess the user should confirm.
