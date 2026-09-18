---
slug: escaped-backslash-in-heredoc
shape: an instrument returning a true answer to a narrower question than the one asked
asked: are the links there?
instrument: '`r''…([a-z\\-]+)\\.md''` in a quoted heredoc'
answered: is there a literal backslash? (matched nothing; reported "no links" about a fix that had landed)
control_passed: true
direction: false-alarm
direction_basis: as-observed
provenance: A-oracle
instances:
- source: Oracle PART 25 §25.15
  ref: scratch/refuted-claims-provenance/oracle-pr-review-2.md
  date: '2026-09-11'
---

# escaped-backslash-in-heredoc

**Asked:** are the links there?

**Instrument:** ``r'…([a-z\\-]+)\\.md'` in a quoted heredoc`

**Actually answered:** is there a literal backslash? (matched nothing; reported "no links" about a fix that had landed)

**Direction (as observed):** `false-alarm` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** Oracle PART 25 §25.15 (2026-09-11) — `scratch/refuted-claims-provenance/oracle-pr-review-2.md`
