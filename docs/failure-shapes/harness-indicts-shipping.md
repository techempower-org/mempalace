---
slug: harness-indicts-shipping
shape: an instrument returning a true answer to a narrower question than the one asked
asked: does the matcher fix break the check?
instrument: the two-way control written to prove it
answered: '*(nothing — the harness was broken)*: `|` used as a field delimiter in strings containing `|`,
  so both arms reported BLIND'
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: authored-here
instances:
- source: 'added during #505/#511'
  ref: docs/specs/2026-09-17-failure-shape-index.md
  date: '2026-09-17'
---

# harness-indicts-shipping

**Asked:** does the matcher fix break the check?

**Instrument:** `the two-way control written to prove it`

**Actually answered:** *(nothing — the harness was broken)*: `|` used as a field delimiter in strings containing `|`, so both arms reported BLIND

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** added during #505/#511 (2026-09-17) — `docs/specs/2026-09-17-failure-shape-index.md`
