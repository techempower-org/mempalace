---
slug: diff-filter-excludes-list-lines
shape: an instrument returning a true answer to a narrower question than the one asked
asked: how many lines changed?
instrument: diff filter `^[+-][^+-]`
answered: …excluding every markdown list line (all start `+- `) → "0 changed"
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: A-wave
instances:
- source: memorypalace wave 2026-09-11 (instruments-and-liveness §4 / COMMON-DRAIN)
  ref: scratch/refuted-claims-provenance/COMMON-DRAIN.md
  date: '2026-09-11'
---

# diff-filter-excludes-list-lines

**Asked:** how many lines changed?

**Instrument:** `diff filter `^[+-][^+-]``

**Actually answered:** …excluding every markdown list line (all start `+- `) → "0 changed"

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** memorypalace wave 2026-09-11 (instruments-and-liveness §4 / COMMON-DRAIN) (2026-09-11) — `scratch/refuted-claims-provenance/COMMON-DRAIN.md`
