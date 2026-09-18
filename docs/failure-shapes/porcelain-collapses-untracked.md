---
slug: porcelain-collapses-untracked
shape: an instrument returning a true answer to a narrower question than the one asked
asked: which files are new?
instrument: '`git status --porcelain`'
answered: 'which **top-level entries** are new? (it collapses untracked dirs: 2 shown, 15 under `-uall`)'
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: A-oracle
instances:
- source: Oracle PART 25 §25.6
  ref: scratch/refuted-claims-provenance/oracle-pr-review-2.md
  date: '2026-09-11'
---

# porcelain-collapses-untracked

**Asked:** which files are new?

**Instrument:** ``git status --porcelain``

**Actually answered:** which **top-level entries** are new? (it collapses untracked dirs: 2 shown, 15 under `-uall`)

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** Oracle PART 25 §25.6 (2026-09-11) — `scratch/refuted-claims-provenance/oracle-pr-review-2.md`
