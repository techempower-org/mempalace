---
slug: hardcoded-path-after-rename
shape: an instrument returning a true answer to a narrower question than the one asked
asked: is the file present after the merge?
instrument: one hardcoded path
answered: is it at *that* path? (a peer's rename falsifies it; blob `f46b601d` identical both sides)
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: A-oracle
instances:
- source: Oracle PART 25 §25.13
  ref: scratch/refuted-claims-provenance/oracle-pr-review-2.md
  date: '2026-09-11'
---

# hardcoded-path-after-rename

**Asked:** is the file present after the merge?

**Instrument:** `one hardcoded path`

**Actually answered:** is it at *that* path? (a peer's rename falsifies it; blob `f46b601d` identical both sides)

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** Oracle PART 25 §25.13 (2026-09-11) — `scratch/refuted-claims-provenance/oracle-pr-review-2.md`
