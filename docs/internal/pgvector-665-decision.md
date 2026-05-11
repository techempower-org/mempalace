# Decision: #665 composition stance — WAIT (with Plan-B trigger)

**Date:** 2026-05-11 PDT
**Plan task:** Phase 0, Task 0.2 of `docs/superpowers/plans/2026-05-10-pgvector-age-migration-impl.md`
**Author:** Claude (Opus 4.7, 1M context) for JP

---

## Decision

**WAIT for upstream PR #665 to merge, then cherry-pick / sync.** Phase 1 starts at **Task 1.A.1** ("Wait" path), not Task 1.B.1 ("Fork-port" path).

**Plan-B trigger (revisit decision when ANY fires):**
- #665 sees no maintainer activity past **2026-06-08** (4 weeks from today), OR
- skuznetsov stops responding to the open review thread (currently engaged), OR
- A blocking review concern remains unresolved 14+ days after we open implementation work on the "wait" path, OR
- We hit a blocker in Phase 1 that requires patching #665's internals to land our AGE-layer work.

If any trigger fires, switch to fork-port path. The 22 days of staleness *so far* are within tolerance because skuznetsov rewrote the PR on 2026-04-19 in response to #995 landing — that's an active author, not an abandoned PR.

## State of #665 as of 2026-05-11

| Field | Value |
|---|---|
| State | OPEN |
| Author | skuznetsov (Sergey Kuznetsov) |
| Last update | 2026-04-19 (22 days ago) |
| Last commit | 2026-04-19 — full rewrite on top of #995/RFC 001 backend contract |
| Size | +1839 / -11, 12 files |
| Base | `develop` |
| Mergeable (gh API) | UNKNOWN (matches our local 4-conflict observation) |
| Labels | area/mcp, area/search, storage, area/install |
| Open concerns | dekoza raised `pg_sorted_heap` bus-factor concern 2026-04-13; skuznetsov acknowledged 2026-04-13, rewrote PR on 2026-04-19 to make pg_sorted_heap optional with pgvector fallback. Thread is not closed. |

## Conflict probe on our `main` (commit `6827b97`)

```
git merge --no-commit --no-ff pr-665 → 4 conflicts:
  README.md                 (trivial — fork README vs upstream README; always-fork-wins per memory)
  mempalace/palace.py       32 LOC of conflict markers — substantive but small
  tests/test_backends.py    19 LOC of conflict markers — substantive but small
  uv.lock                   (trivial — regenerate)
```

Total substantive conflict surface: ~51 LOC across 2 code files. **Moderate**, not heavy. Resolvable in 15–30 min when we cherry-pick.

## Architecture summary of #665 (from PR description)

- Implements `BaseBackend` + `BaseCollection` from the merged #995/RFC 001 contract.
- Backend registry discovery + env/config selection. ChromaDB stays default.
- PostgreSQL collection supports **two paths gated by extension availability**:
  - **Preferred:** `pg_sorted_heap` (niche, bus-factor concern flagged)
  - **Fallback:** `pgvector` (what we'll actually run on `apache/age:release_PG16_1.6.0` since `pg_sorted_heap` isn't in apt or the AGE image)
- INSERT … SELECT FROM unnest() + ON CONFLICT for batch writes.
- First-wins `add()`, last-wins `upsert()`.
- `vector` index created lazily after a threshold.
- Reuses Chroma's default local embedding function — postgres extra only adds `psycopg2-binary`, no new ML stack.
- Tests: 1044 passed on author's machine. Files: `tests/test_backends.py` updated; new tests for filter translation, batch upsert, typed result shapes.

## Why "Wait"

1. **Conflict surface is moderate.** ~51 LOC across 2 code files, plus 2 trivial files. Not heavy enough to justify reimplementing 1839 LOC.
2. **#665 is comprehensive.** It already covers BaseBackend conformance, registry discovery, env routing, batch insert, lazy indexes, typed results. Rewriting that surface area duplicates work we'd want anyway.
3. **`pg_sorted_heap` is opt-in, not invasive.** Gated by `if extension_installed → use it; else → use pgvector`. Our `apache/age:release_PG16_1.6.0` image won't have pg_sorted_heap, so we'd silently run the pgvector fallback — the exact path we wanted.
4. **Upstream flow is preserved.** Building on top of #665 keeps our AGE-layer work proposable as clean follow-on PRs that compose on the BaseBackend contract, rather than a parallel impl that diverges from upstream's evolution.
5. **Author is engaged, not abandoning.** skuznetsov rewrote the PR on 2026-04-19 specifically to compose on the merged backend contract. That's a thoughtful, responsive maintainer.
6. **Staleness isn't yet alarming.** 22 days from the rewrite is within tolerance for a substantial 1839-LOC PR awaiting maintainer review. We bake in a trigger to revisit if it stretches.

## Why NOT "Fork-port"

- We'd be reimplementing roughly 1200–1500 LOC (pg_sorted_heap path stripped, plus tests).
- Risk of duplicating subtle bugs #665 has already fixed (the UNKNOWN mergeable state in part reflects that #665 is moving target, but that's a feature: skuznetsov is refining it).
- When #665 eventually merges, we'd have to reconcile two parallel implementations — net cost is higher than the savings from skipping the cherry-pick.
- The dekoza-vs-skuznetsov pg_sorted_heap thread is open and pertinent — being a real-world consumer of the pgvector fallback path is *valuable signal* to attach to that thread later (per `feedback_upstream_comment_timing`: defer the comment until we have working code).

## Phase 1 implications

Phase 1 starts at **Task 1.A.1** ("Wait" path):

1. Cherry-pick #665's commit range onto `feat/pgvector-age-impl`.
2. Resolve the 4 conflicts (README — fork-wins; palace.py — manual; test_backends.py — manual; uv.lock — regenerate).
3. Run the full existing test suite (`pytest -x -q` against ChromaDB default) — must stay green; any regression in non-postgres code paths is fixed before moving on.
4. Write the smoke test `tests/test_backends_postgres.py::test_postgres_backend_smoke` (already specified in plan Task 1.A.1 Step 3).
5. Spin a `mempalace-db` service in `/opt/mediaserver/docker-compose.yml` on `disks` with the `apache/age:release_PG16_1.6.0`-based image. Bind on `disks` LAN IP (chosen during Phase 1 setup; defaults to ssh tunnel if LAN-bind unsafe).
6. Run smoke test against the running container.

The "Task 1.A.1 → 1.A.5 → onward" sequence in the plan stands as written. Phase 1's remaining tasks (1.2 index conformance, 1.3 where-clause translation, 1.4 CI matrix) are common to both decision branches.

## Tracking obligations

Adding to `scratch/promises.md`:
1. Watch #665 for merge or activity-stall (trigger date 2026-06-08).
2. After Phase 1 lands and smoke test is green, draft a comment for #665's dekoza-vs-skuznetsov thread offering us as a real-world data point for the pgvector-fallback path. **Do not file before code lands**, per `feedback_upstream_comment_timing`.
3. If we make non-trivial improvements to the cherry-picked backend (better error handling, indexes, etc.), file as a follow-on PR composing on top of #665 — don't graft into our cherry-pick.

## Final note

This decision document moves to `docs/internal/pgvector-665-decision.md` per Task 0.2 Step 4 and is committed on `feat/pgvector-age-impl` (not on main per `feedback_own_projects_pr_workflow`).
