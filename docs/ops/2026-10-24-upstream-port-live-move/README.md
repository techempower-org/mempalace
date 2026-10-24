# Runbook: moving an upstream port onto the live palace (2026-10-24, #578)

How the 2026-10 upstream port (#578, upstream through 3.11.0) went live on familiar, as a template for the next one.

| File | What it is |
|---|---|
| `REPORT.md` | The final gate, items a–h: scratch daemon on a production copy, the re-mine cost line, MCP launchers, the wing-prefix question, rollback and move steps, and production observations |
| `HANDOFF.md` | The move-window handoff: go conditions, steps, rollback, and what actually happened |
| `move.sh` / `rollback.sh` | The move and its rollback, as run (move.sh corrected; see below) |
| `daemon-venv-rollback-requirements.txt` | The daemon venv before the move, with mempalace pointed at the local editable checkout |

## Lessons for the next move
- **Check out the target detached** in the live checkout. `move.sh` first tried `switch -c port/upstream-sync`; the
  port's worktree already had that branch checked out, so git refused, right after the daemon stop (~19 s down,
  finished by hand).
- **Take the fresh backup outside the move itself:** its load can OOM the daemon (04:38 that day; REPORT observation
  2). Raise the daemon's MemoryMax for the post-move re-mine drain with a drop-in that sorts after `memory.conf`.
- **"No mine in flight" means processing = 0 and active_mines = 0.** Queued entries are drain deferrals; they persist
  across the restart and drain on the new code.
- **A restored dump needs the AGE graphid fixup and `VACUUM ANALYZE`** before a scratch daemon serves (REPORT,
  production observations 1–2).

## After the move: the re-mine drain
- **Count re-mine progress from the source manifest, not from drawer metadata.** A file is current when its row in
  `mempalace_drawers_source_manifest` (`extract_mode = 'exchange'`) has `convo_chunker_version = 2`. A re-mine stamps
  chunk 0 and the chunks it rewrites. Byte-identical drawers keep no `convo_chunker_version` in their metadata *by
  design* (#575). Counting "drawers without the key" overstated the 10-24 drain: 21,775 looked stale when only the
  dormant files were behind, and one 14-drawer file still showed 10 "stale" after it was current.
  ```sql
  SELECT coalesce(convo_chunker_version::text, 'stale') AS v, count(*)
  FROM mempalace_drawers_source_manifest WHERE extract_mode = 'exchange' GROUP BY 1;
  ```
- **The KG write-through dominates a full re-file.** At ~1.2M Drawer and 1.5M Entity vertices, the per-mention
  `MERGE`/`CREATE` ran ~90% of wall time (76 drawers/min against ~1,050 with `MEMPALACE_KG_WRITETHROUGH=0`). Each
  rewrite also added a second set of MENTIONS edges, because `add_mention` always CREATEs. Turn the write-through off
  for the drain, and on again only after the rewrite fix.
- **The background queue never re-files a dormant file.** The daemon's drain skips a source "unchanged since last
  successful mine" before mempalace's chunker-version check runs. To bring an untouched transcript current, use a
  blocking `POST /mine` (`"background": false`, the file's current wing, `mode: convos`). It takes the same
  `_mine_sem` as the drain.
