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
