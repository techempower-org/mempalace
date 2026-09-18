---
slug: tally-line
shape: an instrument returning a true answer to a narrower question than the one asked
asked: how many failure shapes are in Oracle PART 25?
instrument: that report's closing tally line
answered: what number did the author *state* in the summary?
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: authored-here
instances:
- source: 'added during #505/#511'
  ref: docs/specs/2026-09-17-failure-shape-index.md
  date: '2026-09-17'
---

# tally-line

**Asked:** how many failure shapes are in Oracle PART 25?

**Instrument:** `that report's closing tally line`

**Actually answered:** what number did the author *state* in the summary?

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** added during #505/#511 (2026-09-17) — `docs/specs/2026-09-17-failure-shape-index.md`
