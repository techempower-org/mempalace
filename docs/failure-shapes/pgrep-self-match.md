---
slug: pgrep-self-match
shape: an instrument returning a true answer to a narrower question than the one asked
asked: is the process running?
instrument: pgrep -af <pattern>
answered: does any cmdline contain the pattern? — including this script’s own
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: 2g-sourced
instances:
- source: '2g-c6 session, reported in #503'
  ref: techempower-org/mempalace#503
  date: '2026-09-17'
---

# pgrep-self-match

**Asked:** is the process running?

**Instrument:** `pgrep -af <pattern>`

**Actually answered:** does any cmdline contain the pattern? — including this script’s own

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** 2g-c6 session, reported in #503 (2026-09-17) — `techempower-org/mempalace#503`
