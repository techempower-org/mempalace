# Fork Changelog (jphein/mempalace)

Fork-ahead changes that aren't yet in upstream `MemPalace/mempalace`.
Upstream's release history lives in [`CHANGELOG.md`](CHANGELOG.md);
this file is the supplement.

> **This file is generated.** Edit `docs/fork-changes.yaml` and run
> `scripts/render-docs.py` to regenerate. Hand-edits will be
> overwritten on the next render.

Date-based sections, not semver — the fork tracks `upstream/develop` and
doesn't cut its own release tags. When a fork-ahead row lands upstream,
move the entry to the **Merged into upstream** section at the bottom
(kept ~30 days, then trimmed).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---


## [2026-05-05]


### Added


- **mempalace mined + purge --source-file (mining management surface)** ([`2e6ced9`](https://github.com/jphein/mempalace/commit/2e6ced9))
  Closes the "removing manually mined data" half of JP's
  mining-management ask. Adding is already covered by the existing
  ``mempalace mine <dir>``; this PR adds the symmetric remove +
  list surface.

  ``mempalace purge --source-file <path>`` extends the existing
  purge command with a third filter alongside ``--wing`` and
  ``--room``. Composes with the others (single filter or
  ``$and``). Uses ``collection.delete(where=...)`` — the same
  filtered-delete path shipped by the original purge.

  ``mempalace mined`` is the companion to ``mempalace status``
  that groups by wing × source_file rather than wing × room.
  Answers "which files have I mined into this wing?" so an
  operator can pick targets for ``--source-file`` purge. Honors
  ``--wing`` and ``--limit`` (default 50; ``--limit 0`` shows
  all). Pushes the wing filter into the chromadb ``where``
  clause so a wing-scoped view doesn't scan the full collection
  (Copilot review on jphein/mempalace#4 caught the unfiltered
  sweep). Argparse rejects negative ``--limit`` at parse time
  via a ``_nonneg_int`` validator (also Copilot finding).

  *Tests:* +8 — purge source-file (3) + cmd_mined (3, including dispatch + negative-limit reject) + 2 existing updated
  *Upstream:* [PR #7](https://github.com/MemPalace/mempalace/pull/7)
  *Files:* `mempalace/cli.py`, `tests/test_cli.py`


### Changed


- **Drop wing_ prefix from transcript-derived wings to converge with operator mines** ([`86d4700`](https://github.com/jphein/mempalace/commit/86d4700))
  The fork-only ``_wing_from_transcript_path`` returned
  ``wing_<project>`` for hook-derived wings, but operator-mined
  content from ``mempalace mine ~/Projects/X`` lands in a bare-name
  wing. Result: every project that had both manual-mined content
  AND hook-mined transcripts had its drawers split between
  ``wing_X`` and ``X`` — silently invisible to a search filtered
  by either name.

  Drop the prefix. Fallback ``wing_sessions`` → ``sessions``
  (which already exists with 2,132 drawers in the canonical
  151K palace, so future fallback content converges with older
  fallback content too).

  One-shot data-side rename also applied to the live palace via
  direct SQL UPDATE on chromadb's ``embedding_metadata`` table:
  9 wings totaling 36,189 drawers renamed in a single transaction.
  Hyphen normalization (``wing_realm-sigil`` → ``realm_sigil``,
  ``kiyo-xhci-fix`` → ``kiyo_xhci_fix``,
  ``clock-realm-watch`` → ``clock_realm_watch``) bundled in via
  a follow-up SQL pass to converge with the new
  ``normalize_wing_name`` output.

  *Tests:* −2 / +0 (assertions updated to bare-name shape; 9 string literals adjusted)
  *Upstream:* [PR #9](https://github.com/MemPalace/mempalace/pull/9)
  *Files:* `mempalace/hooks_cli.py`, `tests/test_hooks_cli.py`


- **Retire mempalace_session_recovery collection + read tool** ([`0b945e1`](https://github.com/jphein/mempalace/commit/0b945e1))
  Follow-up to drop-checkpoint-write-path. With nothing writing
  to the recovery collection anymore (hooks moved to verbatim-only
  on the parent branch), the read paths and migration code that
  fed it become dead. Delete them.

  Removed in mempalace/:
  ``_SESSION_RECOVERY_COLLECTION`` / ``get_session_recovery_collection``
  / ``_CHECKPOINT_TOPICS`` (palace.py); ``_get_session_recovery_collection``
  / ``_recovery_collection_cache`` / topic-routing branch in
  ``tool_diary_write`` / ``tool_session_recovery_read`` handler
  and TOOLS dict registration (mcp_server.py);
  ``migrate_checkpoints_to_recovery`` (migrate.py); ``cmd_repair``
  ``--mode reorganize`` (cli.py).

  Removed in tests/: full ``test_session_recovery.py`` (12
  tests); ``TestMigrateCheckpointsToRecovery`` class
  (test_migrate.py, 6 tests); ``TestCheckpointRouting`` and
  ``TestSessionRecoveryRead`` classes (test_mcp_server.py).

  Removed in docs/: ``mempalace_session_recovery_read`` section
  from ``website/reference/mcp-tools.md``.

  Production data on disk was untouched by this code change.
  A separate one-shot operation deleted the collection
  (``client.delete_collection('mempalace_session_recovery')``)
  after dumping its 1,032 archived entries to
  ``~jp/backups/mempalace_session_recovery-2026-05-05.json``
  on disks. Also referenced from the
  ``2026-05-05-verbatim-only-design.md`` spec.

  *Tests:* −18 (12 from test_session_recovery.py + 6 from test_migrate.py)
  *Upstream:* [PR #8](https://github.com/MemPalace/mempalace/pull/8)
  *Files:* `mempalace/palace.py`, `mempalace/mcp_server.py`, `mempalace/migrate.py`, `mempalace/cli.py`, `website/reference/mcp-tools.md`, `tests/test_session_recovery.py`, `tests/test_migrate.py`, `tests/test_mcp_server.py`


- **Drop hook-side checkpoint diary writes — verbatim-only architecture** ([`69768fc`](https://github.com/jphein/mempalace/commit/69768fc))
  The Stop hook used to do two things on each fire: (a) write a
  1KB checkpoint summary diary entry into the dedicated
  ``mempalace_session_recovery`` collection AND (b) auto-mine the
  verbatim transcript into ``mempalace_drawers``.

  (a) is redundant once (b) is searchable. Worse, the recovery
  collection had no semantic-search MCP surface — only filter-based
  reads via ``mempalace_session_recovery_read(session_id, agent,
  since/until, wing)``. So checkpoints in it were structurally
  invisible to ``mempalace_search``. Net effect from a user's
  seat: agents (and JP) couldn't find recent session content via
  search even though everything was on disk.

  Drop (a). Verbatim transcripts in ``mempalace_drawers`` carry
  every word a checkpoint summary would have surfaced — searching
  IS the recovery query.

  ``hook_stop`` silent path: removed ``_save_diary_direct`` call,
  save marker advances unconditionally on each fire, ``systemMessage``
  shape changes from ``"✦ N memories woven into the palace —
  themes"`` to ``"✦ Transcript ingest triggered (wing=...)"``.
  Failure detection moves to daemon-side observability (hook.log
  + systemd journal).

  ``hook_precompact``: removed the recovery-marker write. Mine +
  compaction proceed unchanged.

  Also deleted the now-unused ``_save_diary_direct`` (~120 LOC)
  and its dependencies ``_extract_themes`` + ``_THEME_STOPWORDS``
  (~30 LOC). No remaining callers.

  Ships the architecture spec at
  ``docs/superpowers/specs/2026-05-05-verbatim-only-design.md``.

  *Tests:* −4 ratchet + 4 updated (4 hook tests + 1 OSError test mock _ingest_transcript instead of _save_diary_direct, expect new systemMessage shape; 3 new tests for traversal-rejected, wrong-extension-rejected, wing-derivation-correct)
  *Upstream:* [PR #6](https://github.com/MemPalace/mempalace/pull/6)
  *Files:* `mempalace/hooks_cli.py`, `tests/test_hooks_cli.py`, `docs/superpowers/specs/2026-05-05-verbatim-only-design.md`


### Fixed


- **Preserve dashed project names in transcript-derived wings** ([`d76134d`](https://github.com/jphein/mempalace/commit/d76134d))
  Two findings from Copilot review on jphein/mempalace#9 that
  surfaced a real bug: the previous primary regex's
  ``encoded.rsplit('-', 1)[-1]`` rule collapsed
  ``-home-jp-Projects-realm-watch`` → ``watch`` instead of
  preserving ``realm-watch``. Reorder the resolution: try the
  explicit ``-Projects-<name>`` segment FIRST (preserves dashes),
  fall back to the last-dash-token only when the path is in a
  non-Projects layout (``~/dev/<parent>/<project>``,
  ``~/Users/<user>/<folder>/<project>``).

  Also routes the result through
  ``mempalace.config.normalize_wing_name`` (lowercases, replaces
  spaces/hyphens with underscores) so hook-derived wings match
  operator-mined wing names exactly. Same project mined two ways
  now produces one wing.

  Net behavior: ``-Projects-realm-watch`` → ``realm_watch``
  (matches what ``mempalace mine ~/Projects/realm-watch`` produces
  via ``normalize_wing_name(convo_path.name)``).

  *Tests:* +4 — dashed-project, dashed-project-uppercase, operator-mine-convergence assertion
  *Upstream:* [PR #10](https://github.com/MemPalace/mempalace/pull/10)
  *Files:* `mempalace/hooks_cli.py`, `tests/test_hooks_cli.py`


- **Restore transcript ingest via daemon /mine when PALACE_DAEMON_URL is set** ([`09d2ca6`](https://github.com/jphein/mempalace/commit/09d2ca6))
  Daemon-strict mode (introduced 2026-04-24 in commits ``8c90c0f``
  + ``0e97b19`` to fix the HNSW drift incident) skipped all three
  local mining paths when ``PALACE_DAEMON_URL`` was set, on the
  assumption a daemon-side writer would do the work instead. The
  diary-checkpoint half got that writer via ``/silent-save``, but
  the transcript-ingest half did not. So for ~11 days every Claude
  Code Stop hook left a checkpoint summary in the recovery
  collection and zero verbatim transcript drawers in
  ``mempalace_drawers``. ``mempalace_search`` lost visibility into
  recent sessions even though MCP, daemon, and HNSW were all
  healthy.

  Replace the three skip-and-bail branches
  (``_maybe_auto_ingest``, ``_mine_sync``, ``_ingest_transcript``)
  with POSTs to the daemon's existing ``/mine`` endpoint via a new
  ``_post_daemon_mine()`` helper. Daemon-side path translation
  (so a remote daemon can find client-side paths at its own mount
  points) handled via a companion palace-daemon PR introducing
  ``PALACE_DAEMON_PATH_MAP``.

  Behavior change: transcript ingest now routes to the project
  wing derived via ``_wing_from_transcript_path()``. Replaces
  hardcoded ``"sessions"``; produces e.g. ``wing_memorypalace`` /
  ``wing_realmwatch`` per transcript. (Subsequently dropped the
  ``wing_`` prefix in commit ``86d4700``.)

  Companion: jphein/palace-daemon#1 ``feat(/mine): translate
  client-side paths via PALACE_DAEMON_PATH_MAP``, merged
  2026-05-05.

  *Tests:* +6 — _post_daemon_mine (URL/body/api-key/error paths) + daemon-routed branches in all three mining functions
  *Upstream:* [PR #2](https://github.com/MemPalace/mempalace/pull/2)
  *Files:* `mempalace/hooks_cli.py`, `tests/test_hooks_cli.py`


## [2026-05-03]


### Fixed


- **`cfg.init()` no longer materializes chunking defaults into `config.json`** ([`6ce37c0`](https://github.com/jphein/mempalace/commit/6ce37c0))
  `cfg.init()` was unconditionally writing ``chunk_size: 800``,
  ``chunk_overlap: 100``, and ``min_chunk_size: 50`` into
  ``config.json`` on first run. The values match ``miner.py``'s
  module-level constants but conflict with ``convo_miner.py``'s
  stricter ``MIN_CHUNK_SIZE = 30`` floor — and ``convo_miner.py``
  lines 427-431 explicitly distinguishes "user has tuned this"
  from "user is on defaults" by checking
  ``_file_config.get("min_chunk_size") is None``. Materializing
  the value as a default broke that detection: any user who ran
  ``mempalace init`` then mined conversations would silently lose
  exchanges shorter than 50 characters, even though the convo
  miner's intended floor is 30.

  Surfaced by a pytest fixture leak. ``tests/conftest.py:21-27``
  redirects ``HOME`` to a session-tmp directory so tests don't
  trash the real ``~/.mempalace``. The first test that calls
  ``cmd_init`` writes the bloated default config into the
  session-tmp ``~/.mempalace``, and downstream
  ``test_convo_miner`` runs (in-process, same session) then read
  ``min_chunk_size: 50`` and skip the test fixture's ~30-char
  exchanges entirely. Both tests pass in isolation; the second
  fails when chained.

  Fix: drop the three chunking keys from ``cfg.init()``'s
  default-config-write. The
  ``MempalaceConfig.chunk_size``/``.chunk_overlap``/``.min_chunk_size``
  properties already provide the right fallbacks via
  ``_file_config.get(key, default)`` when the key is absent.
  Users who want to tune chunking still set the keys explicitly;
  the contract ``convo_miner.py`` relies on (``is None`` ⇔
  "untuned") is restored.

  Same fix pushed to the open #1024 PR branch as commit
  ``df9187c`` so the bug doesn't get reintroduced when #1024
  merges. Amends fork-ahead row 17.

  *Tests:* 1548/1548 (was 1546/1548 with 2 isolation failures in test_convo_miner)
  *Upstream:* [PR #1024](https://github.com/MemPalace/mempalace/pull/1024) (OPEN)
  *Files:* `mempalace/config.py`


## [2026-04-27]


### Changed


- **Retire the `kind=` filter — structural split made it inert** ([`7ba28dc`](https://github.com/jphein/mempalace/commit/7ba28dc))
  Phases A–E of the checkpoint collection split (2026-04-25 → 2026-04-26)
  moved every Stop-hook auto-save checkpoint drawer to the dedicated
  ``mempalace_session_recovery`` collection. Empirical check on the
  canonical 151K palace: ``mempalace_drawers`` has zero
  ``topic=checkpoint`` and zero ``topic=auto-save`` drawers; recovery
  collection holds 763. The ``kind=`` post-filter was filtering nothing.

  Deleted: ``_CHECKPOINT_TOPICS`` (moved to ``palace.py`` for write-side
  routing), ``_is_checkpoint_drawer``, ``_apply_kind_text_filter``, the
  ``max(n*20, 100)`` over-fetch hack (back to standard ``n_results * 3``),
  the ``kind=`` parameter on ``search_memories`` / ``build_where_filter`` /
  CLI ``search`` / ``mempalace_search`` MCP tool input_schema, and
  ``TestCheckpointFilter`` (9 tests). Companion fix in
  [palace-daemon](https://github.com/jphein/palace-daemon/commit/4a318d3)
  (v1.7.1) drops ``kind=`` from ``/search`` and ``/context`` HTTP routes.

  *Tests:* −9 (TestCheckpointFilter deleted; suite at 1500)
  *Files:* `mempalace/searcher.py`, `mempalace/mcp_server.py`, `mempalace/palace.py`, `mempalace/migrate.py`, `mempalace/layers.py`, `tests/test_searcher.py`


- **Hoist CLOSET_RANK_BOOSTS to module level + record VecRecall ablation finding** ([`3cb03f3`](https://github.com/jphein/mempalace/commit/3cb03f3))
  Two-step refactor + measurement. First (commit ``f558d3c``):
  hoist ``CLOSET_RANK_BOOSTS = [0.40, 0.25, 0.15, 0.08, 0.04]`` and
  ``CLOSET_DISTANCE_CAP`` from inside ``search_memories`` to module
  scope so they can be tuned from the outside (env var, config flag,
  or in-process patch for A/B benchmarking) without touching the
  function. No behavior change; pure ablation enablement.

  Then (commit ``3cb03f3``): A/B ablation against the 151K canonical
  palace (12-probe set covering recent fork-side decisions + mined-file
  content). Closet boost fires on ~20% of result rows, concentrated
  in queries whose answer lives in mined files; closets are sparse on
  chat-transcript queries (most fork-side decisions). When the boost
  fired, it re-ordered chunks within a single source file rather than
  displacing right answers with wrong ones — i.e. VecRecall's critique
  ([discussions/1129](https://github.com/MemPalace/mempalace/discussions/1129),
  "org-layer in retrieval path drops R@5") did not reproduce here.
  Hybrid degrades to effectively pure-vector for transcript queries
  and re-ranks within-file chunks for mined-file queries; neither
  shape matches the failure mode VecRecall is fixing. Findings noted
  in the comment block above the constants so future-us doesn't have
  to re-run the experiment.

  *Files:* `mempalace/searcher.py`


### Fixed


- **Strip embedded API key from .claude-plugin/ manifests; rely on env inheritance** ([`9f91e18`](https://github.com/jphein/mempalace/commit/9f91e18))
  ``.claude-plugin/.mcp.json`` and ``.claude-plugin/hooks/hooks.json``
  shipped with a real (rotated) API key embedded as a literal in the
  manifest's ``env`` block, plus my homelab daemon URL. Both are
  committed plugin templates that get pulled into every plugin install.

  Fix in two commits: ``8119149`` reverted both manifests to the
  upstream-shape (no env block, in-process MCP), then ``9f91e18``
  restored daemon-routing on ``.mcp.json`` (URL + path) but **without**
  the embedded credential — ``PALACE_API_KEY`` now inherits at runtime
  from ``~/.claude/settings.local.json``'s ``env`` block (which
  Claude Code passes to spawned MCP servers and hooks).

  Net: my fork-main carries the daemon-routed config matching production
  deployment; the literal credential lives one place only (gitignored
  ``settings.local.json``); future plugin installs inherit env rather
  than carrying a stale embedded key. Companion to palace-daemon
  [PR #12](https://github.com/rboarescu/palace-daemon/pull/12) which
  fixes the same class of embedded-default in ``clients/palace-mode``.

  *Files:* `.claude-plugin/.mcp.json`, `.claude-plugin/hooks/hooks.json`


## [2026-04-26]


### Added


- **Canonical YAML manifest + renderer for fork-ahead docs** ([`5a01aec`](https://github.com/jphein/mempalace/commit/5a01aec))
  The fork-ahead narrative previously lived (and drifted) across four
  hand-edited files: README's fork-change-queue table, CLAUDE.md's row
  inventory, FORK_CHANGELOG.md, and the promises tracker. New
  ``docs/fork-changes.yaml`` is now the canonical source; running
  ``scripts/render-docs.py`` regenerates FORK_CHANGELOG.md.
  ``scripts/check-docs.sh`` extended with a render-parity check that
  detects YAML→FORK_CHANGELOG drift, plus the existing test-count /
  commit-hash / upstream-PR-state checks. Researched towncrier, scriv,
  git-cliff, antsibull-changelog — none do single-source →
  multi-target render in this shape. README/CLAUDE/promises
  rendering planned for follow-on commits with marker-based
  insertion.

  *Files:* `docs/fork-changes.yaml`, `scripts/render-docs.py`, `scripts/check-docs.sh`, `FORK_CHANGELOG.md`, `CLAUDE.md`


- **Phase D migration + PreCompact recovery write** ([`42817d7`](https://github.com/jphein/mempalace/commit/42817d7))
  ``migrate_checkpoints_to_recovery(palace_path, batch_size=1000)`` walks
  the main collection in pages, filters drawers with topic in
  ``_CHECKPOINT_TOPICS`` in Python (avoids the chromadb 1.5.x ``$in``/``$nin``
  filter-planner bug), copies them to the recovery collection
  (preserving IDs + metadata), then deletes from main. Idempotent —
  re-running on a fully-reorganized palace returns 0. Add-then-delete
  order: a crash mid-migration leaves a duplicate, not a loss.
  Wired into ``mempalace repair --mode reorganize`` for explicit operator
  runs. PreCompact incorporated — ``hook_precompact`` now writes a
  session-recovery marker mirroring Stop, so context-compaction events
  leave a queryable timestamp in the recovery collection rather than
  nothing. Failures are non-fatal (logged; mining + compaction still
  proceed).

  *Tests:* 6 in TestMigrateCheckpointsToRecovery + 1 in test_hooks_cli
  *Files:* `mempalace/migrate.py`, `mempalace/cli.py`, `mempalace/hooks_cli.py`, `tests/test_migrate.py`


- **Surface drawer_id in search/diary/recovery payloads** ([`9a8bb77`](https://github.com/jphein/mempalace/commit/9a8bb77))
  ChromaDB's primary key was always returned by ``query()`` and ``get()``
  but never plumbed into result-building loops; consumers (e.g.
  familiar.realm.watch's citation-popover loop) couldn't link a hit
  back to the underlying drawer. Three call sites updated for parity:
  ``searcher.search_memories`` (vector path + sqlite BM25 fallback),
  ``mcp_server.tool_session_recovery_read``, ``mcp_server.tool_diary_read``.
  Defensive zip with id-pad: production chromadb always returns ids,
  but several test mocks omit them — pad with ``None`` when absent so
  existing fixtures keep working without touching N tests.

  *Tests:* 1 integration + 1 inline assertion
  *Files:* `mempalace/searcher.py`, `mempalace/mcp_server.py`, `website/reference/mcp-tools.md`


- **scripts/deploy.sh — one-command Syncthing-aware redeploy** ([`8252025`](https://github.com/jphein/mempalace/commit/8252025))
  Single command does the right shape: push fork main → wait for
  Syncthing to reach ``/mnt/raid/projects/memorypalace`` on the deploy
  host → ``systemctl --user restart palace-daemon`` → poll ``/health`` →
  ssh-import-check that today's fork-ahead surface is loaded.
  Replaces a three-step manual ritual that was easy to get wrong
  (e.g. ``pip install --upgrade`` was a no-op on the editable install).

  *Files:* `scripts/deploy.sh`


### Changed


- **Cherry-pick #1094 — coerce None metadatas at chromadb boundary** ([`43d728d`](https://github.com/jphein/mempalace/commit/43d728d))
  Fork main was carrying the per-site ``meta = meta or {}`` guards
  from #999 in eight read paths but didn't have the boundary
  coercion that closes the issue once for all callers. The typed
  ``QueryResult``/``GetResult`` contract declares
  ``metadatas: list[dict]``, never ``list[Optional[dict]]`` — so
  every call site that forgot the per-site guard was a latent
  ``AttributeError``. #1094 (open upstream, jp-authored) coerces
  at ``ChromaCollection.query()`` / ``.get()`` so downstream
  callers always receive ``list[dict]``. Per-site guards retained
  as belt-and-suspenders for paths that might bypass the typed
  wrappers. Three same-family fork-ahead PRs (#1198, #1201, #1083
  review) all pointed at gaps that would have been impossible if
  this pattern had been in place.

  *Tests:* 6 in test_backends.py (mixed/all-None inner lists, padding regression, get-without-metadatas)
  *Upstream:* [PR #1094](https://github.com/MemPalace/mempalace/pull/1094) (OPEN)
  *Files:* `mempalace/backends/chroma.py`, `tests/test_backends.py`


- **Cherry-pick #1087 rewrite — collection.delete(where=) instead of nuke-and-rebuild** ([`366a9ad`](https://github.com/jphein/mempalace/commit/366a9ad))
  Fork main had been carrying ``cmd_purge``'s nuke-and-rebuild
  shape (extract survivors, ``shutil.rmtree``, recreate, re-insert).
  Cherry-picked the post-review rewrite from PR #1087's branch:
  ``ChromaBackend.get_collection`` + ``col.delete(where=...)``.
  The race in #521 is on the upsert path
  (``updatePoint`` / ``repairConnectionsForUpdate``) — filter-delete
  doesn't reach it. Five fixes from @igorls's review now apply to
  our own purge: embedding function preserved, no rmtree window,
  routes through the backend, ``confirm_destructive_action`` reused,
  end-to-end test covers the embedding-fn-survival path.

  *Tests:* 5 in test_cli.py (TestCmdPurge + e2e)
  *Upstream:* [PR #1087](https://github.com/MemPalace/mempalace/pull/1087) (OPEN)
  *Files:* `mempalace/cli.py`, `tests/test_cli.py`


### Fixed


- **Integrity gate prevents quarantine_stale_hnsw from destroying healthy indexes** ([`645ba20`](https://github.com/jphein/mempalace/commit/645ba20))
  Previous behavior fired whenever ``sqlite_mtime - hnsw_mtime`` exceeded
  the (lowered, in #1173) 300s threshold. ChromaDB 1.5.x flushes HNSW
  asynchronously and a clean shutdown does not force-flush, so the
  on-disk HNSW is always meaningfully older than ``chroma.sqlite3`` —
  that's the steady state, not corruption. Quarantine renamed valid
  HNSW segments on every cold-start; chromadb created empty replacements;
  vector recall went to 0/N until rebuild. Confirmed in production on
  the disks daemon journal 2026-04-26 06:56:45: three of three healthy
  253MB segments quarantined on cold-start with 538-557s gaps. Fix:
  stage 2 integrity gate sniffs the chromadb segment metadata file
  for its protocol/terminator bytes (PROTO ``\x80`` head, STOP ``\x2e``
  tail) and a non-trivial size, **without deserializing**. Healthy
  segment with mtime drift → keep in place; truncated/zero-filled →
  quarantine.

  *Tests:* 4 in test_backends.py (renames-corrupt, leaves-healthy-with-drift, leaves-no-metadata, renames-truncated)
  *Upstream:* [PR #1173](https://github.com/MemPalace/mempalace/pull/1173) (MERGED)
  *Files:* `mempalace/backends/chroma.py`, `tests/test_backends.py`


### Performance


- **Cherry-pick #1085 — batch ChromaDB inserts in miner (10–30× faster)** ([`6be6fff`](https://github.com/jphein/mempalace/commit/6be6fff))
  Cherry-picked from upstream PR
  [#1085](https://github.com/MemPalace/mempalace/pull/1085) (@midweste,
  OPEN as of 2026-04-26). New ``_build_drawer()`` helper + ``add_drawers()``
  batch-insert path; ``process_file`` hands the full chunk list to
  ``add_drawers`` instead of looping per-chunk. Hoists ``datetime.now()``
  and ``os.path.getmtime()`` to file-level (2 syscalls per file instead
  of 2N). Reported 10–30× mining speedup upstream. Fork-side resolution
  preserved fork's existing ``DRAWER_UPSERT_BATCH_SIZE=1000``; aliased
  upstream's ``CHROMA_BATCH_LIMIT`` to it. Becomes a no-op when #1085
  merges to develop and we next sync.

  *Upstream:* [PR #1085](https://github.com/MemPalace/mempalace/pull/1085) (OPEN)
  *Files:* `mempalace/miner.py`


## [2026-04-25]


### Added


- **Phases A–C of the checkpoint collection split** ([`e266365`](https://github.com/jphein/mempalace/commit/e266365))
  New ``mempalace_session_recovery`` collection adapter
  (``_SESSION_RECOVERY_COLLECTION`` + ``get_session_recovery_collection``
  in ``palace.py``); ``tool_diary_write`` routes ``topic in _CHECKPOINT_TOPICS``
  to it. New ``mempalace_session_recovery_read`` MCP tool reads recovery
  collection only with optional filters (session_id, agent, since,
  until, wing, limit). Promoted from "future work" to "necessary" by
  the same-day Cat 9 A/B (``kind=all`` 632 tokens/Q vs ``kind=content``
  3 tokens/Q on the canonical 151K-drawer palace). Design doc at
  ``docs/superpowers/specs/2026-04-25-checkpoint-collection-split.md``.

  *Tests:* 12 across test_session_recovery.py + TestCheckpointRouting + TestSessionRecoveryRead
  *Files:* `mempalace/palace.py`, `mempalace/mcp_server.py`, `tests/test_session_recovery.py`, `tests/test_mcp_server.py`, `website/reference/mcp-tools.md`


### Fixed


- **Gate quarantine_stale_hnsw to once-per-palace-per-process** ([`70c4bc6`](https://github.com/jphein/mempalace/commit/70c4bc6))
  ``make_client()`` previously invoked ``quarantine_stale_hnsw`` on every
  reconnect; under steady write load the proactive check kept firing,
  racking up ``.drift-*`` directories every 10–30 minutes. New
  ``ChromaBackend._quarantined_paths: set[str]`` caps it to one fire on
  first open per palace per process. Real cold-start drift still caught
  (replicated/restored palace); real runtime errors still caught via
  palace-daemon's ``_auto_repair``, which calls ``quarantine_stale_hnsw``
  directly and bypasses this gate.

  *Tests:* 2 in test_backends.py (single-fire-per-palace, per-palace independence)
  *Upstream:* [PR #1173](https://github.com/MemPalace/mempalace/pull/1173) (MERGED)
  *Files:* `mempalace/backends/chroma.py`, `tests/test_backends.py`, `tests/conftest.py`


- **palace_graph.build_graph skips None metadata** ([`5fd15db`](https://github.com/jphein/mempalace/commit/5fd15db))
  ``palace_graph.py:95`` was calling ``meta.get("room", "")`` unconditionally;
  ChromaDB returns ``None`` for legacy/partial-write drawers, taking out
  every consumer of ``build_graph`` (graph_stats, find_tunnels, traverse,
  the daemon's ``/stats``). Caught by palace-daemon's ``verify-routes.sh``
  smoke test. Same family as upstream's #999 None-metadata audit, in a
  read path the audit didn't reach.

  *Upstream:* [PR #1201](https://github.com/MemPalace/mempalace/pull/1201) (MERGED)
  *Files:* `mempalace/palace_graph.py`


- **kind= filter on search_memories excludes Stop-hook checkpoints (transitional)** ([`f9f5cc4`](https://github.com/jphein/mempalace/commit/f9f5cc4))
  Three values: ``"content"`` (default, excludes), ``"checkpoint"``
  (recovery/audit only), ``"all"`` (no filter). Two same-day architecture
  corrections: (a) the where-clause filter (``topic $nin [...]``) tripped
  a chromadb 1.5.x filter-planner bug; the exclusion moved to post-filter
  only ([398f42f](https://github.com/jphein/mempalace/commit/398f42f));
  (b) vector top-N is dominated by checkpoints on this palace, so
  post-filter alone empties the result set without aggressive over-fetch
  — pull size raised to ``max(n*20, 100)`` for ``kind != "all"`` (this commit).
  Safety net during the transition; once Phase D ships and existing
  checkpoints migrate, the post-filter and over-fetch hack become
  deletable.

  *Tests:* 9 in TestCheckpointFilter
  *Files:* `mempalace/searcher.py`, `mempalace/mcp_server.py`, `tests/test_searcher.py`


---

## Merged into upstream (recent)


*Trim entries from this list once they're more than ~30 days old.*


*See CHANGELOG.md (upstream) for the full released history.*


- [PR #1173](https://github.com/MemPalace/mempalace/pull/1173) — quarantine_stale_hnsw on make_client + cold-start gate + integrity sniff — 2026-04-26
- [PR #1177](https://github.com/MemPalace/mempalace/pull/1177) — `.blob_seq_ids_migrated` marker guard (closes #1090) — 2026-04-26
- [PR #1198](https://github.com/MemPalace/mempalace/pull/1198) — _tokenize None-document guard in BM25 reranker — 2026-04-26
- [PR #1201](https://github.com/MemPalace/mempalace/pull/1201) — palace_graph.build_graph skips None metadata — 2026-04-26
- [PR #659](https://github.com/MemPalace/mempalace/pull/659) — diary `wing` parameter — 2026-04-23
- [PR #661](https://github.com/MemPalace/mempalace/pull/661) — graph cache with write-invalidation — 2026-04-22
- [PR #673](https://github.com/MemPalace/mempalace/pull/673) — deterministic hook saves — 2026-04-22
- [PR #1021](https://github.com/MemPalace/mempalace/pull/1021) — Claude Code 2.1.114 stdout/silent_save fixes — 2026-04-22
- [PR #999](https://github.com/MemPalace/mempalace/pull/999) — None-metadata guards across read paths — 2026-04-18
- [PR #1000](https://github.com/MemPalace/mempalace/pull/1000) — quarantine_stale_hnsw shipped — v3.3.2
- [PR #1023](https://github.com/MemPalace/mempalace/pull/1023) — PID file guard prevents stacking mine processes — v3.3.2
- [PR #681](https://github.com/MemPalace/mempalace/pull/681) — Unicode checkmark → ASCII — v3.3.2
