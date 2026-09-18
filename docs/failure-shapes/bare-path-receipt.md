---
slug: bare-path-receipt
shape: an instrument returning a true answer to a narrower question than the one asked
asked: why did the palace search fail?
instrument: 'the #497 hook''s receipt under a bare `PATH`'
answered: did the command exit non-zero? — it emits `✗ no answer in 2s (5ms)`, **two numbers in one receipt
  that contradict each other**
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: authored-here
instances:
- source: 'added during #505/#511'
  ref: docs/specs/2026-09-17-failure-shape-index.md
  date: '2026-09-17'
---

# bare-path-receipt

**Asked:** why did the palace search fail?

**Instrument:** `the #497 hook's receipt under a bare `PATH``

**Actually answered:** did the command exit non-zero? — it emits `✗ no answer in 2s (5ms)`, **two numbers in one receipt that contradict each other**

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** added during #505/#511 (2026-09-17) — `docs/specs/2026-09-17-failure-shape-index.md`
