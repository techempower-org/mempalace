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


## [2026-05-17]


### Added


- **mempalace_walk_palace MCP tool — agent walks the palace via AGE Cypher** ([`8022ecb`](https://github.com/jphein/mempalace/commit/8022ecb))
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


- **Backfill AGE graph from existing drawer table — restartable, checkpointed** ([`b3f0206`](https://github.com/jphein/mempalace/commit/b3f0206))
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


- **Wing/Room/Drawer hierarchy as native AGE nodes; Cypher MATCH walks palace structure** ([`ff583c0`](https://github.com/jphein/mempalace/commit/ff583c0))
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


- **Write-through middleware on PostgresCollection — entities populate AGE on every drawer write** ([`3321d83`](https://github.com/jphein/mempalace/commit/3321d83))
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


- **KnowledgeGraphAGE API parity with SQLite KG: add_entity, invalidate, query_entity, query_relationship, timeline, seed_from_entity_facts** ([`ff7187d`](https://github.com/jphein/mempalace/commit/ff7187d))
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


- **KnowledgeGraphAGE skeleton — Apache AGE graph bootstrap over psycopg2** ([`a3ee623`](https://github.com/jphein/mempalace/commit/a3ee623))
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


- **CI: gate postgres-backend tests against a pgvector service container** ([`da0bdbb`](https://github.com/jphein/mempalace/commit/da0bdbb))
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


- **PostgreSQL backend via #665 cherry-pick + fork-side adaptations + smoke tests** ([`5e90c72`](https://github.com/jphein/mempalace/commit/5e90c72))
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


- **README pivots to the four-layer model + Auto Dream as vindication of the verbatim-vs-derivative axis** ([`55b36ca`](https://github.com/jphein/mempalace/commit/55b36ca))
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


- **Defense-in-depth metadata sanitizer at the chromadb-client chokepoint** ([`f499814`](https://github.com/jphein/mempalace/commit/f499814))
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


- **Coerce empty + None metadata to sentinel in both rebuild paths** ([`949cb20`](https://github.com/jphein/mempalace/commit/949cb20))
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
  work originally landed both fixes as commit `848774c` on the
  `fix/repair-empty-metadata` branch (filed upstream as #1459 for
  the first slice); cherry-picked onto fork main as `949cb20` so
  both fixes are live on `jphein/mempalace` immediately.

  *Upstream:* [PR #1459](https://github.com/MemPalace/mempalace/pull/1459) (MERGED)
  *Files:* `mempalace/repair.py`


- **Route Stop/PreCompact hooks through palace-daemon/clients/hook.py** ([`42ded2e`](https://github.com/jphein/mempalace/commit/42ded2e))
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


- **Bulk pre-fetch already-mined set instead of N WHERE queries in mine_convos** ([`248854a`](https://github.com/jphein/mempalace/commit/248854a))
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


- **daemon-route `mempalace status` / `search` / `mine` when PALACE_DAEMON_URL is set** ([`22ef562`](https://github.com/jphein/mempalace/commit/22ef562))
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


- **daemon-route `mcp_server.py` via the `handle_request` JSON-RPC chokepoint** ([`41359ba`](https://github.com/jphein/mempalace/commit/41359ba))
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


- **`hook_verbatim_mode` config flag preserves system tags + full tool I/O during transcript ingest** ([`ef98961`](https://github.com/jphein/mempalace/commit/ef98961))
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
