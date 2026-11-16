# Upstream sync, 2026-11-15

Requested 2026-11-15: sync the fork with upstream. The fork's maintainers review every pick; deployment is a separate, later step. Method: cherry-pick, never a whole merge (fork-first; take only the clearly correct part of a bundled change).

## The window

- Fork: `origin/main` at `e0082acf` (823 commits ahead of upstream). Upstream: `MemPalace/mempalace` `main` at `4df7b830` (2026-11-12), 489 commits ahead of the fork.
- Sync point (merge base): `602d796c`, 2026-10-13. Since then upstream has **281 non-merge commits**. None is already in the fork, by patch identity (`git cherry`) or by a `cherry picked from` trailer.
- Each commit was probed alone against `origin/main` (`git cherry-pick --no-commit`): 57 apply cleanly, 224 conflict. A conflict often only means the fork changed the same lines; the bucket says what to do about it.

## How each commit was bucketed

- **now:** a bug fix, performance fix or safety fix in code our daemon path runs (palace-daemon imports `mempalace.mcp_server`, the miners, the searcher, the knowledge graph and the pgvector backend), that applies cleanly; or a security fix small enough to port now.
- **skip:** upstream-only backends (chroma, sqlite_exact, rust_exact, milvus, qdrant, the remote/sidecar layer) because production is postgres + pgvector; upstream's own CI, site, changelog and docs (the fork keeps its own); integrations and benchmarks we do not run.
- **later:** everything else, each with its reason: fixes that need a port because the fork changed the same code, features and refactors, test-only commits, Windows-only behaviour, the upstream HTTP hub (`mempalace serve`) whose auth work we port as one topic, and the security ports too large to do safely today (each with an owner and a date).

Totals: **now 15**, **skip 45**, **later 221** (after the picks: five commits moved from now to later, each with its reason; first count was now 20, later 216).

## now

Picked in topic PRs, one topic each (`git cherry-pick -x`, so every commit names its upstream origin), tests green; xreview on anything touching the daemon contract or redaction.

| topic | upstream | date | PR | subject | why |
|---|---|---|---|---|---|
| A pql | `6450bc0c` | 2026-10-13 |  | fix(mcp): treat only upper-case PQL clause keywords as keywords | fix, applies cleanly |
| A pql | `45bff470` | 2026-10-15 |  | fix(pql): keep stored content text opaque | fix, applies cleanly |
| B mcp tools | `d4038612` | 2026-10-15 |  | fix(mcp-light): allowlist palace_* targets and reply on stdio errors | fix, applies cleanly |
| B mcp tools | `7bad1ecd` | 2026-10-15 |  | fix(mcp): record tunnel follows only while the writer lock is held | fix, applies cleanly |
| C daemon/hooks | `5528b8ac` | 2026-10-15 |  | fix(proc): one pid_alive with daemon semantics | fix, applies cleanly |
| C daemon/hooks | `f3dfae89` | 2026-10-15 |  | fix(service): route mcp_tool jobs through dispatcher gates | fix, applies cleanly |
| C daemon/hooks | `abe196f9` | 2026-10-31 | #2586 | fix(hooks): collapse &lt;project&gt;/.worktrees/&lt;wt&gt; into the project wing (#2586) | fix, applies cleanly |
| E miners | `9e013e9e` | 2026-10-15 |  | fix(format_miner): hold the palace lock and keep failed rewrites recoverable | fix, applies cleanly |
| E miners | `6981b124` | 2026-10-15 |  | fix(general_extractor): keep paragraphs that match no keyword | fix, applies cleanly |
| E miners | `466968ce` | 2026-10-30 | #2794 | fix(mine): honour .git/info/exclude and skip .worktrees/.claude worktrees (#2599) (#2794) | fix, applies cleanly |
| E miners | `34b742f3` | 2026-10-31 | #518 | fix(entity): add generic code terms to English stopwords (#476) (#518) | fix, applies cleanly |
| F kg | `e704a212` | 2026-10-15 |  | fix(kg): keep entity identity and merge properties | fix, applies cleanly |
| G backends | `722e8fac` | 2026-10-15 |  | perf(pgvector): look up ids with the primary key | perf, applies cleanly |
| H safety | `50c1f959` | 2026-10-15 |  | fix(llm): refuse silent external calls | fix, applies cleanly |
| I credentials | `857fbec4` | 2026-10-18 |  | fix(security): mask endpoint credentials in embedding and LLM errors | security: mask endpoint credentials in embedding and LLM errors (a small port: conflicts) |

## later

| upstream | date | PR | subject | why |
|---|---|---|---|---|
| `15d9c28f` | 2026-10-17 |  | fix(hooks): report a checkpoint queued behind a running mine as queued, not failed | moved from now: its test needs `_queue_diary_checkpoint`, an earlier upstream helper the fork does not have; port with it |
| `0a41b363` | 2026-10-15 |  | fix(search): return closet hits as their own drawers | moved from now: returning closet hits as their own drawers conflicts with the fork reporting a chunked entry as one logical result; needs a port |
| `9bbcebcc` | 2026-10-15 |  | fix(split): keep preamble, short sessions, and colliding names | moved from now: it writes another file name where the fork's non-regular-file guard skips (a FIFO or dangling symlink at the output name); a guard change needs its own decision |
| `45074ca6` | 2026-10-15 |  | fix(backends): keep serializable metadata and publish markers atomically | moved from now: it imports `mempalace.fsio`, an upstream module the fork does not have; port with fsio |
| `ef94cd14` | 2026-10-17 |  | fix(update): publish the release cache through fsio so a reader can't drop it | moved from now: it publishes through `mempalace.fsio`, which the fork does not have; port with fsio |
| `cd92dd8a` | 2026-10-15 |  | fix(fsio): publish config through one Windows-safe atomic writer | Windows-only behaviour; Linux hosts unaffected |
| `501e8d71` | 2026-10-16 |  | fix(proc): bound the Windows pid probe and share the kernel32 setup | Windows-only behaviour; Linux hosts unaffected |
| `43d42bbd` | 2026-10-17 |  | fix(mine): keep stray bytes in UTF-8 files and map every windows-1252 byte | Windows-only behaviour; Linux hosts unaffected |
| `28ed775e` | 2026-10-17 |  | fix(lock): ride out Windows delete-pending opens and wait past LK_LOCK's ten tries | Windows-only behaviour; Linux hosts unaffected |
| `28f12c54` | 2026-10-17 |  | fix(fsio): retry renames a Windows reader blocks, and fail fast on a lasting lock-file denial | Windows-only behaviour; Linux hosts unaffected |
| `4a932d21` | 2026-10-17 |  | fix(fsio): replace links where the old writers did, and retry reads that race a Windows rename | Windows-only behaviour; Linux hosts unaffected |
| `9a377de8` | 2026-10-25 |  | fix(http): keep the hub's port exclusive on Windows | Windows-only behaviour; Linux hosts unaffected |
| `a844900f` | 2026-10-31 | #2869 | fix(convo): select authored time by instant across UTC offsets (#2869) | authored-at: the fork has its own authored_at work (docs/authored-at.md); needs a port against it |
| `aa319ef1` | 2026-10-17 |  | chore(lint): turn on import sorting and unused-noqa checks | chore |
| `7e645f0f` | 2026-10-17 |  | chore: fail on PR and plan history in comments, docstrings and help text | chore |
| `e7ff3fa0` | 2026-10-17 |  | chore(lint): sort imports and drop unused noqa marks | chore, conflicts |
| `dcfdb9d9` | 2026-10-17 |  | chore: state constraints instead of issue history in backend and palace comments | chore, conflicts |
| `f34bf1d5` | 2026-10-17 |  | chore(search): drop dead closet helpers and PR-history comments | chore, conflicts |
| `484d8091` | 2026-10-17 |  | chore(mcp): drop dead code and PR-history comments in the MCP server area | chore, conflicts |
| `9199b73f` | 2026-10-17 |  | chore(cli): wire mempalace export, drop onboarding.py, and clear dead code and PR-history notes | chore, conflicts |
| `719d678e` | 2026-10-17 |  | chore: drop dead helpers and PR-history comments in the remaining modules | chore, conflicts |
| `30046f94` | 2026-10-17 |  | chore(mine): drop helpers nothing calls since the spellcheck and registry rework | chore, conflicts |
| `e622f564` | 2026-10-17 |  | chore(mine): say what the miners' comments guard, not which PR added them | chore, conflicts |
| `c63ba1c5` | 2026-10-17 |  | chore(lint): turn on bugbear, return and commented-out-code checks | chore, conflicts |
| `1a1733d2` | 2026-10-17 |  | chore(lint): drop else after return and spell out implicit None returns | chore, conflicts |
| `654a083d` | 2026-10-17 |  | chore(lint): leave RET503 off, its hits end in raises or endless loops | chore, conflicts |
| `ee8313db` | 2026-10-18 |  | chore: require Python 3.10 (D-20) | chore, conflicts |
| `37b07e8b` | 2026-10-18 |  | chore(release): 3.12.0 | chore, conflicts |
| `c2382a05` | 2026-11-08 |  | chore(deps): bump locked packages with published advisories | chore, conflicts |
| `e35a584e` | 2026-10-15 |  | ci: single-source the coverage threshold | ci, conflicts |
| `2804d85e` | 2026-11-08 |  | ci: run the suite in parallel and cancel superseded PR runs | ci, conflicts |
| `3428b87f` | 2026-10-16 |  | docs(service): name the dispatch path run_mcp_tool uses | docs, conflicts |
| `45c4fcb4` | 2026-10-17 |  | docs(hooks): drop the issue number from the queued checkpoint comment | docs, conflicts |
| `606a1d4b` | 2026-10-30 | #2237 | Docs: OpenClaw Skill Update (#2237) | docs, conflicts |
| `56778911` | 2026-11-10 |  | feat(antigravity): add Aux Pane UI sidecar extension and build-plugins schema upgrade | feat |
| `e97b81e2` | 2026-10-17 |  | feat(mine): re-read every source once for the parsing fixes, reusing unchanged vectors | feat, conflicts |
| `911b1fe5` | 2026-10-30 | #2882 | feat(http): timestamp MCP log file + per-call wait/run timing (#2882) | feat, conflicts |
| `2c2d9f62` | 2026-11-10 |  | feat(antigravity): package zero-touch Marketplace bundle at plugins/mempalace | feat, conflicts |
| `e0f82d96` | 2026-09-30 |  | fix(hooks): file the save-hook diary checkpoint through a live hub | fix, needs a port (conflicts with the fork) |
| `ec7264aa` | 2026-10-15 |  | fix(hooks): legacy bash hooks delegate to hook run | fix, needs a port (conflicts with the fork) |
| `d42074c2` | 2026-10-15 |  | fix(mcp): keep a recoverable copy before update_drawer overwrites | fix, needs a port (conflicts with the fork) |
| `44e62e1f` | 2026-10-15 |  | fix(convo_miner): stamp the read mtime and keep one bad file from aborting | fix, needs a port (conflicts with the fork) |
| `70c22b49` | 2026-10-15 |  | fix: refuse to overwrite a state file that did not parse | fix, needs a port (conflicts with the fork) |
| `eccbb6a5` | 2026-10-16 |  | fix(mcp): record tunnel crossings without the writer lease | fix, needs a port (conflicts with the fork) |
| `155b4475` | 2026-10-16 |  | fix(general_extractor): re-mine general-mode files filed by the old extractor | fix, needs a port (conflicts with the fork) |
| `23a56ae6` | 2026-10-16 |  | fix(fsio): retry a rename a reader blocks and drop the config test seams | fix, needs a port (conflicts with the fork) |
| `edf80ffc` | 2026-10-16 |  | fix(pgvector): skip the query for an empty id list and drop the redundant id filter | fix, needs a port (conflicts with the fork) |
| `b29529c2` | 2026-10-16 |  | fix(service): run mcp_tool jobs through the shared tools/call path | fix, needs a port (conflicts with the fork) |
| `1683156f` | 2026-10-16 |  | fix(mcp): harden the drawer revision log and share one update outcome | fix, needs a port (conflicts with the fork) |
| `ad4b40f6` | 2026-10-16 |  | fix(convo_miner): reuse normalize's reader for the mtime and let fatal errors abort | fix, needs a port (conflicts with the fork) |
| `21e5fdf4` | 2026-10-16 |  | fix(migrate): rebuild through repair and carry over every palace file | fix, needs a port (conflicts with the fork) |
| `f5915003` | 2026-10-16 |  | fix(mcp-http): keep mesh routes off the write queue and read whole integrity verdicts | fix, needs a port (conflicts with the fork) |
| `4ca0a6a4` | 2026-10-16 |  | fix: keep the corrupt-state guard to state files and tidy it | fix, needs a port (conflicts with the fork) |
| `b4bf16ba` | 2026-10-16 |  | fix(hooks): file the queued checkpoint before the mines start | fix, needs a port (conflicts with the fork) |
| `0c18e0e5` | 2026-10-16 |  | fix(split): make a re-run idempotent and never refuse to retire the original | fix, needs a port (conflicts with the fork) |
| `0c70e213` | 2026-10-16 |  | fix(kg): keep legacy entities reachable and fold without merging names | fix, needs a port (conflicts with the fork) |
| `6e0acfd1` | 2026-10-16 |  | fix(search): dedupe hits by stored chunk and drop the second neighbor helper | fix, needs a port (conflicts with the fork) |
| `47b25712` | 2026-10-16 |  | fix(hooks): share one interpreter resolver across the legacy hooks | fix, needs a port (conflicts with the fork) |
| `64b8a2b2` | 2026-10-16 |  | fix(llm): refuse every unaccepted external endpoint at classify | fix, needs a port (conflicts with the fork) |
| `a92e4da5` | 2026-10-16 |  | fix(llm): resolve host.docker.internal and keep the trailing dot when resolving | fix, needs a port (conflicts with the fork) |
| `5483c5a8` | 2026-10-16 |  | fix(format_miner): rework the rewrite onto the real backend with a smaller diff | fix, needs a port (conflicts with the fork) |
| `4dd9b35c` | 2026-10-16 |  | fix(mcp): remove a drawer's revisions when the drawer is deleted | fix, needs a port (conflicts with the fork) |
| `7558d80c` | 2026-10-16 |  | fix(mcp): serialize revision appends and purges under one file lock | fix, needs a port (conflicts with the fork) |
| `ce04e03a` | 2026-10-16 |  | fix(convo): keep user turns as written unless spellcheck is opted in | fix, needs a port (conflicts with the fork) |
| `8b0070cd` | 2026-10-16 |  | fix(hooks): write the checkpoint payload through fsio | fix, needs a port (conflicts with the fork) |
| `03557f56` | 2026-10-16 |  | fix(mine): stop the mine when the mined set cannot be read | fix, needs a port (conflicts with the fork) |
| `8024b45c` | 2026-10-16 |  | fix(search): no default distance cutoff, one cosine scale, and keep unscored BM25 hits | fix, needs a port (conflicts with the fork) |
| `432fa4fa` | 2026-10-16 |  | fix(search): treat a non-positive max_distance as no cutoff, and note the change | fix, needs a port (conflicts with the fork) |
| `a8229654` | 2026-10-16 |  | fix(convo): spell-correct whole user messages and stamp only changed drawers | fix, needs a port (conflicts with the fork) |
| `8ed0d9c4` | 2026-10-16 |  | fix(convo): stamp every drawer of a spellchecked file and hash the text as written | fix, needs a port (conflicts with the fork) |
| `82338ca6` | 2026-10-16 |  | fix(mine): decide every mined-set check after the walk with one comparability rule | fix, needs a port (conflicts with the fork) |
| `e1c7b488` | 2026-10-16 |  | fix(search): follow Chroma's l2/ip distances, gate union hits once, and make min_similarity 1.0 exact | fix, needs a port (conflicts with the fork) |
| `18e6a3fb` | 2026-10-16 |  | fix(search): keep the closet boost through the hybrid rank and on the cosine scale | fix, needs a port (conflicts with the fork) |
| `3d2523e2` | 2026-10-16 |  | fix(search): score a boosted hit from its effective distance on the palace's own scale | fix, needs a port (conflicts with the fork) |
| `74092c94` | 2026-10-16 |  | fix(storage): decode every stored-text reader and encode pushed-down filters | fix, needs a port (conflicts with the fork) |
| `b9460dc4` | 2026-10-16 |  | fix(convo): stamp every lossy parsing step on the drawers it shaped | fix, needs a port (conflicts with the fork) |
| `59388b8e` | 2026-10-16 |  | fix(hub): derive search forwarding and the hub fingerprint from one settings table | fix, needs a port (conflicts with the fork) |
| `e07e13fd` | 2026-10-16 |  | fix(search): break hybrid score ties by vector distance before date | fix, needs a port (conflicts with the fork) |
| `071d1807` | 2026-10-16 |  | fix(convo): record only the winning parser's steps, and every importer's losses | fix, needs a port (conflicts with the fork) |
| `a66d5cd8` | 2026-10-16 |  | fix(convo): record what the Gemini JSON, Claude.ai and Slack importers leave out | fix, needs a port (conflicts with the fork) |
| `d7e67e4a` | 2026-10-16 |  | fix(mine): clear dropped metadata keys on re-file and let a dead backend abort | fix, needs a port (conflicts with the fork) |
| `1798e405` | 2026-10-16 |  | fix(convo): file a conversation under every file that holds it | fix, needs a port (conflicts with the fork) |
| `52077e9c` | 2026-10-16 |  | fix(mine): decode source files without turning bytes into U+FFFD | fix, needs a port (conflicts with the fork) |
| `f83eb0e1` | 2026-10-16 |  | fix(compress): store AAAK summaries in their own collection, not the closets | fix, needs a port (conflicts with the fork) |
| `ed0080c8` | 2026-10-17 |  | fix(embedding): embed a bare string as one document and retry a failed dimension probe | fix, needs a port (conflicts with the fork) |
| `7bfdeea2` | 2026-10-17 |  | fix(backends): update skips missing ids everywhere, and wake-up reports an unreadable palace | fix, needs a port (conflicts with the fork) |
| `9ef21c5d` | 2026-10-17 |  | fix(init): record only explicit choices and name every project file once | fix, needs a port (conflicts with the fork) |
| `4a80e318` | 2026-10-17 |  | fix(compress): delete a drawer's AAAK summary with it and clean old palaces on rebuild | fix, needs a port (conflicts with the fork) |
| `59df4934` | 2026-10-17 |  | fix(mcp): check every tool argument against its schema at dispatch | fix, needs a port (conflicts with the fork) |
| `1156ab03` | 2026-10-17 |  | fix(compress): a missing palace has no AAAK collection to purge | fix, needs a port (conflicts with the fork) |
| `f92a4e24` | 2026-10-17 |  | fix(layers): fetch recent memories inside one wake-up budget | fix, needs a port (conflicts with the fork) |
| `ab5021f0` | 2026-10-17 |  | fix(mine): carry stray bytes through every wire, export and LLM request | fix, needs a port (conflicts with the fork) |
| `f888f321` | 2026-10-17 |  | fix(layers): one reader thread per process, and Hermes reads inline under its lock | fix, needs a port (conflicts with the fork) |
| `80d3eb6f` | 2026-10-17 |  | fix(mcp): show stray bytes to clients as U+FFFD and keep them exact elsewhere | fix, needs a port (conflicts with the fork) |
| `fa8efbad` | 2026-10-17 |  | fix(repair): report the max_seq_id plan that was applied under the lease | fix, needs a port (conflicts with the fork) |
| `9958c87b` | 2026-10-17 |  | fix(mcp): keep the strict switches strict and the handler guards in place | fix, needs a port (conflicts with the fork) |
| `43bffd71` | 2026-10-17 |  | fix(mcp): drop source stamps when update_drawer rewrites a drawer's text | fix, needs a port (conflicts with the fork) |
| `b5319d1b` | 2026-10-17 |  | fix(layers): a missing palace has no memories, and a broken install still fails wake-up | fix, needs a port (conflicts with the fork) |
| `931c80da` | 2026-10-17 |  | fix(init): record a backend chosen with MEMPALACE_BACKEND | fix, needs a port (conflicts with the fork) |
| `947474c8` | 2026-10-17 |  | fix(proxy): keep a search local when this session chose its own settings | fix, needs a port (conflicts with the fork) |
| `0740543d` | 2026-10-17 |  | fix(layers): fail wake-up on a broken install while opening the palace | fix, needs a port (conflicts with the fork) |
| `686454f6` | 2026-10-17 |  | fix(mine): keep closets and AAAK summaries of unchanged drawers on a re-file | fix, needs a port (conflicts with the fork) |
| `8fc9ab2e` | 2026-10-17 |  | fix(mine): a collision is another source's drawer, not the same source's moved chunk | fix, needs a port (conflicts with the fork) |
| `bb85f72e` | 2026-10-17 |  | fix(convo): keep typed blank lines and join Gemini parts as written | fix, needs a port (conflicts with the fork) |
| `a778964f` | 2026-10-17 |  | fix: keep registry rows and sentinels out of every user-facing reader | fix, needs a port (conflicts with the fork) |
| `dcfc3e14` | 2026-10-17 |  | fix: skip the registry facet query when no registry room shows up | fix, needs a port (conflicts with the fork) |
| `93aac28c` | 2026-10-17 |  | fix: name bookkeeping rows by ingest_mode and fill pages past them | fix, needs a port (conflicts with the fork) |
| `579abf8a` | 2026-10-17 |  | fix(sync): keep reading a bare _registry room as a registry row | fix, needs a port (conflicts with the fork) |
| `1627bfeb` | 2026-10-17 |  | fix: cap the L2 scan and keep a real _registry room's drawers prunable | fix, needs a port (conflicts with the fork) |
| `2098beeb` | 2026-10-17 |  | fix(mine): read stored vectors only for unchanged drawers, and re-embed if the read fails | fix, needs a port (conflicts with the fork) |
| `950581fe` | 2026-10-17 |  | fix(lint): bind loop values, chain re-raised errors and make tests assert | fix, needs a port (conflicts with the fork) |
| `48838f9f` | 2026-10-17 |  | fix(update): retry a read that races a publish of the release cache | fix, needs a port (conflicts with the fork) |
| `6b828093` | 2026-10-17 |  | fix(hooks): never import mempalace from the project directory | fix, needs a port (conflicts with the fork) |
| `a1e7a4fe` | 2026-10-17 |  | fix(fsio): create every temporary file exclusively and never write an unfollowed link in place | fix, needs a port (conflicts with the fork) |
| `3137000c` | 2026-10-17 |  | fix(llm): never take JSON nested in a failed candidate, check the closet shape, and mask keys in errors | fix, needs a port (conflicts with the fork) |
| `79022e32` | 2026-10-17 |  | fix: old wing spellings reach every wing-scoped read | fix, needs a port (conflicts with the fork) |
| `0d706124` | 2026-10-17 |  | fix: skip the old Cursor alias when it drops a whole word | fix, needs a port (conflicts with the fork) |
| `1febb8d2` | 2026-10-17 |  | fix(chroma): log an unreadable status fallback and encode tuple filter operands | fix, needs a port (conflicts with the fork) |
| `df06dc86` | 2026-10-17 |  | fix(kg): keep each palace on the graph that holds its facts; reads never create one | fix, needs a port (conflicts with the fork) |
| `e55c7e44` | 2026-10-17 |  | fix(fact_checker): treat the palace as explicit only when --palace is typed | fix, needs a port (conflicts with the fork) |
| `25f6f373` | 2026-10-17 |  | fix(date_window): compare filed_at as instants across naive-local and UTC values | fix, needs a port (conflicts with the fork) |
| `85498c06` | 2026-10-18 |  | fix(recency): order newest-first reads by instant across naive-local and UTC values | fix, needs a port (conflicts with the fork) |
| `d2476723` | 2026-10-18 |  | fix(dynamics): read naive created_at/last_activated as local time | fix, needs a port (conflicts with the fork) |
| `779992a9` | 2026-10-18 |  | fix(mine): keep --palace and media options on daemon-routed adapter mines | fix, needs a port (conflicts with the fork) |
| `07f928eb` | 2026-10-20 |  | fix: preserve revisions and report graph imports accurately | fix, needs a port (conflicts with the fork) |
| `07804291` | 2026-10-30 | #2607 | fix(daemon): emit lifecycle and HTTP access lines to stdout/stderr (#2475) (#2607) | fix, needs a port (conflicts with the fork) |
| `65fb76e9` | 2026-10-30 | #2881 | fix(entities): honor entity_languages in extraction filters (#2811) (#2881) | fix, needs a port (conflicts with the fork) |
| `38063ee0` | 2026-10-30 | #2909 | fix(mcp): hold the peer-writer lease per call, not per process (#2909) | fix, needs a port (conflicts with the fork) |
| `c291f6c9` | 2026-10-30 | #2890 | fix: release the request lock around a checkpoint's embedding phases (#2890) | fix, needs a port (conflicts with the fork) |
| `d15d0580` | 2026-10-31 | #2911 | fix(kg): don't cap the length of values the KG tools only look up (#2911) | fix, needs a port (conflicts with the fork) |
| `647674ea` | 2026-10-31 | #2525 | fix(repair-status): tell a healthy unflushed palace from one that lost its index (#2525) | fix, needs a port (conflicts with the fork) |
| `f4764f68` | 2026-10-31 | #2908 | fix(hooks): derive the Codex wing from the rollout's payload.cwd (#2908) | fix, needs a port (conflicts with the fork) |
| `5a249612` | 2026-10-31 | #2870 | fix(miner): recognize registered entities in continuous CJK text (#2870) | fix, needs a port (conflicts with the fork) |
| `d1069b99` | 2026-10-31 | #2872 | fix: report effective embedding devices and document GPU support (#2872) | fix, needs a port (conflicts with the fork) |
| `0b3d5177` | 2026-10-31 | #2893 | fix: release embedder arena and freed heap after a mine (#2685) (#2893) | fix, needs a port (conflicts with the fork) |
| `ff4961dd` | 2026-10-31 | #2864 | fix(hooks): preserve valid filesystem path chars in shell stdin parsers (#2864) | fix, needs a port (conflicts with the fork) |
| `da6e21fb` | 2026-10-31 | #2867 | fix(palace): lock the palace for writers with a different HOME (#2867) | fix, needs a port (conflicts with the fork) |
| `4324f8b0` | 2026-10-31 | #2898 | fix: Stop hook diary checkpoint always fails on sqlite_exact backend: "attempt to write a readonly database" (#2898) | fix, needs a port (conflicts with the fork) |
| `663d8a12` | 2026-10-31 | #2605 | fix(kg): dedupe identical closed-interval add_triple replays (#2605) | fix, needs a port (conflicts with the fork) |
| `cd385723` | 2026-11-02 | #2914 | fix(llm): retry reasoning models without the parameters they reject (#2914) | fix, needs a port (conflicts with the fork) |
| `2da78c88` | 2026-11-04 | #2938 | fix: land Tier 1 release-blocker fixes before v3.12.0 (#2860) (#2938) | fix, needs a port (conflicts with the fork) |
| `f36b1b69` | 2026-11-04 | #2917 | fix(hooks): clean up convos mine PID slot and bound shutdown (#2917) | fix, needs a port (conflicts with the fork) |
| `deac3076` | 2026-11-04 |  | fix(codex): read response_item messages from newer Codex rollouts | fix, needs a port (conflicts with the fork) |
| `0bf5bc20` | 2026-11-05 |  | fix(miner): validate the wing from mempalace.yaml | fix, needs a port (conflicts with the fork) |
| `d00b94fb` | 2026-11-05 |  | fix(config): link a stored wing whose prefix only shows once slugged | fix, needs a port (conflicts with the fork) |
| `f8e35362` | 2026-11-06 | #2951 | fix(miner): validate room names from mempalace.yaml (#2951) | fix, needs a port (conflicts with the fork) |
| `3a097079` | 2026-11-08 | #2946 | fix(layers): fence stored memory in wake-up and Hermes prompts (#2946) | fix, needs a port (conflicts with the fork) |
| `be634bc7` | 2026-11-08 |  | fix(hooks): supervise mine shutdown outside the interpreter | fix, needs a port (conflicts with the fork) |
| `89d87d85` | 2026-11-08 |  | fix(codex): keep only user.text blocks when Codex tags message content | fix, needs a port (conflicts with the fork) |
| `cbba486d` | 2026-11-08 |  | fix(codex): read no response_item turns from sub-agent rollouts | fix, needs a port (conflicts with the fork) |
| `41a874de` | 2026-11-08 |  | fix(mcp): bind the HTTP server without a reverse DNS lookup | fix, needs a port (conflicts with the fork) |
| `87460ec7` | 2026-11-11 |  | fix: address three 3.12 release regressions | fix, needs a port (conflicts with the fork) |
| `3aa55394` | 2026-11-12 |  | fix(sidecar): keep newest-first pagination consistent | fix, needs a port (conflicts with the fork) |
| `53c46288` | 2026-10-31 | #2919 | Forward mempalace sync to the live hub (#2919) | forward, conflicts |
| `3de80464` | 2026-10-15 |  | perf(layers): wake up without importing the search stack | perf, needs a port (conflicts with the fork) |
| `6e2a0fda` | 2026-10-15 |  | perf(hooks): move Stop diary save off the foreground | perf, needs a port (conflicts with the fork) |
| `5ffa138a` | 2026-10-25 |  | perf(hallways): store hallways in SQLite so a mine touches only its wing | perf, needs a port (conflicts with the fork) |
| `6de95b04` | 2026-10-31 | #2745 | perf(backends): add iter_metadata to sqlite_exact/qdrant and Qdrant scroll pagination (#2745) | perf, needs a port (conflicts with the fork) |
| `cca1f4ab` | 2026-10-15 |  | refactor(mcp): derive tool capability sets from one registry | refactor, conflicts |
| `4d6f151c` | 2026-10-16 |  | refactor(backends): share one marker and vector path across remote backends | refactor, conflicts |
| `5d531fe7` | 2026-10-16 |  | refactor(mcp): keep tool capabilities in one import-free module | refactor, conflicts |
| `2e70bdc8` | 2026-10-16 |  | refactor(mcp): list_drawers builds its filter with build_where_filter | refactor, conflicts |
| `6d275357` | 2026-10-16 |  | refactor(mcp-light): dispatch palace_* through one allowlist per tool | refactor, conflicts |
| `4da9ee7d` | 2026-10-16 |  | refactor(mine): file every miner's drawers through one write-then-prune path | refactor, conflicts |
| `1a2b44a3` | 2026-10-17 |  | refactor(palace): purge what drawers leave behind through one drawers_removed helper | refactor, conflicts |
| `383d0e88` | 2026-10-17 |  | refactor(diary): rebuild a diary through the shared write-then-prune path | refactor, conflicts |
| `9f77b877` | 2026-10-17 |  | refactor: remove dedup.py, which deleted any drawer under 20 characters | refactor, conflicts |
| `2cd12648` | 2026-10-17 |  | refactor(hooks): one interpreter resolver for every hook wrapper | refactor, conflicts |
| `ee41fd81` | 2026-10-17 |  | refactor(hub): forward every hub call through one HubClient | refactor, conflicts |
| `4af367cc` | 2026-10-17 |  | refactor(fsio): let the publisher keep a non-private mode and replace a link | refactor, conflicts |
| `b708fe47` | 2026-10-17 |  | refactor: write every durable state file through fsio | refactor, conflicts |
| `cfa57ea5` | 2026-10-17 |  | refactor(backends): evaluate where filters through one shared _filters module | refactor, conflicts |
| `a84a9c72` | 2026-10-17 |  | refactor(llm): parse every LLM reply and send every model request through llm_client | refactor, conflicts |
| `759e876b` | 2026-10-17 |  | refactor: one wing slug for every producer, and diary and ingest share the project wing | refactor, conflicts |
| `d65f64ce` | 2026-10-17 |  | refactor(backends): validate every write batch through one helper | refactor, conflicts |
| `293b81c0` | 2026-10-17 |  | refactor(env): parse environment variables through one shared helper | refactor, conflicts |
| `616e7c38` | 2026-10-17 |  | refactor(chroma): move the raw-SQLite readers into backends/chroma_sqlite.py | refactor, conflicts |
| `37d04d37` | 2026-10-17 |  | refactor(paths): build every MemPalace state path through mempalace.paths | refactor, conflicts |
| `bf750e94` | 2026-10-17 |  | refactor(kg): resolve every knowledge graph path through paths.knowledge_graph_path | refactor, conflicts |
| `8693e7ce` | 2026-10-17 |  | refactor(timeutil): route persisted timestamp producers through timeutil | refactor, conflicts |
| `1cd72d26` | 2026-10-18 |  | refactor(logging): name module loggers by \_\_name\_\_ and share one warn-once helper | refactor, conflicts |
| `ea996233` | 2026-10-16 |  | fix(storage): gate the stored-text escape on its flag and decode every read path | same topic as 119505da (stored-text escape flag and read paths) |
| `951b14bf` | 2026-11-07 |  | fix: close five pre-release security gaps found in the 3.12 audit | security (five gaps from the 3.12 audit), mostly antigravity hooks plus core files; port the core-file parts with xreview. Owner: the sync maintainer, by 2026-11-22 |
| `1dc2ce4a` | 2026-11-08 |  | fix(security): close three gaps the review of the release blockers found | security follow-up to 951b14bf (fsio, logstream, antigravity); port with it. Owner: the sync maintainer, by 2026-11-22 |
| `96169415` | 2026-10-17 |  | fix: make graph reads write nothing of ours, and sanitize every search path | security-adjacent (sanitize every search path; graph reads write nothing); large port, with 8dd706ab. Owner: the sync maintainer, by 2026-11-22 |
| `8dd706ab` | 2026-10-17 |  | fix: honest graph errors, a sanitizer that says what it changed, typed bookkeeping metadata | security-adjacent (sanitizer, graph errors); a large port against the fork's mcp_server and palace modules. Owner: the sync maintainer, by 2026-11-22 |
| `119505da` | 2026-10-15 |  | fix(storage): escape NUL and lone surrogates reversibly | stored-text escape changes the stored format (pgvector included): the maintainers decide on a migration first |
| `072a332a` | 2026-10-17 |  | style(tests): sort the lexical import in test_closets | style |
| `662adc0c` | 2026-10-16 |  | test(format_miner): compare the stored mtime with the miner's 1 ms tolerance | test, conflicts |
| `d2c0d389` | 2026-10-16 |  | test(hub): fail when a search setting is read without a table row | test, conflicts |
| `fe8900df` | 2026-10-17 |  | test(palace): count mempalace_aaak among the allowed collections | test, conflicts |
| `83da7333` | 2026-10-17 |  | test(fsio): let the failing write_in_place fake take the binary keyword | test, conflicts |
| `d094a5a4` | 2026-10-17 |  | test(layers): give the wake-up budget test headroom on slow CI runners | test, conflicts |
| `4d8707ef` | 2026-10-17 |  | test(hooks): write the stub scripts without the 3.10-only write_text newline argument | test, conflicts |
| `a36e15ef` | 2026-10-30 | #2892 | test: the pathname2url surrogate boundary is 3.12, not 3.13 (#2892) | test, conflicts |
| `9006f9d0` | 2026-10-31 | #2912 | test: clear the developer's own MemPalace env for the test session (#2912) | test, conflicts |
| `37b2d300` | 2026-11-08 |  | test: let communicate() close the lock holder's stdin | test, conflicts |
| `b5bfa9a7` | 2026-11-08 |  | test: keep the suite off the resolver | test, conflicts |
| `4e950dfd` | 2026-11-08 |  | test: stop the suite waiting on the kernel and the resolver | test, conflicts |
| `45349c1e` | 2026-10-16 |  | test(hooks): pin PATH so the python3 fallback test can't pick up mempalace | test-only |
| `d5a01317` | 2026-10-16 |  | test(tunnels): a corrupt tunnels.json is kept aside, not overwritten | test-only |
| `3c3b0b0d` | 2026-10-16 |  | test(mine): pin what a re-mine leaves behind for one file on a real palace | test-only |
| `e03a200f` | 2026-10-17 |  | test: keep palace._DEFAULT_BACKEND the registry's chroma instance after a reset | test-only |
| `1747e5ca` | 2026-10-17 |  | test: pin what every drawer removal path takes with it | test-only |
| `5a22d238` | 2026-10-17 |  | test(diary): pin what a diary re-ingest leaves in a real palace | test-only |
| `f1ee8823` | 2026-10-30 | #2901 | test(embeddinggemma2): skip scipy-dependent audio tests when scipy is absent (#2901) | test-only |
| `9f94639a` | 2026-10-30 | #2896 | test(daemon): lock #2673 module-level retention contract (#2896) | test-only |
| `3c184ada` | 2026-10-30 | #2883 | test(mcp): pin #2748 coexisting case-variant wing rename (#2883) | test-only |
| `6bc21b3e` | 2026-10-15 |  | fix(cli): serve keeps the global --palace | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `d6e1a682` | 2026-10-16 |  | fix(pql): never store keywords that sit between content tokens | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `3d91f06a` | 2026-10-17 |  | feat(serve): require a bearer token on every HTTP bind by default | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `d0405ddd` | 2026-10-17 |  | fix(mcp): close the review gaps in argument checks, tool errors and token creation | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `909549e1` | 2026-10-17 |  | test(stray-bytes): serve the loopback hub tokenless now that the token is on by default | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `490c446f` | 2026-10-17 |  | fix(serve): keep a local-only loopback hub tokenless; require a token once it is reachable | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `2a11534e` | 2026-10-17 |  | fix(http): a tokenless hub refuses proxied and cross-origin requests, and /mcp takes only JSON | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `0741fd5a` | 2026-10-17 |  | fix(serve): refuse --no-token on a proxied hub, and widen the tokenless hub's browser and proxy checks | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `a6b97bd5` | 2026-10-17 |  | refactor(lexical): one tokenizer and BM25 scorer for the searcher and every backend | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `7597c4cf` | 2026-10-30 | #2889 | fix: raise the MCP HTTP listen backlog above socketserver's default of 5 (#2889) | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `2da2f82c` | 2026-11-08 |  | fix(llm): stop sending temperature to Anthropic and raise its token budget | the upstream HTTP hub (mempalace serve) auth: we serve through palace-daemon in-process; port as one topic with xreview |
| `55fe447b` | 2026-10-15 |  | fix(mcp-http): lock node profile and publish one integrity verdict | the upstream HTTP hub (mempalace serve); we serve through palace-daemon in-process. Port with the hub-auth topic |
| `569beda6` | 2026-10-15 |  | fix(migrate): keep closets, graph, and sidecars across a verified swap | upstream's backend-swap migrator; we migrate with migrate_to_postgres. Revisit if we adopt it |

## skip

| upstream | date | PR | subject | why |
|---|---|---|---|---|
| `0647c99e` | 2026-10-15 |  | fix(hermes): configured backend, write lock, core diary | benchmark or an integration we do not run |
| `4d6cb17e` | 2026-10-16 |  | fix(hermes): keep the newest diary entries and import the jsonl once | benchmark or an integration we do not run |
| `0f0a50a8` | 2026-10-17 |  | fix(rust): clear the clippy warnings and declare the 1.88 minimum | benchmark or an integration we do not run |
| `b0c6cdf5` | 2026-10-31 | #2874 | fix(benchmarks): normalize LongMemEval NDCG against the full corpus (#2874) | benchmark or an integration we do not run |
| `540a14e7` | 2026-10-31 | #2876 | fix(recall): also listen on agent/created, so memory reaches DSH 0.2.x sessions (#2876) | benchmark or an integration we do not run |
| `fca68e1c` | 2026-11-08 |  | test(dsh): wait on the hook queue instead of fixed ticks | benchmark or an integration we do not run |
| `c1e2cb98` | 2026-11-09 |  | test(dsh): wait on the wake-up reads instead of fixed ticks | benchmark or an integration we do not run |
| `dd827a88` | 2026-11-10 |  | test(antigravity): isolate XDG_CONFIG_HOME in hermetic Node hook tests | benchmark or an integration we do not run |
| `b6c029a9` | 2026-10-02 |  | ci: re-trigger checks for #2625 | docs/CI/changelog only: the fork keeps its own |
| `8f48f2e1` | 2026-10-15 |  | ci: read the milvus coverage config on coverage 7.10 | docs/CI/changelog only: the fork keeps its own |
| `a4bbadf4` | 2026-10-16 |  | chore(deps): bump actions/upload-artifact from 7.0.1 to 7.0.2 | docs/CI/changelog only: the fork keeps its own |
| `15fabf40` | 2026-10-16 |  | chore(deps): bump actions/download-artifact from 8.0.1 to 8.0.2 | docs/CI/changelog only: the fork keeps its own |
| `1a60c408` | 2026-10-16 |  | ci: run the test workflow on every pull request | docs/CI/changelog only: the fork keeps its own |
| `71394308` | 2026-10-16 |  | docs(changelog): keep the upgrade notes as one list | docs/CI/changelog only: the fork keeps its own |
| `98ac69ac` | 2026-10-16 |  | docs(changelog): note the closet boost surviving the hybrid rank | docs/CI/changelog only: the fork keeps its own |
| `bb1f2a1f` | 2026-10-17 |  | docs(changelog): note AAAK summaries leaving with their drawers | docs/CI/changelog only: the fork keeps its own |
| `9368be02` | 2026-10-17 |  | docs(changelog): note the max_seq_id repair taking the palace lease | docs/CI/changelog only: the fork keeps its own |
| `f9b19f97` | 2026-10-17 |  | docs(changelog): drop the superseded token-on-every-bind note | docs/CI/changelog only: the fork keeps its own |
| `9da2aa32` | 2026-10-17 |  | ci: fail the Rust job on clippy warnings | docs/CI/changelog only: the fork keeps its own |
| `c3376ea6` | 2026-10-17 |  | docs(changelog): note hooks no longer importing mempalace from the project | docs/CI/changelog only: the fork keeps its own |
| `ef4a22df` | 2026-10-18 |  | docs(changelog): cover every user-visible change since 3.11.0 | docs/CI/changelog only: the fork keeps its own |
| `f90cc8b2` | 2026-10-18 |  | docs(changelog): drop in-cycle fixes and correct entry claims after review | docs/CI/changelog only: the fork keeps its own |
| `534dd99b` | 2026-10-18 |  | docs(changelog): list the 3.12.0 release PR | docs/CI/changelog only: the fork keeps its own |
| `f74451af` | 2026-10-18 |  | docs(changelog): list #2861 in 3.12.0 | docs/CI/changelog only: the fork keeps its own |
| `476eb444` | 2026-10-31 | #2878 | chore(deps): bump pypa/gh-action-pypi-publish from 1.14.2 to 1.14.3 (#2878) | docs/CI/changelog only: the fork keeps its own |
| `4f7745af` | 2026-10-31 | #2920 | docs(changelog): fold Wave 1-3 3.12.0 fixes and PR list into [3.12.0] (#2920) | docs/CI/changelog only: the fork keeps its own |
| `c395c2c9` | 2026-11-06 |  | chore(deps): bump docker/login-action from 4.6.0 to 4.7.0 | docs/CI/changelog only: the fork keeps its own |
| `c83f683a` | 2026-11-08 |  | ci: build test palaces on the Windows runner's scratch disk | docs/CI/changelog only: the fork keeps its own |
| `14c0d409` | 2026-11-08 |  | docs(changelog): note the HTTP server bind without reverse DNS | docs/CI/changelog only: the fork keeps its own |
| `944989e4` | 2026-11-08 |  | ci: re-trigger the Tests run on the pull request head | docs/CI/changelog only: the fork keeps its own |
| `fa17a9dc` | 2026-11-08 |  | docs(changelog): fold Unreleased into 3.12.0 and date the release | docs/CI/changelog only: the fork keeps its own |
| `76046382` | 2026-11-08 |  | docs(changelog): fold the #2966 entry into 3.12.0 | docs/CI/changelog only: the fork keeps its own |
| `eeb92059` | 2026-10-17 |  | fix(repair): run the max_seq_id repair under the palace lease and hoist id-set rebuilds | max_seq_id is chroma bookkeeping (and qdrant); not on our path |
| `7da9bd19` | 2026-10-15 |  | fix(sqlite_exact): make retired-reader close behavior deterministic | sqlite_exact is an upstream-only backend |
| `4689d26d` | 2026-10-13 |  | fix(repair): gate the from-sqlite rebuild on FTS5's own integrity check | the from-sqlite rebuild is the retired chroma path; production is postgres |
| `f0a23f02` | 2026-10-14 | #2727 | perf(sqlite-exact): synchronize docs_fts.rowid with documents.rowid for O(log N) writes (#2727) | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `1df2027e` | 2026-10-17 |  | fix(embedding): keep a recorded dimension when the probe fails for the same model | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `1be5ceea` | 2026-10-17 |  | test(chroma): pin every raw-SQLite reader's output on one real palace | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `1f768ccf` | 2026-10-18 |  | perf(sqlite_exact): push $in/$eq filters and index filed_at for get_recent | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `5d6e4475` | 2026-10-31 | #2918 | fix(storage): use OFD locks on Linux even when fcntl lacks F_OFD_SETLK (#2918) | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `4d706a99` | 2026-10-31 | #2906 | fix(chroma): don't quarantine HNSW segments under a live writer (#2906) | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `0208c81d` | 2026-11-07 | #2897 | fix(chroma): quarantine HNSW metadata only after stability checks (#2897) | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `54dc8bf5` | 2026-11-09 |  | test(chroma): hand the write lock over explicitly in the contention test | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `076f987f` | 2026-11-11 |  | test: isolate Chroma warning probe from cleanup warnings | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |
| `ae6cbb3e` | 2026-11-12 |  | fix(chroma): keep recent ordering exact when SQLite reads fail | upstream-only backend (sqlite/chroma/milvus/qdrant/rust/remote): production is postgres+pgvector |

## Status of the picks

| topic | PR | merged | what |
|---|---|---|---|
| A pql | #627 | `2c0e8ceb` | 6450bc0c, 45bff470 |
| B mcp tools | #629 | `bd272759` | d4038612, 7bad1ecd |
| C daemon/hooks | #630 | `f7b742af` | 5528b8ac, f3dfae89, abe196f9 (tests ported to the `wing_` prefix); fork fixes from its review: an `isError` tool result fails the job, the first text block is read, nested worktrees collapse at the earliest marker |
| E miners | #631 | `0578bfce` | 9e013e9e, 6981b124, 466968ce, 34b742f3 |
| F kg | #632 | `2885dc2b` | e704a212 (SQLite knowledge graph ids only; the AGE graph keeps its own) |
| G pgvector | #633 | `f7813341` | 722e8fac |
| H llm consent | #634 | `ee49af2f` | 50c1f959: a non-local LLM endpoint needs `--accept-external-llm` or `MEMPALACE_ACCEPT_EXTERNAL_LLM=1` |
| I credentials | #636 | `51c96db9` | a port of 857fbec4: LLM and embedding errors never repeat an endpoint's text |

No daemon, hook or service was deployed by these merges; that is a separate step after review (the palace lane
deploys after the AMD close-out). The docs site is the exception: picks that re-rendered the API reference under
`website/` triggered deploy-docs on merge, which republished the site's API pages.
