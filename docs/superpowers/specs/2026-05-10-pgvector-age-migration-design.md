# Postgres + pgvector + Apache AGE — substrate migration design

**Status:** Draft (spec, not yet implemented)
**Date:** 2026-05-10
**Authors:** JP + Claude session
**Target:** mempalace fork-side, with upstream contributions where the work is generally useful
**Composes on top of:**
- [#413](https://github.com/MemPalace/mempalace/pull/413) — `BaseCollection` backend seam (merged 2026-04-11)
- [#665](https://github.com/MemPalace/mempalace/pull/665) — `pgvector` / `pg_sorted_heap` backend (OPEN; primary anchor)
- [#574](https://github.com/MemPalace/mempalace/pull/574) — LanceDB substrate-swap + `mempalace migrate` ergonomics (OPEN; template for the migration tool shape)

## Context

mempalace's storage today is split across two layers that live independently:

1. **ChromaDB** — drawers (verbatim chunks), embeddings, metadata, closets. Lives at `~/.mempalace/palace/` (or in JP's deploy, `/mnt/raid/projects/mempalace-data/palace`).
2. **sqlite KG** — knowledge graph (subject/predicate/object triples with `valid_from` / `valid_to` / `source` properties). Lives alongside the ChromaDB directory.

Two stores, two backup paths, two failure modes, no atomic boundary between "I wrote drawer X" and "I added entity edge Y derived from drawer X." In production on the canonical 160K-drawer palace, this has caused real pain: HNSW drift incidents (forced the daemon-strict architecture in fork rows 33/34), divergent migration paths (the `.blob_seq_ids_migrated` marker class), and the operational complexity of having palace-daemon manage two file-based databases that are nominally independent but semantically coupled.

Upstream is moving toward a pluggable backend model — [#413](https://github.com/MemPalace/mempalace/pull/413) merged the `BaseCollection` ABC, and [RFC 001](https://github.com/MemPalace/mempalace/issues/737) is formalizing the contract. Several backend PRs are open: PostgreSQL ([#665](https://github.com/MemPalace/mempalace/pull/665)), LanceDB ([#574](https://github.com/MemPalace/mempalace/pull/574)), Qdrant ([#700](https://github.com/MemPalace/mempalace/pull/700)). #665 is the right substrate for the fork's daemon-deployed setup — Postgres is a mature, operationally familiar database, and pgvector is the standard production embedding-search extension.

Apache AGE adds a second extension on the same Postgres instance: a Cypher-queryable graph layer. Co-locating the KG with the drawer/vector store gives mempalace **one connection pool, one ACID boundary, one backup story** — and unlocks richer graph queries than the current sqlite KG can express.

This spec covers the full substrate swap: vectors via pgvector, KG via AGE, drawer rows and metadata via Postgres tables, all on a single Postgres instance.

## Goals

1. **Substrate consolidation.** One database (Postgres), two extensions (pgvector, AGE), one connection pool, one backup unit.
2. **Atomic write boundary.** A single transaction can write a drawer and add the KG entity edges derived from it. Today these are independent writes against independent stores.
3. **Composition with upstream.** Lean on #665's `mempalace.backends.postgres` for the storage half. Add only what's genuinely fork-side: the AGE-backed KG and the migration tool. File the AGE work as a fork-ahead row; pitch upstream after it earns trust in production.
4. **Restartable, idempotent migration.** Migration of the canonical 160K-drawer palace must be re-runnable without data loss. Phase-checkpointed; each phase reentry-safe.
5. **Practical rollback.** Backed-up ChromaDB palace directory remains intact through the cutover. If anything is wrong post-migration, "restart daemon on the backup" is a deterministic recovery path.
6. **Daemon-deployment-aware.** Cutover assumes palace-daemon is running on the host (per the [foundation-rework spec](https://github.com/jphein/familiar.realm.watch/blob/main/docs/superpowers/specs/2026-05-10-foundation-rework-design.md)). Stop daemon → migrate → restart daemon pointing at Postgres.

## Non-goals

- **Embedder pluggability.** Out of scope. v1 holds ONNX MiniLM-L6-v2 (384d) fixed; the dimension-mismatch guard from [#574](https://github.com/MemPalace/mempalace/pull/574) lands as a safety rail, but actually swapping embedder + reindex is a separate v2 design.
- **Rewriting the search algorithm.** The pgvector backend returns the same shape as ChromaDB returns; ranking, hybrid BM25 fallback (fork row 12 / PR #1005), L1 importance pre-filter (row 7 / PR #660), and closet reranking (row 29) all continue to work without modification.
- **Replacing palace-daemon's HTTP surface.** Endpoints stay the same shape. Internals call into the new backend; clients can't tell.
- **Backups via `pg_dump`.** Tracked in palace-daemon, not here. The daemon's `/backup` endpoint will need updating post-migration — separate work.
- **Concurrent (online) migration with dual-write.** Decided against in the brainstorming pass: complexity vs benefit doesn't pencil out for a single-user palace with a brief downtime budget.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ palace-daemon (HTTP :8085 on disks)                              │
│   GET /search, /context, /graph, ...                             │
│   POST /mine, /memory, /backup, ...                              │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│ mempalace.backends.postgres   (per upstream #665)                │
│   • BaseCollection conformance                                   │
│   • Connection pool (psycopg/asyncpg)                            │
│   • Batched ON CONFLICT inserts                                  │
│   • Lazy HNSW index creation                                     │
└──────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴──────────────┐
              ▼                              ▼
┌──────────────────────────┐   ┌──────────────────────────────────┐
│ mempalace.knowledge_     │   │ mempalace.backends.postgres      │
│   graph_age              │   │   (drawers / closets / meta)     │
│   • KnowledgeGraph ABC   │   │                                  │
│   • Cypher via AGE       │   │                                  │
└──────────────────────────┘   └──────────────────────────────────┘
              │                              │
              └──────────────┬───────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│ PostgreSQL 15+ (single instance)                                 │
│   ├── extension: pgvector  (vector(384) embeddings, HNSW)        │
│   └── extension: age       (graph mempalace_kg)                  │
└──────────────────────────────────────────────────────────────────┘
```

**Backend selection** (composing with #665's env-var / config shape):

| Selector | Default | Effect |
|----------|---------|--------|
| `MEMPALACE_BACKEND` | `chroma` | `chroma` or `postgres`. When `postgres`, also requires `MEMPALACE_POSTGRES_DSN`. |
| `MEMPALACE_POSTGRES_DSN` | — | `postgresql://palace:<pw>@host:port/dbname` |
| `MEMPALACE_KG_BACKEND` | `sqlite` | `sqlite` or `age`. `age` requires `MEMPALACE_BACKEND=postgres` (or palace_dsn explicitly). |

`backend` and `kg_backend` are also readable from `~/.mempalace/config.json` per the existing config-file shape (extends the same `MempalaceConfig` properties skuznetsov adds in #665).

## Schema

### Relational tables (`mempalace` schema)

#### `drawers`

```sql
CREATE TABLE mempalace.drawers (
    id              text PRIMARY KEY,
    collection      text NOT NULL,            -- 'mempalace_drawers' or 'mempalace_closets'
    document        text NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}',
    embedding       vector(384),              -- nullable: closets may not have one
    source_file     text,
    mtime           double precision,
    normalize_version integer,
    importance      integer DEFAULT 0
);

CREATE INDEX drawers_embedding_hnsw_idx ON mempalace.drawers
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX drawers_source_mtime_idx ON mempalace.drawers
    (collection, source_file, mtime);            -- backs bulk_check_mined() (fork row 1)

CREATE INDEX drawers_importance_idx ON mempalace.drawers
    (collection, importance) WHERE importance >= 3;   -- partial: backs L1 pre-filter (row 7 / PR #660)

CREATE INDEX drawers_metadata_gin_idx ON mempalace.drawers
    USING gin (metadata jsonb_path_ops);          -- wing/room/topic filters
```

#### `closets`

If the upstream #665 backend already stores closets in the same `drawers` table with `collection='mempalace_closets'`, we reuse that. Otherwise (and to match the current logical separation), a dedicated table:

```sql
CREATE TABLE mempalace.closets (
    id          text PRIMARY KEY,
    file_path   text NOT NULL,
    content     text NOT NULL,
    topic       text,
    metadata    jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX closets_file_path_idx ON mempalace.closets (file_path);
CREATE INDEX closets_topic_idx ON mempalace.closets (topic);
```

Decision deferred to the implementation plan; the migration tool handles either layout.

#### `backend_meta`

```sql
CREATE TABLE mempalace.backend_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);
```

Required keys at end of migration:

| Key | Example value | Purpose |
|-----|---------------|---------|
| `schema_version` | `1` | Bump on schema change; older mempalace refuses to open. |
| `embedding_model` | `MiniLM-L6-v2` | Dimension-mismatch guard target. |
| `embedding_dim` | `384` | Same. Reject reopens claiming a different dim. |
| `created_at` | `2026-05-10T01:00:00Z` | Provenance. |
| `migrated_from_chroma_at` | `2026-05-10T18:00:00Z` | Optional; present only after a migration. |
| `migration_phase_<n>` | `done` | Phase checkpoint for restartable migration. Removed at end. |

### AGE graph (`mempalace_kg`)

Initialized via `SELECT create_graph('mempalace_kg');` during phase 1.

**Vertex label** `Entity`:
```cypher
(:Entity {name: 'JP', kind: 'person', first_seen: '2026-04-...'})
```

**Edge label** `RELATION` (carries all current sqlite-KG columns as edge properties):
```cypher
(s:Entity)-[r:RELATION {
    relation_type: 'works_on',
    source:        'drawer_abc123',
    valid_from:    '2026-04-25',
    valid_to:      null,
    confidence:    0.9
}]->(o:Entity)
```

Why one edge label instead of one per relation type? Mempalace's KG today stores `relation_type` as a string column on a single `kg_triples` table — already homogeneous. Keeping AGE's edge label uniform preserves that simplicity; queries can filter on `r.relation_type` exactly as the current sqlite KG queries filter on `relation_type =`. A future redesign could shard relation types into separate edge labels for performance, but that's not needed today.

## Upstream alignment

| Surface | Source | Status | Our move |
|---------|--------|--------|----------|
| `BaseCollection` ABC | upstream [#413](https://github.com/MemPalace/mempalace/pull/413) | **merged** | Foundation — no work needed. |
| `pgvector` / `pg_sorted_heap` backend | upstream [#665](https://github.com/MemPalace/mempalace/pull/665) | OPEN, last update 2026-04-19 | Primary anchor. Comment on #665 after this spec lands. If #665 stalls beyond an acceptable window, fork-port it on a non-blocking schedule. |
| Migration CLI ergonomics | upstream [#574](https://github.com/MemPalace/mempalace/pull/574) | OPEN | Mirror the `mempalace migrate` flag shape and dimension-mismatch guard for our `migrate-to-postgres`. Comment on #574 documenting the parallel. |
| Embedder pluggability | upstream [#574](https://github.com/MemPalace/mempalace/pull/574) Phase 2 | OPEN | Out of scope for v1. Track for v2. |
| KG storage layer | none upstream | — | **Fork-side, novel.** `mempalace.knowledge_graph_age`. Earns its own RFC after it runs in production. |
| Migration tool | none upstream | — | **Fork-side initially.** `mempalace migrate-to-postgres`. Generalizes upstream once stable (the `migrate-from-X-to-Y` pattern composes — see #574's parallel). |

## Migration tool

New CLI subcommand: `mempalace migrate-to-postgres --from <chroma_palace_path> --to <postgres_dsn> [--batch-size 1000] [--resume] [--dry-run]`

### Phases

Each phase wrapped in a transaction. Phase checkpoint written to `backend_meta` on success. Re-run with `--resume` (or just rerunning the same command — `--resume` is the default behaviour when checkpoint rows exist) skips completed phases.

| # | Phase | Action | Idempotent? | Checkpoint key |
|---|-------|--------|-------------|----------------|
| 0 | **Preflight** | Connect to both source and target. Verify `pgvector` + `age` extensions are installed. Check that ChromaDB palace embedding-model/dim matches what we'll write into `backend_meta`. Check `PALACE_DAEMON_URL` is not reachable (refuses to proceed if daemon is responsive — would silently lose writes). | Yes (read-only checks) | — |
| 1 | **Schema** | `CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS age;` Create `drawers`, `closets`, `backend_meta` tables with `IF NOT EXISTS`. Create the AGE graph with `create_graph('mempalace_kg')` (guarded against rerun). Indexes deferred to phase 4 for bulk-load speed. | Yes (`IF NOT EXISTS` everywhere) | `migration_phase_schema` |
| 2 | **Drawers** | Stream `chromadb.col.get(include=["embeddings","documents","metadatas"])` in batches of `--batch-size`. Build `unnest(...)` arrays. One `INSERT ... SELECT FROM unnest(...) ON CONFLICT (id) DO NOTHING` per batch. Track running count in `backend_meta.migration_drawer_count` for visibility. | Yes (`ON CONFLICT DO NOTHING`) | `migration_phase_drawers` |
| 3 | **Closets** | Same shape as drawers, smaller cardinality. Usually one or two batches. | Yes | `migration_phase_closets` |
| 4 | **Indexes** | `CREATE INDEX CONCURRENTLY` for HNSW (embedding), BTree (collection, source_file, mtime), partial BTree (collection, importance) where importance >= 3, GIN on metadata. CONCURRENTLY so a crashed CREATE doesn't lock the table on retry. | Mostly (a crashed CONCURRENTLY leaves an INVALID index that must be DROPped first — the phase detects and drops invalid indexes before re-creating) | `migration_phase_indexes` |
| 5 | **KG → AGE** | Read `SELECT subject, predicate, object, relation_type, valid_from, valid_to, source, confidence FROM kg_triples` from the source sqlite KG. For each triple, emit Cypher: `MERGE (s:Entity {name: $subj}) MERGE (o:Entity {name: $obj}) CREATE (s)-[r:RELATION {relation_type: $rt, source: $src, valid_from: $vf, valid_to: $vt, confidence: $conf}]->(o)`. Batched per ~1000 triples. | Yes for entities (`MERGE`). Edges use `CREATE` not `MERGE` to preserve multi-edge semantics if upstream KG ever allowed duplicate triples — track via `backend_meta.migration_triple_count` so a re-run resumes from the offset, not from scratch. | `migration_phase_kg` |
| 6 | **Verify** | `SELECT count(*) FROM drawers WHERE collection = 'mempalace_drawers'` vs Chroma's count — must match. `MATCH ()-[r:RELATION]->() RETURN count(r)` vs sqlite triple count — must match. Sample-read 10 random drawers; assert document and metadata round-trip. Sample-read 5 random triples; assert property round-trip. | Yes (read-only) | `migration_phase_verify` |
| 7 | **Done** | Print cutover instructions (env vars to set, daemon command to run, smoke-test query to try). **Don't change config files automatically** — explicit user action makes the cutover a deliberate step, not a side effect. Clear all `migration_phase_*` checkpoints; keep `migrated_from_chroma_at`. | n/a (terminal) | `migration_done` |

### Crash safety

- Each phase transactional. Crash mid-phase → re-run picks up where it left off based on the `migration_*_count` watermark or `IF NOT EXISTS` semantics.
- Phase 2/3 use `ON CONFLICT DO NOTHING` — re-running over already-copied rows is a no-op, not an error.
- Phase 5's edge creation is the most fragile (no natural primary key on edges in AGE). The watermark count + a "skip first N triples" resume strategy avoids duplicates.
- Phase 4's `CONCURRENTLY` semantics need explicit handling: a crashed `CREATE INDEX CONCURRENTLY` leaves a `INVALID` index that must be dropped before retry. Phase implementation reads `pg_index.indisvalid` and drops invalid indexes before re-creating.

### Performance budget (target, not promise)

On the canonical 160K-drawer palace at the disks NVMe array:

| Phase | Estimated duration | Notes |
|-------|-------------------|-------|
| 0 — Preflight | <1s | Network only |
| 1 — Schema | <5s | Extension creation + a handful of `CREATE TABLE` |
| 2 — Drawers | 3-6 min | 160K rows × 384d embeddings ≈ 250 MB; bottleneck is embedding column transfer. Concurrent `unnest` inserts should land 5-10K rows/sec on NVMe. |
| 3 — Closets | <30s | Small cardinality |
| 4 — Indexes | 2-4 min | HNSW build is the dominant cost; CONCURRENTLY doesn't pause writes (but daemon is stopped anyway). |
| 5 — KG → AGE | 1-3 min | ~10K-50K triples expected; AGE Cypher MERGE is slower than raw SQL but bounded. |
| 6 — Verify | <30s | Three count queries + 15 sample reads |
| **Total** | **~7-15 min** | Will be benchmarked on a dry-run before the actual cutover. |

## Cutover

Manual, deliberate, documented in the migration tool's final output. The tool **does not modify config files** — the cutover is a sequence of operator steps:

1. Stop daemon: `sudo systemctl stop palace-daemon` (system unit per familiar agent's foundation-rework spec)
2. Verify daemon is stopped: `curl http://disks:8085/health` returns connection refused
3. Run: `mempalace migrate-to-postgres --from /mnt/raid/projects/mempalace-data/palace --to "postgresql://palace@disks/mempalace"`
4. Verify phase 6 output is clean (drawer counts match, triple counts match, sample round-trips pass)
5. Update palace-daemon's systemd unit `EnvironmentFile=` to add:
   ```
   MEMPALACE_BACKEND=postgres
   MEMPALACE_POSTGRES_DSN=postgresql://palace@disks/mempalace
   MEMPALACE_KG_BACKEND=age
   ```
6. `sudo systemctl daemon-reload && sudo systemctl start palace-daemon`
7. Smoke tests:
   - `curl http://disks:8085/health` returns 200
   - `curl 'http://disks:8085/search?q=<known-recent-content>'` returns expected drawers (proves vector path)
   - `curl http://disks:8085/graph` returns expected node/edge counts (proves AGE path)
8. Move (don't delete) old palace dir: `mv /mnt/raid/projects/mempalace-data/palace /mnt/raid/projects/mempalace-data/palace.chromadb-backup-2026-05-10`
9. Update fork CLAUDE.md: bump version line to note the substrate change; add a new fork-ahead row noting AGE-backed KG.

## Rollback

Three failure surfaces, three rollback paths:

| Failure point | Rollback |
|---------------|----------|
| **Migration phase fails** | Migration is restartable — re-run the same `mempalace migrate-to-postgres` command. The daemon is still stopped (precondition) and never started against Postgres, so no production writes hit the half-built Postgres palace. Zero data loss. |
| **Cutover smoke test fails** | `sudo systemctl stop palace-daemon`. Remove `MEMPALACE_BACKEND` etc. from the systemd unit (or revert via the file's git history if it's version-controlled). `mv /mnt/raid/projects/mempalace-data/palace.chromadb-backup-2026-05-10 /mnt/raid/projects/mempalace-data/palace`. `sudo systemctl start palace-daemon`. Back on ChromaDB; the half-built Postgres palace is preserved for analysis. |
| **Discovered weeks later** | The `.chromadb-backup-<date>` directory is preserved indefinitely (no automatic cleanup). Even if Postgres has accumulated weeks of writes, the rollback is "restart daemon on ChromaDB, lose the deltas, decide whether to migrate again later." Practical safety net for the case where post-migration brokenness only surfaces under specific load. |

The cleanup-old-backup decision is deliberately a separate human step weeks after the migration — never an automatic part of the tool.

## Testing

| Test surface | Approach |
|--------------|----------|
| `BaseCollection` conformance for the Postgres backend | Inherit upstream's shared conformance suite once it exists (per RFC 001 §4). Until then, mirror `tests/test_backends.py` shape against a Postgres fixture. |
| Migration tool | `tests/test_migrate_to_postgres.py`. Fixture ChromaDB palace (~50 drawers + ~20 KG triples) → ephemeral Postgres → run all 7 phases → assert counts, sample content / triple round-trip, and idempotency (run twice; expect no duplicates, no errors, same end-state). |
| AGE KG | `tests/test_knowledge_graph_age.py` — parallel to `tests/test_knowledge_graph.py`. Same test cases, two backends, identical expected behaviour. Includes the temporal-validation tests upstream landed in #1214/#1167/#1417 (v3.3.5). |
| Dimension-mismatch guard | One test (`test_postgres_dimension_mismatch_rejects_reopen`): open with `MiniLM-L6-v2` (384d), close, attempt reopen claiming `bge-base` (768d) → expect `RuntimeError` with the actionable message from #574. |
| Postgres infra in tests | `pytest-postgresql` (ephemeral instance per session, no Docker) for unit tests. `docker compose` Postgres + extensions for the integration / migration / benchmark suite. CI gets both. |
| Daemon-strict + Postgres | Re-run `tests/test_cli_daemon.py` + `tests/test_mcp_server_daemon.py` against a Postgres-backed daemon to confirm fork rows 33/34 routing is backend-agnostic. |
| Benchmark | Run the existing search-latency benchmark on a 160K-drawer Postgres palace and record results. The 1.3× pre/post token-tax convergence claim (CLAUDE.md row 23) should re-run cleanly because the corpus is the same; only the substrate changed. |

## Open questions and deferred work

1. **AGE compatibility with Postgres 17.** Apache AGE has historically lagged behind latest PG by 6-12 months. Needs a one-off compatibility check before commit. If AGE isn't ready for PG 17, the spec stays on PG 15/16 and bumps the floor only when AGE catches up.
2. **Embedder pluggability** — see Non-goals. v2 design after substrate swap stabilises.
3. **Multi-collection layout** — v1 covers `mempalace_drawers` + `mempalace_closets`. Future collection types add new values of the `collection` text column; no schema migration needed. If a collection ever needs its own indexing strategy, the `collection` filter on the partial index can scale to per-collection partials.
4. **Backup story** — daemon's `/backup` endpoint currently snapshots the ChromaDB dir as a tarball. Post-migration it should run `pg_dump --format=custom` (or a logical-replication snapshot via `pg_basebackup`). **Out of scope for this spec; addressed in palace-daemon, not mempalace.** Tracked as a follow-on palace-daemon issue.
5. **Sync (mempalace#1421 gitignore-aware prune)** — works the same against either backend; the prune is a DELETE-by-source_file. No design changes needed, but verify the migration tool preserves the `source_file` column metadata cleanly so sync's mtime comparisons still find the right rows.
6. **The fork-only `bulk_check_mined()` (row 1)** — depends on `(collection, source_file, mtime)` index, already in the schema. Verify the existing test suite passes against Postgres backend with no code changes.

## Risks

- **AGE production maturity.** AGE has a smaller community than pgvector. Risk: a Cypher query we depend on regresses in an AGE point release. Mitigation: pin AGE version in the deploy; the migration tool records the AGE version in `backend_meta` at migration time.
- **Connection pool exhaustion.** Daemon + migration tool + manual `psql` sessions could collectively exceed Postgres `max_connections`. Mitigation: migration tool uses an explicit connection limit (default 4); daemon's pool size documented in palace-daemon deploy notes.
- **HNSW build time on cold start.** First-ever index build on a fresh 160K-row table is the expensive part of phase 4. Mitigation: documented in the cutover downtime budget; not a recurring cost after initial migration.
- **AGE Cypher injection.** AGE accepts Cypher as a quoted string inside SQL. Naive string interpolation is injection-vulnerable. Mitigation: every Cypher statement built via parameterized binding (no f-strings, no string concatenation with user data); reviewed in implementation plan.

## Composition with palace-daemon

palace-daemon (rboarescu/palace-daemon) is the HTTP-facing service that mempalace runs behind for fork-side deploys. Post-migration, the daemon's internal mempalace calls go through the new Postgres backend transparently — no daemon code changes required, because the substrate swap happens below the `BaseCollection` ABC.

Two palace-daemon-side items that pair with this spec but are NOT part of it:

1. **`/backup` endpoint** must learn to call `pg_dump` instead of tarballing the ChromaDB directory. Tracked separately.
2. **`/health` endpoint** should include Postgres reachability — already does HTTP-level health; should grow a Postgres-extension health check (`SELECT 1 FROM pg_extension WHERE extname IN ('vector', 'age')`) so a misconfigured deploy surfaces fast.

The daemon-strict architecture in fork rows 33/34 (mempalace's `mcp_server.handle_request` + `cli.cmd_{status,search,mine}` both routing to the daemon when `PALACE_DAEMON_URL` is set) is backend-agnostic — it forwards JSON-RPC to the daemon's `/mcp` proxy and never opens a local chromadb client in strict mode. The same forwarding logic works identically over Postgres.

## Sequence

The order matters because each step builds on the previous:

1. **Comment on upstream PRs** (after this spec is merged to fork main): #665 (we're building on top, here's the AGE layer + migration plan), #574 (we're mirroring your `mempalace migrate` shape and dimension-mismatch guard).
2. **Wait for / help land #665**, OR fork-port if it stalls beyond a reasonable window. Either way, end-state is `mempalace.backends.postgres` available in the fork's main.
3. **Implement `mempalace.knowledge_graph_age`** — the AGE-backed KG. Parallel test suite to the existing sqlite KG. Selectable via `MEMPALACE_KG_BACKEND=age`. Fork-side initially; pitch upstream after a few weeks of production use.
4. **Implement `mempalace migrate-to-postgres`** — the migration tool. Phases per the table above. Restartable, idempotent. Comprehensive test suite.
5. **Dry-run on a copy of the canonical palace** — verify phase 6 counts match, sample reads work, daemon-strict still routes correctly with the new backend.
6. **Cutover on the production palace** — stop daemon, migrate, restart daemon pointing at Postgres, verify, move backup aside.
7. **Update fork CLAUDE.md** — bump version line, add fork-ahead row for AGE-backed KG.
8. **Post-cutover observability** — run the existing benchmark suite against the Postgres palace; record any regressions or improvements vs the pre-migration ChromaDB baseline.

The implementation plan (next document, per the writing-plans skill) will decompose steps 3-4 into ordered tasks with file paths and test coverage targets.
