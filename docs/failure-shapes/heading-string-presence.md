---
slug: heading-string-presence
shape: an instrument returning a true answer to a narrower question than the one asked
asked: is every section still present?
instrument: a heading-presence check
answered: does this exact heading *string* appear? (a retitled heading with a byte-identical body read
  as MISSING — a mis-specified invariant, 3 false reds)
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: A-oracle
instances:
- source: Oracle PART 25 §25.1
  ref: scratch/refuted-claims-provenance/oracle-pr-review-2.md
  date: '2026-09-11'
---

# heading-string-presence

**Asked:** is every section still present?

**Instrument:** `a heading-presence check`

**Actually answered:** does this exact heading *string* appear? (a retitled heading with a byte-identical body read as MISSING — a mis-specified invariant, 3 false reds)

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** Oracle PART 25 §25.1 (2026-09-11) — `scratch/refuted-claims-provenance/oracle-pr-review-2.md`
