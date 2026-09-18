---
slug: extractor-truncated-anchor
shape: an instrument returning a true answer to a narrower question than the one asked
asked: does this anchor resolve?
instrument: an anchor extractor
answered: does the **truncated prefix** resolve? (the regex split at an escaped backtick, so `A BARE FILENAME
  IN \"` was the needle)
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: A-oracle
instances:
- source: Oracle PART 25 §25.10
  ref: scratch/refuted-claims-provenance/oracle-pr-review-2.md
  date: '2026-09-11'
---

# extractor-truncated-anchor

**Asked:** does this anchor resolve?

**Instrument:** `an anchor extractor`

**Actually answered:** does the **truncated prefix** resolve? (the regex split at an escaped backtick, so `A BARE FILENAME IN \"` was the needle)

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** Oracle PART 25 §25.10 (2026-09-11) — `scratch/refuted-claims-provenance/oracle-pr-review-2.md`
