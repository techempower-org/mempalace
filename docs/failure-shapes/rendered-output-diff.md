---
slug: rendered-output-diff
shape: an instrument returning a true answer to a narrower question than the one asked
asked: did the data change?
instrument: diff of rendered output
answered: did the output change? (loader normalises the differing field)
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: A-wave
instances:
- source: memorypalace wave 2026-09-11 (instruments-and-liveness §4 / COMMON-DRAIN)
  ref: scratch/refuted-claims-provenance/COMMON-DRAIN.md
  date: '2026-09-11'
---

# rendered-output-diff

**Asked:** did the data change?

**Instrument:** `diff of rendered output`

**Actually answered:** did the output change? (loader normalises the differing field)

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** memorypalace wave 2026-09-11 (instruments-and-liveness §4 / COMMON-DRAIN) (2026-09-11) — `scratch/refuted-claims-provenance/COMMON-DRAIN.md`
