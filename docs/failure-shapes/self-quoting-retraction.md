---
slug: self-quoting-retraction
shape: an instrument returning a true answer to a narrower question than the one asked
asked: does the fix remove the warning?
instrument: '`check-docs.sh` after the fix, on the tree that **documents** the fix'
answered: does any line pair the number with a state word? — the changelog entry describing the defect
  re-supplied both, so the warning returned on a correct fix
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: authored-here
instances:
- source: 'added during #505/#511'
  ref: docs/specs/2026-09-17-failure-shape-index.md
  date: '2026-09-17'
---

# self-quoting-retraction

**Asked:** does the fix remove the warning?

**Instrument:** `check-docs.sh` after the fix, on the tree that **documents** the fix

**Actually answered:** does any line pair the number with a state word? — the changelog entry describing the defect re-supplied both, so the warning returned on a correct fix

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** added during #505/#511 (2026-09-17) — `docs/specs/2026-09-17-failure-shape-index.md`
