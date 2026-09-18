---
slug: fallback-behind-a-pipe
shape: an instrument returning a true answer to a narrower question than the one asked
asked: is the directory absent?
instrument: '`ls … | sed … || echo ABSENT`'
answered: did the **last** command in the pipeline fail? (a fallback behind a pipe can never fire)
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: A-oracle
instances:
- source: Oracle PART 25 §25.6
  ref: scratch/refuted-claims-provenance/oracle-pr-review-2.md
  date: '2026-09-11'
---

# fallback-behind-a-pipe

**Asked:** is the directory absent?

**Instrument:** ``ls … | sed … || echo ABSENT``

**Actually answered:** did the **last** command in the pipeline fail? (a fallback behind a pipe can never fire)

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** Oracle PART 25 §25.6 (2026-09-11) — `scratch/refuted-claims-provenance/oracle-pr-review-2.md`
