---
slug: docstring-asserts-an-unimplemented-control
shape: an instrument returning a true answer to a narrower question than the one asked
asked: does the code enforce what this docstring says it enforces?
instrument: a docstring or comment adjacent to the code
answered: what did the author INTEND when they wrote it? — prose is trusted because proximity reads as
  authority, and it is never re-derived from the code beneath it
control_passed: true
direction: false-confidence
direction_basis: as-observed
provenance: added-by-review
instances:
- source: 'nebula-claude-md-audit, #531 — the rename control'
  ref: techempower-org/mempalace#531
  date: '2026-09-18'
  detail: The harness docstring asserted "a renamed file must fail the set check". The code compared slug
    SETS, which are invariant under rename until slug == stem is also checked. The pre-registered rename
    control passed when it should have failed; the missing half was added and all three controls then
    behaved.
- source: oracle-issue-audit, PART 26.5
  ref: techempower-org/mempalace#499
  date: '2026-09-17'
  detail: '`_fail_daemon`''s docstring read "exit 1 (matches cmd_why / cmd_tags / cmd_graph)". Those three
    commands call it zero times. The claim scoped #499 to 4 verbs when 16 were affected.'
not_this: Distinct from `_fail_daemon_unavailable`, where the documented condition is satisfied-but-insufficient
  rather than false. Here the prose describes a check that does not exist.
---

# docstring-asserts-an-unimplemented-control

**Asked:** does the code enforce what this docstring says it enforces?

**Instrument:** a docstring or comment adjacent to the code

**Actually answered:** what did the author *intend* when they wrote it? Prose next to code is
trusted because proximity reads as authority, and it is almost never re-derived from the code
beneath it.

**Direction (as observed):** `false-confidence` — direction belongs to the (mechanism, question)
pair, so this records how the instances fell, not the only way the mechanism can fail. See
`docs/specs/2026-09-17-failure-shape-index.md`.

**Control passed:** yes — and unusually cleanly. The docstring is *accurate about intent*, the
code runs, and the tests pass. Nothing in the normal signal set contradicts it.

**Not this shape:** `_fail_daemon_unavailable`, where the documented condition is
satisfied-but-insufficient rather than false. Here the prose describes a check that does not exist.

## Instances

1. **#531, the rename control** (2026-09-18) — the harness docstring asserted *"a renamed file
   must fail the set check"*. The code compared slug **sets**, which are invariant under rename
   until `slug == stem` is also checked. The pre-registered rename control passed when it should
   have failed. Added the missing half; all three controls then behaved.
2. **Oracle PART 26.5** (2026-09-17) — `_fail_daemon`'s docstring read *"exit 1 (matches
   `cmd_why` / `cmd_tags` / `cmd_graph`)"*. Those three commands call it **zero** times. The claim
   scoped #499 to 4 verbs when 16 were affected.

⭐ Both were caught by executing the claim, never by reading it. A docstring is the one artefact
whose reader is least likely to re-derive it, because it sits where the answer should be.
