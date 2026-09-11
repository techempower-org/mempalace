# Python API

Auto-generated reference for the `mempalace` Python package.
Source of truth lives in the docstrings under [`mempalace/`](https://github.com/techempower-org/mempalace/tree/main/mempalace) — edit there, not here. Regenerate with `scripts/render-api-docs.py`.

For task-oriented overviews of the main interfaces (search, memory stack, knowledge graph, palace graph, AAAK dialect, configuration), see [Python API Overview](/reference/python-api).

## Modules

### Top-level modules

- [`mempalace.auto_query`](./auto_query) — Auto-query integration for MemPalace.
- [`mempalace.auto_wake`](./auto_wake) — Wake-on-demand for a sleeping palace host.
- [`mempalace.backends`](./backends) — Storage backend implementations for MemPalace (RFC 001).
- [`mempalace.backfill_age`](./backfill_age) — Backfill the AGE graph from an existing drawer table.
- [`mempalace.backups`](./backups) — Writing and pruning palace backups.
- [`mempalace.calibration`](./calibration) — Calibrated confidence for search results.
- [`mempalace.cli`](./cli) — MemPalace — Give your AI a memory. No API key required.
- [`mempalace.closet_llm`](./closet_llm) — closet_llm.py — Generate closets via a user-configured LLM for richer indexing.
- [`mempalace.collision_scan`](./collision_scan) — Pre-mining defense against drawer_id collisions.
- [`mempalace.compact_recovery`](./compact_recovery) — Re-inject what a compaction took away (techempower-org/mempalace#449).
- [`mempalace.config`](./config) — MemPalace configuration system.
- [`mempalace.convo_miner`](./convo_miner) — convo_miner.py — Mine conversations into the palace.
- [`mempalace.convo_scanner`](./convo_scanner) — convo_scanner.py — Parse Claude Code conversation directories into ProjectInfo.
- [`mempalace.corpus_origin`](./corpus_origin) — corpus_origin.py — Detect whether a corpus is an AI-dialogue record and,
- [`mempalace.cross_encoder_rerank`](./cross_encoder_rerank) — Optional cross-encoder reranking for the retrieval path.
- [`mempalace.daemon`](./daemon) — Long-lived local daemon for queued MemPalace writes.
- [`mempalace.date_window`](./date_window) — Shared date-window parsing for read-path filters.
- [`mempalace.dedup`](./dedup) — dedup.py — Detect and remove near-duplicate drawers
- [`mempalace.dialect`](./dialect) — AAAK Dialect -- Structured Symbolic Summary Format
- [`mempalace.diary_ingest`](./diary_ingest) — diary_ingest.py — Ingest daily summary files into the palace.
- [`mempalace.dynamics`](./dynamics) — dynamics.py — Living-connection math for halls + tunnels.
- [`mempalace.embedding`](./embedding) — Embedding function factory with hardware acceleration.
- [`mempalace.encoding_repair`](./encoding_repair) — Safely repair high-confidence UTF-8 mojibake in MemPalace drawers.
- [`mempalace.entities`](./entities) — No-LLM structural entity extraction for the associative graph.
- [`mempalace.entity_detector`](./entity_detector) — entity_detector.py — Auto-detect people and projects from file content.
- [`mempalace.entity_registry`](./entity_registry) — entity_registry.py — Persistent personal entity registry for MemPalace.
- [`mempalace.exporter`](./exporter) — exporter.py — Export the palace as a browsable folder of markdown files.
- [`mempalace.fact_checker`](./fact_checker) — fact_checker.py — Verify text against known facts in the palace.
- [`mempalace.format_miner`](./format_miner) — format_miner.py — proposed for mempalace 3.3.6.
- [`mempalace.general_extractor`](./general_extractor) — general_extractor.py — Extract 5 types of memories from text.
- [`mempalace.hallway_store`](./hallway_store) — Hallway persistence — JSON file (default) or a postgres table (opt-in).
- [`mempalace.hallways`](./hallways) — Hallways — within-wing entity-to-entity connectors.
- [`mempalace.hlc`](./hlc) — hlc.py — Hybrid Logical Clock for RFC 004 op ordering
- [`mempalace.hook_shell`](./hook_shell) — Compatibility helpers for legacy shell hooks.
- [`mempalace.hooks_cli`](./hooks_cli) — Hook logic for MemPalace — Python implementation of session-start, stop, session-end, and precompact hooks.
- [`mempalace.hub_client`](./hub_client) — Small authenticated client for forwarding JSON-RPC to a live Palace hub.
- [`mempalace.ids`](./ids) — Centralized drawer/triple ID construction with collision-safe delimiter.
- [`mempalace.instructions_cli`](./instructions_cli) — Instruction text output for MemPalace CLI commands.
- [`mempalace.kg_canonical_vocab`](./kg_canonical_vocab) — Closed-vocabulary predicate mapping spike (issue #72).
- [`mempalace.kg_canonical_writepass`](./kg_canonical_writepass) — Guarded post-extraction canonical-mapping write pass (issue #72, approach a).
- [`mempalace.kg_llm_extractor`](./kg_llm_extractor) — LLM-based triple extractor for the async KG worker.
- [`mempalace.kg_predicate_norm`](./kg_predicate_norm) — Predicate normalization for the AGE knowledge graph (issue #50).
- [`mempalace.kg_triple_worker`](./kg_triple_worker) — Async worker that drains ``mempalace_kg_extraction_queue``.
- [`mempalace.kg_writethrough`](./kg_writethrough) — KG write-through hooks for PostgresCollection drawer writes.
- [`mempalace.knowledge_graph`](./knowledge_graph) — knowledge_graph.py — Temporal Entity-Relationship Graph for MemPalace
- [`mempalace.knowledge_graph_age`](./knowledge_graph_age) — AGE-backed implementation of KnowledgeGraph (Apache AGE on Postgres).
- [`mempalace.layers`](./layers) — layers.py — 4-Layer Memory Stack for mempalace
- [`mempalace.llm_client`](./llm_client) — llm_client.py — Minimal provider abstraction for LLM-assisted entity refinement.
- [`mempalace.llm_refine`](./llm_refine) — llm_refine.py — Optional LLM refinement of regex-detected entities.
- [`mempalace.logstream`](./logstream) — logstream.py — Agent coordination event log for MemPalace (RFC 003)
- [`mempalace.logsync`](./logsync) — logsync.py — Anti-entropy sync engine for the logstream (RFC 004 step 0)
- [`mempalace.mcp_proxy`](./mcp_proxy) — Thin stdio front end for the MemPalace MCP server.
- [`mempalace.mcp_server`](./mcp_server) — MemPalace MCP Server — read/write palace access for Claude Code
- [`mempalace.migrate`](./migrate) — mempalace migrate — Recover a palace created with a different ChromaDB version.
- [`mempalace.migrate_hallways`](./migrate_hallways) — Move ``hallways.json`` into the postgres hallway table (#442).
- [`mempalace.migrate_to_postgres`](./migrate_to_postgres) — ChromaDB → Postgres (pgvector + AGE) migration tool.
- [`mempalace.miner`](./miner) — miner.py — Files everything into the palace.
- [`mempalace.multi_encoder`](./multi_encoder) — Multi-encoder retrieval — query N encoder-bound palaces, RRF-fuse.
- [`mempalace.normalize`](./normalize) — normalize.py — Convert any chat export format to MemPalace transcript format.
- [`mempalace.novelty`](./novelty) — novelty.py — Gzip-based novelty scoring for drawers
- [`mempalace.novelty_wiring`](./novelty_wiring) — novelty_wiring.py — write-time novelty tagging
- [`mempalace.onboarding`](./onboarding) — onboarding.py — MemPalace first-run setup.
- [`mempalace.palace`](./palace) — palace.py — Shared palace operations.
- [`mempalace.palace_graph`](./palace_graph) — palace_graph.py — Graph traversal layer for MemPalace
- [`mempalace.palace_graph_age`](./palace_graph_age) — Palace structure (Wing → Room → Drawer) as native AGE graph nodes.
- [`mempalace.pending_queue`](./pending_queue) — Append-only journal for mine requests that couldn't reach the daemon.
- [`mempalace.project_scanner`](./project_scanner) — project_scanner.py — Detect projects and people from real signal.
- [`mempalace.provenance`](./provenance) — Source provenance and staleness for search hits.
- [`mempalace.query_sanitizer`](./query_sanitizer) — query_sanitizer.py — Mitigate system prompt contamination in search queries.
- [`mempalace.ratings`](./ratings) — Feedback ratings for search results (#159, Tier 1).
- [`mempalace.recency`](./recency) — Recency weighting for search results (#158).
- [`mempalace.repair`](./repair) — repair.py — Scan, prune corrupt entries, and rebuild HNSW index
- [`mempalace.replica`](./replica) — replica.py — Per-palace replica identity (RFC 004 transport seam / provenance)
- [`mempalace.room_detector_local`](./room_detector_local) — room_detector_local.py — Local setup, no API required.
- [`mempalace.room_taxonomy`](./room_taxonomy) — Canonical room taxonomy — soft-warn validation.
- [`mempalace.rrf`](./rrf) — Reciprocal Rank Fusion — combine ranked lists from N retrievers.
- [`mempalace.searcher`](./searcher) — searcher.py — Find anything. Exact words.
- [`mempalace.server_registry`](./server_registry) — Per-palace discovery registry for the MemPalace HTTP MCP hub.
- [`mempalace.service`](./service) — Shared service operations used by daemon-backed entry points.
- [`mempalace.sources`](./sources) — Source adapter subsystem (RFC 002).
- [`mempalace.spellcheck`](./spellcheck) — spellcheck.py — Spell-correct user messages before palace filing.
- [`mempalace.split_mega_files`](./split_mega_files) — split_mega_files.py — Split concatenated transcript files into per-session files
- [`mempalace.sweeper`](./sweeper) — sweeper.py — Message-granular miner that catches what the file-level
- [`mempalace.sync`](./sync) — sync.py — Gitignore-aware drawer prune (#1252).
- [`mempalace.tag_extraction`](./tag_extraction) — TF-IDF auto-tag extraction for drawer write time (#201).
- [`mempalace.tags`](./tags) — Multi-label tags for drawers (techempower-org/mempalace#39).
- [`mempalace.tasks`](./tasks) — High-level task envelopes shared by CLI and remote MCP clients.
- [`mempalace.transport`](./transport) — transport.py — The RFC 004 transport seam (Layer 1).
- [`mempalace.update_awareness`](./update_awareness) — Opt-in, cached release awareness without automatic installation.
- [`mempalace.version`](./version) — Single source of truth for the MemPalace package version.
- [`mempalace.wal`](./wal) — Side-effect-free write-ahead log for MemPalace write operations.
- [`mempalace.write_routing`](./write_routing) — Shared daemon-routing policy for MemPalace write callers.
- [`mempalace.write_sanitizer`](./write_sanitizer) — write_sanitizer.py — Observation-grade input hygiene for the write path (#40).

### auto_query

- [`mempalace.auto_query.decisions`](./auto_query/decisions) — Decision logger for the auto-query system.
- [`mempalace.auto_query.depth_cache`](./auto_query/depth_cache) — TTL cache for the periodic depth-refresh injection.
- [`mempalace.auto_query.formatter`](./auto_query/formatter) — Result formatter for the auto-query integration.
- [`mempalace.auto_query.injected`](./auto_query/injected) — Per-session record of drawers auto-query has already injected.
- [`mempalace.auto_query.router`](./auto_query/router) — Tool router for auto-query integration.
- [`mempalace.auto_query.runner`](./auto_query/runner) — Auto-query runner — chains signal extraction, routing, and formatting.
- [`mempalace.auto_query.signals`](./auto_query/signals) — Signal extraction for auto-query context classifier.

### backends

- [`mempalace.backends._sidecar`](./backends/_sidecar) — Shared embedder-identity sidecar (RFC 001).
- [`mempalace.backends.base`](./backends/base) — Storage backend contract for MemPalace (RFC 001).
- [`mempalace.backends.chroma`](./backends/chroma) — ChromaDB-backed MemPalace storage backend (RFC 001 reference implementation).
- [`mempalace.backends.embedding_wrapper`](./backends/embedding_wrapper) — Core-side embedding adapter for explicit-vector backends.
- [`mempalace.backends.milvus`](./backends/milvus) — Milvus backend for MemPalace.
- [`mempalace.backends.pgvector`](./backends/pgvector) — Postgres + pgvector backend for MemPalace.
- [`mempalace.backends.postgres`](./backends/postgres) — Optional PostgreSQL-backed MemPalace storage backend.
- [`mempalace.backends.qdrant`](./backends/qdrant) — Qdrant REST backend for MemPalace.
- [`mempalace.backends.registry`](./backends/registry) — Backend registry + entry-point discovery (RFC 001 §3).
- [`mempalace.backends.sqlite_exact`](./backends/sqlite_exact) — SQLite exact-vector backend for MemPalace.

### integrations

- [`mempalace.integrations.hermes`](./integrations/hermes) — MemPalace memory provider for Hermes.

### sources

- [`mempalace.sources.aider`](./sources/aider) — Aider source adapter (RFC 002).
- [`mempalace.sources.base`](./sources/base) — Source adapter contract for MemPalace (RFC 002).
- [`mempalace.sources.codex`](./sources/codex) — Codex CLI source adapter (RFC 002).
- [`mempalace.sources.context`](./sources/context) — ``PalaceContext`` facade passed to source adapters (RFC 002 §9).
- [`mempalace.sources.conversations`](./sources/conversations) — Conversation source adapter (RFC 002 §9).
- [`mempalace.sources.filesystem`](./sources/filesystem) — Filesystem source adapter (RFC 002 §9).
- [`mempalace.sources.gemini`](./sources/gemini) — Gemini CLI source adapter (RFC 002).
- [`mempalace.sources.opencode`](./sources/opencode) — OpenCode source adapter (RFC 002).
- [`mempalace.sources.registry`](./sources/registry) — Source adapter registry + entry-point discovery (RFC 002 §3).
- [`mempalace.sources.transforms`](./sources/transforms) — Reference implementations of the reserved content transformations (RFC 002 §1.4).
- [`mempalace.sources.warp`](./sources/warp) — Warp terminal source adapter.
