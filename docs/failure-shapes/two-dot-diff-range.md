---
slug: two-dot-diff-range
shape: an instrument returning a true answer to a narrower question than the one asked
asked: what did this branch do?
instrument: '`git diff main..branch`'
answered: tree-vs-tree — the peer's additions render as this branch's deletions (5 false)
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: A-oracle
instances:
- source: Oracle PART 25 §25.12
  ref: scratch/refuted-claims-provenance/oracle-pr-review-2.md
  date: '2026-09-11'
---

# two-dot-diff-range

**Asked:** what did this branch do?

**Instrument:** ``git diff main..branch``

**Actually answered:** tree-vs-tree — the peer's additions render as this branch's deletions (5 false)

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** Oracle PART 25 §25.12 (2026-09-11) — `scratch/refuted-claims-provenance/oracle-pr-review-2.md`
