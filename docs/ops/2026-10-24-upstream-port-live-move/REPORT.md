# memorypalace port — final gate (items a–h), Fri 2026-10-23

Port: techempower-org/mempalace#578, head **95026931**.
- jp-58 gated f47a3b45 at 19:2x.
- 95026931 adds one fix found here (section a). CI on it: 29 check-runs, 25 success, 4 skipped by design.
- **Needs jp-58's re-gate.**
- Live checkout untouched.

## (e) Rollback sha and the live-checkout move (exact steps; nothing run yet)

The live service is `palace-daemon.service` (system unit, User=jp). It runs
`/home/jp/.local/share/palace-daemon/venv/bin/python main.py --palace /srv/mempalace-data/palace`, and that venv has
an **editable** install of `/home/jp/Projects/memorypalace` (direct_url.json → file:///home/jp/Projects/memorypalace).

- **Rollback sha:** `6641c5599ee2036b2910a01252ab900f32e32bb1`, the live checkout's `main` = origin/main (#577,
  2026-10-21). The checkout is clean except an untracked `.claudeignore`.
- The venv's `pip freeze` before the move is saved at `final-gate/daemon-venv-freeze-before.txt` (94 lines).

Move, in the quiet window jp-58 picks (fleet idle, no Stop-hook saves in flight):
1. Fresh backup: `sudo systemctl start mempalace-backup` and wait for it; confirm today's dump size is above ~3.4 GB.
2. Merge #578 to `main` (jp-58's merge call; it's a merge commit, not a squash, so upstream history stays).
3. `git -C ~/Projects/memorypalace fetch origin && git -C ~/Projects/memorypalace merge --ff-only origin/main`.
4. `uv pip install --python ~/.local/share/palace-daemon/venv/bin/python -e "/home/jp/Projects/memorypalace[postgres]"`
   (picks up the new core deps; editable, so the code is already live after step 3).
5. `sudo systemctl restart palace-daemon`, then check: `curl -s localhost:8085/health`, one `mempalace search`,
   `/status/fast` drawer count = pre-move count, one hook-style transcript mine.
6. Watch `journalctl -u palace-daemon -f` for 10 minutes.

Rollback (any failure in 5–6):
1. `git -C ~/Projects/memorypalace checkout -B main 6641c5599ee2036b2910a01252ab900f32e32bb1`
2. `uv pip sync --python ~/.local/share/palace-daemon/venv/bin/python final-gate/daemon-venv-rollback-requirements.txt`
   (the pre-move freeze, with its `-e git+…@6641c559` line pointed back at the local editable checkout, so the venv
   follows the reset working tree rather than a fresh git clone)
3. `sudo systemctl restart palace-daemon`, then the same checks as step 5.

The port's only postgres schema change is additive: `ALTER TABLE … ADD COLUMN IF NOT EXISTS convo_chunker_version
integer` on the source manifest (diff 6641c559..f47a3b45 of backends/postgres.py, knowledge_graph_age.py and
hallway_store.py). The old code ignores the column, so a rollback runs against a DB the new code has touched.

## (c) MCP launchers and --palace

Every launcher on familiar (`ps`, 19:2x):
- 9× `python3 ~/Projects/palace-daemon/clients/mempalace-mcp.py --daemon http://localhost:8085`, the Claude plugin's
  MCP. A pure HTTP proxy to the daemon that imports no mempalace (its docstring says so), so the port can't change it.
- 1× the daemon. main.py resolves `--palace` itself (`_palace_path_from_argv`) and exports `MEMPALACE_PALACE_PATH`
  BEFORE `import mempalace.mcp_server`.
  - Verified on the port branch, with a daemon-style import with `MEMPALACE_PALACE_PATH=X` and argv
    `--palace Y`: `mcp_server._config.palace_path == X`. The import has no argv side effects (upstream #2528/#2534),
    and the env path wins.
  - `python -m mempalace.mcp_server --palace X` honours the flag (port test
    `test_python_dash_m_server_honours_palace_flag`).
- **Pass.**

## (d) The wing-prefix question

Facts:
- **Daemon path (production, PALACE_DAEMON_URL set):** hooks post `wing=wing_<project>` to /mine. The daemon's
  `normalize_wing_slug` strips `wing_` (palace-daemon rooms.py), which is why live transcript wings are unprefixed
  (`palace_nemotron` 2,634 drawers).
- **Local spawn path (no daemon):** after the port, upstream's `_ingest_wing` files under the unprefixed project wing,
  the same result as the daemon path.
- **Diary writes:** `_save_diary_direct(wing=project_wing)` still writes `wing_<project>`; the live palace has 77 small
  `wing_*` wings (largest `wing_jp`, 92). Unchanged by the port.
- Upstream unified this in 759e876b ("diary and ingest share the project wing"). That's in upstream **develop** after
  1392e181, not in upstream/main, so it's outside this port.

**Recommendation:** no change in this port. Production behaviour is identical before and after (the daemon
normalizes). The diary/transcript split is pre-existing, and 759e876b fixes it upstream with a `wing_aliases` read
path and no re-keying. Take it in the next port, from upstream/main once it's released. Don't re-key the 77 wings now.

## (b) The #2591 re-mine: how it actually triggers in production

Upstream #2591 marks every exchange-mode drawer and manifest row without `convo_chunker_version` stale (the
fork's #576 manifest follows suit). Production doesn't re-mine in bulk:
- palace-daemon's drain mines only queued sources, and `_drain_should_skip_unchanged` skips a source whose size and
  mtime match its last successful mine (main.py:1033).
- So a transcript re-mines when its file changes: an active session's Stop/PreCompact hook posts it, the port's
  `file_already_mined` now says stale, and the whole transcript re-embeds once (the #414 incremental path compares
  positional chunks, and the new chunker changes them). Later saves return to append-only.
- **Dormant transcripts keep their old-chunker drawers,** still searchable, until something re-mines them (a manual
  `mempalace mine … --mode convos`). There's no burst at cutover.

### (b) Cost line, measured on the scratch copy (today's 02:22 nightly dump; never the live DB)
- **Stale under #2591:** 1,054,481 exchange-mode drawers (86% of 1,221,257) across **10,049 source transcripts,
  932 MB** of text.
- **Only 927 of those sources still exist on familiar** (254,371 drawers, 3.78 GB of JSONL). The other 9,122 sources
  (800,110 drawers) are gone from disk, likely Claude Code's transcript cleanup (not verified). They can never
  re-mine; their drawers stay old-chunker and stay searchable.
- **What changes and when** (by file mtime):

  | changed within | files | JSONL | drawers |
  |---|---|---|---|
  | 1 day | 18 | 1.8 GB | 114,630 |
  | 7 days | 71 | 1.93 GB | 124,069 |
  | 30 days | 460 | 3.77 GB | 246,106 |

  The first-day set, the long-running live sessions, is the real cutover cost: each re-embeds in full on its next save.
- **Mine time:**
  - MiniLM on familiar's CPU measured 63 chunks/s, and the samples show almost every chunk re-embeds (e.g. 702/709).
  - First-day burst: about 114,630 / 63 ≈ 30 min of embedding, plus writes. Call it **40–60 min of serial drain time,
    spread over those 18 sessions' next saves**; other mines queue behind each one.
  - The whole on-disk set is about 70 min of embedding, spread over about 30 days.
  - Scratch timings (HDD, 3 GB cap): 9 sample re-mines, 21–730 s each, ~24 s/MB plus ~95 s per file. That's an upper
    bound, not production speed.
- **DB growth:** the 8 timed samples went 1,729 → 1,785 drawers (+56, +3.2%) and 1,357,901 → 1,390,036 bytes of text
  (+2.4%); one file's text tripled (25.8 vs 7.8 KB: the old chunker had dropped text, which is #2591's point).
  Extrapolated over the on-disk set: about +8k drawers, tens of MB including vectors and indexes. Negligible against
  24 GB.
- **Does it complete?** All 9 sample re-mines plus 1 daemon-routed mine finished with exit 0, wrote
  `convo_chunker_version`, and the manifest rows record version 2. **Not proven:** a full re-mine of all 927 on
  scratch (about 50 h on the HDD); not run.

### (a) Scratch daemon on the copy
palace-daemon's live code (1.10.0) ran with the **port's** mempalace (gate venv: uv CPython 3.12.14 like production,
port worktree editable, palace-daemon requirements, chroma-hnswlib 0.7.6) against the scratch DB. It used
127.0.0.1:18085, an isolated HOME with no daemon_url, and a 2 GB scope.
- **18/18 checks pass** (logs/gate_checks.json):
  - status/fast and search/fast;
  - MCP: search, status, list_wings, kg_query, kg_timeline (with the 6a `total`/`offset`), the agent-less diary_read
    (#501), list_drawers, list_hallways, bulk get_drawers;
  - Cypher;
  - writes: add_drawer → get_drawer (fast path, with tags) → delete_drawer; diary_write; kg_add; kg_invalidate.
- **Hook-style `POST /mine`** of a real transcript with wing `wing_fwork`: returncode 0 in 33 s. The drawers were
  re-filed with the chunker version, and the wing was normalized to `fwork` (confirms d).
- **Found and fixed here (95026931):** #414's metadata-only refresh of unchanged chunks never stamped
  `convo_chunker_version`. After a re-mine, chunk 0's `content_hash` stayed invisible to `prefetch_content_hashes`
  (cross-file dedupe).
  - Regression test added; it went red when perturbed.
  - Re-checked on scratch: chunk 0 now carries both the version and the content_hash.
- **Latency is not comparable:** the scratch DB is on a 7200 rpm HDD (sda) with a 3 GB cap; live is NVMe with a warm
  cache. Scratch MCP search 3.8 s against live 0.28 s.

### Production observations (not port regressions; the move should know them)
1. **The nightly dump does not restore AGE cleanly.** A plain restore leaves `ag_catalog.ag_graph.graphid` (18327)
   unequal to the graph schema's new OID (16986), and every KG call then fails with "graph with oid … does not exist".
   Fix after any restore, in one transaction:
   `SET LOCAL session_replication_role = replica; UPDATE ag_catalog.ag_graph SET graphid = <ns oid> WHERE name =
   'mempalace_kg'; UPDATE ag_catalog.ag_label SET graph = <ns oid> WHERE graph = <old>;`
   Worth adding to the backup runbook; disaster recovery depends on it.
2. **A restored DB needs `VACUUM ANALYZE`** before the daemon serves. Without it, the status and list fast paths hit
   statement_timeout, fall back to loading all 1.2M rows of metadata, and the scratch daemon OOMed at 2 GB. The same
   fallback exists in live code: the live unit's MemoryMax is 4 GB and its recorded MemoryPeak is 4.29 GB.
3. **palace-daemon refuses to start without `hnswlib`** (chroma-hnswlib 0.7.6, which is not in its requirements.txt).
   The live venv has it, and step 4 of the move (`uv pip install -e`) leaves it in place.

## (f)–(h)
- **(f) Full suite after 6e:** 9935 passed / 0 failed at f47a3b45. 95026931's change is covered by CI's full Linux
  matrix (green) and 190 local convo/manifest tests; the whole local suite wasn't re-run after it.
- **(g) cmd_mine's misspelled-model check runs before daemon routing.** **Recommendation:** keep it. A misspelled
  local embedding_model is a real misconfiguration that the hooks' local fallback would also hit, and it's an
  immediate, clear exit 1.
- **(h) The concurrent miner path doesn't use mined_mtimes:** speed only, and production mines convos one file at a
  time through the daemon. Defer.
