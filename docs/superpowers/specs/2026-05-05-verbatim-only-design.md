# Verbatim-only mempalace — design

- **Project:** `mempalace` (jphein fork) + `palace-daemon` (jphein fork)
- **Date:** 2026-05-05
- **Status:** Spec — awaiting JP review. Phase 1 (transcript-ingest restoration) shipped same day as PRs jphein/mempalace#2 and jphein/palace-daemon#1; this Phase 2 spec describes the architectural shift that follows.
- **Decided by:** JP, 2026-05-05, in response to "agents don't know how to search both parts." `mempalace_session_recovery` had no semantic-search MCP surface, so checkpoints in it were structurally invisible to `mempalace_search`. Rather than build the missing surface, eliminate the bifurcation.

## One-line summary

Drop the checkpoint summary write path entirely. Stop hook produces only verbatim transcript ingest (Phase 1) — no more 1-KB summary diaries. Retire the `mempalace_session_recovery` collection and the migration that moved checkpoints into it. The palace contains verbatim conversations + manually mined files. Period.

## Goals

1. **One collection, one search path.** `mempalace_drawers` is the only searchable store. No bifurcation, no filter-out-the-other-half logic.
2. **No information lost vs status quo.** Verbatim transcripts already contain the words a checkpoint summary would have surfaced. `mempalace_search` over the transcript IS the recovery query.
3. **Composable with the existing wing taxonomy.** Transcripts continue routing to `wing_<project>` (already done in Phase 1). No new metadata schema, no tags layer, no `kind=` filter.
4. **Clean upstream story.** Phase 2 changes are fork-only; upstream's recovery model still serves users who want summaries. Document the divergence in CLAUDE.md and `fork-changes.yaml`; don't push the abandonment upstream.

## Non-goals

- Replacing transcript ingest. Phase 1 already restored it. Phase 2 only removes the *summary* half of the stop hook.
- Building a tags system. JP raised tags as a future feature, but Phase 2 is "remove the bifurcation," not "build a richer one." Tags are a Phase 4+ design.
- Reverting the wing-derivation behavior change from Phase 1 — `wing_<project>` for transcripts stays.
- Touching upstream PRs that ship checkpoint routing (`rboarescu/palace-daemon` #8, #14, #18). They serve users on the upstream summary model and are no longer on JP's path; leave open.

## Context — what exists, what changes

| Thing | State after Phase 1 | Changes in Phase 2 |
|---|---|---|
| Stop hook → `_save_diary_direct` → `tool_diary_write` → `mempalace_session_recovery` collection | Writes a 1-KB checkpoint summary on every fire | **Deleted.** Stop hook no longer calls `_save_diary_direct`. |
| Stop hook → `_ingest_transcript` → daemon `/mine` | Writes verbatim transcript chunks to `mempalace_drawers` (per-project wing) | **Unchanged.** This becomes the only Stop-hook write path. |
| `mempalace_session_recovery` collection (763 drawers as of 2026-05-05) | Active; receives all checkpoints | **Emptied** — production data is verbatim summaries of conversations whose verbatim text already lives in `mempalace_drawers`. JP confirms (this spec gates on his ack) that he wants hard delete, not archive-rename. |
| `tool_session_recovery_read` MCP tool | Filter-only reader (session_id / agent / since / wing) | **Deleted.** No collection to read. |
| `tool_diary_write` topic-routing branch (`if topic in _CHECKPOINT_TOPICS:`) | Routes to recovery collection | **Deleted branch.** Diary writes always go to `mempalace_drawers`. The remaining diary-write callers (agent journals, decision logs) keep working. |
| `mempalace.migrate.migrate_checkpoints_to_recovery` | Idempotent walk that moves checkpoints from `mempalace_drawers` → `mempalace_session_recovery` | **Deleted.** Production already migrated; Phase 2 reverses the migration via a one-shot delete-then-purge of the recovery collection contents. |
| `mempalace repair --mode reorganize` (CLI dispatch) | Calls `migrate_checkpoints_to_recovery` | **Deleted mode.** `mempalace repair` keeps `--mode rebuild` (HNSW-from-sqlite). |
| palace-daemon `lifespan` auto-migrate (`PALACE_AUTO_MIGRATE_CHECKPOINTS=1`) | Calls migrate function on startup | **Deleted.** Lifespan no longer migrates. |
| `tests/test_session_recovery.py` (12 tests) | Active | **Deleted.** Tests assume the parallel-collection design. |
| `tests/test_migrate.py::TestMigrateCheckpointsToRecovery` (6 tests) | Active | **Deleted.** Module being removed. |
| PreCompact hook → `_save_diary_direct` recovery marker | Writes checkpoint marker on context-compaction event | **Deleted.** PreCompact still triggers transcript ingest via `_ingest_transcript`; no diary marker. |
| CLAUDE.md row 23 (checkpoint collection split — phases A–E) | Records the build-up | **Updated** — row marked "reverted in Phase 2 / 2026-05-05" with cross-reference to this spec. The architectural narrative is preserved as historical record of the round trip. |
| `docs/fork-changes.yaml` | Has the row 23 entry | **New entry** for Phase 2 reversion + replaces row 23 status. |

## Architecture

### Hook write paths (after Phase 2)

```
Stop hook fire:
  └── _ingest_transcript(transcript_path)
       └── (daemon-strict) POST /mine {dir, wing=wing_<project>, mode=convos}
       └── (local) subprocess.Popen([mempalace, mine, dir, --mode=convos, --wing=wing_<project>])

PreCompact hook fire:
  └── _ingest_transcript(transcript_path)  # same as Stop, sync version via _mine_sync if MEMPAL_DIR is set
```

That's it. No diary write, no checkpoint summary, no recovery marker.

### Collection layout

- `mempalace_drawers` — single collection. Contains every searchable drawer: manually-mined project files (`wing_techempower`, `wing_realmwatch`, etc.), verbatim conversation chunks (`wing_<project>` per Phase 1), agent diary entries (non-checkpoint topics), knowledge-graph drawers.
- `mempalace_session_recovery` — **deleted at Phase 2 deploy time.** Hard-delete via the daemon's `/repair --mode purge-recovery` (new mode, see below) or a one-shot `chromadb.PersistentClient.delete_collection("mempalace_session_recovery")`.

### MCP surface (after Phase 2)

| Tool | Status | Notes |
|---|---|---|
| `mempalace_search` | Unchanged | Already only reads `mempalace_drawers`. The deleted `kind=` filter is already gone (CLAUDE.md row 21). |
| `mempalace_diary_read` | Unchanged | Reads from `mempalace_drawers`; topic filter still works for non-checkpoint topics. |
| `mempalace_diary_write` | **Simplified** | Topic-routing branch deleted. Always writes to `mempalace_drawers`. |
| `mempalace_session_recovery_read` | **Deleted** | No corresponding collection. |
| All other tools | Unchanged | Knowledge graph, tunnels, status, etc. — none touched the recovery collection. |

## Migration

### Production data (canonical 151K palace)

- `mempalace_drawers`: ~150K drawers. **Untouched.**
- `mempalace_session_recovery`: 763 drawers (per CLAUDE.md row 23 cleanup observation, 2026-04-27). **Hard-deleted.**

JP confirmed during the design conversation (this spec gates on the explicit ack at review): the 763 checkpoint summaries don't carry information that isn't also in `mempalace_drawers` via verbatim transcript chunks. Hard-delete, no archive.

### One-shot migration script

```python
# scripts/phase2_purge_recovery.py — runs once, on disks, after Phase 2 code is deployed.
import chromadb
client = chromadb.PersistentClient(path="/mnt/raid/projects/mempalace-data/palace")
try:
    client.delete_collection("mempalace_session_recovery")
    print("Deleted mempalace_session_recovery")
except ValueError:
    print("Already absent")
```

Run after the daemon is updated, before clients reconnect. Reversible by running the migration backward — but with the migration code deleted, that requires checkout of pre-Phase-2 code. JP signs off on irreversibility at review.

### Code deletion checklist

**In `~/Projects/memorypalace`:**
- `mempalace/migrate.py` — delete (or keep as empty module if downstream imports `from mempalace.migrate import ...` exist; check first)
- `mempalace/mcp_server.py::tool_session_recovery_read` — delete handler + remove from `TOOLS` dict
- `mempalace/mcp_server.py::_get_session_recovery_collection` — delete
- `mempalace/palace.py::get_session_recovery_collection` — delete
- `mempalace/palace.py::_SESSION_RECOVERY_COLLECTION` constant — delete
- `mempalace/palace.py::_CHECKPOINT_TOPICS` and topic-routing branch in `tool_diary_write` — delete
- `mempalace/cli.py` `repair` dispatch — remove `reorganize` mode
- `mempalace/hooks_cli.py::_save_diary_direct` — delete the function (keep the imports it depended on if used elsewhere; audit callers first)
- `mempalace/hooks_cli.py::hook_stop` — remove the `_save_diary_direct` call
- `mempalace/hooks_cli.py::hook_precompact` — remove the diary-write branch
- `tests/test_session_recovery.py` — delete file
- `tests/test_migrate.py::TestMigrateCheckpointsToRecovery` — delete class (keep file if other migrate tests exist)
- `website/reference/mcp-tools.md` — remove `mempalace_session_recovery_read` doc entry

**In `~/Projects/palace-daemon`:**
- `main.py::lifespan` — remove the `migrate_checkpoints_to_recovery` block + `PALACE_AUTO_MIGRATE_CHECKPOINTS` env var
- `main.py::repair` `reorganize` mode — remove
- Add new mode `/repair?mode=purge-recovery` (one-shot, idempotent: deletes the `mempalace_session_recovery` collection if present, returns 200 either way) — for the deploy script to call cleanly

**In `~/Projects/memorypalace/CLAUDE.md`:**
- Row 23 status updated to "reverted in Phase 2 (2026-05-05) — see specs/2026-05-05-verbatim-only-design.md"
- New row entry for Phase 2 in the fork-change-queue table
- `docs/fork-changes.yaml` entry

## Test plan

After deletions:
- `python -m pytest tests/ -q` — full suite green; expect 12 tests removed from `test_session_recovery.py` and 6 from `test_migrate.py`. Net change: -18 tests, 0 new tests (all the surviving paths already had coverage).
- Manual verify on dev palace: `mempalace_diary_write` of a checkpoint-topic entry lands in `mempalace_drawers`, not in any other collection.
- Manual verify on dev palace: `mempalace_search` of recent session content returns hits (not the empty-result symptom that drove this work).

After deploy on disks:
- `curl /repair -d '{"mode":"purge-recovery"}'` returns 200.
- `curl /stats` shows only `mempalace_drawers`.
- A live Stop hook fire writes only the transcript chunks to `mempalace_drawers`; nothing to recovery (because there's no collection).
- `mempalace_session_recovery_read` MCP call returns "tool not found" or a clean error.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Hard-delete of 763 checkpoints is irreversible | Pre-deploy: dump the recovery collection to a tarball at `/mnt/raid/backups/recovery-2026-05-05.tar.gz`. If JP later wants something back, it's recoverable from that tarball. Tarball is gitignored, manual cleanup. |
| Upstream may merge the checkpoint-routing PRs (#8, #14, #18) and create merge conflicts on next sync | Document in CLAUDE.md row entry that fork is intentionally diverged. Future syncs handle conflicts by keeping fork-side deletion. |
| Other code paths depend on `_CHECKPOINT_TOPICS` constant | Grep first; the constant only appears in the topic-routing branch and tests, both of which are being deleted. |
| Future-self forgets the architecture and re-introduces summary writing | Memory note saved (`memory/project_verbatim_only_shift.md`). Spec preserved here. CLAUDE.md row updated. |

## Sequencing (post-Phase-1 verification)

1. **Land Phase 1 first.** PRs jphein/mempalace#2 + jphein/palace-daemon#1 reviewed by Copilot and merged. Daemon redeployed. Verify transcripts mining for ~24 hours of normal use; backfill the 11-day gap with `phase1_backfill.sh` (Task #16).
2. **Open Phase 2 PRs.** Two PRs, mirroring Phase 1:
   - `jphein/mempalace fix/drop-checkpoint-write-path` — code + test deletions
   - `jphein/palace-daemon fix/drop-recovery-migration` — lifespan + repair-mode changes
3. **Copilot review + JP approval, then merge.**
4. **Deploy + run purge.** `pip install --upgrade /mnt/raid/projects/memorypalace`, `systemctl --user restart palace-daemon`, then `curl /repair -d '{"mode":"purge-recovery"}'`.
5. **Verify post-deploy.** A few Stop hook fires, then `mempalace_search` over recent content; assert non-empty + non-checkpoint results. `curl /stats` confirms one collection only.
6. **Update docs.** CLAUDE.md row 23 status, fork-changes.yaml, MEMORY.md.

## Open questions for JP at review

1. **Hard-delete confirm.** This spec assumes "delete the 763 checkpoints, no archive." Tarball-then-delete is the proposed safety net. OK?
2. **Upstream PR posture.** Leave #8, #14, #18 open silently? Comment on them with the divergence reason? Close them? Recommend: leave open, no comment until/unless upstream review activity makes it timely.
3. **Pre-Phase-2 verify duration.** Is "24 hours of normal Phase 1 use" enough before pulling Phase 2 trigger, or want longer? Recommend 48 hours minimum so we see at least one PreCompact event in the wild.
