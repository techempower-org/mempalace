---
slug: own-vocabulary-grep
shape: an instrument returning a true answer to a narrower question than the one asked
asked: did the README row render?
instrument: '`grep -c ''failure-shape''`'
answered: does the **lowercase-hyphenated literal** appear? — the rendered text is "failure SHAPES" (different
  case, a space not a hyphen), so a present-and-correct row read as missing
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: authored-here
instances:
- source: 'added during #505/#511'
  ref: docs/specs/2026-09-17-failure-shape-index.md
  date: '2026-09-17'
---

# own-vocabulary-grep

**Asked:** did the README row render?

**Instrument:** ``grep -c 'failure-shape'``

**Actually answered:** does the **lowercase-hyphenated literal** appear? — the rendered text is "failure SHAPES" (different case, a space not a hyphen), so a present-and-correct row read as missing

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** added during #505/#511 (2026-09-17) — `docs/specs/2026-09-17-failure-shape-index.md`
