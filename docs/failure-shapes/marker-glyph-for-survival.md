---
slug: marker-glyph-for-survival
shape: an instrument returning a true answer to a narrower question than the one asked
asked: did the rules survive the split?
instrument: a marker-grep over rule lines
answered: how many lines carry a marker glyph? (363 candidates; wrong instrument for survival)
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: A-oracle
instances:
- source: Oracle PART 25 §25.9
  ref: scratch/refuted-claims-provenance/oracle-pr-review-2.md
  date: '2026-09-11'
---

# marker-glyph-for-survival

**Asked:** did the rules survive the split?

**Instrument:** `a marker-grep over rule lines`

**Actually answered:** how many lines carry a marker glyph? (363 candidates; wrong instrument for survival)

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** Oracle PART 25 §25.9 (2026-09-11) — `scratch/refuted-claims-provenance/oracle-pr-review-2.md`
