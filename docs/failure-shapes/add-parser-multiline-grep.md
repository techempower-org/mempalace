---
slug: add-parser-multiline-grep
shape: an instrument returning a true answer to a narrower question than the one asked
asked: is the command registered?
instrument: a grep for `add_parser(`
answered: is it registered *on one line*? (multi-line call → false 0)
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: A-wave
instances:
- source: memorypalace wave 2026-09-11 (instruments-and-liveness §4 / COMMON-DRAIN)
  ref: scratch/refuted-claims-provenance/COMMON-DRAIN.md
  date: '2026-09-11'
---

# add-parser-multiline-grep

**Asked:** is the command registered?

**Instrument:** `a grep for `add_parser(``

**Actually answered:** is it registered *on one line*? (multi-line call → false 0)

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** memorypalace wave 2026-09-11 (instruments-and-liveness §4 / COMMON-DRAIN) (2026-09-11) — `scratch/refuted-claims-provenance/COMMON-DRAIN.md`
