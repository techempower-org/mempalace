---
slug: sha-prefix-match
shape: an instrument returning a true answer to a narrower question than the one asked
asked: 'does a doc claim PR #459 is OPEN?'
instrument: '`check-docs.sh`''s PR-state step'
answered: does the token "open" appear on a line containing the string `459`? — `/459` matched the **commit
  sha** `459efab`, and "open" came from "**open**-and-refuse sequence"
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: authored-here
instances:
- source: 'added during #505/#511'
  ref: docs/specs/2026-09-17-failure-shape-index.md
  date: '2026-09-17'
---

# sha-prefix-match

**Asked:** does a doc claim PR #459 is OPEN?

**Instrument:** ``check-docs.sh`'s PR-state step`

**Actually answered:** does the token "open" appear on a line containing the string `459`? — `/459` matched the **commit sha** `459efab`, and "open" came from "**open**-and-refuse sequence"

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question) pair, so this
records how the instance fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — which is what makes this a failure shape rather than a bug. The positive
control proved the reader worked, not that the reader was aimed at the question asked.

**Instance:** added during #505/#511 (2026-09-17) — `docs/specs/2026-09-17-failure-shape-index.md`
