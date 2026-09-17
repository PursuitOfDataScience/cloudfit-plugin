---
type: llm
weight: 2
---

The reply answers the question asked, which is no:

- It says the card is loaded (about 88% HBM) and idle (about 18% compute) at the same time,
  and that this combination is a data-pipeline stall: loader workers, tokenisation, or I/O.
- It says raising the batch size will not fix it, and does not suggest raising sequence
  length or KV-cache either. Those knobs are for an EMPTY card, and this one is full.
- It points the user at the input pipeline as the thing to change.

A reply that advises raising batch size, or that treats 88% HBM as headroom to fill, fails.
