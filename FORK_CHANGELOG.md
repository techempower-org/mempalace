# Fork Changelog (techempower-org/mempalace)

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


## [2026-09-10]


### Added


- **Every search hit carries source_kind (+ staleness when decidable) and the CLI renders the caveat** ([`5632dc2`](https://github.com/techempower-org/mempalace/commit/5632dc2))
  A palace search returns the *indexed copy* of whatever was mined, and
  transcripts are the only thing mined continuously (the Stop / PreCompact
  hooks); curated project documents are re-indexed only by a manual
  ``mempalace mine``. So the copy that comes back for a project fact is
  usually a session transcript quoting a claim, never the document that
  later corrected it — and nothing on the hit said which of the two the
  reader was holding. Measured: ``2g/CLAUDE.md`` gained a
  ``FIVE HANDSETS REFUSE THIS NETWORK`` headline on 2026-09-03 and a
  REFUTED banner on 2026-09-05, while the indexed card is dated 2026-09-01
  and contains neither; a wing-wide keyword query at limit 40 returned 21
  hits (20 ``.jsonl``, 1 diary, 0 ``CLAUDE.md``). Two peer sessions read
  the transcript copy as current.

  New ``mempalace/provenance.py`` classifies each hit as
  ``transcript`` / ``memory`` / ``diary`` / ``file`` / ``unknown`` and,
  when the source is an absolute path that exists locally, whether the
  file has been modified since it was indexed. ``source_stale`` returns
  ``None`` for undecidable — a daemon hit carries a basename only, so it
  can never be located — and that is explicitly not a denial of
  staleness. ``annotate()`` stamps the fields at every result-assembly
  site: the CLI's fast / hybrid / MCP-envelope routes and all three
  ``searcher`` sites, so MCP ``mempalace_search`` carries them from the
  daemon host too. ``--format table`` prints the caveat under each hit,
  ``compact`` tags it with the source shape, and the header says so when
  nothing curated matched at all.

  The staleness CAVEAT is deliberately not rendered for transcripts, only
  the field. Measured over 48 production transcript hits: 15 stale, 21
  not, 12 undecidable — but a live session's transcript is appended to
  continuously, so any wing with an open session reads stale and the note
  would fire constantly while saying nothing the transcript caveat
  already says. Staleness is the signal that matters for a curated
  document, which is the #451 case: a project ``CLAUDE.md`` that grew a
  REFUTED banner after its drawer was indexed.
  ``auto_query.runner._is_curated`` now delegates to the same predicate,
  so the ranking that prefers curated hits and the caveat the CLI prints
  cannot disagree about which hits are curated.

  *Tests:* 65 (test_provenance x52, test_cli_daemon TestCmdSearchProvenance x12, test_hnsw_capacity provenance x1)
  *Files:* `mempalace/provenance.py`, `mempalace/cli.py`, `mempalace/searcher.py`, `mempalace/auto_query/runner.py`


- **mempalace mine <file> --mode projects — targeted re-index of one curated document** ([`787af46`](https://github.com/techempower-org/mempalace/commit/787af46))
  Projects mode required a directory, so refreshing one edited ``CLAUDE.md``
  meant re-mining the whole tree and holding the palace write lock for as
  long as that takes (the ``2g`` corpus that surfaced this is 111 MB). The
  measured consequence: palace search kept returning the *transcript* copy
  of a claim that ``2g/CLAUDE.md`` had since refuted. The indexed copy of
  the card was filed 2026-09-01T14:13; the claim landed in the file on 09-03
  and the REFUTED banner on 09-05, so the indexed chunk contained neither.
  Transcripts are mined continuously by the Stop/PreCompact hooks; curated
  project docs are mined only by a manual whole-directory run nobody makes.
  A path naming a regular file now mines exactly that file, as part of its
  project: the root is the nearest ancestor carrying ``.git`` /
  ``mempalace.yaml`` (``resolve_project_root``), which supplies the wing and
  the relative path room detection keys off — so ``~/Projects/2g/CLAUDE.md``
  is wing ``2g``. The file's existing drawers are REPLACED rather than
  duplicated (``process_file`` already deletes by absolute ``source_file``;
  the single-file path returns the same resolved absolute path a directory
  walk produces, so the delete finds them), and the stored-mtime skip still
  applies, so re-queuing an unchanged file costs nothing.
  ``scan_project``'s per-file gate is extracted to
  ``file_passes_scan_gates``; ``scan_single_file`` applies the identical
  gates without the walk, so there is no second filtering policy to drift.
  The one deliberate difference: a rejection of a file the operator named BY
  NAME explains itself on stderr, where the tree walk stays silent. A
  ``.jsonl`` handed to projects mode is now an explicit exit-2 error naming
  ``--mode convos`` instead of a silent no-op.

  *Tests:* 36 (test_miner_single_file x24, test_cli_mine_single_file x12)
  *Files:* `mempalace/miner.py`, `mempalace/cli.py`


### Fixed


- **Post-compaction recovery: clear the auto-query dedupe and re-inject wake-up content, not a pointer** ([`22858070`](https://github.com/techempower-org/mempalace/commit/22858070))
  A context compaction keeps the session id and throws the context away,
  which broke both halves of the palace's post-compaction behaviour. The
  per-session auto-query dedupe (#429) records the drawers already shown so
  they are not repeated; after a compaction those drawers are gone from the
  agent and still marked shown, so the dedupe suppressed exactly the
  material that had just been lost — and suppressed more of it the longer
  the session ran (measured on katana 2026-09-10: one live session's
  ``~/.mempalace/auto_query/injected/<session>.json`` held 119 ids). The
  hooks then offered a pointer, "run ``mempalace wake-up``", and fleet
  evidence from 2026-09-03 is that agents do not act on invitations; hooks
  that inject without asking do. ``mempalace.compact_recovery`` now runs on
  ``SessionStart(source=compact)``: it clears the session's dedupe file
  first and unconditionally — the half that needs no network — then fetches
  the wing's wake-up story and returns it as ``additionalContext``, content
  first and the pointer last. It reads Claude Code's SessionStart JSON on
  stdin, prints the hook payload on stdout and always exits 0, so a
  recovery aid can never fail a session; with the daemon down the dedupe is
  still cleared and the visible line says so, and a clear that could not
  remove the file reports 0 rather than the count it wanted. Measured end
  to end against familiar: 125 ms total, 46 ms of it the daemon, ~900
  tokens injected. The dedupe file moved to ``auto_query.injected`` so the
  SessionStart path can clear it without importing the classifier, and
  ``runner`` gained two public seams so the recovery reuses one daemon
  transport and one hook-safe key lookup. Capped by
  ``compact_recovery.max_chars`` (``COMPACT_RECOVERY_MAX_CHARS``, default
  4000 chars ~= 1000 tokens). The daemon timeout is sized for the
  *sleeping* host rather than the happy path: familiar is Slumber-Ward
  sleepable and a sleeping host blackholes the SYN instead of refusing it,
  so ``connect()`` blocks for the whole ceiling — measured 4,074 ms, 81% of
  the hook's own 5 s timeout and 8x the 500 ms startup-injection budget,
  for a call that answers from cache in 33-48 ms. 1.5 s keeps a sleeping
  host under 2 s of hook time with ~30x headroom over the observed round
  trip. ``hooks/palace-session-start.sh`` is now in the repo as the
  canonical copy; its compact branch is one delegated call.

  *Tests:* 22 (test_compact_recovery x18, test_session_start_hook_compact x4)
  *Files:* `mempalace/compact_recovery.py`, `mempalace/auto_query/injected.py`, `mempalace/auto_query/runner.py`, `mempalace/config.py`, `hooks/palace-session-start.sh`


- **Daemon-strict mine refuses to derive a wing from a document path it cannot classify locally** ([`a30293b`](https://github.com/techempower-org/mempalace/commit/a30293b))
  The daemon-strict branch decided file-vs-directory with ``is_file()`` on
  the CLIENT filesystem while the daemon mines its own host's copy. A path
  present there and absent here answered False, fell through to the
  directory rule, and derived the wing from the FILENAME — measured,
  ``~/Projects/2g/CLAUDE.md`` lands in a wing called ``claude.md``. Silent,
  permanent misfiling of exactly the kind targeted re-index exists to
  prevent. The disagreement is one-sided: for a directory "basename" is
  right whether or not we can see it, while a file's wing comes from its
  project root, so only a file misfiles. An unresolvable path carrying a
  suffix the miner reads as text now exits 2 naming ``--wing``, quoting the
  junk wing it declined to use; anything else keeps the historic rule.
  Deliberately not a blanket refusal — the daemon legitimately mines paths
  the client cannot see (synced, or ``PALACE_DAEMON_PATH_MAP``-remapped),
  a contract ``test_routes_projects_mode_to_daemon`` has pinned since before
  single-file mining existed. Residual hole, stated in the docstring rather
  than papered over: a remote-only file with no suffix still takes the
  basename rule.

  *Tests:* 3 (test_cli_mine_single_file: refusal, explicit --wing, unseen directory)
  *Files:* `mempalace/cli.py`


- **Postgres write path scrubs lone surrogates, nested metadata and ids, not just top-level NULs** ([`66ca19f`](https://github.com/techempower-org/mempalace/commit/66ca19f))
  ``backends/pgvector.py`` has stripped both byte classes Postgres refuses
  since upstream #1829/#1833 — NUL and lone UTF-16 surrogates — because one
  stray byte anywhere in a mined corpus aborts the whole batch.
  ``backends/postgres.py`` only ever grew the NUL half (#417), and only over
  documents and top-level metadata values, leaving three live ways for a
  single transcript to take down a mine: a lone surrogate (psycopg cannot
  UTF-8-encode the parameter, and ``json.dumps`` escapes it to a ``\udXXX``
  sequence the ``::jsonb`` cast rejects); a NUL nested one level down in a
  list, a sub-dict or a dict key, which serialized to an escape the same
  cast rejects; and ids, which bind into a ``text`` column unscrubbed.

  ``_replace_nul_bytes`` is widened into ``_scrub_unstorable``, keeping
  #417's contract exactly — U+FFFD substitution plus a counted
  ``nul_bytes_replaced``, so the substitution stays provenanced and never
  silent — and adding the symmetric ``lone_surrogates_replaced``. It now
  runs at all four bind sites rather than only ``add``/``upsert``:
  ``update()`` serializes metadata into its own ``::jsonb`` cast, and
  ``get()``/``delete()`` bind ids into text comparisons, so scrubbing writes
  alone would have filed a drawer under an id its own caller could no longer
  look up.

  ``pgvector.py`` is byte-identical to ``upstream/develop`` and is left that
  way — sharing a helper would make every future upstream sync conflict on
  it. The drift the issue was filed about is caught instead by a parity test
  driven by pgvector's own sanitizers: after our scrub, ``_strip_nul`` and
  ``strip_lone_surrogates`` must both find nothing left to do. The
  substitution policies differ on purpose (pgvector deletes the byte, this
  fork replaces and counts it), so the compared invariant is storability,
  not equality.

  *Tests:* 21 (test_postgres_unstorable_bytes: surrogate replace + count, astral pairs intact, id scrub, nested metadata + keys, scalars unchanged, #417 contract, four bind sites, 8-case pgvector parity sweep)
  *Files:* `mempalace/backends/postgres.py`, `tests/test_postgres_unstorable_bytes.py`, `tests/test_postgres_nul_bytes.py`


## [2026-09-03]


### Added


- **mempalace doctor — one-screen health check of the memory workflow (bridge, daemon, wing, hooks, replay)** ([`04dde83`](https://github.com/techempower-org/mempalace/commit/04dde83))
  Five check lines: MCP bridge resolvable on PATH, daemon reachable + palace
  size, this project's wing present (drawers + rank), save hooks firing
  (hook.log freshness), replay backlog. Exits non-zero if a layer is broken;
  ``--json`` for scripts. Guards the failure mode where the MCP bridge was
  unresolvable fleet-wide for days and nothing surfaced it (#425). Validated
  all-green by a fresh session on turn 1.

  *Tests:* 4 (test_cli_doctor)


### Changed


- **Legacy hallways verb deprecated in favour of hallway list; now honours --json (#407)** ([`1509c32`](https://github.com/techempower-org/mempalace/commit/1509c32))
  Prints a one-line stderr notice pointing at the first-class daemon-routed
  verb and emits JSON when asked instead of silently ignoring ``--json`` (a
  ``jq`` pipeline used to receive human text and exit 0). Kept for scripts;
  removal is a later release.

  *Tests:* 1 (test_cli_hallways)


### Fixed


- **Wing-scoped vector search no longer returns 0 rows: enable pgvector hnsw.iterative_scan per connection** ([`2ea774d`](https://github.com/techempower-org/mempalace/commit/2ea774d))
  A filtered kNN (``WHERE wing = %s ORDER BY embedding <=> q LIMIT k``) is
  planned as an HNSW index scan *then* a filter: the index hands back its
  ``ef_search`` nearest rows palace-wide and the filter discards the ones
  outside the wing, so a wing outside the global top-N got zero rows however
  good its best match was (``EXPLAIN: Rows Removed by Filter: 47`` on the
  757K-drawer production palace; same query unscoped returned 20, exact scan
  returned 5). Two fleet sessions reported this as a "bm25-fast misroute with
  no fallback" — every arm was genuinely empty. ``SET hnsw.iterative_scan =
  relaxed_order`` is now issued on every new backend connection (pgvector
  >= 0.8; session-scoped, connection is autocommit; tolerated on older
  pgvector; ``MEMPALACE_PG_HNSW_ITERATIVE_SCAN`` knob). The CLI's
  both-arms-empty case now sets a ``fallback`` field and carries hybrid's
  warnings instead of a bare "0 results" under a bm25-fast banner.
  Acceptance: the exact fleet repro went from 0 results to the curated fact
  file at 0.575, top hit.

  *Tests:* 6 (test_postgres_iterative_scan x5, test_cli_daemon both-empty)
  *Files:* `mempalace/backends/postgres.py`, `mempalace/cli.py`


- **Curated memory-file hits clear a lower confidence floor; harness prompt-echo drawers filtered as exhaust** ([`31b74d9`](https://github.com/techempower-org/mempalace/commit/31b74d9))
  The flat 0.50 floor (#422) suppressed a correct 0.486 curated-memory-file
  hit — "flat thresholds punish exactly the corpus you just added" (fleet
  finding, openwrt-a0). One-fact-per-file ``memory/*.md`` drawers now clear
  ``floor - 0.10``. Harness prompt-echo drawers ("Investigate per the
  method…", "Get started. Read…") are instruction text mirrored back, not
  knowledge — filtered like session manifests in auto-query and wake-up L1
  (gnome-speaks-46).

  *Tests:* 3 (test_auto_query_quality)


- **Only the MCP-server entrypoints parse argv at import; other importers get defaults (#409)** ([`e3e0579`](https://github.com/techempower-org/mempalace/commit/e3e0579))
  Importing ``mempalace.mcp_server`` parsed the *importing* process's argv
  at module scope: a live ``mempalace --palace X wings --json`` left
  ``MEMPALACE_PALACE_PATH`` set process-wide, and ``--backend
  not-a-real-backend`` anywhere in argv made the import raise ``KeyError``.
  ``_is_server_entrypoint()`` gates the parse (mempalace-mcp / mcp_server.py
  argv0, or ``__main__`` = mempalace.mcp_server/mcp_proxy;
  ``MEMPALACE_MCP_PARSE_ARGV=1`` forces it). Fallout: the palace-daemon had
  relied on this scrape by accident and fell back to ``~/.mempalace/palace``
  — fixed in palace-daemon#250 (resolve ``--palace`` before the import) plus
  an explicit env var on the host.

  *Tests:* 5 (test_mcp_server_argv_guard, incl. subprocess imports both ways)


- **AGE knowledge-graph connection survives a bad query (rollback) and a dead socket (reconnect once) (#405)** ([`2135df7`](https://github.com/techempower-org/mempalace/commit/2135df7))
  ``KnowledgeGraphAGE`` is cached process-wide (``mcp_server._call_kg``, the
  daemon), so one ``QueryCanceled`` (statement_timeout) left the transaction
  aborted with no rollback and every later statement failed; a dead socket
  (host suspend/resume) left the connection closed. Measured: one slow walk
  took KG reads down for every client on the host, three times in a day.
  ``_with_conn_retry`` wraps ``_run_cypher``/``_cypher_scalar``:
  statement-level errors roll back and re-raise unchanged; connection-level
  errors drop the socket, reconnect once, retry.

  *Tests:* 5 (test_kg_age_reconnect, scripted fake psycopg2)


- **Replay drops legacy whole-project convos requests instead of re-mining them forever (#426)** ([`b5b774f`](https://github.com/techempower-org/mempalace/commit/b5b774f))
  ``~/.mempalace/pending/*.jsonl`` held dir-shaped ``mode: convos`` entries
  from before single-transcript ingest; every replay re-posted a whole
  project directory mine (hours on the write lock) and the entry survived to
  be replayed again. ``_is_legacy_dir_request()`` (convos mode, non-.jsonl
  path under ``/.claude/projects/``) consumes them and reports
  ``dropped_legacy``; the daemon now refuses them at ``/mine`` too.

  *Tests:* 3 (test_pending_queue)


- **wake-up L1 skips bookkeeping drawers (AUTO-SAVE, manifests) and ranks curated memory files first (#421 #423)** ([`a4012dd`](https://github.com/techempower-org/mempalace/commit/a4012dd))
  L1 was ~80% ``AUTO-SAVE`` lines and session manifests on busy wings.
  ``_is_exhaust_drawer`` skips them; ``_is_curated_source`` boosts
  ``memory/*.md`` drawers to importance 4.0 so a fresh session opens on the
  distilled facts. Fleet validation (labels-de): "not one AUTO-SAVE line" —
  the whole scry chain, token scheme, and three registration failures with
  their causes, in ~778 tokens.

  *Tests:* 6 (test_layers)


- **tool_checkpoint forwards session_id to the diary write; declared in both tool schemas (#408)** ([`e5e31f0`](https://github.com/techempower-org/mempalace/commit/e5e31f0))
  ``mempalace_checkpoint`` dropped the caller's ``session_id`` on the way to
  ``tool_diary_write``, so checkpoint diaries could not be tied back to the
  session that wrote them. Threaded through and declared in
  ``mempalace_checkpoint.diary`` and ``mempalace_diary_write``.

  *Tests:* 2 (test_checkpoint_session_id)


- **Hooks ask the daemon to run checkpoint mines in the background (202 + serial drain) — no more 30s timeouts, journaled replays, and double mines** ([`79b192b`](https://github.com/techempower-org/mempalace/commit/79b192b))
  ``_post_daemon_mine`` sends ``background: true``. The daemon (palace-daemon
  #242–#244) queues the mine, answers 202 in ~2ms, drains serially, requeues
  on palace-lock contention instead of quarantining, and recovers an
  interrupted batch at startup. Before: ``/mine`` blocked until the subprocess
  finished, the hook's 30s timeout fired on every real mine, a replay was
  journaled, and the daemon finished the original anyway — every long mine
  ran twice and the write lock stayed hot (#426; design palace-daemon#233).

  *Tests:* 1 updated (test_hooks_cli payload contract)


- **Thin-vector warning no longer tells pgvector users to rebuild an HNSW index** ([`43d3a28`](https://github.com/techempower-org/mempalace/commit/43d3a28))
  The ``vector ranked N — run mempalace repair to rebuild the HNSW index``
  warning is a ChromaDB diagnosis. On postgres the same shape means the query
  has no semantic neighbour inside the distance threshold (an identifier-soup
  query on a healthy 86K-drawer wing scored 0.359 and an agent nearly ran a
  long, pointless rebuild under the write lock). Backend-aware wording now
  explains the threshold and suggests identifiers / broader phrasing /
  ``--max-distance``, stating explicitly that it is not an index fault.

  *Tests:* 2 (test_vector_underdelivered_warning)


- **Checkpoint ingest mines THIS transcript, not the whole project dir (#414/#426 mechanism)** ([`16a5236`](https://github.com/techempower-org/mempalace/commit/16a5236))
  In daemon-strict mode ``_ingest_transcript`` posted ``path.parent`` — the
  whole project transcript directory — so every Stop/PreCompact checkpoint
  re-mined every session in the project. Measured: one such mine ran 6h06m
  (2h CPU) holding the palace write lock while three sessions were live.
  Post the single ``.jsonl`` (convo_miner already supported it; the daemon
  side accepts it via palace-daemon#241 ``_is_mineable_path``).

  *Tests:* 1 updated (test_ingest_transcript_routes_through_daemon)


- **Auto-query signal quality: user's-words depth query + wing inventory, peer-block stripping, identifier signals via BM25 fast route, exhaust filter, 0.50 floor, per-session dedupe, curated-first, visible receipts (#419 #420 #422 #424)** ([`0d92cef`](https://github.com/techempower-org/mempalace/commit/0d92cef))
  Fleet check-in of all 8 live sessions (2026-09-03): 7 had never deliberately
  queried the palace, because the auto-query returned the same session-manifest
  / diary AUTO-SAVE drawers every turn, fired on generic English scraped from
  peer-agent messages, and never on domain identifiers. Fixes: the depth
  refresh searches with the user's own words (wing-scoped, over-fetched) and
  carries a wing inventory line; ``<cross-session-message>`` /
  ``<teammate-message>`` / ``<system-reminder>`` blocks are stripped and
  sentence-initial generic capitals no longer score; identifier-shaped tokens
  are first-class signals retrieved per-term through the daemon's BM25 fast
  route (20–300ms vs 3–5s hybrid on an 86K-drawer wing) with hybrid fallback;
  sessions/diary/checkpoint exhaust and below-floor hits (similarity < 0.50,
  BM25-only < 1.5) never inject; a drawer is never injected twice per
  session; curated ``…/memory/*.md`` drawers rank first; every fired query
  emits a ``RECEIPT`` the hook renders as a visible terminal line
  (``✦ palace ← "…" [wing] → 3 hits via bm25-fast (34ms)``), including
  timeouts and cached fires. Umbrella: #428.

  *Tests:* 17 (test_auto_query_quality) + 5 updated


### Performance


- **Re-mine a transcript incrementally — embed only new or changed chunks; stored drawers are the watermark (#414)** ([`cf1e997`](https://github.com/techempower-org/mempalace/commit/cf1e997))
  Per pass: prefetch id→document for the source file, plan deterministic
  ids, delete only orphans, metadata-only ``update()`` of
  ``source_mtime``/``chunk_total`` on byte-identical chunks (before the new
  upserts, so a mid-pass crash leaves count < chunk_total and the
  completeness rule re-mines), then embed + upsert only new/changed chunks.
  No new watermark storage; cross-host safe. Measured on a 16 MB live
  transcript: first mine 1446 texts / 52.5 s; re-mine after 10% append 77 /
  3.9 s; touch-only 0 / 1.3 s. Fallbacks: prefetch failure → full rebuild;
  update failure → re-file in full.

  *Tests:* 15 (13 unit + 2 end-to-end counting texts that reach the embedder)


## [2026-09-01]


### Changed


- **Sync upstream/develop through v3.9.0 (e8098348): 101 commits — durable config writes, strategy-aware candidate pools, BM25-under-threshold admission, search hub-forward, embeddinggemma batch override, cli_compatible search** ([`09713d9`](https://github.com/techempower-org/mempalace/commit/09713d9))
  Fourth large sync (merge commit, per the post-#394 rule). Upstream
  brought: atomic + fsync'd config persistence with symlink-following
  and unreadable-file quarantine (``_persist_file_config``), a
  strategy-aware candidate pool (``_candidate_pool_limits`` — union
  keeps the requested top-vector slice, the vector path stays 4×
  wide through closet enrichment), BM25 lexical hits admitted under
  a strict ``max_distance`` by computing their stored-embedding
  distance (af7bca77 — replaces the old skip-entirely rule), CLI
  search forwarding to a live hub, ``cli_compatible`` search for the
  hub forwarder, an ``update`` CLI verb, and the embeddinggemma
  ``session.run()`` sub-batch override (#2330).

  Fork-preserved through 15 conflict files: hybrid-default search
  (``candidate_strategy="hybrid"`` — validation extended rather than
  upstream's vector/union set, with unset-vs-explicit preserved so
  ``cli_compatible`` still rejects only explicit non-vector picks),
  adaptmem_ft beside the new embeddinggemma batch kwarg, the
  daemon-strict CLI search block ahead of upstream's hub-forward,
  the drawer/read/graph CLI verb families in the dispatch dict, the
  date-window full-pool rule composed with the new pre-enrichment
  trim, and the postgres BM25 arm conservatively keeping the old
  distance-guarantee (it has no stored-embedding access, so
  upstream's admit-under-threshold path can't apply). The
  ``sqlite_bm25_fallback`` top-up entries gained ``source_path`` /
  ``authored_at`` — the one builder missing the shared entry shape,
  exposed by the wider enrichment pool. Tool surface is 49 (upstream
  absorbed most previously-upstreamed fork tools; their four still
  fork-only tool docs re-added to ``mcp-tools.md``). Upstream's
  version-gated repair test feature-detects ``pathname2url``
  behavior instead (3.13 semantics were backported to 3.12.13).


## [2026-08-21]


### Added


- **CLI wave 3/3: `mempalace diary`, `kg`, `walk` and `rate` first-class verbs (#354, #357, #359, #361)** ([`ff9ee22`](https://github.com/techempower-org/mempalace/commit/ff9ee22))
  Third lane of the CLI wave: ``diary write|read --agent``, ``kg
  add|invalidate|timeline`` (no ``kg stats`` — redundant with
  ``status``, parser-rejection test pins the decision), ``walk``
  with ``--follow palace|tunnels``, and ``rate``. Destructive verbs
  gate on ``--confirm`` and refuse hard when non-interactive.
  ``kg timeline`` documents the backend's 100-row cap
  (``_KG_TIMELINE_TOOL_CAP``) instead of silently truncating.

  All three lanes route daemon-first through the shared helpers that
  landed with the wave: ``_import_mcp_server()`` (stdout-hijack
  repair around the mcp_server import, with a subprocess test
  pinning the statement-order property it relies on) and the
  env-scoped ``--palace`` context manager (``MEMPALACE_PALACE_PATH``
  snapshot taken above the import, so mcp_server's module-scope argv
  parse can't leak it — #409 tracks moving that parse into main()).

  *Tests:* 83 (test_cli_diary, test_cli_kg, test_cli_walk, test_cli_rate)


- **CLI wave 2/3: `mempalace wings`, `taxonomy`, `aaak spec`, `hallway`, `checkpoint --dry-run` (#356, #358, #360, #362)** ([`2848e96`](https://github.com/techempower-org/mempalace/commit/2848e96))
  Second lane: ``wings --sort name|count`` (REST-first via the
  daemon's fast status endpoint — 0.33s live where the MCP tool
  times out at 620K drawers, palace-daemon#239 tracks
  fast-intercepts for the rest), ``taxonomy [--wing]``, ``aaak
  spec`` (raw dialect spec on stdout), ``hallway list|delete``
  (coexists with legacy ``hallways``; deprecation is #407), and
  ``checkpoint --dry-run``. #360's ``--session``/``list`` flags were
  deliberately not built — the underlying tool has no session
  concept; palace-daemon#240 + #408 track growing one properly.

  *Tests:* 82 (test_cli_wings, test_cli_taxonomy, test_cli_aaak, test_cli_hallway, test_cli_checkpoint)


- **CLI wave 1/3: `mempalace drawer get|add|delete|update` and `duplicate check` (#355, #363)** ([`836d1a1`](https://github.com/techempower-org/mempalace/commit/836d1a1))
  First lane of the ten-issue CLI wave (#191 umbrella): first-class
  ``drawer`` verbs and ``duplicate check``. ``drawer update``
  rewrites metadata only — a guard test pins that content is
  immutable (verbatim-always). ``drawer delete`` gates on
  ``--confirm``. Introduced the canonical ``_import_mcp_server()``
  helper every later lane adopted: repairs the #225 stdout hijack
  around local-path imports, prefers the caller's live stream over
  mcp_server's import-time snapshot, and documents the one residual
  case (stdout already aliased to stderr on entry) as out of scope.

  *Tests:* 66 (test_cli_drawer, test_cli_duplicate)


## [2026-08-20]


### Changed


- **Sync upstream/develop through v3.8.0 (3e56979f): 166 commits — date-window search (since/before), openai-compat embeddings, RFC 002 adapter dispatch (#2062), mcp_proxy entry point, chunk_total metadata; ancestry repaired so future syncs replay only new commits** ([`b3527b1`](https://github.com/techempower-org/mempalace/commit/b3527b1))
  Third large sync. Ancestry first: #394 landed as a squash merge, so
  git's merge base stayed pre-v3.7 and this sync initially wanted to
  replay all 433 already-resolved commits. A tree-identical
  ``merge -s ours 8516db7f`` records the true synced point; from here
  on sync PRs land as MERGE COMMITS, never squash.

  Upstream brought: ``since``/``before`` date-window filtering across
  search/list/CLI (#463 family), the ``openai-compat`` embedding
  backend (local /v1/embeddings servers), formalized RFC 002
  source-adapter dispatch with typed exits (#2062 — the fork's
  adapter lineage coming home; ``cmd_mine --source`` now uses it),
  ``mempalace-mcp`` console script repointed at ``mcp_proxy``,
  ``chunk_total`` drawer metadata + interrupted-mine cleanup
  (#2183/#2122), read-paired ``source_mtime`` (#22), overview-cache
  invalidation on writes, and Termux/Docker install docs.

  Fork-preserved through 15 conflict files: adaptmem_ft encoder
  (alongside openai-compat — four embedding options now), novelty
  tagging threaded into the restructured miners next to
  ``chunk_total``, daemon-strict CLI routing (now getattr-tolerant of
  minimal configs), the delegating CLI ``search()`` with upstream's
  window parse + HNSW fence composed in, room-taxonomy warnings next
  to upstream's cache invalidation, tags + the ``96f83d7`` union
  guard. Window correctness extended to every fork-only candidate
  source upstream couldn't know about: the postgres BM25 arm, the
  graph expansion in ``_merge_hybrid_candidates``, and the sqlite
  fallback top-up all honor ``[since, before)``; the pre-fusion trim
  keeps the full in-window pool so BM25-strong drawers deep in the
  vector order still surface (upstream review finding, fork flow).


## [2026-08-09]


### Changed


- **Sync upstream/develop through v3.7.0 (8516db7f): 433 commits — hub forward, temp-collection repair promote, round-trippable drawer IDs, kg_supersede, HNSW CLI fence, 62 conflict files resolved** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  Second large sync (the first was ``b46f18d``, post-v3.5.0). Brings
  v3.6.0 + v3.7.0: the ``mempalace serve`` hub + hook forwarding,
  temp-collection repair promotion with backup-restore semantics,
  round-trippable drawer IDs (#2090), ``kg_supersede`` (#2014-family),
  the CLI HNSW-divergence fence, ``normalize_conversations``
  per-conversation dedup, daemon lock-contention retry (#2029), and
  repair ``--dry-run`` honored for ``--mode from-sqlite`` (#2138).

  Fork-preserved through 62 conflict files: the postgres/pgvector/AGE
  backend dispatch (incl. the ``96f83d7`` union-merge guard, now with
  upstream's ``drawer_id`` field composed in — ``TestUnionPostgresPath``
  green), ``_coerce_wing``, daemon-strict hook routing (now nested
  inside upstream's one-probe ``_hook_write_routing_context``),
  ``hook_verbatim_mode`` threaded through upstream's restructured
  ``normalize``/``convo_miner``, the #1367 embeddings-preserve ported
  onto the new repair flow, adaptmem_ft dispatch, and the daemon-routed
  CLI surface. Precompact adopts upstream's active-transcript-only
  mining. MCP tool surface is now 48 tools (upstream additions on the
  fork's 39); doc/manifest counts reconciled against ``mcp_server.TOOLS``.


### Fixed


- **Postgres backend: _coerce_wing write guard — wing-less writes land in 'general' (not the '' column default) and separator/case variants normalize; scripts/wing_hygiene.py migrates the historical rows (dry-run default)** ([`979da51`](https://github.com/techempower-org/mempalace/commit/979da51))
  The drawers table's wing column defaults to ``''::text``, so any
  writer that omitted the key filed drawers under an unreachable
  empty-string wing (54 smoke-test drawers leaked on 2026-05-12),
  and raw-dirname writers forked near-duplicate wings
  (``kiyo-xhci-fix`` vs ``kiyo_xhci_fix``). ``_coerce_wing`` now
  guards every postgres write choke point — upsert, update, and
  ``rename_wing``'s destination (the source stays literal so
  operators can rename *away* from a malformed wing). The one-off
  migration for historical rows is ``scripts/wing_hygiene.py``
  (dry-run by default; ``--commit`` / ``--delete-debris`` are
  operator calls, tracked in #381). Issue #381.

  *Tests:* 18 pure tests (test_wing_coercion.py) + 2 TEST_POSTGRES_DSN-gated integration tests
  *Files:* `mempalace/backends/postgres.py`, `scripts/wing_hygiene.py`, `tests/test_wing_coercion.py`, `tests/test_backends_postgres.py`


- **sync-outline: empty-secret env vars fall back to documented defaults; unreachable nightly cron retired (0-for-41 by construction) — workflow_dispatch stays** ([`5227da4`](https://github.com/techempower-org/mempalace/commit/5227da4))
  Part of #368. With the ``OUTLINE_BASE_URL`` secret absent, CI
  substitutes an empty string that the ``.get`` default passed
  through, breaking every URL join; ``or``-fallbacks fix that.
  The nightly schedule could never succeed from hosted runners —
  ``outline.jphe.in`` resolves to a Tailscale CGNAT address — so
  the cron trigger is retired in favor of manual dispatch until
  the homelab-timer half of #368 lands.

  *Files:* `scripts/sync-outline.py`, `.github/workflows/sync-outline.yml`


- **Searcher: restore mempalace_search on postgres — guard capability-path conversion in union merge (UnboundLocalError since the 2026-07-02 sync)** ([`96f83d7`](https://github.com/techempower-org/mempalace/commit/96f83d7))
  The ``b46f18d`` upstream sync kept upstream's unconditional tail of
  ``_merge_bm25_union_candidates`` (``bm25_extra = []`` + ``for hit in
  lexical.hits``) after the fork's postgres dispatch branch. On the
  postgres backend the fetched BM25 candidates were clobbered and the
  loop read ``lexical`` — bound only on the capability path — raising
  ``UnboundLocalError`` on every union/hybrid search. MCP
  ``mempalace_search`` and REST ``/search`` 500'd from 2026-07-02
  until the 2026-08-09 deploy (issues #378/#384); ``/health`` stayed
  green throughout, which spawned palace-daemon#229 (``/health/ready``).
  The fix binds both names before the try and converts lexical hits
  only when the capability path fetched them; ``TestUnionPostgresPath``
  now guards the arm for the next sync (#383 watch item).

  *Tests:* TestUnionPostgresPath — first coverage of the fork-only postgres arm
  *Files:* `mempalace/searcher.py`, `tests/test_hybrid_candidate_union.py`


## [2026-07-02]


### Changed


- **Sync upstream/develop through da5a48c (post-v3.5.0): remote MCP server w/ TLS + read-only, graph auto-population, Qdrant facets, list_drawers date filters, 213 commits** ([`b46f18d`](https://github.com/techempower-org/mempalace/commit/b46f18d))
  Merged 213 upstream commits (post-v3.5.0 ``da5a48c``). Notable
  upstream additions: the turnkey secure remote MCP server with TLS and
  a read-only server mode (#1877 / #1900), associative-graph
  auto-population from mined sessions + ``cmd_hallways`` (#1895),
  Qdrant server-side metadata facets (#1868), ``since``/``before``
  date filters on ``list_drawers`` (#1128 / #1891), authored-timestamp
  preservation from transcripts (#1890), ``mine_palace_lock``
  re-entrancy for the HTTP transport (#1859), a pgvector metadata-only
  fetch fix (#1892), SQLite magic-header ``detect()`` (#1893 / #1896),
  FTS5 auto-heal (#1878), a host-root-logger fix (#1860 / #1885),
  LaTeX extensions, and dependency bumps (ruff 0.15.20).

  ~50 conflicted files resolved by composing rather than choosing
  sides: ``tool_list_drawers`` carries BOTH the upstream date filters
  and the fork tag filters; ``tool_status`` keeps the fork's postgres
  fast path (#267) and gains the upstream facets sweep; the HTTP
  transport keeps host pinning and gains TLS + read-only; the merged
  plugin hook config stays the fork's five-event ms-timeout shape. The
  merged MCP tool surface stays at 39 tools (upstream's 34 plus fork
  tools); all doc and manifest tool-count claims reconciled against the
  live ``mcp_server.TOOLS`` count.

  *Files:* `mempalace/mcp_server.py`, `mempalace/searcher.py`, `mempalace/cli.py`, `mempalace/convo_miner.py`, `mempalace/embedding.py`, `mempalace/backends/base.py`, `mempalace/backends/pgvector.py`, `tests/conftest.py`, `tests/test_mcp_server.py`, `tests/test_backends.py`


### Performance


- **Auto-query: TTL cache for the deterministic depth-refresh injection — repeat fires 0ms vs ~850ms daemon round-trip** ([`be903e6`](https://github.com/techempower-org/mempalace/commit/be903e6))
  The periodic depth refresh fires a deterministic query ("session
  context <wing>") whose warm daemon round-trip measures ~780–880ms —
  well over the 500ms hook latency budget — while its results barely
  change within a session. Repeat fires are now served from a small
  on-disk TTL cache (default 900s; ``auto_query.depth_cache_ttl`` /
  ``AUTO_QUERY_DEPTH_CACHE_TTL``, 0 disables): live A/B shows the
  second fire at 202ms end-to-end with 0ms daemon time vs 957ms cold.
  Only the first fire per TTL window pays the daemon call; every path
  fails open, and content-driven queries are never cached.

  *Tests:* 8 tests (TestCacheKey, TestRoundTrip, TestRunnerIntegration)
  *Files:* `mempalace/auto_query/depth_cache.py`, `mempalace/auto_query/runner.py`, `mempalace/config.py`, `tests/test_auto_query_depth_cache.py`


## [2026-07-01]


### Fixed


- **Auto-query firing fixes: frozen turn counter, dead wing scoring, lowercase entities, turn-1 cadence** ([`fad3e27`](https://github.com/techempower-org/mempalace/commit/fad3e27))
  Auto-query was injecting on only ~1.7% of real prompts because of
  three defects, not a threshold preference (found by a read-only
  investigation against the 3,935-decision corpus). The hook derived
  its session id from the never-set ``CLAUDE_SESSION_ID`` env var, so
  the time-based fallback minted a fresh id every prompt and froze the
  turn counter at 1 — the periodic depth signal could never fire. Wing
  scoring was dead in production: ``mempalace_list_wings`` over
  ``/mcp`` scans every drawer and times out, so ``known_wings`` was
  always empty and the +3/+2 bonuses never fired. And the capital-only
  entity regex made lowercase project names invisible.

  Fixes: session id now comes from the hook's stdin JSON (fallback
  ``CLAUDE_CODE_SESSION_ID``); wings are fetched from the fast
  ``/status/fast`` route and cached to disk so scoring survives a
  sleeping daemon; a lowercase wing/alias pass (word-boundary,
  min-length 5, common-word blocklist) registers terse lowercase
  mentions; path/orchestration stopwords stop junk fires on
  ``~/Projects`` fragments; the depth refresh fires on turn 1 and
  every 10 turns (most sessions are short — turn-15 left the majority
  with zero recall); an optional known-entities registry is loaded
  when present. The firing threshold is intentionally unchanged.

  *Tests:* 21 tests (TestDepthSignal, TestLowercaseWingMatch, TestPathAndOrchestrationNoise, TestWingCache, TestKnownEntitiesLoad, TestSessionIdFromStdin)
  *Files:* `mempalace/auto_query/signals.py`, `mempalace/auto_query/__main__.py`, `hooks/palace-auto-query.sh`, `tests/test_auto_query_signals.py`, `tests/test_auto_query_main.py`, `tests/test_auto_query_hook.py`


## [2026-06-29]


### Added


- **Auto-query: periodic depth signal, unknown-entity 0->1 bump, broader temporal patterns** ([`864d7a4`](https://github.com/techempower-org/mempalace/commit/864d7a4))
  Added a content-independent periodic depth signal to auto-query: a
  broad project-scoped palace pull that re-anchors context mid-session
  to counteract "lost in the middle" attention degradation. Routed at
  priority 1.5 (below task resumption, above content-derived signals).
  Also bumped unknown-entity score 0 -> 1 (a bare capitalized name is
  a weak-but-real recall signal) and broadened the temporal patterns
  (recently, a while ago, few days ago, last sprint, back when, used
  to). Landed with the PostCompact timeout-bounds fix for the plugin
  hook config.

  *Tests:* 26 tests (TestDepthSignal, TestDepthRoute, broadened temporal patterns)
  *Files:* `mempalace/auto_query/signals.py`, `mempalace/auto_query/router.py`, `mempalace/auto_query/__init__.py`, `mempalace/auto_query/runner.py`, `tests/test_auto_query_signals.py`, `tests/test_auto_query_router.py`


## [2026-06-26]


### Changed


- **Sync upstream/develop through v3.5.0 (73e74bf): MCP HTTP transport, source_file filter, checkpoint tool, SessionEnd hook, 185 commits** ([`8711e1c`](https://github.com/techempower-org/mempalace/commit/8711e1c))
  Merged 185 upstream commits (v3.4.1 ``b5c79a1`` + v3.5.0 ``e8f96dd``).
  Notable upstream additions: the MCP HTTP transport with a
  DNS-rebind guard (#1806), the ``mempalace_checkpoint`` batch-save
  tool, ``delete_by_source`` (#1722), the ``source_file`` search
  filter (#1815), a security-hardening bundle (#1864), the
  all-layer-0 HNSW quarantine fix (#1716), Cursor / Continue.dev /
  Gemini-CLI session adapters, the SessionEnd save hook (#1341), and
  an opt-in local write daemon (#1783 family).

  27 conflicts resolved by composing rather than choosing sides: the
  fork's tags filter, RRF fusion, ``adaptmem_ft`` encoder, verbatim
  mode, and PALACE_DAEMON_URL routing now coexist with the upstream
  additions. ``build_where_filter`` carries both the fork ``tags``
  (``$contains_all``) and the upstream ``source_file`` clauses;
  ``tool_search`` / the BM25 mergers thread both. The fork's
  postgres/RETIRED-marker preflights run alongside upstream's
  missing-db check; both daemon abstractions (fork HTTP + upstream
  job-queue) are kept, with an explicit ``mine --daemon`` flag taking
  precedence over ambient HTTP routing. Embedder ORT thread cap
  (#1068) threaded through the EF constructors.

  *Files:* `mempalace/searcher.py`, `mempalace/mcp_server.py`, `mempalace/hooks_cli.py`, `mempalace/config.py`, `mempalace/cli.py`, `mempalace/embedding.py`, `mempalace/normalize.py`, `tests/conftest.py`


## [2026-06-14]


### Performance


- **Restore concurrent file mining via parallel-prepare/serial-write (regression from a dropped sync hunk); opt-in --workers** ([`42a107b`](https://github.com/techempower-org/mempalace/commit/42a107b))
  The ``--workers`` flag had been stranded in ``cli.py`` — advertised
  but consumed nowhere after an upstream sync dropped the
  ``ThreadPoolExecutor`` wiring originally added in ``5cd14bd``, so
  mining ran serially regardless of the flag. Re-implemented on the
  current architecture (not a cherry-pick) with a **parallel-prepare /
  serial-write** split: worker threads run the embedding-free prep
  (read, ``detect_room``, ``chunk_text``, chunk-cap, mtime/date) via
  ``_prepare_file``; the main thread performs every
  ``collection.upsert`` serially via ``_write_prepared`` under
  ``mine_lock``. Because embedding happens inside the backend's
  ``upsert(documents=...)`` and all upserts stay on the main thread,
  the encoder is single-threaded regardless of worker count — so this
  composes with the ONNX intra-op cap (upstream #1071) without
  depending on it, and the single-writer property structurally avoids
  the parallel-insert HNSW corruption class (#330). Default
  ``workers=1`` everywhere (sequential, zero behavior change);
  parallelism is opt-in. 3 new tests (workers=1 vs N equivalence,
  write-serialization, prep-error isolation).

  *Files:* `mempalace/miner.py`, `mempalace/cli.py`, `tests/test_miner.py`


## [2026-06-11]


### Changed


- **Sync upstream/develop through v3.4.0 (2ec4bae): RFC-001 backend stack, diary checkpoints restored, 113 commits** ([`373fdf2`](https://github.com/techempower-org/mempalace/commit/373fdf2))
  Merged 113 upstream commits (~40 PRs) including the v3.4.0 release
  and the RFC-001 pluggable-backend stack (#1679, #1727 metric-aware
  similarity, #1731/#1734 embedder identity, #1732 advisory-locked
  maintenance hooks), wing-normalize (#1675/#1702 — opt-in
  ``migrate-wings``, vetted safe for the production palace),
  delimiter-safe drawer ids (#1666, new drawers only), the
  additive-mining ``file_already_mined`` fix, and miner robustness
  (#1137/#1100/#1102/#1622/#1602). Headline resolutions: **diary
  checkpoints restored** (JP, 2026-06-11) — the silent stop-hook path
  writes a themed, agent_name-filed diary entry AND ingests the
  verbatim transcript, marker advancing only on confirmed diary
  write; ``palace.get_collection`` unified (upstream resolution +
  mismatch protection + embedder-identity enforcement, fork postgres
  DSN options + KG write-through); searcher keeps the fork pipeline
  with upstream's metric-aware ``_distance_to_similarity`` threaded
  through ``_hybrid_rank``; backend precedence aligned to RFC 001
  (config.json beats ``MEMPALACE_BACKEND``); MCP server now 39 tools.
  Suite: 4230 passed (was 3908 collected, now 4282).

  *Files:* `mempalace/palace.py`, `mempalace/searcher.py`, `mempalace/mcp_server.py`, `mempalace/hooks_cli.py`, `mempalace/config.py`, `mempalace/cli.py`


## [2026-06-10]


### Added


- **auto_wake: opt-in wake-on-demand for a sleeping palace-daemon host (wake command + /health poll + single retry)** ([`8e0d896`](https://github.com/techempower-org/mempalace/commit/8e0d896))
  The palace daemon often runs on a Wake-on-LAN-armed host that
  suspends to save power, so "connection refused" routinely means
  *asleep*, not *down*. With ``"auto_wake"`` configured in
  ``~/.mempalace/config.json`` (a command string, or an object with
  ``command`` / ``timeout_seconds`` / ``poll_interval_seconds``),
  the CLI's six daemon call sites route through
  ``auto_wake.urlopen_with_wake()``: on a connection-level failure
  it runs the wake command, polls ``/health`` until the deadline,
  and retries the original request once. HTTP errors never trigger
  a wake (the daemon answered — 404-fallback paths stay intact),
  the attempt is once-per-process, ``PALACE_AUTO_WAKE=0``
  force-disables, and any malformed config resolves to *off* — a
  typo must never make the CLI run an unexpected shell command.
  Hooks deliberately stay out: they have a latency budget, and
  their failed mines are already journaled and replayed by
  ``pending_queue``.

  *Tests:* 31 — tests/test_auto_wake.py (config normalization fail-open-to-off, HTTP-vs-connection eligibility gate, attempt/poll/deadline, once-per-process guard, urlopen wrapper retry)
  *Files:* `mempalace/auto_wake.py`, `mempalace/config.py`, `mempalace/cli.py`, `tests/test_auto_wake.py`


## [2026-05-31]


### Performance


- **AGE graph-walk: auto edge-endpoint indexes in backfill + bind anonymous RELATION targets (mempalace#335)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  Follow-ups to the Cat 7b hybrid-latency root-cause (palace-daemon
  ``docs/perf/2026-05-30-hybrid-graph-walk-latency.md``): the per-entity
  graph-walk Cypher in ``searcher._graph_expand_*`` and the daemon's
  ``/search/age-fused`` lookup were the entire hybrid-vs-union latency
  delta, for two reasons now fixed here.

  **Auto edge-endpoint indexes.** AGE only btree-indexes a label table's
  own ``id``, never the ``start_id`` / ``end_id`` graphid columns the edge
  walks join on — so every per-entity lookup parallel-seq-scanned the whole
  edge table (MENTIONS 6.69M rows on the prod palace, ~5.8s cold for a hot
  entity). ``KnowledgeGraphAGE._ensure_edge_endpoint_indexes`` now creates
  ``idx_mentions_{start,end}_id`` + ``idx_relation_{start,end}_id``
  (``CREATE INDEX IF NOT EXISTS``, skips any label whose table doesn't exist
  yet), mirroring ``_ensure_drawer_unique_index``. ``backfill_age.backfill``
  calls it after the edge pass so fresh palaces get the indexes
  automatically. Names/columns are kept in sync with palace-daemon's
  ``scripts/age_graph_indexes.sql`` + ``POST /backfill-age/indexes``
  (the operator-online ``CONCURRENTLY`` path).

  **Bind the anonymous RELATION target.** ``MATCH (a:Entity)-[r:RELATION]->()``
  left the far endpoint anonymous, so AGE built a Parallel Append over every
  vertex label (~1.58M rows) and nested-loop-joined to validate the endpoint,
  spilling to ``/dev/shm``. RELATION is always (Entity)->(Entity); binding the
  open end to ``:Entity`` collapses the Append to a single Entity scan and is
  row-for-row identical (verified on a 300K-edge AGE graph: ``->()`` and
  ``->(:Entity)`` both returned 1816 rows; with the start_id index the RELATION
  scan became a bitmap index scan, 152ms→46ms). All four expand sites updated.

  A third follow-up (raise ``mempalace-db`` off Docker's 64MB ``--shm-size``)
  is deploy-side — the container is built from the sibling ``disks`` repo, not
  provisioned here — so it's documented as an operator action in
  ``docs/operators/2026-05-31-age-graph-walk-shm-size.md`` (required value:
  ``--shm-size=256m``).

  *Tests:* 6 — tests/test_knowledge_graph_age.py::{test_edge_endpoint_index_targets_are_the_four_we_expect, test_ensure_edge_endpoint_indexes_skips_absent_then_installs} + tests/test_searcher_stopwords.py::TestGraphExpandBoundEndpoint (seeds/entities anonymous-target guards); the index test is a @pgmark integration run against a live AGE container (skip-when-absent → install-when-present → idempotent)
  *Files:* `mempalace/knowledge_graph_age.py`, `mempalace/backfill_age.py`, `mempalace/searcher.py`, `docs/operators/2026-05-31-age-graph-walk-shm-size.md`


## [2026-05-29]


### Added


- **pluggable adaptmem_ft encoder backend selectable via MEMPALACE_EMBEDDING_MODEL (closes #308)** ([`5fba6d8`](https://github.com/techempower-org/mempalace/commit/5fba6d8))
  The fork's encoder layer (``mempalace/embedding.py``) previously
  selected between ``minilm`` and ``embeddinggemma`` via
  ``MEMPALACE_EMBEDDING_MODEL``. AdaptMem-trained encoders (FT on
  LongMemEval, SentenceTransformer-shaped) now slot in as a third
  backend ``adaptmem_ft``, loading a local checkpoint from
  ``MEMPALACE_ADAPTMEM_PATH``. Local-first is preserved — nothing is
  downloaded; the path must already exist on the user's machine.

  The backend subclasses ``chromadb.api.types.EmbeddingFunction``
  (not the bare ``__call__`` + ``name()`` pattern the other two use),
  so ``Collection.query`` finds ``embed_query`` and does not silently
  fall back to the default embedder — the trap documented at
  ``mempalace/embedding.py:110-131``. ``name()`` returns ``"default"``
  so an existing 384-dim MiniLM palace accepts queries from this EF
  without an embedding-function-name rejection (a different vector
  space still requires ``rebuild-index``).

  The model loads lazily on first ``__call__`` (import-safe, cheap to
  construct) with a helpful install error if ``sentence_transformers``
  is absent. Off by default — selecting it is a deliberate user
  choice, never a silent fallback.

  Note: the adaptmem ``kaggle_bundle_bi/ft-300-base`` export is
  config + tokenizer only (no weight file), so it is not a loadable
  checkpoint as-is; the smoke test skips until a complete checkpoint
  lands. Tracked on the adaptmem side.

  *Tests:* 19 + 1 opt-in smoke — tests/test_adaptmem_ft.py (EmbeddingFunction-subclass identity / embed_query presence, name()=='default' spoof, lazy load on first encode call, MEMPALACE_ADAPTMEM_PATH resolution from env→config→None, dispatch from get_embedding_function, sentence_transformers lazy-import error message, device resolution, class-cache isinstance stability; smoke test loads the real checkpoint when a complete one is present, skips gracefully on the config+tokenizer-only ft-300-base export)
  *Files:* `mempalace/embedding.py`, `mempalace/config.py`, `tests/test_adaptmem_ft.py`


## [2026-05-28]


### Added


- **scripts/maintain-fork-changes.py + ship-prep step 1: resolve commit:HEAD placeholders and de-dup yaml entries (#316)** ([`9060e09`](https://github.com/techempower-org/mempalace/commit/9060e09))
  Two mechanical cleanups that the rebase chain landing #311 / #312
  / #314 / #315 surfaced as recurring drift, now bundled into a
  reusable script and wired into ``scripts/ship-prep.sh`` as
  step 1:

  1. **commit:HEAD resolution.** Authors write ``commit: HEAD`` on
     the PR branch because the squash-merge SHA isn't known yet.
     Nothing in the existing flow rewrites the placeholder
     post-merge, so the rendered ``FORK_CHANGELOG.md`` accumulates
     broken ``[HEAD](commit/HEAD)`` links. The pass scans each
     entry's text for ``#NN`` references and looks up the closing
     commit via ``git log`` matching priority order: closes/fixes >
     trailing ``(#N)`` in subject > any mention. Highest priority
     wins; entries without a resolvable ``#NN`` are left alone.

  2. **De-duplication by ``id:``**. Long rebase chains (e.g. #310
     rebased over #309 → #314 → #315) re-insert the same entry
     each iteration if the conflict resolution takes ``--ours`` on
     the yaml. The pass keeps the first occurrence and drops the
     rest, reporting what it removed.

  Both passes are idempotent — running on a clean tree produces
  no diff. ``--check`` mode reports drift without writing.
  ``--no-resolve-head`` / ``--no-dedup`` opt-outs cover the case
  where the author wants the raw current state.

  ``scripts/ship-prep.sh`` (#312) now runs this as step 1 so the
  check-docs run that closes the chain catches any drift the
  maintenance pass missed.

  *Tests:* 10 — tests/test_maintain_fork_changes.py (dedup drops second occurrence + idempotent on clean yaml + handles three copies, resolve_head replaces when match found + leaves when no match + dry-run + picks first resolvable in window + first wins when multiple match + indentation preserved, build_issue_to_sha respects closing-phrase priority)
  *Files:* `scripts/maintain-fork-changes.py`, `scripts/ship-prep.sh`, `tests/test_maintain_fork_changes.py`


- **scripts/ship-prep.sh — one command bumps README test count and runs all three doc renderers (#312)** ([`4677db8`](https://github.com/techempower-org/mempalace/commit/4677db8))
  Every fork-ahead PR has needed the same hand-driven dance after a
  rebase: bump ``README.md``'s "<N> tests pass on ``main``" phrase,
  run ``scripts/render-docs.py --target all``, run
  ``scripts/render-llms-full.py``, run ``scripts/render-api-docs.py``,
  then ``scripts/check-docs.sh`` to verify. Five steps, four scripts,
  one ``sed``. ``scripts/ship-prep.sh`` bundles them.

  The script's pytest-discovery follows the same fallback chain
  ``scripts/check-docs.sh`` uses (#311 — repo venv, then main
  checkout's venv via ``git rev-parse --git-common-dir``, then
  PATH), so it works from a worktree without an activated
  environment. The test-count bump is idempotent — no-op if the
  number is already correct — so re-running after a follow-up
  commit is safe.

  ``--no-check`` skips the trailing ``check-docs.sh`` verification
  for the case where you intentionally want to inspect the
  regenerated diff before re-running checks.

  *Files:* `scripts/ship-prep.sh`


- **mempalace_search MCP input schema accepts fusion_mode (convex|rrf) and forwards to search_memories (#302)** ([`f753ec4`](https://github.com/techempower-org/mempalace/commit/f753ec4))
  #162 / PR #295 added ``fusion_mode`` to ``search_memories()`` with
  ``"convex"`` default and ``"rrf"`` opt-in, validated by the
  ``_FUSION_RANKERS`` registry. palace-daemon#105 adds the same
  parameter to its ``/search/hybrid`` HTTP surface so daemon-fronted
  callers can A/B at production scale. But the MCP boundary in
  ``mempalace/mcp_server.py`` whitelists callable arguments against
  the declared ``input_schema`` ``properties`` — and ``fusion_mode``
  wasn't in the list. Daemon-forwarded values were silently dropped
  before reaching ``search_memories``, and the end-to-end A/B never
  worked.

  This change adds ``fusion_mode`` to the ``mempalace_search``
  input schema (enum ``["convex", "rrf"]``, mirroring
  ``candidate_strategy``'s shape), threads the parameter through
  ``tool_search`` into the ``search_memories()`` call, and surfaces
  it on the ``trace`` dict alongside ``candidate_strategy`` /
  ``sources`` when ``include_trace=true``. The default stays
  ``"convex"`` — same default as ``search_memories``' own signature,
  so unmodified callers see no behavior change.

  Three unit tests cover the kwarg path: ``fusion_mode`` reaches
  ``search_memories`` with the default, the explicit value passes
  through, and the MCP whitelist via ``handle_request`` accepts the
  arg rather than returning ``-32602 Unknown parameter`` (the bug
  this issue was filed to fix).

  *Tests:* 3 — tests/test_mcp_server.py (fusion_mode forwarded to search_memories incl. convex default + rrf override, fusion_mode survives MCP whitelist via handle_request, schema advertises both modes in enum)
  *Files:* `mempalace/mcp_server.py`, `tests/test_mcp_server.py`


- **mempalace why + tunnels — explain a drawer + inventory cross-wing tunnels (slice of #191)** ([`fdcd0b4`](https://github.com/techempower-org/mempalace/commit/fdcd0b4))
  Two more verbs join the daemon-fast-path family started by
  ``tags`` / ``overlap`` / ``list`` / ``move`` / ``stats`` /
  ``cypher`` / ``graph``. Same shape: daemon-only, ``--format=table``
  (default) or ``--format=json`` / ``--json``, sibling failure-mode
  exit codes (1 for daemon-down or unreachable, 2 for input
  validation or inner-error envelopes).

  ``mempalace why <drawer_id>`` answers "why would this drawer
  surface?" by composing three read-only daemon calls into one
  report — no ``searcher.py`` changes, pure orchestration over
  existing read paths. (1) ``mempalace_get_drawer`` for the wing,
  room, tag set, and content snippet. (2) A read-only Cypher hop
  against AGE for the drawer's ``:MENTIONS``-Entity edges, top-N
  by mention count (``--entities``, default 10) — the drawer ID
  is sanitized through ``sanitize_kg_value`` and inlined as a
  Cypher literal because the daemon's ``/cypher`` endpoint accepts
  only ``{cypher, graph}``. (3) ``mempalace_search`` keyed on the
  drawer's own first non-blank paragraph for the nearest semantic
  neighbors (``--neighbors``, default 5); the drawer itself is
  filtered out (self-distance 0). A 500 on the entities hop (AGE
  not configured on a chroma-only backend) degrades to an empty
  MENTIONS block — the report still renders. The minimum-viable
  "debugging lens" for retrieval calibration work: see *where* a
  drawer lives, *what* it links to in the KG, and *what siblings*
  it sits next to in vector space — all without standing up a
  notebook.

  ``mempalace tunnels [--wing W] [--passive]`` wraps the existing
  ``mempalace_list_tunnels`` MCP tool. Default returns explicit
  tunnels only (the agent-wired records at
  ``~/.mempalace/tunnels.json``); ``--passive`` opts in to the
  inferred passive overlap (rooms appearing in 2+ wings,
  computed from ``graph_stats`` per issue #75's
  explicit/passive merge). ``--wing W`` filters to tunnels
  touching one wing. The daemon already had this read path
  exposed via MCP; the missing piece was the CLI surface —
  previously you had to call ``stats`` and grep, or hit the
  MCP endpoint directly with curl.

  Slice of #191 (Polished CLI experience). No upstream PR yet;
  both verbs depend on daemon-side state (``mempalace_list_tags``,
  ``mempalace_list_tunnels``, ``/cypher``, ``mempalace_search``)
  that upstream doesn't have, so they ship fork-only until either
  the daemon merges or there's a generic local-palace adapter.

  *Tests:* 36 — tests/test_cli_why.py (three-block report with location/MENTIONS/NEIGHBORS rendering, self-drawer filtered from neighbors, JSON envelope shape, get_drawer + /cypher + mempalace_search composition with drawer-id-as-literal Cypher, /cypher 500 degrades to empty entities not failure, --neighbors/--entities limits, missing/whitespace drawer_id rejection, daemon-down/unreachable + drawer-not-found + search-down exit codes, argparse wiring incl. --neighbors negative rejection); tests/test_cli_tunnels.py (table with kind column, empty payload, --wing scope label in header, --passive opts in mixed kinds, JSON pass-through, --wing/--passive forwarding to mempalace_list_tunnels arguments, daemon-down + inner-error exit codes, argparse wiring)
  *Files:* `mempalace/cli.py`, `tests/test_cli_why.py`, `tests/test_cli_tunnels.py`


- **RRF vs convex-blend rerank — A/B measurement on our corpus (#162)** ([`ea5d567`](https://github.com/techempower-org/mempalace/commit/ea5d567))
  Closes the measurement promised by #162 (the harness landed
  in #247 — this PR runs it). Adds a self-contained ``--mine-corpus``
  mode to ``scripts/eval_fusion_ab.py`` that mines the named
  directory into a fresh local ChromaDB palace and runs the
  convex-vs-RRF A/B against it. Doesn't touch the production daemon
  or share GPU capacity with real callers; the prior
  ``--i-know-the-backfill-is-done`` gate stays on the
  ``--palace-path`` mode for future daemon-side wiring.

  The probe-set loader now accepts both the v2 dict shape
  (``{"_meta": ..., "probes": [{query, expected, why}, ...]}``,
  which the only checked-in probe file ``scripts/probes_v2_git_derived.json``
  uses) and the legacy list-of-lists shape. This matches the
  multi-encoder eval harness's loader so probe sets are
  interchangeable between the two.

  Findings + per-probe data in
  ``docs/research/2026-05-28-rrf-vs-hybrid-rerank-ab.md``
  (the human-readable summary) and the companion ``.json`` (raw
  ranks + deltas for follow-up analysis). **One-line:** RRF
  underperforms the convex blend on this corpus — MRR 0.4075 →
  0.3758 (−0.0318), Recall@10 52% → 47% (−5 pp), 20 regressions
  to 10 improvements. The convex blend's vector-heavy weighting
  (0.6/0.4) is doing real work; RRF's score-scale-agnostic
  treatment loses strong vector signal in the rank-1-under-convex
  cases. Convex stays the default; ``fusion_mode="rrf"`` stays
  shipping as the explicit opt-in for callers who want it.

  Not in scope:

  * Running against the production palace. The daemon's
    ``/search/hybrid`` hard-codes ``candidate_strategy="hybrid"``
    and doesn't forward a ``fusion_mode`` body field, so RRF can't
    be driven remotely today. Filed forward as a palace-daemon
    change if the A/B result motivates it.
  * Sweeping the RRF ``k`` smoothing constant. Default 60 (Cormack
    2009); a sweep is a follow-up if RRF is competitive enough to
    be worth refining.

  *Tests:* 29 — tests/test_eval_fusion_ab.py (probe-loader accepts v2 dict + legacy list shapes, rejects scalars and dicts-without-probes-list; main rejects --mine-corpus + --palace-path together; main refuses --palace-path without --i-know-the-backfill-is-done; pre-existing run-orchestration + scoring-math tests retained)
  *Files:* `scripts/eval_fusion_ab.py`, `tests/test_eval_fusion_ab.py`, `docs/research/2026-05-28-rrf-vs-hybrid-rerank-ab.md`, `docs/research/2026-05-28-rrf-vs-hybrid-rerank-ab.json`


- **KG triples gain SPOC context slot + worker auto-derives valid_from from drawer metadata (#161)** ([`b87ce05`](https://github.com/techempower-org/mempalace/commit/b87ce05))
  KG triples now carry a fourth axis — ``context`` — that anchors a
  fact to where it was witnessed (e.g. ``drawer:abc123``,
  ``conversation:2026-05-28``). The ``add_triple`` write path on the
  AGE backend stores it as a property on the ``RELATION`` edge; every
  read path (``query_triples``, ``query_entity``, ``query_relationship``,
  ``timeline``) surfaces it in the result dict. Triples written
  before this slot existed read back with ``context=None``, so
  consumers don't need a missing-key check.

  The async KG-extraction worker (``kg_triple_worker.py``) now:

  * **Anchors every auto-extracted triple** to its witnessing drawer
    via ``context=f"drawer:{drawer_id}"`` — the SPOC fourth axis is
    always populated on auto-derived facts.
  * **Auto-derives ``valid_from``** from the drawer's metadata
    when the LLM extractor doesn't supply one. Priority order is
    ``timestamp`` (sweeper / convo_miner) → ``filed_at`` (legacy diary)
    → ``session_created_at`` (opencode adapter); first non-empty
    wins. Missing keys leave ``valid_from`` open, which read paths
    already treat as "active since forever."
  * **Defers to the extractor** when it does emit an explicit
    ``valid_from`` — a date the LLM parsed out of the prose
    ("starting May 2025") is more specific than the drawer's
    authored time and takes precedence.

  The MCP-tool surface grew matching parameters:

  * ``mempalace_kg_add`` accepts ``context`` (AGE backend stores;
    SQLite silently ignores so callers don't need to branch on
    backend).
  * ``mempalace_kg_timeline`` accepts ``as_of`` and validates it
    through the same ISO-8601 gate as ``mempalace_kg_query``. The
    accepted value round-trips in the response so callers can echo
    the temporal slice.

  No AGE schema migration was needed — the slot is just an
  additional property on existing edges. Triples written before this
  change continue to read back cleanly with ``context=None``.

  *Tests:* 20 — tests/test_kg_triple_worker.py (context-cypher inclusion/omission, `_derive_valid_from` priority/None paths, worker anchors context=drawer:id, derives valid_from from metadata timestamp, extractor valid_from wins over drawer timestamp, missing timestamp writes open valid_from); tests/test_knowledge_graph_age.py (add_triple persists context, optional/omitted, query_entity returns context, timeline with as_of, timeline without entity respects as_of, timeline returns context field); tests/test_mcp_server.py (kg_timeline rejects invalid as_of, includes as_of in response, default omits as_of, kg_add rejects context with null bytes)
  *Files:* `mempalace/knowledge_graph_age.py`, `mempalace/kg_triple_worker.py`, `mempalace/mcp_server.py`, `tests/test_knowledge_graph_age.py`, `tests/test_kg_triple_worker.py`, `tests/test_mcp_server.py`


### Changed


- **README.md landscape table — refresh upstream MemPalace star count from ~23K → ~53K (current 2026-05-28)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  The verbatim-first landscape table in README.md cited MemPalace
  upstream as "~23K stars" — stale since 2026-04. ``gh api
  /repos/MemPalace/mempalace`` returns 52,993 stars / 6,995 forks
  on 2026-05-28; refreshing to "~53K stars (upstream, 2026-05-28)"
  with the explicit reading date so future drift is visible.

  Other systems in the same landscape table also carry star counts
  (89K claude-mem, 48K Mem0, 22.8K Zep, etc.); they're not refreshed
  in this PR because each requires its own API call and verification
  against the system's actual repo. A periodic refresh script would
  be the principled fix — out of scope here.

  *Tests:* 8 — tests/test_cli_daemon.py (cmd_wakeup: routes-to-daemon-when-strict, forwards-wing-argument, local-path-when-palace-arg-given, daemon-error-exits-nonzero; cmd_mined: routes-to-daemon-when-strict, forwards-wing-and-limit, json-passthrough, daemon-error-exits-nonzero)
  *Files:* `README.md`


- **README.md + docs/ECOSYSTEM.md — soften 'engram-2 17% E2E QA' framing per the 2026-05-24 research doc's unsubstantiated finding (#319)** ([`ddf00b4`](https://github.com/techempower-org/mempalace/commit/ddf00b4))
  Three docs referenced the engram-2 "17% E2E QA for MemPalace"
  attribution with inconsistent framing:

  - ``README.md:163`` treated it as a critique we're answering.
  - ``docs/ECOSYSTEM.md:45`` stated it as a factual claim engram-2
    makes.
  - ``docs/research/2026-05-24-memory-system-benchmarks.md:215``
    documented that the 17% attribution to engram-2 is **not
    substantiated** in their published materials — what engram-2
    actually published is a ~17-point gap between their own LoCoMo
    score (74.5%) and SOTA (91.7%), attributed to the answerer model.

  The 2026-05-24 doc is the more recent and more carefully sourced;
  README + ECOSYSTEM.md predated the fact-check. This PR adopts the
  2026-05-24 doc as source-of-truth:

  - ``README.md`` "Active investigations" reframes the entry as
    "End-to-end QA measurement on the post-structural-fix palace" —
    the corpus-shape pathology that prompted #168 is real and the
    structural fix closed it on our corpus; the deliverable becomes
    a positive measurement rather than a rebuttal.
  - ``docs/ECOSYSTEM.md`` describes what engram-2 actually published
    and cross-links the research doc for readers wanting more.

  #168 itself stays open — the deliverable (publish E2E numbers at
  ``notebook/data/cat9-postmigrate-e2e/REPORT.md``) is unchanged.
  Only the framing of what the numbers are answering changes.

  *Files:* `README.md`, `docs/ECOSYSTEM.md`


### Fixed


- **kg_llm_extractor rewrites AGE dollar-quote tag in triples so drawers indexing palace source code don't fail at add_triple (#313)** ([`3fb9428`](https://github.com/techempower-org/mempalace/commit/3fb9428))
  Drawers indexing palace-daemon / mempalace source code contain
  ``mp_age_q`` verbatim (the AGE dollar-quote tag the cypher
  wrapper uses to delimit its outer SQL literal). When the LLM
  extracted triples about those drawers — e.g. subject="LINE 1",
  predicate="select", object="* FROM cypher($1, $mp_age_q$" — the
  triple's text carried the tag substring straight to
  ``_cypher_literal``, which raised ``Cypher literal contains the
  AGE dollar-quote tag 'mp_age_q'; reject upstream in the
  sanitizer.`` The drawer's KG presence was incomplete and the
  worker logged a warning per failed write.

  The error message itself flagged the fix location ("reject
  upstream in the sanitizer"). This change adds a substring
  rewrite at ``_validate`` — the boundary where the LLM hands
  triples back — replacing ``mp_age_q`` with ``MP_AGE_Q_LIT``.
  The case-sensitive substring check in ``_cypher_literal`` no
  longer fires on the rewritten value (Python's ``in`` operator
  is case-sensitive). Predicate-normalization happens before the
  rewrite, so the lowercased predicate's tag substring is the
  one that gets caught and the final predicate ends up with the
  upper-case placeholder — readable, KG-queryable, safe.

  The roundtrip test (``test_validate_output_survives_cypher_literal``)
  proves the end-to-end fix: a triple whose object is the verbatim
  ``_AGE_DQ_TAG = "mp_age_q"`` source line now passes through
  ``_cypher_literal`` for all three fields without raising.

  *Tests:* 6 — tests/test_kg_extractor.py (rewrites subject + object + predicate, leaves clean triples unchanged, handles multiple occurrences, end-to-end roundtrip survives _cypher_literal)
  *Files:* `mempalace/kg_llm_extractor.py`, `tests/test_kg_extractor.py`


- **scripts/check-docs.sh finds pytest via main checkout when run from a worktree, fails hard instead of silently skipping test-count check (#311)** ([`1d19a8b`](https://github.com/techempower-org/mempalace/commit/1d19a8b))
  Working a fork-ahead PR in a worktree (the standard pattern per
  CLAUDE.md), ``bash scripts/check-docs.sh`` reported "docs clean"
  even when the README test count was stale — because ``REPO_ROOT``
  resolves to the worktree directory which has no ``.venv``,
  ``command -v pytest`` finds nothing in a fresh shell, and step 1
  silently exits via the ``warn "no pytest available — skipping"``
  branch. CI then failed the same check on push (CI installs deps
  fresh, so ``pytest`` is on PATH there).

  The fallback chain is now: ``$REPO_ROOT/.venv/bin/pytest`` →
  ``$(git rev-parse --git-common-dir)/../.venv/bin/pytest`` (the
  main checkout's venv, found via the worktree's git common dir)
  → ``$(command -v pytest)``. Missing pytest in all three locations
  now fails hard with an install hint instead of warning-and-skipping;
  a silent skip lets stale test counts reach CI, and re-running the
  check in CI to discover this is a worse signal than failing
  locally with a clear remediation.

  *Files:* `scripts/check-docs.sh`


- **kg_triple_worker retries add_triple within-worker on transient psycopg errors instead of abandoning to lease-reclaim (#298)** ([`36c0b02`](https://github.com/techempower-org/mempalace/commit/36c0b02))
  When postgres dropped a connection mid-``add_triple`` (network
  blip, OOM-restart, statement-timeout fire) the worker would
  surface the exception, mark the drawer's queue row in the
  ``claimed-but-not-completed`` state, and abandon the in-flight
  triples. The drawer would then wait for the 15-minute
  ``CLAIM_LEASE_SECONDS`` window to expire (PR #277) before any
  worker re-claimed it. Lease-reclaim is the correct shape for
  permanently-dead workers, not transient connection blips on a
  live worker.

  This change wraps the cypher execute in a small retry helper —
  :func:`_execute_with_retry` — that catches the two psycopg error
  families that fire on dropped connections (``OperationalError``,
  ``InterfaceError``) and retries with linear backoff (default
  2s, 4s, 6s; 12-second upper bound) before giving up. Other
  exceptions (cypher syntax errors, ``_cypher_literal`` rejects,
  ``DataError`` from a bad value) propagate on the first attempt —
  retrying those would mask bugs and burn time.

  Tuning knob: ``PALACE_KG_ADD_TRIPLE_MAX_ATTEMPTS`` env var
  (default 3). The hard floor stays the lease-reclaim path — if
  every retry fails, the drawer still survives via the dead-worker
  reclaim that PR #277 provides.

  Observed in production 2026-05-28 09:59 PDT: postgres
  OOM-killed by its cgroup memory cap (tracked separately at
  techempower-org/familiar.realm.watch#50). Six in-flight triples
  on a single drawer were abandoned; the drawer recovered ~15
  minutes later via lease-reclaim. With this change the recovery
  would have been ~12 seconds.

  *Tests:* 6 — tests/test_kg_triple_worker.py (succeeds first attempt, recovers after OperationalError, recovers after InterfaceError, raises after max attempts, does not retry non-transient ValueErrors, env override PALACE_KG_ADD_TRIPLE_MAX_ATTEMPTS)
  *Files:* `mempalace/kg_triple_worker.py`, `tests/test_kg_triple_worker.py`


- **mempalace_kg_stats returns structured backend-unavailable envelope on transient psycopg failures (#299)** ([`8fd0b01`](https://github.com/techempower-org/mempalace/commit/8fd0b01))
  Observed in production 2026-05-28 09:59 PDT (familiar): postgres
  OOM-killed under writethrough load. The `mempalace_kg_stats` MCP
  tool propagated the raw `psycopg.OperationalError` to the
  envelope as `Tool error in mempalace_kg_stats` — an opaque
  -32000 internal error to the caller, with no signal that
  "retry in a moment" is the right response.

  Wraps `tool_kg_stats` with a try/except that catches the two
  psycopg families that fire on dropped connections
  (`OperationalError`, `InterfaceError`) and returns
  `{"error": "backend_unavailable", "detail": "...", "retryable": true}`.
  Other exceptions (cypher syntax, value validation, schema
  mismatch) still propagate — those are bugs, not transient
  backend state, and "retryable" would mask them.

  Transient-error classifier broken out as
  `_is_transient_postgres_error()` so other MCP tools can adopt
  the shape without re-implementing the family check. Broader
  `_call_kg` refactor left as a follow-up — the smallest-blast-
  radius fix is the right shape while
  techempower-org/familiar.realm.watch#50 (raise postgres
  MemoryMax cap) is being addressed.

  *Tests:* 3 — tests/test_mcp_server.py (psycopg.OperationalError surfaces structured envelope, psycopg.InterfaceError same, non-transient ValueError propagates)
  *Files:* `mempalace/mcp_server.py`, `tests/test_mcp_server.py`


## [2026-05-27]


### Added


- **mempalace bulk-move — multi-drawer metadata relocation by source wing/room (#191)** ([`1ca544b`](https://github.com/techempower-org/mempalace/commit/1ca544b))
  ``mempalace bulk-move --wing W --room R --to-wing W2 --to-room R2``
  is the multi-drawer complement to ``move``. It selects every drawer
  matching a source wing/room via offset-paginated ``GET /list`` and
  PATCHes each match to a target wing/room. As with ``move``, the
  verbatim-always principle forbids touching drawer text — there is
  **no ``--content`` flag**, and the parser rejects it.

  The safety model is deliberately conservative because the command
  mutates many drawers at once:

  * a **source filter is required** — at least one of ``--wing`` /
    ``--room`` — so it can never operate on the whole palace by
    accident (exit 2 if absent);
  * a **target is required** — at least one of ``--to-wing`` /
    ``--to-room`` (exit 2 if absent);
  * **dry-run is the default** — without ``--apply`` it prints a
    per-drawer ``cur → target`` preview and sends zero PATCH calls;
  * ``--apply`` **prompts for confirmation on a TTY** (skip with
    ``--yes``) and **refuses to run unattended** — non-TTY or
    ``--json`` without ``--yes`` exits 2 — so a pipeline can't
    silently mass-mutate the palace;
  * one drawer's PATCH failing **never aborts the batch** — failures
    are collected and reported (``moved N, failed M`` with the failed
    ids), and the process exits 2 if any failed.

  Failure modes mirror the ``list`` / ``move`` sibling family:
  daemon unreachable / 404 / 401 / 403 during listing → exit 1;
  missing selection or target, or any PATCH failure → exit 2;
  ``--format=json`` emits a structured ``{matched, dry_run, moved,
  failed, source, target}`` envelope.

  *Tests:* 23 — tests/test_cli_bulk_move.py (validation of required source filter and target, dry-run default with no-PATCH assertion, --apply+--yes happy path, TTY prompt accept/decline, non-TTY/json refusal without --yes, partial-failure continue+report, DaemonError during PATCH, pagination across multiple /list pages, list-failure exit 1, inner-error exit 2, json envelope shapes, argparse wiring incl. --content rejection)
  *Files:* `mempalace/cli.py`, `tests/test_cli_bulk_move.py`


## [2026-05-26]


### Added


- **mempalace move — fast direct-to-daemon single-drawer wing/room relocation (#191)** ([`d007b6f`](https://github.com/techempower-org/mempalace/commit/d007b6f))
  ``mempalace move <drawer_id> --wing W --room R`` relocates a
  single drawer to a different wing/room. It is the single-drawer
  complement to the existing bulk ``rename-wing``, and the next
  slice of the polished-CLI work after the analytics quartet
  (list / graph / cypher / stats).

  The command wraps the daemon's ``PATCH /memory/{drawer_id}``
  route — one network hop, no AGE locks. It sends only the
  supplied ``wing`` / ``room`` keys; at least one is required. An
  empty PATCH is an ambiguous no-op the daemon would reject with a
  400, so ``move`` refuses it client-side (exit 2) and sends no
  request at all.

  There is deliberately **no ``--content`` flag**, even though the
  daemon route accepts content edits. The fork's verbatim-always
  principle forbids the human CLI from ever mutating stored drawer
  text — ``move`` relocates metadata only. The parser rejects
  ``--content`` outright, and a test guards that contract.

  Output mirrors the sibling fast-daemon commands:
  ``--format=table`` (default) prints an old→new confirmation
  (unchanged fields are marked ``(unchanged)`` since the daemon's
  update response carries only the new values, and there's no
  cheap single-drawer GET route to read the prior ones);
  ``--json`` / ``--format=json`` passes the daemon envelope
  through unchanged. The X-API-Key header is sent the same way as
  the other REST commands.

  Failure modes match the
  ``cmd_list``/``cmd_graph``/``cmd_cypher``/``cmd_stats`` family:
  daemon unreachable / 404 / 401 / 403 → exit 1; an inner-error
  envelope (``success=False`` — drawer not found or a
  sanitize/validation failure) → exit 2; a missing
  ``PALACE_DAEMON_URL`` → exit 2.

  *Tests:* 24 — tests/test_cli_move.py (flag propagation for --wing/--room/both, drawer_id in URL path, table+json output, missing-both-flags refusal without PATCH, daemon-down across unreachable/404/401/403/inner-error, parser acceptance incl. rejection of --content)
  *Files:* `mempalace/cli.py`, `tests/test_cli_move.py`


- **mempalace cypher — read-only Cypher query CLI (#191)** ([`32a41b1`](https://github.com/techempower-org/mempalace/commit/32a41b1))
  A new ``mempalace cypher`` subcommand — the arbitrary-walk escape
  hatch that composes with the snapshot view shipped in
  ``mempalace graph``. Wraps the palace daemon's
  ``POST /cypher`` endpoint, which runs the supplied query against
  Apache AGE on the postgres backend inside a ``READ ONLY``
  transaction (write verbs are rejected server-side with SQLSTATE
  25006 → HTTP 403). The CLI does **not** re-implement a
  client-side blocklist of write verbs — that would drift; the
  daemon is the source of truth for what is read-only.

  Flags: positional ``QUERY`` (required), ``--graph`` (default
  ``mempalace_kg``), ``--format table|json|csv``, ``--limit N``
  (advisory hint only — the daemon's ``statement_timeout`` from PR
  #228 is the real ceiling for runaway queries).

  Failure modes mirror ``cmd_list`` / ``cmd_graph`` with one
  addition for the read-only contract. Daemon-unreachable
  (``DaemonError`` from timeout or network failure) prints a
  stderr hint and exits 1. Non-2xx HTTP statuses are classified
  through a tri-state return: ``403`` triggers a friendly
  ``"this endpoint is read-only; rewrite as MATCH/RETURN"`` hint
  and exits 2; ``401``/``404``/``503`` exit 1 with the generic
  unreachable message. An inner-error envelope from the daemon
  (``{"error": "..."}`` with no ``rows`` / ``data``) exits 2 — but
  a response carrying *both* ``rows`` and an ``error`` field is
  treated as success, since the rows present mean the query did
  run (defensive against deprecation-warning chatter).

  Empty / whitespace-only queries exit 2 without contacting the
  daemon at all — instant feedback, zero daemon load.

  No new MCP tool is added — the AI path already drives AGE via
  the daemon's ``POST /cypher`` directly. This bridges operators
  and scripts to the same endpoint with structured output and
  sane exit codes.

  Slice of the polished-CLI umbrella issue #191, building on the
  ``mempalace list`` (cli-list-drawer-browser) and
  ``mempalace graph`` (cli-graph-kg-snapshot) slices.

  *Tests:* 21 — tests/test_cli_cypher.py (flag propagation, three formats, empty rows, daemon-down + 403 read-only hint)
  *Files:* `mempalace/cli.py`, `tests/test_cli_cypher.py`


- **mempalace graph — fast direct-to-daemon KG structural snapshot (#191)** ([`499f42d`](https://github.com/techempower-org/mempalace/commit/499f42d))
  A new ``mempalace graph`` subcommand — the structural counterpart
  to ``mempalace list``. Wraps the palace daemon's ``GET /graph?limit=``
  endpoint, which returns a pre-aggregated palace shape (wings,
  rooms, passive tunnels) plus a KG slice (top-N entities, sample
  RELATION/MENTIONS triples, kg_stats with global totals).

  Read-only, safe to run during backfill — the daemon assembles
  the snapshot from pre-aggregated tables, not from a live AGE
  Cypher walk. Recall-preserving by design: ``--limit`` only caps
  the KG entity sample (and 2x for MENTIONS triples per the
  daemon's openapi spec); wings, rooms, and tunnels always ship
  in full.

  Flags: ``--limit N`` (default 500, sanity-clamped to [1, 50000]
  to match the daemon's hard ceiling), ``--format table|full|json``.
  ``table`` is the default summary view — palace structure block
  (wing/room/tunnel/drawer counts) + top-10 wings by drawer count +
  KG stats + sample entities and triples. ``full`` enumerates every
  wing, every room breakdown, every tunnel, and every sampled
  entity/triple/mention with no truncation — useful for piping
  into ``grep`` or further analysis. ``json`` mirrors the daemon's
  response shape exactly (``wings``, ``rooms``, ``tunnels``,
  ``kg_entities``, ``kg_triples``, ``kg_mentions``, ``kg_stats``).

  Daemon-unreachable (``DaemonError`` from timeout or network
  failure, or ``_call_daemon_rest`` returning ``None`` on 404/401/403)
  prints a stderr hint and exits 1, matching the cmd_list /
  cmd_status fallback. With ``--format json`` the failure surfaces
  as a structured envelope on stdout so machine callers get a
  parseable shape. An ``error`` payload from the daemon with no
  structural keys (e.g. ``palace_unavailable``) exits 2 to match
  the same contract.

  No new MCP tool is added — the AI path can already query AGE
  directly via ``POST /cypher`` for finer-grained graph walks.
  This bridges operators and scripts to the same pre-aggregated
  snapshot the daemon already serves.

  Slice of the polished-CLI umbrella issue #191, building on the
  ``mempalace list`` slice (cli-list-drawer-browser).

  *Tests:* 16 — tests/test_cli_graph.py (flag propagation, limit clamping, three formats, daemon-down fallback)
  *Files:* `mempalace/cli.py`, `tests/test_cli_graph.py`


- **mempalace list — fast direct-to-daemon drawer browser (#191)** ([`257137b`](https://github.com/techempower-org/mempalace/commit/257137b))
  A new ``mempalace list`` subcommand that wraps the palace daemon's
  ``GET /list`` REST endpoint (which itself wraps the existing
  ``mempalace_list_drawers`` MCP tool). Pure metadata browse — no
  ranking, no embedding, no exclusion. Recall-preserving by design:
  every drawer matching the optional ``--wing`` / ``--room`` filter
  is reachable via ``--offset``, and no drawer is dropped.

  This is the human/script counterpart to ``mempalace_list_drawers``;
  the AI path continues to use that MCP tool. **No new MCP tool is
  added** — the CLI just bridges agents and operators to the same
  underlying listing.

  Flags: ``--wing W`` / ``--room R`` (metadata filters), ``--limit N``
  (default 20, sanity-capped at 1000), ``--offset N`` (pagination),
  ``--format table|compact|full|json``. ``table`` is the default
  multi-line preview view; ``compact`` is one line per drawer for
  pipelines; ``full`` is labelled sections with no truncation;
  ``json`` mirrors the upstream tool shape.

  Daemon-unreachable (``DaemonError`` or ``_call_daemon_rest``
  returning ``None``) prints a stderr hint and exits 1, matching the
  cmd_status fallback and the graceful 401/403 handling added in
  850e08c. With ``--format json`` the failure surfaces as a
  structured envelope on stdout. An ``error`` payload from the
  daemon (e.g. ``palace_unavailable``) exits 2 to match the
  cmd_status contract.

  Slice of the polished-CLI umbrella issue #191. Read-only, safe
  during backfill.

  *Tests:* 18 — tests/test_cli_list.py (flag propagation, four formats, daemon-down fallback)
  *Files:* `mempalace/cli.py`, `tests/test_cli_list.py`


- **Recency decay weighting in search + mempalace prune --stale-days CLI (#158)** ([`558d327`](https://github.com/techempower-org/mempalace/commit/558d327))
  Two derivative-store extensions that sit *next to* the verbatim record,
  neither of which touches stored content.

  **Recency weighting.** ``search_memories`` can apply a small, bounded
  distance shift based on a drawer's age (``mempalace.recency``):
  exponential decay so a fresh drawer is nudged up, fading to half its
  boost after one half-life. The shift is capped (max 0.03 cosine-distance
  units, below the weakest closet-boost rung) so it can reorder neighbours
  but never push a relevant drawer out of the result set — 100% recall is
  preserved. A drawer with no parseable ``filed_at`` is treated as ageless
  (zero adjustment), never penalized. The signal ships **dark**: gated by
  ``PALACE_RECENCY_BOOST`` (default off, ``=1`` enables) so we A/B it on our
  own corpus before trusting it; half-life is tunable via
  ``PALACE_RECENCY_HALFLIFE_DAYS``. Read live, so the daemon picks up the
  toggle without a restart.

  **Prune CLI.** ``mempalace prune --stale-days N`` removes drawers older
  than N days from an optional ``--wing`` / ``--room`` scope. Because it
  destroys data on a *time* predicate rather than an explicit selection, it
  is **dry-run by default** — nothing is deleted unless ``--confirm`` is
  passed. Undated drawers are never pruned (we don't delete a drawer we
  can't date). Age is decided in Python (chromadb ``where=`` can't
  range-compare the ISO-timestamp string), then deletion is by explicit id
  list.

  Upstream tracks Weibull decay + a Tier-0 LLM rerank in
  MemPalace/mempalace#1032 (informational); the fork-side contribution is
  the independent prune CLI and the off-by-default recency knob. Fully
  local: no network, no external API, no telemetry.

  *Tests:* 20 — tests/test_recency_prune.py (age parsing, recency adjustment, searcher integration, prune CLI)
  *Files:* `mempalace/recency.py`, `mempalace/searcher.py`, `mempalace/cli.py`, `tests/test_recency_prune.py`


- **mempalace_rate_memory MCP tool + bounded rating signal in search ranking (#159)** ([`583536c`](https://github.com/techempower-org/mempalace/commit/583536c))
  A new ``mempalace_rate_memory(drawer_id, useful: bool)`` MCP tool lets
  an agent or user record whether a search result was helpful. The rating
  is stored as drawer *metadata* — two counters (``rating_useful`` /
  ``rating_not_useful``) accumulated via a metadata-only ``col.update``.
  The verbatim drawer content is never touched, honoring the
  verbatim-always principle: ratings live alongside the words, never
  inside them.

  ``search_memories`` reads the net rating (useful − not_useful) and
  applies a bounded, capped cosine-distance shift (``mempalace.ratings``:
  0.03 per net point, capped at ±0.12 — deliberately below the weakest
  closet-boost rung). A useful drawer moves up, an unhelpful one moves
  down, but the shift can only reorder neighbours — it can never push a
  relevant drawer out of the result set, so 100% recall is preserved.
  Each hit surfaces its ``rating_score`` for transparency. The signal is
  gated by ``PALACE_RATING_BOOST`` (default on; ``=0`` disables for A/B or
  debugging), read live so the daemon picks it up without a restart.

  Tier 1 (explicit ratings) only. Tier 2 (implicit echo/fizzle signals
  from issue #159) is deferred to a follow-up — kept out of this slice to
  stay small and focused. Fully local: no network, no external API, no
  telemetry.

  *Tests:* 22 — tests/test_rate_memory.py (ratings helpers, MCP tool, searcher integration)
  *Files:* `mempalace/ratings.py`, `mempalace/mcp_server.py`, `mempalace/searcher.py`, `tests/test_rate_memory.py`


- **RRF fusion mode + convex-vs-RRF A/B harness (#162)** ([`6c9d10c`](https://github.com/techempower-org/mempalace/commit/6c9d10c))
  ``search_memories`` gains a ``fusion_mode`` parameter selecting how the
  merged candidate pool is finally ranked: ``"convex"`` (default — the
  existing weighted vector+BM25 blend in ``_hybrid_rank``) or ``"rrf"``
  (Reciprocal Rank Fusion of the vector and BM25 rank orderings via the
  pure ``mempalace.rrf`` primitives). RRF fuses rank positions rather than
  blending incomparable cosine/Okapi score scales — the question #82 left
  untested when it found raw-vector RRF lift didn't survive the hybrid
  pipeline.

  ``scripts/eval_fusion_ab.py`` is the A/B apparatus: runs both pipelines
  over a probe set (same ``[query, expected, why]`` JSON format as the
  multi-encoder harness) and reports MRR / Recall@5 / Recall@10 plus
  per-probe rank deltas. The scoring math is pure and unit-tested; the
  live run is gated behind an explicit acknowledgement flag and DEFERRED
  until the KG backfill completes, since running it hits daemon ``/search``
  and steals GPU/daemon capacity. Honors
  ``feedback_test_retrieval_against_our_corpus`` — A/B on our corpus, not
  trusted from literature.

  *Tests:* tests/test_rrf_rank.py (13), tests/test_eval_fusion_ab.py (18)
  *Upstream:* [PR #247](https://github.com/MemPalace/mempalace/pull/247)
  *Files:* `mempalace/searcher.py`, `scripts/eval_fusion_ab.py`, `tests/test_rrf_rank.py`, `tests/test_eval_fusion_ab.py`


- **mempalace stats: add ROOMS breakdown (drawer count by room) to the dashboard** ([`1673465`](https://github.com/techempower-org/mempalace/commit/1673465))
  ``mempalace stats`` (#191, PR #193) surfaced drawer counts by wing
  but not by room — even though the daemon's ``/status/fast`` payload
  already returns both ``wings`` and ``rooms`` maps. The issue's
  analytics scope explicitly asks for "drawer count by wing/room".

  Add a ``ROOMS`` section to the human dashboard (mirroring the WINGS
  block's sorted bars + ``--top`` truncation) and a ``rooms`` key to
  the ``--json`` payload. Wings answer "which domains", rooms answer
  "which kinds of memory" — the canonical 7-room taxonomy
  (references, discoveries, architecture, problems, planning,
  sessions, decisions, plus diary/debugging). Zero extra daemon cost:
  rooms ride along in the same ``/status/fast`` response already
  fetched for the WINGS block. Degrades to "(no rooms)" against older
  daemons that omit the ``rooms`` key.

  *Tests:* 4 — test_cli_stats.py::TestCmdStatsDaemon::{test_renders_rooms_section,test_rooms_section_handles_missing_rooms,test_top_truncates_rooms} + rooms assertion in test_json_output_shape
  *Files:* `mempalace/cli.py`, `tests/test_cli_stats.py`


- **Calibrated confidence field on search results + Brier-score eval column** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  Surfaces an optional ``confidence`` field alongside each search hit:
  a calibrated probability that the hit is relevant, derived from the
  raw ``similarity`` via isotonic regression on a labeled probe set.
  The new ``mempalace/calibration.py`` provides a ``Calibrator``
  (sklearn isotonic when available, pure-python Pool-Adjacent-Violators
  fallback otherwise), JSON save/load, and the ``brier_score`` /
  ``expected_calibration_error`` scoring rules.

  ``search_memories`` loads the calibrator from a configured path
  (``calibration_path`` — env ``MEMPALACE_CALIBRATION_PATH`` or
  ``config.json``) and applies it after the hybrid re-rank. When no
  calibrator is configured or the file is missing, no ``confidence``
  field is emitted — the system never fakes a calibrated score, and
  callers already handle the absent key. Hits with ``similarity=None``
  (BM25-only / graph-source) carry no vector signal and so get no
  confidence. Default behavior is unchanged: calibration is opt-in.

  ``scripts/eval_multi_encoder_rrf.py`` gains a Brier-score + ECE
  column so future retrieval changes can be evaluated on calibration
  quality, not just rank quality (MRR/Recall). The column is only
  populated when a calibrator is configured; otherwise the eval prints
  a note rather than a misleading zero. Implements
  techempower-org/mempalace#167; analysis in
  docs/research/uncertainty-aware-retrieval.md.

  *Tests:* 30 — tests/test_calibration.py (27), tests/test_searcher_confidence.py (3), plus 3 in tests/test_config.py
  *Files:* `mempalace/calibration.py`, `mempalace/searcher.py`, `mempalace/config.py`, `scripts/eval_multi_encoder_rrf.py`, `tests/test_calibration.py`, `tests/test_searcher_confidence.py`, `tests/test_config.py`


- **Evaluation doc: curated-authority vs auto-mined separation (#202)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  ``docs/research/2026-05-26-multi-palace-separation.md`` — a
  principle-first evaluation companion to the engineering design at
  ``docs/designs/multi-palace-separation.md``. Frames the
  curated-authority-vs-auto-mined problem from upstream #1018
  (@kostadis), weighs five separation options against the fork's
  design principles (verbatim-always, entity-first, incremental-only,
  local-first) plus the codified read-surface-parity lesson from P8,
  and recommends collection-partitioning over multi-palace with
  read-surface parity as a schema invariant. Cites #169, #46, #76.
  Feeds the #169 Phase 1 input. Closes evaluation half of #202.

  *Files:* `docs/research/2026-05-26-multi-palace-separation.md`


### Changed


- **mempalace stats migrates to GET /stats REST + exposes graph/status sections (#191)** ([`853bb25`](https://github.com/techempower-org/mempalace/commit/853bb25))
  ``mempalace stats`` migrates from a 3-4-call MCP-tool fan-out
  (``mempalace_status`` + ``mempalace_kg_stats`` +
  ``mempalace_graph_stats`` + optional ``mempalace_list_tags``)
  to a single ``GET /stats`` REST hit. The unified envelope
  returns three blocks: ``kg`` (entities, triples,
  relationship_types), ``graph`` (rooms, tunnels, edges), and
  ``status`` (drawer counts, wings, rooms, protocol / AAAK text).
  One network hop instead of four — same data, faster.

  The slice also widens the analytics surface beyond the original
  issue spec. ``--section`` now accepts three real values plus
  ``all``:

  - ``--section=kg`` — knowledge graph counts. Useful for "how
    many entities/triples does the palace know" without scrolling
    past wing breakdowns.
  - ``--section=graph`` — the AGE graph's structural picture
    (room count, tunnel rooms shared by 2+ wings, edge totals,
    top tunnels). Useful for understanding cross-wing reach.
  - ``--section=status`` — wing/room drawer counts plus the
    canonical 7-room taxonomy footprint. Richer than ``mempalace
    status``, which uses ``/status/fast`` for health-only.
  - ``--section=all`` (default) — every block, in the same order
    as the dashboard before this change.

  ``--no-relationship-types`` suppresses the relationship_types
  list, which in production carries 1000+ entries and dominates
  the table render when scripting. Table mode replaces the list
  with the count; json mode swaps the list for
  ``{"relationship_types_count": N}``, keeping the daemon
  envelope contract intact for jq pipelines that need to know the
  count without parsing the array.

  ``--format=table|json`` is the canonical flag; ``--json``
  remains as a shorthand for backward compatibility. Table mode
  suppresses ``protocol`` and ``aaak_dialect`` (text blobs from
  the ``status`` block, not analytics); json mode passes them
  through so consumers piping to jq see every field the daemon
  emitted.

  ``--tags`` continues to fire an extra ``mempalace_list_tags``
  MCP call because ``/stats`` deliberately does not include the
  tag breakdown — tag counts can be 100K+ entries on a populated
  palace and don't belong in a fast-path summary.

  Failure modes now match the sibling
  ``cmd_list``/``cmd_graph``/``cmd_cypher`` family: daemon
  unreachable surfaces as exit 1 (changed from exit 2 under the
  multi-call implementation), 404/401/403 as exit 1, inner-error
  envelopes as exit 2, and a missing ``PALACE_DAEMON_URL`` as
  exit 2. The exit-code alignment is the only behavior change
  visible to scripts: ``stats`` now signals "daemon down" with
  the same exit code as ``list``/``graph``/``cypher``.

  Slice of #191.

  *Tests:* 32 — tests/test_cli_stats.py (flag propagation across all four --section values, table+json formats, empty/partial payloads, daemon-down across unreachable/404/401/403/inner-error)
  *Files:* `mempalace/cli.py`, `tests/test_cli_stats.py`


- **Formalize wing/room derivation order; demote entity detector to last-resort hint (#157)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  Document and enforce the derivation order for wing/room assignment
  as an explicit contract:
  ``cwd > transcript path > project directory hint > (optional) entity
  hint > unfiled``. This ratifies README Architectural Principle 2 —
  "derived hierarchy from unambiguous signals outperforms
  hand-classified hierarchy."

  Two new functions in ``hooks_cli.py`` make the contract concrete:
  ``derive_wing(transcript_path, project_dir=None, entity_hint=None)``
  wraps the existing cwd/transcript-path resolver
  (``_wing_from_transcript_path``) and adds the project-directory hint
  and a last-resort entity hint; ``derive_room(content, room_hint=None,
  entity_hint=None)`` mirrors the contract for the room axis over the
  canonical 7-room taxonomy.

  The entity detector is demoted from a gate to a *hint, never a gate*:
  it is the last branch in both functions, reached only when every
  unambiguous signal above it is absent, and a confident entity match
  can never override a cwd / transcript-path / project-directory signal.
  The room result stays FK-safe (always a canonical room). New design
  doc at ``docs/designs/hierarchy-derivation-order.md``; 11 unit tests
  pin the priority order on synthetic inputs (no live palace/daemon).

  *Tests:* 11 — tests/test_hooks_cli.py (derive_wing/derive_room priority-order suite)
  *Files:* `mempalace/hooks_cli.py`, `tests/test_hooks_cli.py`, `docs/designs/hierarchy-derivation-order.md`


### Fixed


- **Apply AGE statement_timeout in same transaction as cypher() (PR #228 follow-up)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  The PR #228 hotfix added ``SET LOCAL statement_timeout = '3s'`` to
  ``_graph_expand_from_entities`` but ran it as a bare ``execute()``
  under ``conn.autocommit = True``. With autocommit, each
  ``execute()`` runs in its own implicit transaction — so the
  ``SET LOCAL`` ended immediately and the next ``cypher()`` call
  opened a fresh transaction at the session default of 0. Result:
  2026-05-26 prod saw 16 AGE backends running 5+ minutes each. JP
  mitigated with ``ALTER ROLE palace SET statement_timeout = '30s'``;
  this PR makes the in-code guard actually fire.

  Fix: wrap ``SET LOCAL`` + the ``cypher()`` execute in
  ``with conn.transaction():`` so both share one BEGIN/COMMIT and
  the LOCAL setting scopes the cypher call. Verified empirically
  with psycopg 3.3.4: bare execute pattern takes 0.5s on
  ``pg_sleep(0.5)`` (no timeout); transaction-wrapped pattern fires
  after 0.10s as expected. Regression test pins the source shape so
  future refactors can't quietly un-wrap the block.

  Companion: ``docs/operators/2026-05-26-age-statement-timeout.sql``
  adds a btree expression index on the AGE-native ``a.name``
  access expression. Drops the Entity-side lookup from
  ``Parallel Seq Scan`` (cost 7650 over 472K rows) to
  ``Index Scan`` (cost 32). The existing ``idx_entity_name`` GIN
  index doesn't accelerate the equality predicate AGE generates.

  *Tests:* 1 — test_searcher_stopwords.py::TestGraphExpandCypherSafety::test_statement_timeout_shares_transaction_with_cypher
  *Files:* `mempalace/searcher.py`, `tests/test_searcher_stopwords.py`, `docs/operators/2026-05-26-age-statement-timeout.sql`


## [2026-05-25]


### Added


- **LLM-based KG triple extraction: queue table, async worker, llama.cpp on familiar** ([`59ac0bc`](https://github.com/techempower-org/mempalace/commit/59ac0bc))
  Two-layer KG architecture: the existing regex extractor produces
  ``MENTIONS`` edges inline on every write; a new LLM-based pipeline
  produces typed ``(Entity)-[:RELATION]->(Entity)`` triples
  asynchronously. The async path decouples extraction latency from
  write latency — session mines no longer block on the LLM.
  Components: a ``mempalace_kg_extraction_queue`` table populated by
  the writethrough hook, an async worker (``mempalace-kg-extract``)
  that claims batches with ``UPDATE ... SKIP LOCKED`` and posts to
  a llama-server endpoint, and a backfill driver
  (``scripts/backfill_kg_triples.py``) for the existing 364K
  drawers. Operator surface: systemd unit at
  ``deploy/systemd/mempalace-kg-extract.service``, environment
  template at ``kg-extract.env.example``, full operator guide at
  ``docs/kg-extraction.md``, and a palace-daemon ``GET
  /kg-extract/status`` endpoint (landed separately in the daemon
  repo). Backfill driver supports 24 in-flight workers per process
  and trivial side-by-side parallelism via the SKIP LOCKED claim.

  *Tests:* test_kg_extractor.py + test_kg_extraction_queue.py + test_kg_triple_worker.py + test_backfill_kg_triples.py
  *Files:* `docs/specs/kg-triple-extraction.md`, `mempalace/kg_llm_extractor.py`, `mempalace/kg_triple_worker.py`, `mempalace/kg_writethrough.py`, `scripts/backfill_kg_triples.py`, `deploy/systemd/mempalace-kg-extract.service`, `deploy/systemd/kg-extract.env.example`, `docs/kg-extraction.md`, `tests/test_kg_extractor.py`, `tests/test_kg_extraction_queue.py`, `tests/test_kg_triple_worker.py`, `tests/test_backfill_kg_triples.py`


## [2026-05-24]


### Added


- **mempalace stats — palace analytics dashboard (#191)** ([`6f994fb`](https://github.com/techempower-org/mempalace/commit/6f994fb))
  ``mempalace stats`` composes ``mempalace_status`` +
  ``mempalace_kg_stats`` + ``mempalace_graph_stats`` (and optionally
  ``mempalace_list_tags``) into a single read-only view of corpus
  health. Renders wings with proportional bars, KG entity/triple
  counts with a relationship-type preview, graph room/tunnel/edge
  counts with the top cross-wing tunnel rooms, and an opt-in
  ``--tags`` breakdown. Standard ``--json`` / ``--quiet`` / ``--top``
  flags. Daemon-only — refuses to run when ``PALACE_DAEMON_URL`` is
  unset rather than surfacing a stale split-brain view from local
  chromadb. Partial daemon failures (e.g. KG offline) inline the
  error in the affected section instead of blanking the dashboard.

  *Tests:* 13 — test_cli_stats.py
  *Files:* `mempalace/cli.py`, `tests/test_cli_stats.py`


### Changed


- **Promote verbatim-vs-derivative essay from research/ to README (#170)** ([`6a264d9`](https://github.com/techempower-org/mempalace/commit/6a264d9))
  The verbatim-vs-derivative axis essay
  (``docs/research/verbatim-vs-derivative-axis.md``) is the
  standalone treatment of Principle 1. Linked inline from the
  README's "What this is" and "Why this fork exists" sections,
  and called out by name in a new "Sources — Synthesis and
  research" subsection alongside the True Memory comparison,
  benchmark survey, and three-patterns research. Also fixes
  two broken anchors in the essay's Further reading section
  (pointed at the old README locations of "the four layers"
  and "the thesis"; both moved to ``docs/ARCHITECTURE.md`` in
  the README pivot) and refreshes the essay's Last-revised
  date. Closes #170.

  *Files:* `README.md`, `docs/research/verbatim-vs-derivative-axis.md`


## [2026-05-23]


### Added


- **CLI wiring: mempalace mine --source <adapter> (#57)** ([`5ed9fa7`](https://github.com/techempower-org/mempalace/commit/5ed9fa7))
  ``mempalace mine --source <name>`` routes through the adapter's
  ``ingest()`` method. Supports ``--source list`` to enumerate
  installed adapters, ``--dry-run`` for preview via
  ``source_summary()``, wing override, and incremental skip checks.
  Handles ``KeyboardInterrupt`` gracefully with partial-progress
  reporting.

  *Tests:* 13 — test_cli_source.py
  *Files:* `mempalace/cli.py`, `tests/test_cli_source.py`


- **Warp terminal source adapter (#62)** ([`2e85585`](https://github.com/techempower-org/mempalace/commit/2e85585))
  ``WarpSourceAdapter`` ingests both command sessions (grouped by
  ``session_id``) and AI queries from Warp's SQLite database at
  ``~/.local/state/warp-terminal/warp.sqlite``. Commands are
  formatted as terminal transcripts with prompt-like prefixes;
  AI queries are formatted as exchange-pair markdown. Graceful
  degradation if ``ai_queries`` table is absent.

  *Tests:* 27 — test_sources_warp.py
  *Files:* `mempalace/sources/warp.py`, `tests/test_sources_warp.py`, `pyproject.toml`


- **OpenCode adapter smoke test against real DB (#56)** ([`a9ed72b`](https://github.com/techempower-org/mempalace/commit/a9ed72b))
  Nine smoke tests validating the OpenCode adapter against the real
  18MB database (35 sessions, 69 drawers). Verifies shape, content
  format, wing derivation, source_file stability, metadata flatness,
  and session uniqueness. Excluded from default CI via ``@slow`` mark.

  *Tests:* 9 — test_opencode_smoke.py (marked @slow)
  *Files:* `tests/test_opencode_smoke.py`


- **Codex, Gemini, and Aider source adapters (#61, #59)** ([`0c23165`](https://github.com/techempower-org/mempalace/commit/0c23165))
  Three new corpus-origin adapters: ``CodexSourceAdapter`` parses
  Codex CLI JSONL (``session_meta`` + ``event_msg``),
  ``GeminiSourceAdapter`` parses Gemini CLI JSONL
  (``session_metadata`` + ``user``/``gemini``), and
  ``AiderSourceAdapter`` parses Aider markdown chat history
  (``# aider chat started at`` headers + ``####`` user turns).
  All registered as entry points.

  *Tests:* 31 — test_sources_codex.py, test_sources_gemini.py, test_sources_aider.py
  *Files:* `mempalace/sources/codex.py`, `mempalace/sources/gemini.py`, `mempalace/sources/aider.py`, `tests/test_sources_codex.py`, `tests/test_sources_gemini.py`, `tests/test_sources_aider.py`, `pyproject.toml`


- **Filesystem + conversation source adapters (#63)** ([`9a1facf`](https://github.com/techempower-org/mempalace/commit/9a1facf))
  Thin adapter wrappers around ``miner.scan_project()`` and
  ``convo_miner.scan_convos()`` implementing the
  ``BaseSourceAdapter`` interface. ``FilesystemSourceAdapter``
  yields ``DrawerRecord`` per chunk with route hints;
  ``ConversationSourceAdapter`` does the same for conversation
  transcript files.

  *Tests:* 25 — test_sources_filesystem.py, test_sources_conversations.py
  *Files:* `mempalace/sources/filesystem.py`, `mempalace/sources/conversations.py`, `tests/test_sources_filesystem.py`, `tests/test_sources_conversations.py`


### Fixed


- **Widen auto-query signal patterns for natural recall phrases** ([`33e780e`](https://github.com/techempower-org/mempalace/commit/33e780e))
  The ``_EXPLICIT_RE`` pattern in ``auto_query/signals.py`` only
  matched ``remind me`` — missed ``remember``, ``do we have``,
  ``what did we``, etc. Added 6 new patterns covering natural
  recall phrases. Also fixed shell pre-filter ordering bug (turn
  counter was computed after the filter that referenced it) and
  bumped hook timeout from 2000ms to 5000ms.

  *Files:* `mempalace/auto_query/signals.py`


### Performance


- **Native rename_wing backend operation + CLI command (#154)** ([`d045f83`](https://github.com/techempower-org/mempalace/commit/d045f83))
  The ``mempalace_rename_wing`` MCP tool was unusably slow on
  postgres — the inherited ``update()`` re-embedded every document
  via ``upsert()``. Fix: ``PostgresCollection.rename_wing()``
  override with single ``UPDATE SET wing = %s WHERE wing = %s``
  (atomic, milliseconds for 20K+ drawers). Also adds
  ``PostgresCollection.update()`` metadata-only fast path,
  simplifies MCP tool from 40-line loop to 3-line delegation,
  and adds ``mempalace rename-wing`` CLI subcommand with daemon
  routing and ``--dry-run`` support.

  *Tests:* 7 — test_backends.py, test_mcp_server.py, test_cli.py
  *Files:* `mempalace/backends/base.py`, `mempalace/backends/postgres.py`, `mempalace/cli.py`, `mempalace/mcp_server.py`


## [2026-05-22]


### Added


- **Standalone essay: the verbatim-vs-derivative axis (#47)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  ``docs/research/verbatim-vs-derivative-axis.md`` — 3300-word
  essay developing the fork's core architectural claim: store
  verbatim, derive lazily, derivatives are replaceable. Covers
  empirical signal (recovery-collection 210x token gap), vendor
  validation (Anthropic Dreams API), competitive analysis
  (Mem0/Letta/Cognee/Hindsight), and five named limits. Closes #47.

  *Files:* `docs/research/verbatim-vs-derivative-axis.md`


- **Research doc: uncertainty-aware retrieval analysis (#84)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  ``docs/research/uncertainty-aware-retrieval.md`` analyses the
  MIT CSAIL paper *Beyond Binary Rewards* (Damani et al.,
  arXiv:2507.16806) for applicability to mempalace's hybrid
  search stack. The headline technique (RLCR) does not transfer
  directly; two transferable kernels proposed: calibrated
  ``confidence`` field via isotonic regression, and Brier-score
  eval column in the harness. Closes #84.

  *Files:* `docs/research/uncertainty-aware-retrieval.md`


- **Design doc: scope/collection filter on mempalace_search (#76)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  ``docs/designs/scope-collection-filter.md`` evaluates the four
  design alternatives raised in
  techempower-org/mempalace#76 for whether ``mempalace_search``
  should expose a cross-collection filter now that the storage
  layer (postgres + pgvector + chroma) supports
  multi-collection-per-palace as a first-class capability:

  1. **Status quo, document.** ``mempalace_search`` stays
     drawers-only. Cheapest; ratifies the post-PR-#8 reality.
  2. **``collections=`` parameter.** Generalises the retired
     ``kind=`` filter; ranks across collections via RRF.
  3. **Per-collection sibling tools** (``mempalace_search_<X>``).
     Most additive; **echoes the recovery-collection failure mode
     where writes shipped before the read tool did.**
  4. **Federated default.** Single tool reads all collections,
     fused via RRF. Highest implementation cost; calibration
     regressions are silent.

  Recommendation: **Option 1 today, Option 2 the day a second
  MCP-visible collection earns its read surface.** PR #8 retired
  ``mempalace_session_recovery``; this fork now exposes exactly
  one MCP-visible collection (``mempalace_drawers``). Resurrecting
  a cross-collection parameter today is a future-need overshoot.
  Option 2's API delta (``collections: list[str] | None = None``,
  default ``["mempalace_drawers"]``) is forward-compatible and
  stays non-breaking when triggered. Option 3 is deferred
  indefinitely — the recovery-collection split is the case study
  for why writes-before-reads goes wrong.

  Includes a trade-offs matrix across complexity, performance,
  UX, backwards compat, and the
  "echoes a known failure mode" axis; impact analysis on
  palace-daemon ``/search``, SME's
  ``MemPalaceDaemonAdapter``, and the MCP tool surface; explicit
  trigger conditions for revisiting Option 1 → Option 2.

  Closes techempower-org/mempalace#76.

  *Files:* `docs/designs/scope-collection-filter.md`


- **Agent-shaped CLI surface — --json / --quiet for non-MCP integration** ([`25ed900`](https://github.com/techempower-org/mempalace/commit/25ed900))
  ``mempalace status``, ``mempalace search`` and ``mempalace mined``
  now accept ``--json`` / ``-j`` and ``--quiet`` / ``-q`` flags
  (both pre- and post-subcommand). With ``--json`` the command
  emits a JSON document on stdout whose shape mirrors the matching
  MCP tool response (``mempalace_search``, ``mempalace_status``),
  so an agent can switch between MCP and CLI without rewriting
  parsers. With ``--quiet`` decorative chrome (banner lines,
  divider rules, the daemon-routing announcement on stderr) is
  suppressed.

  Auto-detect: when stdout is not a TTY, quiet mode is on by
  default so piped output (``mempalace status | jq …``) stays
  clean. Explicit ``--quiet`` / ``--json`` still override the
  detection.

  Exit codes (per techempower-org/mempalace#44):

  - ``0`` success / at least one result
  - ``1`` no results (search returned empty; mined found nothing)
  - ``2`` palace unavailable (daemon unreachable, palace missing,
    ``chroma.sqlite3`` absent)
  - ``64`` bad args (argparse default)

  This is the substrate for non-Claude-Code agents (opencode,
  codex, gemini-cli, aider) that don't have MCP source-adapters
  yet, plus hooks and slash commands that shell out to the CLI
  from any harness without native MCP support. Composes with the
  multi-agent ecosystem integration tracked in #38.

  Tracks techempower-org/mempalace#44.

  *Tests:* 30 tests in tests/test_cli_json.py — TestResolveQuiet (6),
TestEmitJson (3), TestCmdStatusJson (4), TestCmdSearchJson (5),
TestCmdMinedJson (3), TestQuietSuppressesChrome (1),
TestParserAcceptsFlags (8).

  *Files:* `mempalace/cli.py`, `tests/test_cli_json.py`


- **Design eval: multi-palace separation — curated vs auto-mined (#45)** ([`43547c4`](https://github.com/techempower-org/mempalace/commit/43547c4))
  ``docs/designs/multi-palace-separation.md`` evaluates two
  architectures for @kostadis's curated-vs-auto-mined separation
  raised in upstream discussion #1018: collection partitioning
  inside one palace versus multiple palaces side-by-side. Answers
  the six design questions in issue #45 (palace_path shape,
  per-hook target, search surface, daemon routing, shared
  embedder, CLI surface) on the collection-partition track first,
  with multi-palace as a deferred promotion if the partition
  layer falls short.

  Carries forward the P8 lesson explicitly — the recovery-collection
  split shipped 2026-04-25 and was retired 2026-05-05 because
  the partition was write-side without read-side parity. The
  design rules every named partition must earn a search surface
  from day one; ``searchable: false`` is opt-out, not the default.

  Coordinates with #76 (scope/collection filter on search) — the
  ``partition`` / ``partitions`` parameter shape proposed here
  is the same surface #76 needs, picking one name avoids drift
  between two parallel designs.

  Design, not implementation. No runtime change. Tracks
  techempower-org/mempalace#45.

  *Files:* `docs/designs/multi-palace-separation.md`


- **Document .sh shim delegation to palace-daemon (counter-position to upstream #1069)** ([`bf0a4d0`](https://github.com/techempower-org/mempalace/commit/bf0a4d0))
  Upstream MemPalace/mempalace#1069 wants the per-event hook
  ``.sh`` wrappers consolidated into shims delegating to
  ``mempalace hook run`` — i.e. all hook logic lives inside the
  ``mempalace`` Python CLI. This fork went the opposite
  direction post-2026-05-11: the shims delegate to
  ``palace-daemon/clients/hook.py`` (a stdlib-only Python
  script in a sibling repo) which speaks HTTP to a single
  FastAPI gateway. ``mempalace`` itself is no longer in the
  hook call path.

  ``docs/fork-decisions/sh-shim-strategy.md`` captures the
  rationale (back-compat with stale Claude Code sessions,
  operational simplicity, graceful absence when palace-daemon
  isn't installed), the delegation diagram, the shim
  template, and the conditions under which we'd re-converge
  with upstream #1069.

  ``scripts/mempalace-search.sh`` is a non-hook sample of the
  same delegation pattern — HTTP GET to ``/search`` against
  the daemon — included as a copy-paste template for
  contributors adding new delegating shims.

  No code or runtime change: this entry documents existing
  shims (``.claude-plugin/hooks/*.sh``,
  ``.codex-plugin/hooks/*.sh``) that have been delegating to
  palace-daemon since the 2026-05-11 split-brain fix.

  Tracks techempower-org/mempalace#69.

  *Files:* `docs/fork-decisions/sh-shim-strategy.md`, `scripts/mempalace-search.sh`


- **Honor ~/.mempalace/RETIRED marker — refuse default palace, surface retire message** ([`798cf14`](https://github.com/techempower-org/mempalace/commit/798cf14))
  Recurring confusion source on this fork: any code path that opens
  mempalace without ``PALACE_DAEMON_URL`` set silently falls
  through to the default chroma palace at ``~/.mempalace/palace``
  and reports a smaller, stale drawer count. Today an opencode
  agent ran the CLI directly and saw 24,920 drawers, then noticed
  the daemon said 310,031 and got confused trying to reconcile —
  the local palace was a retired pre-pgvector-cutover artifact
  with broken HNSW.

  When a user has retired their local palace in favor of a
  daemon-routed setup, they can drop a text file at
  ``~/.mempalace/RETIRED`` explaining the situation. Three layers
  honor it:

  - ``mempalace/mcp_server.py:_check_local_palace_retired`` —
    ``_get_collection_chroma`` calls this at entry. If the marker
    exists AND the configured palace path resolves to the
    default, the open is refused; ``_no_palace`` returns a new
    error type ``palace.local_retired`` with the marker contents
    as the message. ``MEMPALACE_ALLOW_RETIRED_PALACE=1`` is the
    escape hatch for forensic reads of the archived palace.
  - ``mempalace/palace.py:_open_collection_or_explain`` — the
    four CLI-facing "No palace found" emit sites route through a
    new inner ``_emit_palace_missing()`` helper that checks the
    marker first.
  - ``mempalace/cli.py:_print_retired_local_palace_or_default`` —
    four ``cmd_*`` paths that printed
    ``"No palace found at ... Run: mempalace init <dir>"``
    directly now route through the helper.

  Tests: ``tests/test_local_palace_retired.py`` (4 cases —
  default+marker triggers retired text; no-marker falls through
  to init hint; explicit ``--palace`` ignored; escape-hatch env
  var bypasses).

  *Files:* `mempalace/mcp_server.py`, `mempalace/palace.py`, `mempalace/cli.py`, `tests/test_local_palace_retired.py`


### Fixed


- **Empty repo .opencode/opencode.json mcp block — disabled flag wasn't being respected** ([`7133eee`](https://github.com/techempower-org/mempalace/commit/7133eee))
  ``.opencode/opencode.json`` from upstream PR #1567 spawns a
  local ``mempalace.mcp_server`` when opencode launches in the
  repo root. On daemon-routed setups this creates **two MCP
  servers exposing different palaces** — the user-level wrapper
  hitting the daemon, and the repo-level spawn hitting the
  local store. PR #108 attempted ``enabled: false`` but opencode
  merges entries by name across configs and the repo
  ``command`` overrode the user-level one anyway.

  This PR strips the ``mcp`` block from the repo config
  entirely. User-level wrapper becomes the sole source of truth.
  Contributors who don't have a daemon set up can add their own
  user-level config or a ``.opencode/opencode.local.json``
  (gitignored).

  *Files:* `.opencode/opencode.json`


- **Drop \$comment from .opencode/opencode.json — schema rejects unknown root keys** ([`637bb01`](https://github.com/techempower-org/mempalace/commit/637bb01))
  Commit ``47018e5`` (PR #108) added a ``\$comment`` field to
  ``.opencode/opencode.json`` explaining why the repo-level MCP
  entry defaulted to ``enabled: false``. Opencode's strict
  schema validator rejected the unknown root key with
  ``ConfigInvalidError`` and refused to start the TUI:

      Error: 4 of 5 requests failed: Unexpected server error.
      Affected startup requests: config.providers, provider.list,
      app.agents, config.get

  JSON parses (``\$comment`` is a popular convention,
  JSON-Schema recognizes it), but opencode doesn't allow it.
  Removing the field unblocks startup; explanations move to
  commit messages + PR descriptions.

  *Files:* `.opencode/opencode.json`


- **Disable repo-level MCP entry by default + venv-python fallback** ([`47018e5`](https://github.com/techempower-org/mempalace/commit/47018e5))
  First attempt at the daemon-routed-vs-local-spawn double-server
  problem from upstream PR #1567's ``.opencode/opencode.json``.
  Set ``enabled: false`` on the repo entry; switched the spawn
  command from bare ``python -m mempalace.mcp_server`` to
  ``.venv/bin/python -m mempalace.mcp_server`` so a contributor
  flipping ``enabled`` back to true would actually find mempalace
  installed (uv-managed clones have it in the venv, not on PATH).

  Superseded same-day by ``opencode-repo-config-empty-mcp``
  (#110) — opencode's config merger doesn't actually honor
  ``enabled: false`` for command overrides, so the entry had to
  be stripped entirely.

  *Files:* `.opencode/opencode.json`


## [2026-05-21]


### Added


- **Bundled OpenCode live-capture plugin that bypasses option-K v1.2.1 bugs (filed upstream as #4, #5)** ([`5522623`](https://github.com/techempower-org/mempalace/commit/5522623))
  Adds `examples/opencode/live-capture/` — a self-contained
  OpenCode plugin (JS) + Python helper that POSTs verbatim
  session transcripts to the daemon's `/silent-save` endpoint on
  every `session.idle`.

  Background: while documenting the integration recipe
  (`opencode-integration-recipe` below), end-to-end testing
  revealed that the upstream option-K
  [`opencode-plugin-mempalace`](https://www.npmjs.com/package/opencode-plugin-mempalace)
  v1.2.1 cannot actually push drawers in a daemon-routed setup —
  two compounding bugs, both filed upstream:

  - [option-K#4](https://github.com/option-K/opencode-plugin-mempalace/issues/4):
    The plugin subscribes to `chat.message`, which OpenCode never
    publishes. The message counter never increments, `session.idle`
    sees `hasPendingMessages() === false`, and **the plugin never
    mines a drawer**. Verified by inspecting bus types in
    `~/.local/share/opencode/log/*.log` against the canonical event
    taxonomy at [opencode.ai/docs/plugins](https://opencode.ai/docs/plugins).
  - [option-K#5](https://github.com/option-K/opencode-plugin-mempalace/issues/5):
    Even with #4 patched, `mineSync` calls `mempalace mine <dir>`,
    which the *remote* daemon evaluates against ITS OWN filesystem.
    For multi-host setups (palace-daemon on a different machine
    from OpenCode), the daemon returns 400 because the local
    path doesn't exist on its filesystem.

  The bundled plugin sidesteps both by:

  1. Subscribing to `session.idle` / `session.deleted` /
     `session.status[idle]` directly (no message counter).
  2. Reading OpenCode's local SQLite session DB client-side.
  3. POSTing the extracted transcript to the daemon's
     `/silent-save` endpoint (the same endpoint MemPalace's
     Claude Code stop hook uses).

  The Python helper imports `_extract_session_messages` and
  `_session_transcript` from `mempalace/sources/opencode.py` so
  the transcript shape matches `OpenCodeSourceAdapter` exactly.

  Also splits the previously combined option-K patch into two
  independently-applicable files:

  - `examples/opencode/option-k-plugin-daemon-routing.patch` —
    Fix 1 (option-K#1, `isInitialized()` daemon detection).
  - `examples/opencode/option-k-plugin-message-updated.patch` —
    Fix 2 (option-K#4, `chat.message` → `message.updated`).

  `docs/integrations/opencode.md` now documents both deployment
  options (bundled plugin for remote-daemon, option-K plugin +
  patches for local palaces).

  *Files:* `examples/opencode/live-capture/mempalace-live-capture.js`, `examples/opencode/live-capture/capture-session.py`, `examples/opencode/option-k-plugin-daemon-routing.patch`, `examples/opencode/option-k-plugin-message-updated.patch`, `docs/integrations/opencode.md`


- **Documented OpenCode integration recipe (read-side MCP + push plugin + retrospective adapter)** ([`60dc9e6`](https://github.com/techempower-org/mempalace/commit/60dc9e6))
  Adds `docs/integrations/opencode.md` and an `examples/opencode/`
  directory capturing the three-direction OpenCode + MemPalace
  integration recipe for daemon-routed setups:

  - **Read** — `~/.config/opencode/opencode.jsonc` MCP entry pointing
    at `palace-daemon/clients/mempalace-mcp-wrapper.sh` (sources
    `~/.config/palace-daemon/env` so the API key never lands in
    plaintext config).
  - **Push (live)** — option-K's `opencode-plugin-mempalace` npm
    package (project-basename wings, default 15-message threshold,
    session.idle flush, SIGINT/SIGTERM rescue, pre-compaction
    injection — closest match to MemPalace's Claude Code stop-hook
    pattern).
  - **Pull (retrospective)** — the cherry-picked
    `OpenCodeSourceAdapter` from upstream PR #1484, run via
    `mempalace mine --source opencode` for one-shot backfill of
    historical sessions.

  Includes a re-applicable patch
  (`examples/opencode/option-k-plugin-daemon-routing.patch`) for
  option-K's plugin v1.2.1 issue #1, where `isInitialized()` passes
  `--palace <local-dir>/.mempalace/palace` to `mempalace status`,
  forcing a local-store lookup that bypasses `PALACE_DAEMON_URL`
  routing. Without the patch the plugin re-runs `mempalace init
  --yes <dir>` on every OpenCode start (idempotent against the
  daemon, just wasteful); with the patch init+isInitialized
  short-circuit when daemon-routed mode is detected.

  Why the recipe lives here rather than upstream: this fork's
  single-writer-via-palace-daemon shape doesn't match upstream's
  assumed local-CLI install pattern (Milofax #297, geco #1524, and
  Dxrk #1567 all assume a local mempalace install). Upstream PRs
  will eventually subsume parts of this — when they do, the YAML
  entries become removable; for now the recipe captures what
  actually works on a daemon-routed box.

  *Files:* `docs/integrations/opencode.md`, `examples/opencode/opencode.jsonc.example`, `examples/opencode/option-k-plugin-daemon-routing.patch`


- **.opencode/opencode.json — repo-root MCP config so opencode picks up mempalace automatically** ([`ba16b82`](https://github.com/techempower-org/mempalace/commit/ba16b82))
  Cherry-pick of upstream PR #1567 (Dxrk777). Adds
  `.opencode/opencode.json` so that running `opencode` in the
  mempalace repo root automatically wires `mempalace` as a local
  MCP server — useful for contributors who use OpenCode for
  development on the project itself.

  Two-commit cherry-pick:

  - `013ac63` — initial config with `command: ["mempalace-mcp"]`
  - `ba16b82` — gemini-code-assist review feedback: switch to
    `command: ["python", "-m", "mempalace.mcp_server"]` for
    portability across install methods (pip vs uv vs dev install)

  This is the **dev/contributor surface** — it lives in the repo
  and only matters when running OpenCode against the repo root.
  Per-user setups should use `~/.config/opencode/opencode.jsonc`
  with the daemon-aware wrapper (see `docs/integrations/opencode.md`).

  *Upstream:* [PR #1567](https://github.com/MemPalace/mempalace/pull/1567) (CLOSED)
  *Files:* `.opencode/opencode.json`


- **OpenCodeSourceAdapter (RFC 002) — retrospective ingest of OpenCode SQLite sessions** ([`2ffe652`](https://github.com/techempower-org/mempalace/commit/2ffe652))
  Cherry-pick of upstream PR #1484. Adds
  `mempalace/sources/opencode.py` — an RFC 002 `BaseSourceAdapter`
  that ingests OpenCode AI-coding-CLI session transcripts from
  `~/.local/share/opencode/opencode.db` into the palace, formatted
  to match `convo_miner`'s exchange-pair drawer shape.

  Five-commit cherry-pick:

  - `2c368c6` — initial adapter (482-line `opencode.py`, 6
    opencode-namespaced reference transformations in
    `transforms.py`, entry-point registration, 28 tests, sample
    SQLite-schema-verbatim fixture)
  - `3ff7043` — gemini-code-assist review fixes: missing
    `opencode_session_version` in metadata (broke incremental
    `is_current`), `_skip_requested` private access, `filed_at`
    hoisted out of chunk loop, PEP 8 import position
  - `9531532` — igorls review fixes: ruff F401/E402 in tests,
    route-hint wing/drawer-stage precedence mismatch (RFC 002 §2.5),
    unjustified `# noqa` cleanup
  - `18ab021` + `2ffe652` — CI ruff 0.4.x format passes

  Adapter conformance via RFC 002 §7.3 declared-transformation
  round-trip; one drawer per exchange-pair; `source_file` shape
  `opencode://<absolute-db-path>#session=<sid>`; wing routes from
  `session.directory` basename (matching the live-capture plugin's
  taxonomy); incremental ingest works via `opencode_session_version`.

  Originated from JakobSachs's spadework on upstream PR #23 (DB
  schema reverse engineering, session/message/part traversal,
  tool-input/tool-output stripping). PR #23 is still OPEN but
  CONFLICTING and unresponsive since 2026-04-08; #1484 carries
  `Co-authored-by: JakobSachs` per coordination on #23.

  *Tests:* 28 OpenCode adapter tests pass; full suite 2133 passed / 33 skipped (zero regressions on fork main + 60-commit upstream sync baseline)
  *Upstream:* [PR #1484](https://github.com/MemPalace/mempalace/pull/1484) (OPEN)
  *Files:* `mempalace/sources/opencode.py`, `mempalace/sources/transforms.py`, `mempalace/sources/context.py`, `pyproject.toml`, `tests/test_sources_opencode.py`, `tests/fixtures/opencode/sample_session_2026_05_12/README.md`, `tests/fixtures/opencode/sample_session_2026_05_12/build_fixture.py`, `tests/test_corpus_origin_integration.py`


- **Pending-writes journal + replay so daemon outages stop being silent** ([`0c34464`](https://github.com/techempower-org/mempalace/commit/0c34464))
  Closes a silent-data-loss gap exposed by the 2026-05-17 power
  event: when ``PALACE_DAEMON_URL`` is set and the daemon's
  backend is unreachable, the hook write path (``_post_daemon_mine``)
  logged the failure and dropped the request — no local fallback,
  no retry, no user-visible signal. Hooks fell silent for ~3 days
  before noticed.

  New ``mempalace/pending_queue.py`` appends each dropped request
  to ``~/.mempalace/pending/YYYY-MM-DD.jsonl`` (fsynced, atomic).
  ``mempalace replay`` and the session-start hook drain the queue
  by re-issuing requests to the daemon once it recovers, with
  ``(dir, wing, mode)`` dedup so a long outage doesn't replay the
  same target dozens of times.

  Session-start hook also emits a one-line ``systemMessage``
  warning when the daemon ``/health`` is non-OK or the queue has
  pending entries — throttled to once per ``session_id``.

  *Tests:* tests/test_pending_queue.py (16 tests: enqueue, count, replay,
dedup, atomic rewrite, CLI integration, partial-failure exit code).
tests/test_hooks_cli.py: added 3 session-start cases + extended
``test_post_daemon_mine_returns_false_on_error`` to assert the
enqueue side effect.

  *Files:* `mempalace/pending_queue.py`, `mempalace/hooks_cli.py`, `mempalace/cli.py`, `tests/test_pending_queue.py`, `tests/test_hooks_cli.py`


### Fixed


- **Stub resources/list + prompts/list so MCP clients stop ERROR-logging on connect** ([`6ca0670`](https://github.com/techempower-org/mempalace/commit/6ca0670))
  OpenCode 1.15.x and other MCP clients probe ``resources/list``
  and ``prompts/list`` on connect to discover server
  capabilities. mempalace's MCP server only exposes ``tools/*``;
  these probes returned ``-32601: Unknown method``, which
  clients log as ERROR every session. Two log-noise lines per
  session, surfacing as scary text in opencode's TUI.

  Both methods are optional per the MCP spec. Return empty
  lists instead of the error, matching what other MCP servers
  without resources/prompts do (e.g. server-everything, the
  filesystem reference server).

  *Tests:* tests/test_mcp_server.py — two new TestProtocol cases asserting
empty-list shape and absence of ERROR.

  *Files:* `mempalace/mcp_server.py`, `tests/test_mcp_server.py`


- **MCP server distinguishes 'backend unreachable' from 'no palace found'** ([`0c34464`](https://github.com/techempower-org/mempalace/commit/0c34464))
  The CLI's misleading ``Run: mempalace init <dir>`` hint was
  the diagnostic blocker during the 2026-05-17 outage: the daemon
  was up, postgres was down, and every search returned "No palace
  found" — pointing JP at a re-init that wasn't the problem.

  ``mempalace/mcp_server.py:_get_collection_postgres`` now records
  the last connection error in a module-level slot, and
  ``_no_palace()`` reads it to return one of three responses:

    * ``palace.backend_unreachable`` — for psycopg2 OperationalError
      (the actual power-event failure), with hint
      ``Check: docker ps mempalace-db``.
    * ``palace.backend_error`` — for any other backend exception,
      with hint to check ``journalctl -u palace-daemon``.
    * ``"No palace found"`` — only when there's no recent error
      (the legitimate uninitialised case).

  Backend cache is also dropped on failure so the daemon's pool
  reconnects cleanly when postgres comes back, without needing a
  daemon restart.

  *Tests:* tests/test_mcp_server_backend_unreachable.py (5 tests: default,
OperationalError mapping, generic error mapping, error capture
on failure, error cleared on successful reopen).

  *Files:* `mempalace/mcp_server.py`, `mempalace/cli.py`, `tests/test_mcp_server_backend_unreachable.py`


## [2026-05-17]


### Added


- **mempalace_walk_palace MCP tool — agent walks the palace via AGE Cypher** ([`8022ecb`](https://github.com/techempower-org/mempalace/commit/8022ecb))
  Phase 6 of the AGE-integration plan. Exposes the "agent walks into
  the palace finding wings, rooms, drawers" metaphor as a single MCP
  tool over the unified palace+entity graph (Wing → Room → Drawer →
  MENTIONS → Entity) built across Phases 1-4 in this branch.

  Three traversal modes via mutually-exclusive anchors:
  - `start_wing="memorypalace"` — walks down the hierarchy: rooms
    (d=1), drawers (d=2), entities (d=3)
  - `start_room="problems"` — drawers across all wings (d=1), then
    entities (d=2)
  - `start_entity="pgvector"` — inverse walk: drawers mentioning it
    (d=1), then the rooms+wings containing them (d=2)

  Result envelope: `{start, depth, walk: [{wing, room, drawer, entity}],
  stats: {wings_touched, rooms_touched, drawers_touched, entities_touched}}`.

  Smoke-tested on `sme_lme_bench`: `walk_palace(start_entity='pgvector',
  depth=2)` returns the 3 drawers mentioning it plus their containing
  rooms+wings; `walk_palace(start_room='postgres', depth=2)` returns
  the postgres.py drawer plus its 3 mentioned entities.

  Requires `MEMPALACE_BACKEND=postgres` and AGE graph populated via
  `kg_writethrough` (Phase 2) or `backfill_age` (Phase 4).

  *Files:* `mempalace/mcp_server.py`


- **Backfill AGE graph from existing drawer table — restartable, checkpointed** ([`b3f0206`](https://github.com/techempower-org/mempalace/commit/b3f0206))
  Phase 4 of the AGE-integration plan. New module
  `mempalace/backfill_age.py` with CLI entry point that reads the
  drawer table once and builds the full Wing/Room/Drawer/MENTIONS
  graph in AGE. Companion to `migrate_to_postgres` — that script
  copies chroma → postgres, this one copies postgres-drawers →
  postgres-AGE.

  Design:
  - Restartable via `mempalace_kg_backfill_state` checkpoint table
    (phase, key) — re-running skips already-processed (wing, room)
    or drawer keys.
  - Idempotent via MERGE on identity columns; safe to re-run.
  - Bounded memory via named server-side cursor — never loads the
    full drawer table into memory.
  - Configurable scope: `--wing memorypalace` for one wing,
    `--skip-palace` to add only entity edges to existing structure,
    `--skip-entities` for fast "high-level palace map" first pass.

  Companion `add_mention(drawer_id, entity_name)` method on
  `KnowledgeGraphAGE` for the (Drawer)-[:MENTIONS]->(Entity) edge
  pattern. CREATE-ALWAYS edge semantics (no upsert) — matches the
  SQLite KG triples-table behavior. AGE 1.6.0 doesn't support `SET`
  on edge properties or `coalesce` in SET, so callers that want
  idempotency track state externally (backfill checkpoint table
  does this).

  Tested on `sme_lme_bench` (1181 docs wing chunks → 6015 entities
  + 13721 MENTIONS edges in 5.85 min). Production palace projection:
  ~22 hours for 274K drawers — overnight job.

  *Files:* `mempalace/backfill_age.py`, `mempalace/knowledge_graph_age.py`


- **Wing/Room/Drawer hierarchy as native AGE nodes; Cypher MATCH walks palace structure** ([`ff583c0`](https://github.com/techempower-org/mempalace/commit/ff583c0))
  Phase 3 of the AGE-integration plan. Mirrors `mempalace.palace_graph`'s
  SQL-aggregation pattern into AGE so Cypher MATCH walks the palace
  structure natively — no SQL aggregation per query.

  New module `mempalace/palace_graph_age.py`:
  - `populate_from_postgres(kg, dsn, table_name, skip_drawers,
    skip_tunnels)` — reads drawer table, builds
    Wing/Room/Drawer/SHARED_VIA in AGE. Idempotent via MERGE.
    Three-pass design so `skip_drawers` gives a fast high-level
    palace map without per-drawer cost on huge palaces.
  - `walk_wing(kg, wing, depth)` — structured walk primitive
    returning `[{wing, room, drawer, entity}]` rows.
  - `list_wings`, `list_rooms_in_wing`, `list_drawers_in_room`,
    `tunnels_from_wing` — read-side helpers ready for MCP-tool
    wiring (Phase 6).

  Schema:
  ```
  Wing  -[:CONTAINS]->  Room  -[:CONTAINS]->  Drawer  -[:MENTIONS]->  Entity
  Wing  -[:SHARED_VIA {via_room}]-  Wing      (tunnels)
  ```

  The MENTIONS edges connect structural location (Phase 3) to the
  kg_writethrough layer (Phase 2) into one unified graph an agent
  can navigate. AGE Cypher dialect respected: no edge-type union
  `[:A|B]` (AGE 1.6.0 errors), so `walk_wing(depth=3)` uses
  `[:RELATION]` with a property filter instead.

  Smoke-tested on `sme_lme_bench`: 5344 chunks across 2 wings
  (code/docs) → 237 rooms, 238 CONTAINS edges, 1 SHARED_VIA tunnel
  via the `cli` room (appears in both wings).

  *Files:* `mempalace/palace_graph_age.py`


- **Write-through middleware on PostgresCollection — entities populate AGE on every drawer write** ([`3321d83`](https://github.com/techempower-org/mempalace/commit/3321d83))
  Phase 2 of the AGE-integration plan. Adds a write-through hook on
  `PostgresCollection.add`/`upsert` that extracts entities from the
  document and creates `(Drawer)-[:MENTIONS]->(Entity)` edges in
  AGE. Means the KG is populated as the palace is filled, not as a
  separate offline pass.

  Plumbing:
  - `PostgresCollection._insert_rows` — after the row commits, calls
    `self._kg_writethrough(drawer_id, document, metadata)` if
    registered. Hook errors caught + logged, never raised — KG
    enrichment is opportunistic, never blocks writes.
  - `PostgresCollection.set_kg_writethrough(hook)` — registration
    API. Default (no hook) is zero overhead — vector-only behavior
    byte-identical to pre-Phase-2.

  New module `mempalace/kg_writethrough.py`:
  - `make_age_writethrough(kg, extractor)` — canonical hook factory.
    Caps at `max_entities_per_drawer` (default 100) so per-drawer
    write latency stays bounded.
  - `make_null_writethrough()` — no-op for tests / disabling.
  - `make_writethrough_from_env()` — env-var-driven config:
    `MEMPALACE_KG_WRITETHROUGH=1` + `MEMPALACE_KG_EXTRACTOR=regex|null`.
  - `_builtin_regex_extractor` — fallback when SME's extractor isn't
    importable. Captures capitalized proper nouns, hyphenated
    identifiers, version strings.

  Extractor is pluggable: any callable matching `(text) -> list[Entity]`
  where Entity has `.name` works. Tested with SME's two-pass regex
  extractor; spaCy and LLM extractors are next on the swap-in list.

  Smoke test: fresh AGE graph + `coll.upsert(2 drawers about
  Atakan/FT-300/AGE/mempalace-Phase-2)` → 10 entities, 8 MENTIONS
  edges, all current.

  *Files:* `mempalace/backends/postgres.py`, `mempalace/kg_writethrough.py`


- **KnowledgeGraphAGE API parity with SQLite KG: add_entity, invalidate, query_entity, query_relationship, timeline, seed_from_entity_facts** ([`ff7187d`](https://github.com/techempower-org/mempalace/commit/ff7187d))
  Phase 1 of the AGE-integration plan. Brings `KnowledgeGraphAGE` to
  API parity with `mempalace.knowledge_graph.KnowledgeGraph` (the
  SQLite backend). Previously only `add_triple`, `query_triples`,
  `stats`, `clear` were implemented; the 5 missing methods make AGE
  a drop-in replacement for SQLite without requiring callsite
  changes.

  Methods added (all mirror SQLite semantics):
  - `add_entity(name, entity_type, properties)` — MERGE pattern;
    last-write-wins on type/properties since AGE 1.6.0 has no `ON
    CREATE SET`.
  - `invalidate(subject, predicate, object_, ended)` — SET valid_to
    on every active matching triple; inverted-interval guard reads
    existing valid_from first and rejects if ended < valid_from.
  - `query_entity(name, as_of, direction)` —
    outgoing/incoming/both direction filter + as_of temporal filter.
  - `query_relationship(predicate, as_of)` — filter triples by
    relation_type, optional temporal filter.
  - `timeline(entity_name, limit)` — chronological ORDER BY with
    default limit 100.
  - `seed_from_entity_facts(entity_facts)` — bulk-load from
    ENTITY_FACTS dict shape used by `fact_checker.py`.
  - `_entity_id(name)` — id derivation helper matching SQLite KG.

  AGE Cypher dialect gaps documented + worked around:
  - No `ON CREATE SET` → unconditional `SET` on MERGE.
  - No multi-column `RETURN` with AS aliases inside dollar-quoted
    `cypher()` — wired the existing `_run_cypher` alias-parsing path
    to handle all 6 methods cleanly.
  - No list literals (`RETURN [a, b]`) — workaround not needed for
    these methods, but documented for downstream callers.

  Smoke-tested end-to-end: 3 triples (`Atakan -[works_on]-> adaptmem`,
  `Atakan -[works_on]-> mempalace-PRs`, `FT-300 -[trained_by]->
  Atakan`); query_entity outgoing → 2 results; query_entity incoming
  → 1; invalidate(`mempalace-PRs`) → 1 affected, re-query shows
  `valid_to=2026-05-17, current=False`; timeline returns 3 rows
  ordered by valid_from; stats: entities=4, triples=3,
  current_facts=2, expired_facts=1.

  *Files:* `mempalace/knowledge_graph_age.py`


## [2026-05-11]


### Added


- **KnowledgeGraphAGE skeleton — Apache AGE graph bootstrap over psycopg2** ([`a3ee623`](https://github.com/techempower-org/mempalace/commit/a3ee623))
  First commit toward the Apache AGE-backed knowledge graph layer
  that the migration plan calls for. Skeleton class
  `KnowledgeGraphAGE` in `mempalace/knowledge_graph_age.py` opens a
  Postgres connection, loads the AGE extension, sets
  `search_path = ag_catalog, "$user", public` for the session, and
  creates a graph named `mempalace_kg` in `ag_catalog.ag_graph` if
  absent. Idempotent bootstrap; safe to instantiate repeatedly.

  Composes with the pgvector substrate already on main: same
  `apache/age:release_PG16_1.6.0` + `postgresql-16-pgvector`
  derived image; same `mempalace-db` container on disks; same
  psycopg2-binary dependency from the `[postgres]` extra. No new
  driver surface — keeps the dep tree clean.

  Selectable via `MEMPALACE_KG_BACKEND=age` once the
  config-routing layer lands in a follow-up commit; until then,
  `mempalace.knowledge_graph.KnowledgeGraph` (SQLite) stays the
  default and only path. The AGE class mirrors the SQLite KG's
  public interface (constructor + close + context manager) so
  callers can eventually swap backends without code changes.

  Three pytest.skipif-gated tests in
  `tests/test_knowledge_graph_age.py`:
  - `test_age_kg_instantiates` — class constructs cleanly,
    closes without exception.
  - `test_age_graph_created` — `mempalace_kg` is registered in
    `ag_catalog.ag_graph` with a non-null `graphid` after init.
  - `test_age_context_manager` — `with KnowledgeGraphAGE(...) as
    kg:` pattern closes the connection on exit (verifies
    `_conn.closed` is True after).

  Implementation notes:
  - `autocommit=False` matches the SQLite KG's transaction
    semantics so the eventual unified write API can swap
    underneath without semantic surprise. The bootstrap commits
    its own changes; subsequent write operations will control
    their own transactions.
  - Both `LOAD 'age'` and the `SET search_path` are
    session-scoped — any future method taking a fresh cursor on
    this connection must re-run them before issuing Cypher.

  Future commits in this layer: `add_triple()` via Cypher
  MERGE/CREATE, query operations, temporal filtering (`as_of`
  queries), and the `MempalaceConfig.kg_backend` routing flag.

  *Tests:* 1854 passed, 1 skipped, 106 deselected (with `TEST_POSTGRES_DSN`
set against the homelab mempalace-db at disks.jphe.in:5433).
+3 vs the post-sync 1851 baseline; zero regressions.

  *Files:* `mempalace/knowledge_graph_age.py`, `tests/test_knowledge_graph_age.py`


- **CI: gate postgres-backend tests against a pgvector service container** ([`da0bdbb`](https://github.com/techempower-org/mempalace/commit/da0bdbb))
  Adds a `test-postgres` job to `.github/workflows/ci.yml` that
  runs in parallel with the existing `test-linux` / `test-windows`
  / `test-macos` / `lint` matrix. Service container is the public
  `pgvector/pgvector:pg16` image with health checks; a pre-test
  Python step installs the `vector` extension via psycopg2 (no
  `psql` install on the runner needed).

  Test scope is `tests/test_backends_postgres.py` only — three
  `pytest.skipif`-gated tests for backend registration, drawer
  round-trip, and L2 vector distance ordering. The full pytest
  suite is already exercised by `test-linux` without the postgres
  extra; running it again with `TEST_POSTGRES_DSN` set would
  double the suite time on every PR for the marginal coverage of
  three additional tests. The targeted job gives the regression
  signal we want — postgres backend works end-to-end against a
  real database — without that cost.

  AGE is deliberately not in the CI image. The `apache/age` +
  pgvector combined image we deploy on the homelab `mempalace-db`
  isn't needed in CI yet — no test in the repo exercises
  AGE-specific behavior. When knowledge-graph layer tests land,
  the CI image swap (push our derived image to ghcr, or build
  inline) is a separate concern.

  Job timing on first run: 52 seconds total including service
  container startup, pip install of `.[dev,postgres]`, extension
  create, and the 3 smoke tests.

  *Files:* `.github/workflows/ci.yml`


- **PostgreSQL backend via #665 cherry-pick + fork-side adaptations + smoke tests** ([`5e90c72`](https://github.com/techempower-org/mempalace/commit/5e90c72))
  Cherry-pick of skuznetsov's upstream PR
  [#665](https://github.com/MemPalace/mempalace/pull/665) — adds a
  PostgreSQL backend built on the merged #995 / RFC 001
  `BaseBackend` contract. Supports `pg_sorted_heap` when the
  extension is installed; falls back to `pgvector` (the path this
  fork actually runs). INSERT … SELECT FROM unnest() + ON CONFLICT
  for batch writes; lazy vector index creation after a row-count
  threshold; first-class `wing` / `room` columns with btree
  indexes; metadata as `jsonb` with `$eq` / `$ne` / `$in` / `$nin`
  / `$and` / `$or` filter translation. Optional install via
  `pip install -e ".[postgres]"` — only adds `psycopg2-binary`,
  no new ML dependency stack.

  Composition stance is WAIT-for-#665 to merge upstream rather
  than fork-port. Full rationale at
  `docs/internal/pgvector-665-decision.md` (commit `fbd8dbd`):
  conflict surface is moderate (~51 LOC across `palace.py` +
  `tests/test_backends.py` + trivial README/uv.lock); #665 is
  comprehensive and architecturally aligned; the `pg_sorted_heap`
  codepath is gated by extension availability so our deployment
  runs the pgvector fallback cleanly. Documented Plan-B trigger:
  switch to fork-port path if no #665 maintainer activity past
  2026-06-08.

  Four fork-side adaptations rode along with the cherry-pick:

  - **`palace.py` compat shim** (`5e90c72`). `_DEFAULT_BACKEND`
    re-aliased to `get_backend("chroma")` so existing
    `mcp_server.py` cache-clearing on `._clients` / `._freshness`
    and the `palace.close_palace` call site keep working without
    callers migrating to the new abstraction. Migration of those
    five call sites is a follow-up commit; the shim is
    transitional, not permanent.

  - **`palace.get_collection()` accepts None for collection_name**
    (`5c7f234`). Upstream #665 tightened the contract from
    `Optional[str] = None` to `str = DEFAULT_COLLECTION_NAME` and
    resolved the default only when the literal sentinel was
    passed. Fork-side callers
    (`searcher.search_memories`, `convo_miner`, `sweeper`,
    `diary_ingest`, etc.) pass `collection_name=None` per the
    pre-#665 fork convention; the tight contract propagated None
    to chromadb and produced 30 test_searcher failures. Accepting
    both forms (None and `DEFAULT_COLLECTION_NAME`) restores the
    green floor without disturbing #665's structure.

  - **`test_palace_get_collection_uses_configured_collection_name`
    signature update** (`941342b`). `fake_get_collection` now
    accepts `palace=PalaceRef` and `options=` kwargs;
    monkeypatches via `MEMPALACE_COLLECTION_NAME` env var rather
    than the legacy `get_configured_collection_name` function
    (which is now a thin back-compat wrapper around
    `MempalaceConfig().collection_name`).

  - **Smoke tests** (`04c6294`). Three `pytest.skipif`-gated
    integration tests in `tests/test_backends_postgres.py` —
    backend registration as singleton, drawer add/get round-trip,
    L2 distance ordering. Documented in
    `docs/internal/pgvector-665-decision.md` as the contract
    proxy for "the backend is end-to-end working." Activated by
    setting `TEST_POSTGRES_DSN`; skipped by default so machines
    without postgres still see a green floor.

  Substrate stood up on the homelab: `mempalace-db` container at
  `disks.jphe.in:5433` (LAN-bound, internal-only) running PG16 +
  pgvector 0.8.2 + AGE 1.6.0 via `apache/age:release_PG16_1.6.0` +
  apt-installed `postgresql-16-pgvector`. Password in Vaultwarden
  as `mempalace-db-postgres`. Init via `init.sql` mounted at
  `/docker-entrypoint-initdb.d/`. Build context at
  `/opt/mediaserver/mempalace-db/`.

  *Tests:* 1851 passed, 1 skipped, 106 deselected (with `TEST_POSTGRES_DSN`
set). +23 vs the pre-cherry-pick 1828 baseline — 20 from #665's
new postgres backend tests, 3 from the new smoke file. Zero
regressions in non-postgres paths.

  *Upstream:* [PR #665](https://github.com/MemPalace/mempalace/pull/665) (OPEN)
  *Files:* `mempalace/backends/postgres.py`, `mempalace/backends/__init__.py`, `mempalace/backends/registry.py`, `mempalace/palace.py`, `mempalace/config.py`, `pyproject.toml`, `tests/test_backends.py`, `tests/test_backends_postgres.py`, `tests/test_config_extra.py`, `docs/internal/pgvector-665-decision.md`, `docs/postgres_backend.md`, `scripts/install_pg_backend.sh`


### Changed


- **README pivots to the four-layer model + Auto Dream as vindication of the verbatim-vs-derivative axis** ([`55b36ca`](https://github.com/techempower-org/mempalace/commit/55b36ca))
  Substantial README rewrite (+137/-100) reflecting three things
  that landed between the previous refresh (`a67be3f`, the
  2026-05-10 develop sync) and now:

  - **Four-layer model promoted to the lede.** Storage / encoder /
    retrieval / consumption as independently improvable surfaces;
    the empirical claim that model size doesn't fix invocation
    discipline (RLM-Qwen-7B and RLM-Llama-70B both ceiling at
    46.67% recall while Familiar's deterministic pipeline hits
    78.33% on the same jp-realm-v0.1 corpus). Calibration paragraph
    hedges the absolute numbers; methodology disclosure lives in
    `docs/research/`. The earlier "recovery-collection migration"
    lede moves down to "What this fork has learned."

  - **Auto Dream framed as vindication.** Anthropic shipped Auto
    Dream in two research-preview surfaces in late April: a
    consolidator inside Claude Code (manual `/dream` or auto-trigger
    at 24h + 5 sessions; mutates `~/.claude/projects/<project>/memory/`
    in place) and a Managed Agents Dreams API (REST, beta header
    `dreaming-2026-04-21`, models `claude-opus-4-7` and
    `claude-sonnet-4-6`, up to 100 sessions, non-destructive output
    store). The Dreams API design ratifies the verbatim-input /
    derivative-output axis. Replaces the prior "neither has
    consolidation" framing (which was wrong post-2026-04-21) with
    an affirmative claim: the verbatim layer doesn't need
    consolidation; it needs durability.

  - **Substrate section moves from "exploring" to "in flight."**
    Names the live `mempalace-db` test container on the homelab
    LAN, the cherry-pick on `feat/pgvector-age-impl`, and the
    documented Plan-B trigger date (2026-06-08).

  New section: "Convergence with peer systems" triangulates across
  Familiar (deterministic pipeline), CampaignGenerator (hierarchical
  AAAK pruning), Kent (APO trained policy), adaptmem (encoder
  fine-tune). Four agreements: verbatim storage as base layer, no
  LLM in the index path, wings as scope routing, consumption gap
  is real. Divergence is where intelligence above retrieval lives.

  Tactical corrections in the same diff: test count `~1500 → ~1850`,
  sync date `2026-04-27 → 2026-05-10`, fork-ahead count `~16 → ~14`,
  drawer count `151K → ~160K`, setup commands now lead with
  `uv sync --extra dev` (matching the project CLAUDE.md), PR table
  regenerated against `gh pr list` showing 10 open jphein PRs.

  Six new files in `docs/research/` committed alongside the README
  as the citation surface: adaptmem-orthogonal-layers,
  compass_artifact_wf-28bac4e8, compass_artifact_wf-ad108fcc,
  convergent-findings-kostadis-comparison, three-mempalace-consumers,
  three-patterns-for-agent-memory.

  *Files:* `README.md`, `docs/research/adaptmem-orthogonal-layers.md`, `docs/research/compass_artifact_wf-28bac4e8-71d9-4175-837a-d4ad563aec8d_text_markdown.md`, `docs/research/compass_artifact_wf-ad108fcc-3960-4eab-ad5d-234bf365b2f4_text_markdown.md`, `docs/research/convergent-findings-kostadis-comparison.md`, `docs/research/three-mempalace-consumers.md`, `docs/research/three-patterns-for-agent-memory.md`


### Fixed


- **Defense-in-depth metadata sanitizer at the chromadb-client chokepoint** ([`f499814`](https://github.com/techempower-org/mempalace/commit/f499814))
  Companion to the repair.py sanitizers in #1458 / `949cb20`
  (which fixed `_extract_drawers` and `_rebuild_one_collection`).
  A 151,478-drawer rebuild against the canonical palace still
  failed at ~120K drawers with the same `ValueError: Expected
  metadata to be a non-empty dict, got 0 metadata attributes in
  add` from chromadb's `validate_metadata` — the traceback ran
  through `mempalace/backends/chroma.py:add → chromadb
  Collection.add → validate_insert_record_set →
  validate_metadatas → validate_metadata`.

  Even with sanitization at both repair-layer extract points,
  something between the repair-layer sanitizer and chromadb's
  actual write call reshapes the metadatas list — likely
  chromadb's upsert internally splitting into add+update paths,
  or a deeper preprocessing step. Sanitizing at the
  chromadb-client chokepoint catches whatever the upstream path
  misses.

  New helper `ChromaCollection._sanitize_metadatas_for_chromadb`
  coerces any None or empty-dict entry to
  `{"_repaired_empty_meta": True}` (same sentinel as the repair.py
  paths; searchable via `where={"_repaired_empty_meta": True}`).
  Both `add()` and `upsert()` route through it. Cost is one list
  comprehension per write call — negligible.

  Direct-to-main commit (not via PR) because the in-progress
  151K-drawer rebuild on disks needed the fix live to make
  forward progress; standard PR-review cadence would have stalled
  the rebuild for hours. Defense-in-depth at the chokepoint is
  independently mergeable upstream once the rebuild completes
  and we have time to file it.

  *Files:* `mempalace/backends/chroma.py`


- **Coerce empty + None metadata to sentinel in both rebuild paths** ([`949cb20`](https://github.com/techempower-org/mempalace/commit/949cb20))
  ChromaDB 1.5.x rejects both None and empty-dict entries in the
  `metadatas` list (raises `ValueError: Expected metadata to be a
  non-empty dict`). Two functions in `mempalace/repair.py` construct
  the metadatas list that feeds chromadb's upsert during a rebuild:

  - `_extract_drawers` (around line 139) — extracts drawers from
    sqlite ground truth for rebuild; passes them straight through.
  - `_rebuild_one_collection` (around line 816) — collects the
    extracted drawers and calls `col.upsert(...)`.

  Both were vulnerable to the same ValueError, which would abort
  a multi-hour palace rebuild ~80% of the way through if a
  historical drawer had a sparse metadata row. Mempalace drawers
  always carry at least wing/room, so this is defensive against
  corruption in `embedding_metadata` or pre-rooms-and-wings data.

  Fix coerces both None and empty-dict entries to a sentinel
  `{"_repaired_empty_meta": True}` that satisfies chromadb's
  validator AND is discoverable later via
  `where={"_repaired_empty_meta": True}` so an operator can find
  and investigate the rows the rebuild papered over.

  The `_extract_drawers` slice is covered by upstream PR #1459;
  the `_rebuild_one_collection` slice is fork-only — the bug
  surfaces only when a rebuild reaches the upsert path after
  extraction, which is the specific operational shape this fork's
  151K+ drawer palace has been exercising. JP's parallel-session
  work originally landed both fixes on the
  `fix/repair-empty-metadata` branch (filed upstream as #1459 for
  the first slice); cherry-picked onto fork main as `949cb20` so
  both fixes are live on `jphein/mempalace` immediately.

  *Upstream:* [PR #1459](https://github.com/MemPalace/mempalace/pull/1459) (MERGED)
  *Files:* `mempalace/repair.py`


- **Route Stop/PreCompact hooks through palace-daemon/clients/hook.py** ([`42ded2e`](https://github.com/techempower-org/mempalace/commit/42ded2e))
  Replaces the bash wrapper invocation pattern in
  `.claude-plugin/hooks/hooks.json` with a single Python entrypoint
  via the daemon's hook client. Both Stop and PreCompact now invoke
  `python3 /home/jp/Projects/palace-daemon/clients/hook.py` with
  explicit `--hook stop --harness claude-code` /
  `--hook precompact --harness claude-code` arguments and a 30s
  timeout.

  Description on the manifest names this the 'post-2026-05-11
  split-brain fix' — the daemon's hook client now owns the routing
  decision (daemon vs local) instead of forking it across two
  bash scripts that previously made independent decisions about
  where to send the work. Hooks weren't firing reliably under the
  previous shape; the staged file (`hooks.json.layer2-staged`,
  created 2026-05-11 06:01) just needed promotion.

  The previously-active `mempal-stop-hook.sh` and
  `mempal-precompact-hook.sh` stay in the tree — they're still
  tested by `tests/test_claude_plugin_hook_wrappers.py` and may be
  invoked by non-Claude-Code agents through different paths.
  They're alternate invocation surfaces, not dead code.

  Fork-only deployment config: the absolute path
  `/home/jp/Projects/palace-daemon/clients/hook.py` is specific
  to JP's homelab layout. Won't go to upstream as-is; the path
  shape would need to become discovery-based first (similar to
  how `MEMPALACE_PYTHON` + `$PLUGIN_ROOT/venv/bin/python3` +
  system fallback works in CLAUDE.md row 19's venv-aware
  resolution pattern).

  *Files:* `.claude-plugin/hooks/hooks.json`


### Performance


- **Bulk pre-fetch already-mined set instead of N WHERE queries in mine_convos** ([`248854a`](https://github.com/techempower-org/mempalace/commit/248854a))
  Replaces the N+1 `col.get(where={"source_file": <path>}, ...)`
  per-conversation pattern in `mempalace/convo_miner.py:mine_convos`
  with a single bulk pre-fetch — `col.get(where={"source_file":
  {"$in": [<all paths>]}})` returns all already-mined paths in one
  query, then the per-conversation check becomes a hash-set
  membership test.

  On a ~160K-drawer palace with thousands of Claude Code transcripts
  under mine scope, the old shape spent the bulk of `mine` wall-time
  in chromadb WHERE traversal even when 99% of the conversations
  were already mined. The new shape collapses the upfront-check
  cost from O(N) round-trips to O(1).

  The `bulk_check_mined()` helper this PR exercises was Row 1 of
  the original CLAUDE.md fork-ahead inventory — first noted as
  fork-only on 2026-04-10, finally pushed upstream as the standalone
  perf change once the helper had been battle-tested through ~6 weeks
  of fork-side mining.

  *Tests:* 28 convo_miner tests pass; full suite 1828/1828 (pre-merge baseline)
  *Upstream:* [PR #1474](https://github.com/MemPalace/mempalace/pull/1474) (MERGED)
  *Files:* `mempalace/convo_miner.py`


## [2026-05-07]


### Added


- **daemon-route `mempalace status` / `search` / `mine` when PALACE_DAEMON_URL is set** ([`22ef562`](https://github.com/techempower-org/mempalace/commit/22ef562))
  Companion to the `mcp_server` routing in commit `41359ba`. Closes
  the last desktop-side path that opened a local chromadb client.

  Adds `_daemon_strict()`, `_call_daemon_tool()`,
  `_post_daemon_mine_cli()` helpers in `cli.py` mirroring the gate
  already in `mempalace.hooks_cli` and `mempalace.mcp_server`.
  `cmd_status`, `cmd_search`, `cmd_mine` route through the daemon
  when `PALACE_DAEMON_URL` is set:

  - Read paths (`status`, `search`) → JSON-RPC `tools/call` against
    the daemon's `/mcp` endpoint. Output is formatted to match
    the local `miner.status` / `searcher.search` printers — same
    human-readable shape, with the daemon URL surfaced in the
    header so the reader knows which view they're looking at.

  - Write path (`mine`) → POST `/mine` (same endpoint
    `hooks_cli._post_daemon_mine` already uses). CLI-friendly
    errors print to stderr and exit non-zero; hooks_cli's variant
    logs silently because a missed-mine isn't worth crashing a
    hook.

  `--palace <path>` always overrides routing — explicit path
  means the user asked for THAT palace, not the canonical one.

  Local-only commands (`init`, `repair`, `export`, `sweep`,
  `purge`, `mined`, `wakeup`) stay local because they need on-host
  filesystem access (HNSW rebuild, palace dump, sweeper
  deduplication state). When `mempalace-data/` is archived those
  commands will fail with "no palace found" until pointed
  elsewhere with `--palace` — that's the right "your data is at
  the daemon, not local" signpost.

  Live smoke against `disks.jphe.in:8085`: `mempalace status`
  returns 160,351 drawers, `mempalace search "daemon routing"`
  returns properly-formatted hits.

  *Tests:* 14 new tests in `tests/test_cli_daemon.py` — gate semantics,
`_call_daemon_tool` body shape + JSON-RPC error surfacing,
`_post_daemon_mine_cli` body shape + stderr-on-failure, mine
routing in both projects and convos modes, fall-through-to-local
when env var is unset. Suite 1591 passed (1577 + 14 new).

  *Files:* `mempalace/cli.py`, `tests/test_cli_daemon.py`


- **daemon-route `mcp_server.py` via the `handle_request` JSON-RPC chokepoint** ([`41359ba`](https://github.com/techempower-org/mempalace/commit/41359ba))
  Mirrors the `PALACE_DAEMON_URL` gate that `hooks_cli.py` shipped
  on 2026-04-24 (the daemon-strict fix for the HNSW drift
  incident). Closes the last in-process write path inside
  `mempalace.mcp_server` that bypassed the daemon.

  Adds `_daemon_strict()` and `_forward_to_daemon()` helpers and
  gates at the JSON-RPC chokepoint in `handle_request()`: when
  `PALACE_DAEMON_URL` is set and `PALACE_DAEMON_STRICT != "0"`,
  every method (`initialize`, `tools/list`, `tools/call`, `ping`)
  is forwarded to palace-daemon's `/mcp` proxy and the daemon's
  response is returned verbatim. Notifications skip the network
  round-trip per JSON-RPC spec.

  Single chokepoint at `handle_request` is functionally equivalent
  to per-handler gates — every JSON-RPC method funnels through it
  — and avoids 30+ duplicated branches across the TOOLS dispatch.
  No local chromadb client opens in strict mode. Startup
  `_refresh_vector_disabled_flag()` HNSW probe is skipped when
  daemon-strict (the daemon owns its palace's capacity).

  `tests/conftest.py` updated to scrub
  `PALACE_DAEMON_URL`/`PALACE_DAEMON_STRICT`/`PALACE_API_KEY` at
  module load (matching the existing HOME-redirect pattern) so
  existing local-path tests don't accidentally hit the live
  daemon when run from a shell where the env var is set.

  Pitchable upstream as a single-file replacement for the
  standalone `palace-daemon/clients/mempalace-mcp.py` bridge —
  anyone running `python -m mempalace.mcp_server` with the env
  var set now gets daemon proxying natively.

  Also: `~/.mempalace/config.json` had its `palace_path` key
  removed (was pinning `/home/jp/Projects/mempalace-data/palace`);
  falls back to default `~/.mempalace/palace`. With row 34 also
  shipped, `mempalace-data/` (308 MB) has no live consumers and
  is archivable.

  *Tests:* 15 new tests in `tests/test_mcp_server_daemon.py` — gate
semantics, `_forward_to_daemon` body shape, network-failure
surfacing as JSON-RPC error envelope, forwarded
`initialize`/`tools/call`/error propagation, sentinel TOOLS
patch proving no local handler runs in strict mode. End-to-end
smoke against `disks.jphe.in:8085` returns 160,351 drawers
from the canonical palace. Suite 1577 passed.

  *Files:* `mempalace/mcp_server.py`, `tests/conftest.py`, `tests/test_mcp_server_daemon.py`


## [2026-05-05]


### Added


- **mempalace mined + purge --source-file (mining management surface)** ([`2e6ced9`](https://github.com/techempower-org/mempalace/commit/2e6ced9))
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


- **`hook_verbatim_mode` config flag preserves system tags + full tool I/O during transcript ingest** ([`ef98961`](https://github.com/techempower-org/mempalace/commit/ef98961))
  `normalize()` defaults match upstream — system tags, hook chrome,
  Read/Edit/Write tool results, long Bash output, and large
  Grep/Glob match lists are stripped or truncated so chunk
  embeddings don't drift on chrome tokens. That's the right
  default for a search-quality optimization but it also drops
  content a verbatim-archive consumer wants to keep.

  Adds a `hooks.verbatim_mode` opt-in in `config.json`
  (`MempalaceConfig.hook_verbatim_mode`, default `False`).
  `mempalace.convo_miner.mine_convos` reads the flag and passes
  `verbatim=...` through `normalize()` →
  `_try_normalize_json()` → `_try_claude_code_jsonl()` →
  `_extract_content()` → `_format_tool_use()` /
  `_format_tool_result()` / `strip_noise()`. When `verbatim` is
  true: `strip_noise` is a passthrough; Bash commands and
  unknown-tool JSON inputs aren't 200-char truncated; Bash output
  isn't head/tail-collapsed; Grep/Glob match lists aren't capped;
  Read/Edit/Write results are included rather than omitted;
  unknown-tool output isn't byte-capped.

  Other transcript schemas (Codex, Gemini, claude.ai, ChatGPT,
  Slack) didn't truncate to begin with, so they're already
  verbatim — the flag is a no-op for them.

  Daemon path picks up the toggle transparently because the
  daemon spawns `mempalace mine ...` as a subprocess that goes
  through `convo_miner.mine_convos`.

  Backs JP's 2026-05-05 question — "we're not missing any tool
  calls or anything, right?" — without altering the upstream
  default for installs that benefit from chrome-stripped
  embeddings.

  *Tests:* 9 new tests in `tests/test_normalize.py::TestVerbatimMode` —
covers strip_noise passthrough, Bash and unknown-tool input
no-truncation, Read/Edit/Write result inclusion, Bash
head/tail no-collapse, Grep/Glob match no-cap, unknown-tool
byte no-cap, full JSONL round-trip, default-off contract, and
config-file readback. Suite total 1562 passed.

  *Files:* `mempalace/config.py`, `mempalace/convo_miner.py`, `mempalace/normalize.py`, `tests/test_normalize.py`


### Changed


- **Drop wing_ prefix from transcript-derived wings to converge with operator mines** ([`86d4700`](https://github.com/techempower-org/mempalace/commit/86d4700))
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


- **Retire mempalace_session_recovery collection + read tool** ([`0b945e1`](https://github.com/techempower-org/mempalace/commit/0b945e1))
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


- **Drop hook-side checkpoint diary writes — verbatim-only architecture** ([`69768fc`](https://github.com/techempower-org/mempalace/commit/69768fc))
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


- **Preserve dashed project names in transcript-derived wings** ([`d76134d`](https://github.com/techempower-org/mempalace/commit/d76134d))
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


- **Restore transcript ingest via daemon /mine when PALACE_DAEMON_URL is set** ([`09d2ca6`](https://github.com/techempower-org/mempalace/commit/09d2ca6))
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


- **`cfg.init()` no longer materializes chunking defaults into `config.json`** ([`6ce37c0`](https://github.com/techempower-org/mempalace/commit/6ce37c0))
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

  Same fix pushed to the open #1024 PR branch (squash-merged
  upstream) so the bug doesn't get reintroduced when #1024
  merges. Amends fork-ahead row 17.

  *Tests:* 1548/1548 (was 1546/1548 with 2 isolation failures in test_convo_miner)
  *Upstream:* [PR #1024](https://github.com/MemPalace/mempalace/pull/1024) (MERGED)
  *Files:* `mempalace/config.py`


## [2026-04-27]


### Changed


- **Retire the `kind=` filter — structural split made it inert** ([`7ba28dc`](https://github.com/techempower-org/mempalace/commit/7ba28dc))
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


- **Hoist CLOSET_RANK_BOOSTS to module level + record VecRecall ablation finding** ([`3cb03f3`](https://github.com/techempower-org/mempalace/commit/3cb03f3))
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


- **Strip embedded API key from .claude-plugin/ manifests; rely on env inheritance** ([`9f91e18`](https://github.com/techempower-org/mempalace/commit/9f91e18))
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


- **Canonical YAML manifest + renderer for fork-ahead docs** ([`5a01aec`](https://github.com/techempower-org/mempalace/commit/5a01aec))
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


- **Phase D migration + PreCompact recovery write** ([`42817d7`](https://github.com/techempower-org/mempalace/commit/42817d7))
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


- **Surface drawer_id in search/diary/recovery payloads** ([`9a8bb77`](https://github.com/techempower-org/mempalace/commit/9a8bb77))
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


- **scripts/deploy.sh — one-command Syncthing-aware redeploy** ([`8252025`](https://github.com/techempower-org/mempalace/commit/8252025))
  Single command does the right shape: push fork main → wait for
  Syncthing to reach ``/mnt/raid/projects/memorypalace`` on the deploy
  host → ``systemctl --user restart palace-daemon`` → poll ``/health`` →
  ssh-import-check that today's fork-ahead surface is loaded.
  Replaces a three-step manual ritual that was easy to get wrong
  (e.g. ``pip install --upgrade`` was a no-op on the editable install).

  *Files:* `scripts/deploy.sh`


### Changed


- **Cherry-pick #1094 — coerce None metadatas at chromadb boundary** ([`43d728d`](https://github.com/techempower-org/mempalace/commit/43d728d))
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


- **Cherry-pick #1087 rewrite — collection.delete(where=) instead of nuke-and-rebuild** ([`366a9ad`](https://github.com/techempower-org/mempalace/commit/366a9ad))
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


- **Integrity gate prevents quarantine_stale_hnsw from destroying healthy indexes** ([`645ba20`](https://github.com/techempower-org/mempalace/commit/645ba20))
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


- **Cherry-pick #1085 — batch ChromaDB inserts in miner (10–30× faster)** ([`6be6fff`](https://github.com/techempower-org/mempalace/commit/6be6fff))
  Cherry-picked from upstream PR
  [#1085](https://github.com/MemPalace/mempalace/pull/1085) (@midweste,
  OPEN as of 2026-04-26). New ``_build_drawer()`` helper + ``add_drawers()``
  batch-insert path; ``process_file`` hands the full chunk list to
  ``add_drawers`` instead of looping per-chunk. Hoists ``datetime.now()``
  and ``os.path.getmtime()`` to file-level (2 syscalls per file instead
  of 2N). Reported 10–30× mining speedup upstream. Fork-side resolution
  preserved fork's existing ``DRAWER_UPSERT_BATCH_SIZE=1000``; aliased
  upstream's ``CHROMA_BATCH_LIMIT`` to it. **2026-05-16:** #1085 was
  closed by @midweste, superseded by merged upstream
  [#1185](https://github.com/MemPalace/mempalace/pull/1185) (wider
  scope: same batching + optional GPU acceleration). The fork-side
  cherry-pick is now a no-op against develop; drop on next sync.
  **2026-05-24:** reassessed and kept — *absorbed into fork
  architecture*. Over the 73 commits to ``miner.py``,
  ``convo_miner.py``, and ``format_miner.py`` since the cherry-pick
  landed, the fork built on top of these primitives in ways
  upstream #1185 does not provide:

  1. ``add_drawers()`` is a fork-only public API
     (``mempalace/miner.py``). Upstream #1185 inlined batching into
     ``process_file`` and exposed no public batch function. The
     fork's ``add_drawers`` returns the fork-only
     ``(added, batch_ids, warnings)`` tuple wired to room-taxonomy
     validation (#86) and is consumed by ``test_room_taxonomy.py``.
  2. ``DRAWER_UPSERT_BATCH_SIZE`` / ``CHROMA_BATCH_LIMIT`` are
     fork-only sub-batching knobs. Upstream does one giant upsert
     per file (OOM risk on pathological files); the fork
     sub-batches in groups of 1000. The knob is referenced from
     ``miner.py``, ``convo_miner.py``, and ``format_miner.py``,
     and monkeypatched by ``test_miner.py`` /
     ``test_convo_miner_unit.py`` to drive the sub-batch loops.
  3. ``_build_drawer_metadata`` carries fork-only Tier 6a
     extensions (``line_start``, ``line_end``, ``content_date``)
     that closet pointers depend on. Tested at
     ``tests/test_miner.py``.

  Net: the cherry-pick is no longer redundant with upstream and
  must not be dropped. Closes
  [#165](https://github.com/techempower-org/mempalace/issues/165).

  *Upstream:* [PR #1085](https://github.com/MemPalace/mempalace/pull/1085) (CLOSED)
  *Files:* `mempalace/miner.py`


## [2026-04-25]


### Added


- **Phases A–C of the checkpoint collection split** ([`e266365`](https://github.com/techempower-org/mempalace/commit/e266365))
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


- **Gate quarantine_stale_hnsw to once-per-palace-per-process** ([`70c4bc6`](https://github.com/techempower-org/mempalace/commit/70c4bc6))
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


- **palace_graph.build_graph skips None metadata** ([`5fd15db`](https://github.com/techempower-org/mempalace/commit/5fd15db))
  ``palace_graph.py:95`` was calling ``meta.get("room", "")`` unconditionally;
  ChromaDB returns ``None`` for legacy/partial-write drawers, taking out
  every consumer of ``build_graph`` (graph_stats, find_tunnels, traverse,
  the daemon's ``/stats``). Caught by palace-daemon's ``verify-routes.sh``
  smoke test. Same family as upstream's #999 None-metadata audit, in a
  read path the audit didn't reach.

  *Upstream:* [PR #1201](https://github.com/MemPalace/mempalace/pull/1201) (MERGED)
  *Files:* `mempalace/palace_graph.py`


- **kind= filter on search_memories excludes Stop-hook checkpoints (transitional)** ([`f9f5cc4`](https://github.com/techempower-org/mempalace/commit/f9f5cc4))
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


- [PR #1024](https://github.com/MemPalace/mempalace/pull/1024) — Configurable chunk_size / chunk_overlap / min_chunk_size — 2026-05-15
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
