---
slug: single-line-grep-wrapped-prose
shape: an instrument returning a true answer to a narrower question than the one asked
asked: is the phrase present?
instrument: '`grep -c ''CHANNEL LIST''`'
answered: is it present *on one line*? (hard-wrapped → 0 on correct text)
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: A-wave
instances:
- source: memorypalace wave 2026-09-11 (instruments-and-liveness §4 / COMMON-DRAIN)
  ref: scratch/refuted-claims-provenance/COMMON-DRAIN.md
  date: '2026-09-11'
---

# single-line-grep-wrapped-prose

**Asked:** is the phrase present?

**Instrument:** ``grep -c 'CHANNEL LIST'``

**Actually answered:** is it present *on one line*? (hard-wrapped → 0 on correct text)

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** memorypalace wave 2026-09-11 (instruments-and-liveness §4 / COMMON-DRAIN) (2026-09-11) — `scratch/refuted-claims-provenance/COMMON-DRAIN.md`
