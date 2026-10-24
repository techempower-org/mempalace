# HANDOFF — memorypalace port live move (Sat 2026-10-24, window from 05:15, hard stop 06:30 PDT)

Lane: morpheus-palace / palace-nemotron-d9. Lead: jp-58 (`uds:/run/user/1000/cc-socks/946638.sock`).
PR techempower-org/mempalace#578, head **95026931**; CI re-gated PASS by jp-58 (29 runs: 25 success, 4 skipped).
Full gate report: `final-gate/REPORT.md`. This folder: `~/Projects/memorypalace/scratch/upstream-sync-2026-10-21/final-gate/`.

## Done
- Fresh backup taken: `sudo systemctl start mempalace-backup` → `/srv/backups/databases/mempalace-20261024.sql.gz`
  (3,533,704,929 bytes, 04:43, Result=success). This morning's 02:22 dump is copied to
  `/mnt/ollama-models/mempalace-gate/dumps/`.
- llama-server-extractor was already inactive and disabled before the backup; nothing to restore.
- Pre-move baseline (04:35): health ok, daemon 1.10.0, `/status/fast` **1,230,649 drawers / 343 wings**.
- The pre-move venv freeze is saved, with a rollback requirements file pointing mempalace at the local editable
  checkout.
- Scripts written and syntax-checked: `move.sh`, `rollback.sh`.
- **04:38:31: production palace-daemon was OOM-killed by its own 4 GB memcg (not by me); systemd restarted it at
  04:38:44.** Likely chain: my backup's pg_dump load → status fast path hit statement_timeout (journal 04:39) → the
  all-rows fallback blew the cap. That's REPORT.md observation #2. Tell jp-58 in the board line.

- **05:4x, on jp-58's ask:** drop-in `/etc/systemd/system/palace-daemon.service.d/zz-port-move-memory.conf`
  (MemoryMax=6G, MemoryHigh=5G, reason in its comment), plus `daemon-reload`. Verified live: cgroup memory.max =
  6442450944, memory.high = 5368709120. It's named zz- because memory.conf (4G) sorts after the older
  system.control 50-MemoryMax.conf (8G, shadowed). rollback.sh deletes it and reloads. Put the new MemoryMax and the
  MemoryPeak in the board line.

## Go conditions (check right before running)
1. `curl -s -H "X-API-Key: $PALACE_API_KEY" http://localhost:8085/mine/status`: queued, processing and active_mines
   all 0 (move.sh aborts by itself otherwise).
2. ListAgents: every lane at its prompt. Peek panes with `tmux capture-pane -p -t %N`; "shell" in ListAgents can be a
   background monitor, so read the pane.
3. Time before 06:30. Past 06:30: STOP, don't run, and re-propose to jp-58 (the 40–60 min drain must not overlap the
   08:00 wake-ups).

## Steps
1. Run `final-gate/move.sh` as ONE Bash call. It does:
   1. re-check the queue, a clean tree, and HEAD == 6641c559;
   2. stop palace-daemon;
   3. switch the live checkout `~/Projects/memorypalace` to a local branch `port/upstream-sync` tracking origin and
      ff to 95026931;
   4. `uv pip install -e .[postgres]` into `~/.local/share/palace-daemon/venv`;
   5. start the daemon and wait for /health.
2. Verify, then send jp-58 the board line below:
   - `/health`;
   - `/status/fast` count (expect ≥ 1,230,649);
   - one hook-style `POST /mine` of a small dormant transcript, with `wing` = its CURRENT stored wing so nothing moves;
     record the rc;
   - one MCP `mempalace_search`;
   - one MCP `mempalace_kg_query` (entity palace-daemon);
   - `systemctl show palace-daemon -p MemoryPeak`.
3. Don't touch rollback material until jp-58 says **"palace verified"**. jp-58 then merges #578. After the merge,
   switch the live checkout to `main` and `git pull --ff-only`; the tree stays the same.

## Rollback (any failure)
`~/Projects/memorypalace/scratch/upstream-sync-2026-10-21/final-gate/rollback.sh`: stops the daemon, `switch main`,
`reset --hard 6641c5599ee2036b2910a01252ab900f32e32bb1`, `uv pip sync daemon-venv-rollback-requirements.txt`,
starts the daemon and waits for /health. The DB needs nothing: the only schema change is an additive column.

## Board line format (jp-58's ask)
`BOARD port/upstream-sync @ <head sha> live: /health <status/version/uptime> · /status/fast <drawers> · POST /mine rc
<n> (<file>, <secs>) · search <n hits, ms> · kg_query <ok, facts> · MemoryPeak <bytes> · 04:38 restart = OOM (see
above) · rollback staged until "palace verified"`, then Changed / Checked / Evidence / Not verified.

## After "palace verified" (lane follow-ups, jp-58)
1. Add the AGE graphid fix to the backup runbook, plus a restore smoke test.
2. chroma-hnswlib into palace-daemon's requirements.txt.
3. Ticket the status/list all-rows fallback (MemoryPeak over MemoryMax; it OOMed live at 04:38).
4. Take upstream 759e876b in the next port.
5. Remove `/mnt/ollama-models/mempalace-gate` (28 GB) 24 h after the move.

## STATE (05:39): MOVED, LIVE at 95026931; waiting on jp-58's "palace verified"
- 05:37:22 daemon stopped; move.sh then aborted on the branch-name collision (worktree). Finished by hand: detached
  checkout of origin/port/upstream-sync, venv sync, start at 05:37:36/41 (health ok). About 19 s down.
- Verified: /status/fast 1,230,739; POST /mine rc 0 (f72e0929, 23 s, 32/36 stamped, wing unchanged); search 5 hits;
  kg_query 5,818 facts; MemoryMax 6G/High 5G; MemoryPeak 580 MB.
- Board line sent to jp-58 at 05:39.
- **Next:**
  1. On "palace verified": jp-58 merges #578. Then `git -C ~/Projects/memorypalace switch main && git pull --ff-only`.
     Rollback material stays until then.
  2. Watch MemoryPeak and the drain through the re-mine burst.
  3. Do follow-ups (i)–(iv).
  4. rm the 28 GB gate dir 24 h after the move (Sun 05:37).
