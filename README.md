# MemPalace (techempower-org fork)

**TechEmpower's production fork of [MemPalace/mempalace](https://github.com/MemPalace/mempalace)** (transferred from `jphein/mempalace` in May 2026)

[![version-shield](https://img.shields.io/badge/version-3.3.5-4dc9f6?style=flat-square&labelColor=0a0e14)](https://github.com/techempower-org/mempalace/releases) [![upstream-shield](https://img.shields.io/badge/upstream-3.3.5-7dd8f8?style=flat-square&labelColor=0a0e14)](https://github.com/MemPalace/mempalace/releases)
[![python-shield](https://img.shields.io/badge/python-3.9+-7dd8f8?style=flat-square&labelColor=0a0e14&logo=python&logoColor=7dd8f8)](https://www.python.org/)
[![license-shield](https://img.shields.io/badge/license-MIT-b0e8ff?style=flat-square&labelColor=0a0e14)](LICENSE)

---

This fork tracks `upstream/develop` through the 2026-05-10 sync (commit `a67be3f`, fork merge via `chore/sync-develop-2026-05-10`) and runs in production on a **273K-drawer Postgres + pgvector + Apache AGE palace** behind [palace-daemon](https://github.com/techempower-org/palace-daemon) on `disks:8085`. It carries ~434 fork-ahead changes that compose with — not replace — bensig's release direction; the v3.3.5 release (2026-05-10) includes our co-authored `_get_collection` retry-once via upstream #1377. ~1850 tests pass on `main`. The new things here are *what we've learned*, not just what we've fixed.

**2026-05-15 substrate cutover complete.** What was on the `feat/pgvector-age-impl` branch is now on `main` and serving production traffic. **Hybrid retrieval** (`candidate_strategy="hybrid"` — vector ∪ postgres-`tsvector` BM25 ∪ AGE graph-expanded candidates, hybrid-reranked) shipped end-to-end through `palace-daemon`'s new `/search/keyword` and `/search/hybrid` endpoints. **8 of 9 bench suites pass** on the postgres backend (`test_chromadb_stress` skipped, doesn't apply) — no regressions on the search heap hot path vs the chromadb-era numbers. See [`docs/operators/pgvector-cutover-runbook.md`](docs/operators/pgvector-cutover-runbook.md) for the operator narrative and [`docs/benchmarks/2026-05-15-remaining-benches.json`](docs/benchmarks/2026-05-15-remaining-benches.json) for the bench artifact.

## The four layers

Agent memory architecture has stratified into four orthogonal layers over the last six months. The cleanest way to read the field — and the cleanest way to read what this fork is doing — is to treat them as independently improvable, because the empirical evidence is that improvements stack rather than substitute.

1. **Storage.** Verbatim drawers; no LLM in the index path. ChromaDB upstream, with this fork running [Postgres + pgvector + Apache AGE](#substrate-postgres--pgvector--apache-age) **on `main` as of 2026-05-15** — the chromadb-era code path still works (`MEMPALACE_BACKEND=chroma`), but production traffic flows through pgvector + AGE now. Convergent across five 2026-vintage systems (MemPalace, Longhand, Celiums, mcp-memory-service, engram) — the verbatim-first cluster reached independent critical mass in April.
2. **Encoder.** The embedding model itself, normally invisible in architectural debates because most systems treat it as a vendor decision. @nakata-app's [adaptmem](https://github.com/nakata-app/adaptmem) ([discussion #1249](https://github.com/MemPalace/mempalace/discussions/1249)) shows contrastive fine-tuning on hard negatives lifts retrieval orthogonally: MemPalace raw 0.966 R@5 → +FT-300 0.980 → +hybrid_v4 0.990 R@5 / 0.916 R@1. Independent reproduction of MemPalace's published number (0.966 matches exactly), then orthogonal lift stacked on top. The encoder layer is the most under-explored across OSS memory systems.
3. **Retrieval.** How vectors are queried, combined, reranked. Three patterns co-exist and are not mutually exclusive: deterministic pipelines ([Familiar](https://github.com/jphein/familiar.realm.watch) v0.3.9 hits **78.33%** recall on the jp-realm-v0.1 corpus), LLM-orchestrated retrieval ([RLM](https://github.com/alexzhang13/rlm) with Qwen 2.5 7B and Llama 3.3 70B both **ceiling at 46.67%** — model size doesn't fix it), parallel hybrid ([Hindsight](https://hindsight.vectorize.io/blog/2026/03/27/parallel-hybrid-search)'s 91% R@10 via four-way RRF). The 32-point gap between Familiar and RLM is entirely invocation: RLM agents zero-call the tool on 22–25 of 30 questions; Familiar's pipeline runs on all 30 because the agent doesn't choose.
4. **Consumption.** What happens after retrieval. This is where the R@k → end-to-end QA gap lives — MemPalace's 96.6% R@5 → 82.6% E2E QA on Issue #39 reproduction; @terrizoaguimor's [Celiums](https://celiums.ai/) benchmarking shows 100% retrieval rate but only 62.3% QA with Opus 4.6. Three architectural bets compete: algorithm (Familiar's always-on pipeline), human-in-the-loop ([Kostadis's retrieve/render isolation](https://github.com/kostadis/CampaignGenerator/blob/main/docs/rlm_paper_comparison.md) CI invariant), trained policy ([Kent's APO recall games](https://github.com/kenchambers/kent)). The right answer probably depends on workload; the field doesn't yet know.

*Calibration on the SME numbers:* 30 questions, beta-level instrumentation, substring-on-filename scoring. The defensible findings are deltas under identical conditions; absolute percentages are decoration. Methodology disclosure and the broader four-layer synthesis live in [`docs/research/three-patterns-for-agent-memory.md`](docs/research/three-patterns-for-agent-memory.md) and the [compass artifact](docs/research/compass_artifact_wf-ad108fcc-3960-4eab-ad5d-234bf365b2f4_text_markdown.md). The shape of the result — invocation ceiling — generalizes; the exact numbers do not.

The three operational principles in [the thesis below](#the-thesis-operational-principles) are claims about layer 1 (verbatim storage) and how retrieval over it actually fails in production. They follow from this four-layer model; they aren't all of it.

## The thesis (operational principles)

The fork has converged on three principles. Treat them as the design test for future work over the verbatim layer.

### 1. Verbatim vs. derivative is the canonical axis

The unit of memory in MemPalace is the verbatim utterance — chats, tool calls, mined files, the literal text the user produced or witnessed. Anything else (summaries, KG triples, agent journals, AAAK-encoded reflections, Auto-Dream consolidated indices) is *derivative* of that verbatim record. Derivative writes are useful but they are a different kind of thing: their right access pattern is event-shaped (session_id, time, agent) or scoped (cwd, project), not unconditioned semantic similarity over the whole corpus.

Most public AI memory systems frame the problem the other way around: ingest raw, transform on write, store the derivative as canonical. Mem0 extracts "memories." Zep and Letta tier and summarize. Cognee builds a knowledge graph. Hindsight retains/recalls/reflects with LLM-extracted facts. In each, the verbatim original is gone — or at best, retrievable only through a layer of inference that already lost nuance. The fork's bet is the inverse: keep verbatim canonical, key derivative layers for their actual access pattern, and treat any derivative store as rebuildable from the verbatim. Derivative layers can then be replaced or re-derived without losing underlying truth. The April-2026 verbatim cohort (Longhand, Celiums, mcp-memory-service, MemPalace) converged on this within ~8 days of each other; the timing is suggestive.

Mixing verbatim and derivative in one corpus is a real failure mode — the structural fix in May 2026 was to drop one half of an earlier split entirely (see [What this fork has learned](#what-this-fork-has-learned)). Future derivative layers can still live in sibling collections keyed for their access pattern — but only if and when each one earns its own MCP read surface. Without that, a side collection becomes invisible to search.

This axis is implicit in upstream's [RFC 001](https://github.com/MemPalace/mempalace/pull/743) (`get_collection(palace, collection_name=...)` already supports it) but isn't yet named in the spec. Worth making explicit upstream — multi-collection-by-purpose is the architectural move that future backends should plan for, and the substrate work below depends on it.

### 2. Corpus shape eats retrieval algorithm for breakfast

A week of filter tuning, BM25 fallback, and over-fetch parameters could not make `kind=content` return more than 3 tokens per question on the canonical palace. ~640 Stop-hook auto-save checkpoint drawers — 0.4% of the corpus — dominated 80%+ of every vector top-N because they were short, query-term-saturated, and embedded close to recent prompts. Recall@5 was 0.984 the whole time. End-to-end answer quality collapsed.

Then we moved them out of the corpus. One structural change — a separate ChromaDB collection — and `kind=content` jumped to 1,267 tokens per question. The fix that retired the checkpoint split in May 2026 went further in the same direction: stop writing the derivative half at all. The lesson is durable: when corpus shape is wrong, no amount of post-filter cleverness substitutes for fixing the corpus.

This generalizes to every retrieval system that ingests by default and filters by query. Solve it at write time, by purpose, not at query time, by predicate.

### 3. The right to measure is the local-first benefit

The usual case for local AI memory is data sovereignty. The deeper benefit is *the right to audit your own integration shape*. Cat 9 in the SME framework — "the Handshake" — names a class of failure that recall benchmarks miss: the gap between retrieval working and the model actually being grounded on the retrieved content. We could only measure it because we own every layer of the stack. A vendor product would have shown us 0.984 R@5 on a dashboard and called it a day.

The SME jp-realm-v0.1 numbers in the four-layer section above are this principle made operational. The 46.67% / 78.33% delta is *invocation discipline*, not retrieval quality — a distinction no offline-only benchmark catches. The 96.6% R@5 → 82.6% E2E QA gap on independent reproduction is the same story at a different layer. Sovereignty wins arguments; auditability wins debugging sessions. The TechEmpower bridge essay at [`notebook/essays/2026-04-25-techempower-bridge.md`](https://github.com/jphein/notebook/blob/main/essays/2026-04-25-techempower-bridge.md) develops the underlying claim further.

## What this fork has learned

Four claims that fall out of the thesis when you take it seriously and run it in production for a few months.

**Corpus shape is not a tuning parameter; it's an architectural choice.** The 2026-04-25 → 2026-04-26 collection split closed a 210× pre/post token gap that no amount of `kind=` filtering, over-fetch tuning, or BM25 fallback had touched. Three weeks later, on 2026-05-05, the structural shift went one step further: drop the derivative half entirely. Hooks now write only verbatim transcript chunks; the dedicated `mempalace_session_recovery` collection and its read tool retired. One collection, one search path, no kind-filter, no over-fetch hack. The story of those three weeks is the empirical case that retrieval algorithms have less leverage over end-to-end quality than the shape of what you ingest; when the corpus is wrong-shaped, you don't filter your way out — you split, then if the split was a workaround for the wrong write contract, you stop writing the derivative half.

**Verbatim storage is load-bearing as the canonical layer.** Derivative work (KG, summaries, decay scores, embeddings under different models, Auto-Dream consolidated indices) is welcome as long as it stays *next to* the verbatim record, not replacing it. The integrity of every downstream layer depends on being able to re-derive from the original — drop the original and every layer above it is fragile. This is also the architectural read on Anthropic's [Dreams API](#two-memory-layers) below: the API design treats input as read-only and produces a *separate* output store you can diff and discard. That's the verbatim-vs-derivative axis showing up in a vendor API design, not just in this fork's writeups.

**The right to measure is the local-first benefit that matters in production.** Sovereignty wins arguments; auditability wins debugging sessions. Cat 9 / The Handshake on this fork's deployment was findable because we own every layer of the stack — a vendor product would have shown 0.984 R@5 on a dashboard and called it shipped.

**The integration gap (Cat 9 / Handshake) is real, reproducible, and measurable.** Engram-2's "17% E2E QA" claim landed on a real failure surface — checkpoint domination of vector top-N — and the structural fix demonstrably closes it on this corpus. The 632/3 → 974/1267 token convergence is the structural-fix proxy; the end-to-end LongMemEval run on the post-migration palace is instrumented, with results to publish at `notebook/data/cat9-postmigrate-e2e/REPORT.md`.

Underneath all four, the operational work that doesn't make headlines is still mostly the two hard things — **naming** (wing/room/topic taxonomies, the verbatim-vs-derivative split was itself a naming clarification, multi-label tags, embedding-model identity across collections) and **cache invalidation** (HNSW staleness detection, graph-cache write-invalidation, the `kind=` filter that went inert post-split, decay/recency weighting, stale auto-loaded docs, the `.blob_seq_ids_migrated` marker). Karlton's joke is durable for a reason: every retrieval system eventually has to engineer good answers to both, and this one is no exception. The four-layer model and the operational thesis are the parts of the work that generalize; the two-hard-things are the part that keeps showing up on every PR.

## Convergence with peer systems

The strongest defense of the fork's architectural choices in 2026 isn't anything this fork wrote — it's that three other production-shaped systems built on the MemPalace substrate independently arrived at compatible designs. Different domains, different teams, different downstream consumers, same four agreements at the architectural level.

- **[Familiar](https://github.com/jphein/familiar.realm.watch)** (jphein) — deterministic retrieval pipeline running local Gemma against MemPalace. Rerank, temporal decay, extractive compression, grounding directives, all running unconditionally. Measured: 78.33% recall on jp-realm-v0.1.
- **[CampaignGenerator](https://github.com/kostadis/CampaignGenerator)** (@kostadis, with kostadis/mempalace fork) — RPG session-prep over a 2 TB local PDF library + 5etools JSON. Rank-bucketed AAAK projections enable hierarchical pruning before drawer-level vector search runs. Measured: **19.82× cost reduction at 0% recall@10 loss** on a 281-entry benchmark fixture. Articulates the *why* of deterministic intermediate compression more clearly than the fork itself does (drift + recall-loss-at-prune-step failure modes when LLMs do intermediate compression).
- **[Kent](https://github.com/kenchambers/kent)** (@kenchambers) — typed async agent runtime with APO (Automatic Prompt Optimization via Microsoft Agent Lightning) training memory invocation policies. Recall games (recall@k / scope / closet fidelity / tunnel utility). Heartbeat agent for between-conversation memory maintenance. Channel-as-wing automatic routing. Measured: APO Round 01 drawer-aware queries average 0.323 embedding similarity vs 0.027 for unrelated (3/3 pairwise wins).
- **[adaptmem](https://github.com/nakata-app/adaptmem)** (@nakata-app) — sits at the encoder layer, not the consumption layer; orthogonal lift across retrieval modes; clean independent reproduction of MemPalace's published 0.966 R@5 number via monkey-patch encoder swap on `longmemeval_bench.py`.

The four agreements across these systems: (1) verbatim storage as the base layer, (2) no LLM in the index path, (3) wings as scope routing rather than required classification, (4) the consumption problem is real and not solved by retrieval quality. The divergence — where intelligence above retrieval lives — is where the field's debate actually is, and it's an interesting place to be wrong in different directions. The three-way (Familiar / CampaignGenerator / Kent) comparison plus the adaptmem orthogonality finding live at [`docs/research/three-mempalace-consumers.md`](docs/research/three-mempalace-consumers.md) and [`docs/research/adaptmem-orthogonal-layers.md`](docs/research/adaptmem-orthogonal-layers.md).

## Why this fork exists

We surveyed the memory-system landscape in April 2026 and found no verbatim-first local system with MCP. Every alternative transforms content on write — extracted facts, knowledge graphs, tiered summaries — losing the original text.

| System | Verbatim? | Local? | MCP? | First public | Notes |
|---|---|---|---|---|---|
| **MemPalace** | Yes | Yes | Yes | 2026-04-06 (v3.0.0) | What we have. ~160K drawers in production. Verbatim drawers + wings/rooms scope + SQLite KG + BM25/vector hybrid search. |
| [Longhand](https://glama.ai/mcp/servers/Wynelson94/longhand) | Yes | Yes | Yes, 17-tool MCP | 2026-04-14 (v0.5.5) | Closest cousin. Claude Code-specific — reads `~/.claude/projects/*.jsonl` directly. SQLite (raw JSON per event) + ChromaDB (embeddings of pre-computed "episodes"). Deterministic file-state replay via stored diffs. |
| [Celiums](https://celiums.ai/) | Yes | Yes (SQLite, Docker, or DO) | Yes, 6-tool MCP | 2026-04-08 (repo) | Stores full module text with PAD emotional vectors, importance scores, and circadian metadata. Bundles a 500K+ expert-module knowledge base alongside personal memory — different product shape. |
| [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) | Yes by default (opt-in consolidation) | Yes (SQLite) or Cloudflare Workers | Yes | 2024-12-26 | The long-standing verbatim option. Turn-level storage; MiniLM embeddings local. Targets LangGraph / CrewAI / AutoGen plus Claude. |
| [Hindsight](https://github.com/vectorize-io/hindsight) | No — LLM extracts facts | Yes (Docker) | Yes | 2026-01-05 | Three ops: retain / recall / reflect. Original text is lost. |
| [Mem0](https://github.com/mem0ai/mem0) / [OpenMemory](https://github.com/mem0ai/mem0/tree/main/openmemory) | No — extracts "memories" | Partial | Yes | 2023-06 | Cloud-first; OpenMemory is local-mode sibling. |
| [Cognee](https://github.com/topoteretes/cognee) | No — knowledge graph | Yes | Yes | 2023-08 | "Knowledge Engine" via ECL pipeline. |
| [Letta](https://github.com/letta-ai/letta) | No — tiered summarization | Yes | No | 2023-10 (as MemGPT) | Rebrand kept the repo. |
| [engram](https://github.com/NickCirv/engram) | Structured fields, not raw | Yes | Yes | 2026-04-11 | Go + SQLite FTS5. |
| [CaviraOSS OpenMemory](https://github.com/CaviraOSS/OpenMemory) | No — temporal graph | Yes | Yes | 2025-10-26 | SQL-native. |

The April-2026 verbatim cluster (MemPalace, Celiums, Longhand, engram all within ~8 days) is striking — it suggests the "store it raw and retrieve well" pattern reached independent critical mass right around the same time. The differentiator: **verbatim storage is the foundation; everything else (tags, KG, decay, summaries, consolidated indices) is enrichment layered on top.** If any layer fails or needs rebuilding, the underlying truth is still there. The same architectural call has been winning in observability for a decade — Grafana Loki's verbatim-event store, with the recent [Kafka rearchitect](https://www.infoq.com/news/2026/04/grafana-loki-ai-agents/) (10× faster aggregated queries, 20× less data scanned), is what mature verbatim-first systems eventually do under scale pressure — useful precedent for the substrate section below.

## Substrate: Postgres + pgvector + Apache AGE

*Status (2026-05-15): **shipped on `main`** and serving production traffic. Postgres backend cherry-picked from upstream [#665](https://github.com/MemPalace/mempalace/pull/665) (skuznetsov's PostgreSQL backend on the RFC 001 `BaseBackend` contract) plus our pgvector lazy-index race fix (commit `4566f8a`, also posted to #665 as an operator follow-up). Live on `disks.jphe.in:5433` running PG16 + pgvector 0.8.2 + AGE 1.6.0 via `apache/age:release_PG16_1.6.0` + `postgresql-16-pgvector`. **273K-drawer palace.** Hybrid retrieval (vector ∪ tsvector BM25 ∪ AGE graph) shipped end-to-end as `candidate_strategy="hybrid"`, exposed via `palace-daemon`'s `/search/keyword` and `/search/hybrid` endpoints. 8/9 mempalace bench suites pass on postgres backend ([`docs/benchmarks/2026-05-15-remaining-benches.json`](docs/benchmarks/2026-05-15-remaining-benches.json)). Original WAIT-for-#665-upstream-merge stance superseded; the cutover happened on our timeline. Full operator narrative at [`docs/operators/pgvector-cutover-runbook.md`](docs/operators/pgvector-cutover-runbook.md).*

This is composition, not a fork-led architectural shift: `BaseBackend` + `BaseCollection` + `PalaceRef` + the entry-point registry already live in upstream develop at [`mempalace/backends/`](https://github.com/MemPalace/mempalace/tree/develop/mempalace/backends), explicitly designed so third-party backends register via Python entry points without touching core. The architectural decision was upstream's; the fork's contribution is choosing pgvector + AGE as one specific implementation worth picking — and running it.

**What this would consolidate.** Vector search, full-text search, graph traversal, and the temporal entity-relationship store all in a single engine. Today: ChromaDB (HNSW vectors), SQLite (BM25 + KG triples + corpus_origin index), graph cache (in-process). Under Postgres: one connection, one transaction model, one backup story, one operational surface.

**The bridge pattern.** Microsoft's [pgvector ↔ Apache AGE post](https://techcommunity.microsoft.com/blog/adforpostgresql/combining-pgvector-and-apache-age---knowledge-graph--semantic-intelligence-in-a-/4508781) (Raunak, 2026-04-15) describes the architectural reference: pgvector cosine similarity scores written as `SIMILAR_TO` edges in the AGE property graph, making vector similarity itself a traversable relationship. The KG-extraction work ([P4/P5](#planned-work)) lands much more naturally when the graph is in-database than it does in a separate SQLite alongside ChromaDB.

**What stays the same.** The verbatim-first commitment is unchanged — Postgres tables would hold the same canonical raw text, just on a different storage engine. The multi-collection-by-purpose pattern (Principle 1 of the thesis) maps directly onto Postgres schemas or per-collection tables. Composition with upstream stays the rule, including here: this is a backend implementation against the seam, not a parallel reimplementation. If the evaluation pans out, the natural ship shape is a separate `pip install` package wired via entry-point registration; the fork's main branch keeps tracking upstream develop and ChromaDB stays the default.

**The composition discipline.** Writing the implementation against #665's contract means the fork picks up improvements skuznetsov ships on the way to merge, rather than diverging. dekoza's bus-factor concern on the optional `pg_sorted_heap` path was addressed by skuznetsov's 2026-04-19 rewrite — pgvector is now the broadly-available default; `pg_sorted_heap` is opt-in for self-managed installs. Our deployment runs the pgvector fallback, which makes us a real-world consumer worth attaching to that thread once Phase-1-equivalent work lands and the smoke run is green.

**What's still open.** Embedding-model identity across the migration window. Operational ergonomics versus the current daemon-fronted ChromaDB story. Whether the bridge pattern survives at 150K+ drawers without a custom indexing strategy. Whether the bench numbers justify the migration cost at all. The honest version: *we run the queries now and read the numbers, instead of speculating about them.*

## What this fork ships, organized by axis

Three bands of work, all instances of the principles above. Detail rows in the [appendix](#fork-change-inventory) at the bottom.

- **Structural retrieval fixes (Principle 1, Principle 2).** Verbatim-only model: hooks no longer write 1KB checkpoint summaries; auto-mined transcript chunks land in `mempalace_drawers` and `mempalace_search` reaches them directly. The earlier dedicated `mempalace_session_recovery` collection (Apr 25–May 5) and its read-only `mempalace_session_recovery_read` MCP tool have been retired (May 5 — see `docs/superpowers/specs/2026-05-05-verbatim-only-design.md`). Net result: one collection, one search path, no kind-filter / over-fetch hack. `drawer_id` surfacing on every search/diary hit so callers can build citation popovers and follow-ups.
- **Single-writer architecture (Principle 3).** [palace-daemon](https://github.com/jphein/palace-daemon) is the only process that opens the palace; clients connect over HTTP. ChromaDB 1.5.x's HNSW concurrency hazards (`#974`/`#965`/`#823` family) become structurally impossible. Cold-start integrity sniff-test on segment metadata files prevents `quarantine_stale_hnsw` from destroying healthy indexes during async-flush lag. Cherry-pick of upstream [#1085](https://github.com/MemPalace/mempalace/pull/1085) for 10–30× mining speedup; fork-side boundary-level None-metadata coercion that closes a per-site-guard family, filed upstream as [#1094](https://github.com/MemPalace/mempalace/pull/1094).
- **Deterministic hook saves (Principles 1+2+3 compose).** Silent saves bypass auto-memory conflicts entirely — the LLM is no longer in the save path, so `decision: "block"` race conditions and Claude's auto-memory winning over MCP tools both go away. Verbatim transcript ingest is the entire save path; the save marker advances on each fire and `systemMessage` reports the wing the ingest landed in. PreCompact does the same — sync-mines the transcript before context boundary, no separate marker write.

## Quickstart

```bash
git clone https://github.com/jphein/mempalace.git
cd mempalace
uv sync --extra dev          # recommended; or: python -m venv venv && pip install -e ".[dev]"

uv run mempalace init ~/Projects --yes
uv run mempalace mine ~/Projects/myproject
uv run mempalace search "why did we switch to GraphQL"
```

For a daemon-fronted deployment (recommended once palace size reaches the multi-thousand-drawer range), see [palace-daemon](https://github.com/jphein/palace-daemon)'s setup. The fork's `scripts/deploy.sh` is a one-command Syncthing-aware redeploy: push fork main, restart palace-daemon, post-restart import-check that the new fork-ahead surface is loaded.

## What it looks like in production

A Stop hook fires every 15 messages in Claude Code, triggers verbatim transcript mining via the daemon's `/mine` endpoint (no LLM in the loop), and renders a terminal line so the user sees the ingest land:

```json
{"systemMessage": "✦ Transcript ingest triggered (wing=wing_realmwatch)"}
```

`search_memories` (via `mempalace_search` MCP tool) returns results with scope-authoritative context so callers can tell when the vector layer underdelivered:

```json
{
  "query": "kiyo xhci usb crash fix razer",
  "total_before_filter": 15,
  "available_in_scope": 160351,
  "warnings": [],
  "results": [
    {"drawer_id": "drawer_kiyo-xhci-fix_technical_a8b2c4...", "wing": "projects",
     "room": "technical", "similarity": 0.859, "matched_via": "drawer", ...},
    {"drawer_id": "drawer_kiyo-xhci-fix_technical_d5e7f9...", "wing": "kiyo-xhci-fix",
     "room": "technical", "similarity": 0.852, "matched_via": "drawer", ...}
  ]
}
```

When the HNSW index is genuinely degraded (rare, post-fix), the same call returns `warnings: ["vector search returned 0 of 5 requested; filled 5 from sqlite+BM25 keyword match"]` with hits tagged `"matched_via": "sqlite_bm25_fallback"` — data is never silently hidden.

## Architectural principles

Three operational principles that inform PR review alongside the thesis above. They predate the thesis but converge on the same conclusions, and they're the design tests against which new fork-side work gets evaluated.

### 1. Lazy derivation with graceful fallback is the pattern

Write the raw text first; derive everything else lazily, from unambiguous signals, with a graceful fallback when derivation fails. The verbatim archive is the one thing that must always succeed. Optional enrichment (LLM topic extraction, AAAK encoding, concept chunking) is welcome as long as it stays opt-in, additive, and never a prerequisite for the write to complete.

The inverse — making classification a *gate* — is where the fork's earliest visible bugs came from: `room=None` crashes, a stopword list at 285 English entries papering over false positives, wing misassignment. Entity detection misfires, classifiers force wrong rooms, LLM-extracted "facts" lose nuance and can't be un-extracted. The fork's design test for any new write-path feature is now: *does this require interpreting content at write time?* If yes, derive lazily instead.

Same instinct as the verbatim-vs-derivative axis. Derivative work belongs *next to* the verbatim record, never *replacing* it. Anthropic's Dreams API (below) made the same call at the vendor-API level: input read-only, output a separate store.

### 2. Derived hierarchy from unambiguous signals outperforms hand-classified hierarchy

Hierarchy works when it's derived from unambiguous signals (cwd, transcript path, project directory) — not when it's hand-classified by content inspection. The earlier mistake was conflating "hierarchy is bad" with "mandatory synchronous classification is bad" — different claims.

**Good uses of hierarchy, which we keep:**
- **Browseable scope** for serendipitous recall across ~160K drawers.
- **Deletion and retention as a unit.** Purging an abandoned project is one operation, not a risky query-then-delete.
- **Disambiguation without query gymnastics.** The same keyword across years of unrelated work.
- **Auto-surfacing priors.** A wing derived from cwd is a cheap, unambiguous scoping signal.

**Bad uses, which we're unwinding:**
- Required at write time (caused all the crashes).
- Derived from content-inspection heuristics (NER, keyword matching) rather than unambiguous signals.
- Single-label, as if every drawer had one true parent. Cross-cutting concerns belong in tags ([P0](#planned-work)).
- Deep nesting when shallow would do.

### 3. Algorithmic effort belongs on retrieval, not on write-time classification

Spend the algorithmic budget on retrieval, where quality compounds. Classification quality has a hard ceiling set by the accuracy of the classifier, and a write-time classifier won't be that accurate. Vector + BM25 + optional scope filter already beats the hierarchy on its own. Tags ([P0](#planned-work)), feedback ([P3](#planned-work)), and decay ([P2](#planned-work)) extend without requiring write-time commitment.

Effort spent tuning the entity detector is effort not spent on the thing that pays compounding returns — which, per the [four-layer model](#the-four-layers), is now visibly four independent surfaces, not one.

## Two memory layers

Claude Code has two complementary memory layers, used in tandem:

| Layer | Storage | Size | Consolidation | Purpose |
|---|---|---|---|---|
| **Auto-memory** | `~/.claude/projects/*/memory/*.md` | ~17 files (this project) | **Auto Dream** (research preview in both Claude Code and the Managed Agents Dreams API; beta header `dreaming-2026-04-21`) | Preferences, feedback, context |
| **MemPalace** | palace-daemon at `http://disks.jphe.in:8085` (ChromaDB on the daemon host) | ~160K drawers | None — deliberately. See below. | Verbatim conversations, tool output, code |

**Auto Dream's arrival ratifies the verbatim-vs-derivative axis.** Anthropic shipped Auto Dream in two surfaces in late April, both research preview: a [research-preview consolidator inside Claude Code](https://claudefa.st/blog/guide/mechanics/auto-dream) (manual `/dream` or auto-trigger when both 24 hours and 5+ sessions have elapsed since the last run; reads `~/.claude/projects/<project>/memory/` + MEMORY.md + JSONL transcripts + daily logs; writes back to the same memory directory) and a [Dreams API in Managed Agents](https://platform.claude.com/docs/en/managed-agents/dreams) (REST research preview, beta header `dreaming-2026-04-21`, supported models `claude-opus-4-7` and `claude-sonnet-4-6`, up to 100 sessions per dream, optional 4096-character `instructions`).

The Dreams API design is the more architecturally interesting half because it ratifies an axis this fork's been arguing: **the input memory store is never modified; the dream produces a separate output store you can review, attach, or discard.** Non-destructive consolidation of verbatim inputs, with a review gate, where the model curates rather than mutates. That is the verbatim-vs-derivative pattern at the level of a vendor API. The April-2026 verbatim cohort and Anthropic's Dreams API landed on the same architectural call from different starting points.

**Why MemPalace deliberately doesn't consolidate the verbatim layer.** Both Dreams surfaces consume the *same* JSONL transcripts that MemPalace mines. The pairing is complementary, not competitive: Auto Dream curates a small high-signal index for the model to read on every turn; MemPalace stores the corpus verbatim for post-hoc retrieval. Consolidating MemPalace's verbatim layer would forfeit exactly the property that makes it useful — the ability to audit, re-derive, and recover the original utterance. Decay ([P2](#planned-work)), feedback ([P3](#planned-work)), KG ([P4](#planned-work)), and tags ([P0](#planned-work)) are the right places to invest derivative effort because they sit *next to* the verbatim record, not on top of it. The verbatim layer doesn't need consolidation; it needs durability.

## Planned work

Reorganized 2026-04-26 around the verbatim-vs-derivative axis. Each item evaluated against the three architectural principles + the three thesis principles above.

### Verbatim-store improvements

- **P0 — Multi-label tags** *(1-2 days, additive)*. Tags are the cross-cutting-concerns layer that hierarchy can't provide. Add `tags` metadata (3-8 per drawer) extracted during mining via TF-IDF or longest-non-stopword heuristic. Adjacent: [#1033](https://github.com/MemPalace/mempalace/pull/1033) (`<private>` tag filter, @zackchiutw) is single-purpose; full multi-label additive on top. Optional opt-in `--enrich` flag for Haiku-extracted topic tags (96.6% R@5 baseline → competitive before rerank).
- **P1 — Derive hierarchy from unambiguous signals** *(half day)*. Reframe from "best-effort classification" to "derive from cwd, transcript path, project directory." Default wing to source dir name (already mostly works). Demote entity detector to last-resort hint, not gate. Documents the derivation order: cwd → transcript path → project hint → (optional) entity hint → unfiled.
- **P6 — Input sanitization on writes** *(half day)*. Strip known injection patterns. Flag with `sanitized: true` metadata, don't block. 10K char cap. Low priority while local-only.

### Derivative-store work (the new axis)

- **P8 — Corpus partitioning by purpose** *(architectural, on hold)*. The recovery-collection split (Apr 25 → May 5, 2026) was the first attempt at this — moved Stop-hook checkpoints to a dedicated `mempalace_session_recovery` collection. Retired May 5: splitting required every read path to query both collections, but the recovery side never got a semantic-search MCP surface, so checkpoints became invisible to `mempalace_search`. The architectural pattern stays valid for future siblings (KG-triple store ([P4](#p4-anchor)), Haiku-enriched topic docs, transcript-mine outputs in the [#1083](https://github.com/MemPalace/mempalace/issues/1083) family), but each new sibling collection has to earn its own read tool before it gets writes. Worth flagging in [RFC 001](https://github.com/MemPalace/mempalace/pull/743) so future backends know that multi-collection-per-palace is the pattern AND that read-surface parity is a precondition.
- **P4 — KG auto-population + entity resolution** *(1.5 days)*. <a id="p4-anchor"></a> Hooks extract `subject/predicate/object` triples on every save using heuristics (no LLM). Triples land in their own store (KG SQLite is already separate, P8-aligned). Normalize entity IDs; alias table + Levenshtein. Triples are *derived* — re-mine if extraction improves; verbatim untouched. *Note: under the [Postgres + pgvector + AGE substrate work](#substrate-postgres--pgvector--apache-age), the graph lives in-database (AGE) rather than in a separate SQLite, which makes this work meaningfully more natural to implement.*
- **P5 — Temporal fact validity** *(1 day, depends on P4)*. KG triples get a context slot (SPOC: subject-predicate-object-context). Reference: Zep's [Graphiti](https://github.com/getzep/graphiti). *Same Postgres+AGE caveat as P4 — temporal validity ranges are SQL-native on Postgres in a way they aren't across two engines.*

### Cross-cutting

- **P2 — Decay / recency weighting** *(tracked upstream)*. Handled by [#1032](https://github.com/MemPalace/mempalace/pull/1032) (Weibull decay, MERGEABLE). Independent `mempalace prune --stale-days 180` CLI is still a fork opportunity.
- **P3 — Feedback loops** *(rerank tracked upstream; rating still open)*. #1032 covers Tier 0 LLM rerank (96.6% → 99.4% with Haiku). Tier 1+: `mempalace_rate_memory(drawer_id, useful: bool)` MCP tool, implicit echo/fizzle signals. Reference: [Celiums](https://celiums.ai/)'s novelty + emotional + circadian importance scoring.
- **P7 — Alternative storage modes** *(tracked upstream + fork-side pgvector+AGE implementation in flight)*. Upstream owns the [RFC 001](https://github.com/MemPalace/mempalace/pull/743) seam and the four backend-implementation PRs. Fork is running [Postgres + pgvector + Apache AGE](#substrate-postgres--pgvector--apache-age) as one specific implementation against that seam — composition, not a parallel reimplementation. See the dedicated section earlier for substrate state and Plan-B trigger.

### Deprioritized

- **Expanding hierarchy types** (tunnels, closets, new room categories). Adding categories doesn't address the write-time classification problem. Tags (P0) and derived scope (P1) do.
- **Full architecture rewrite** — not worth migration cost.
- **Dual-granularity ANN, dream engine, foresight signals** ([Karta](https://github.com/rohithzr/karta)-inspired) — require LLM calls on every write. Zero-LLM philosophy makes these opt-in at best.
- **FTS5 parallel index** — right idea (engram proves it), significant infrastructure alongside ChromaDB. Revisit after tags and decay are proven.

## Active investigations

### Engram-2's "17% E2E QA" critique — closing

[engram-2](https://github.com/199-biotechnologies/engram-2) published a benchmark note stating MemPalace achieves 0.984 R@5 on LongMemEval but only 17% end-to-end question-answering accuracy. The structural fix shipped 2026-04-25 → 2026-04-26 demonstrably closes one concrete instance — checkpoint domination of `mempalace_search` results — on this corpus. Pre-migration `kind=content` returned 3 tokens/Q; post-migration it returns 1,267. End-to-end LongMemEval-S through this fork against a modern reader model is instrumented; results will land at `notebook/data/cat9-postmigrate-e2e/REPORT.md`. The corpus-shape thesis is the durable claim; the E2E number will be the operationalization.

### Hybrid retrieval A/B

BM25 + vector with reciprocal rank fusion vs current hybrid-rerank pipeline. Don't pre-decide the winner. The honest version is "I don't know which is better on my corpus and want to find out."

### Cat 9 / The Handshake as a generalizable measurement

The SME framework's Cat 9 is an underappreciated piece of the memory-systems landscape — every deployment runs into the integration gap; the field's benchmarks deliberately don't measure it. The [four-layer section](#the-four-layers) above leads with the cleanest numbers we have: 46.67% / 78.33% on RLM-vs-Familiar at the retrieval-into-consumption boundary. Scaling that comparison up across the verbatim-first cohort (Longhand, Celiums, mcp-memory-service) is the right next move; adapter work tracked at [`jphein/multipass-structural-memory-eval`](https://github.com/jphein/multipass-structural-memory-eval). Grafana's [o11y-bench](https://grafana.com/blog/o11y-bench-open-benchmark-for-observability-agents/) (April 2026) is the same instinct applied to observability — bench what agents actually *do* with the data, not just retrieval-side metrics — and worth tracking as the pattern matures across domains.

### Multi-palace separation — curated "authority" vs auto-mined memory

@kostadis raised in upstream [#1018](https://github.com/MemPalace/mempalace/discussions/1018): a manually curated palace alongside the auto-mined chat palace. The hooks dump everything into one palace today, polluting curated content. Right fix is multi-palace support with per-hook target flag — design needs review (does it fit the single-`palace_path` model? does it want `palace_name` aliases?). P8 (collection partitioning) might absorb this — different collections per purpose inside the same palace, vs. multiple palaces. Decide once we've tried the lighter move first.

### Stale auto-loaded docs

Knowledge lives across 7+ layers: global CLAUDE.md, project CLAUDE.md, auto-memory, docs/, superpowers specs, code comments, MemPalace. The auto-loaded layers go stale and actively mislead. MemPalace is the only layer that *can't* go stale (verbatim + timestamped) but never auto-loaded. Planned `/verify-docs` slash command pattern-matches version strings, file paths, PR numbers, URLs, and verifies against current state. Cleaning stale docs prevents more wrong assumptions than any amount of auto-querying.

## Composition with upstream

A meaningful shift in 2026-04 and 2026-05: this fork increasingly *composes with* upstream rather than carrying parallel implementations.

- **Cherry-picks (in-flight upstream PRs we use early):** [#665](https://github.com/MemPalace/mempalace/pull/665) PostgreSQL backend (commit `5e90c72`, the substrate work above), [#1085](https://github.com/MemPalace/mempalace/pull/1085) batched inserts (`6be6fff` — CLOSED 2026-05-05, superseded by merged [#1185](https://github.com/MemPalace/mempalace/pull/1185); safe to drop on next sync), [#1087 rewrite](https://github.com/MemPalace/mempalace/pull/1087) `cmd_purge` via `delete(where=)` (`366a9ad`), [#1094](https://github.com/MemPalace/mempalace/pull/1094) None-metadata coercion (`43d728d`).
- **Co-authored merges:** [#1377](https://github.com/MemPalace/mempalace/pull/1377) (surgical `_get_collection` retry-once, shipped in v3.3.5 — originated from this fork via #1286 which igorls closed and re-extracted with `Co-authored-by` credit).
- **Coordinated reviews:** [#1199](https://github.com/MemPalace/mempalace/pull/1199) (rmdes' unbounded-ingest fix — pulled and tested locally, +1 with composition note), [#1219](https://github.com/MemPalace/mempalace/pull/1219) (pepo72's drawer_id — narrower than ours; offered the diary/recovery extension), [RFC 001 #743](https://github.com/MemPalace/mempalace/pull/743) (storage backend spec — flagged the multi-collection-by-purpose pattern as worth naming explicitly).
- **Closed in favor of upstream:** [#1171](https://github.com/MemPalace/mempalace/pull/1171) cross-process write lock (closed 2026-04-25 — Felipe's [#976](https://github.com/MemPalace/mempalace/pull/976) `mine_global_lock` at the right layer plus daemon-strict architecture obsoleted ours).

The fork ships structural moves first, validates them on the canonical palace, then either contributes upstream as PRs or aligns with upstream's parallel implementation. The substrate work is the cleanest current example: rather than fork-port #665, wait for it to merge and run our implementation on top of its contract, with a documented trigger to switch strategies if upstream stalls. The composition is the point.

## Ecosystem — third-party projects, forks, and evaluation frameworks

From a 2026-04-21 sweep of upstream MemPalace issue + comment + discussion history, updated with peer-build entries surfaced in May. State moves; check the repos directly for current status.

### Companion tools (compose with MemPalace, don't replace it)

- **[palace-daemon](https://github.com/rboarescu/palace-daemon)** (@rboarescu) — FastAPI gateway + MCP-over-HTTP proxy. Three asyncio semaphores (read / write / mine). Pins correctness floor at MemPalace ≥3.3.2. **This fork migrated to palace-daemon on 2026-04-24** ([`c09582c`](https://github.com/jphein/mempalace/commit/c09582c) wired MCP + hooks; [`0e97b19`](https://github.com/jphein/mempalace/commit/0e97b19) added daemon-strict mode). All reads and writes from the plugin flow through the daemon; auto-migrate-on-startup of the checkpoint split landed as palace-daemon [`034023c`](https://github.com/jphein/palace-daemon/commit/034023c). JP's deployment runs at [`jphein/palace-daemon`](https://github.com/jphein/palace-daemon).
- **[engram](https://github.com/NickCirv/engram)** (@NickCirv) — File-read interception for AI coding assistants. Uses MemPalace as one of six context providers via `mcp-mempalace mempalace-search`; caches with 1h TTL. Upstream [discussion #798](https://github.com/MemPalace/mempalace/discussions/798).
- **[engram](https://github.com/harreh3iesh/engram)** (@harreh3iesh — different project, same name) — Hooks + tools for AI memory, first-class MemPalace backend. **Stuck detector** (`PreToolUse` hook counts Grep/Glob calls and nudges the AI when spinning) is a pattern worth borrowing. Upstream [discussion #748](https://github.com/MemPalace/mempalace/discussions/748).
- **[cdd-mempalace](https://github.com/fuzzymoomoo/cdd-mempalace)** (@fuzzymoomoo) — Bridge library mapping Context-Driven Development methodology onto wings/halls/rooms. Multiple active upstream PRs.

### Evaluation frameworks

- **[multipass-structural-memory-eval](https://github.com/M0nkeyFl0wer/multipass-structural-memory-eval)** (@M0nkeyFl0wer) — Nine-category diagnostic framework. **"Category 9: The Handshake"** tests integration under production model usage, not just offline retrieval — the gap the [four-layer](#the-four-layers) lede's invocation finding lives in. Forked at [jphein/multipass-structural-memory-eval](https://github.com/jphein/multipass-structural-memory-eval). The mempalace-daemon adapter at `sme/adapters/mempalace_daemon.py` talks HTTP/MCP only — no parallel `PersistentClient`, daemon-strict-compatible. The Cat 9 A/B harness used for the 2026-04-25 → 2026-04-26 measurements lives here.

### Adjacent / competing memory systems

- **[agentmemory](https://github.com/rohitg00/agentmemory)** (@rohitg00) — BM25 + vector hybrid. **95.2% R@5** on LongMemEval-S with same MiniLM embedding model. Filed methodology review in upstream [#747](https://github.com/MemPalace/mempalace/discussions/747).
- **[engram-2](https://github.com/199-biotechnologies/engram-2)** — Rust CLI, deterministic, SQLite + FTS5 only. Hybrid via Gemini embeddings + FTS5 reciprocal rank fusion. **0.990 R@5** vs MemPalace's 0.984 with no reranking, claims **17% end-to-end QA** for MemPalace — the critique the structural-fix work above closes. Memory-layer-budgeting (identity / critical / topic / deep tiers with token accounting) is worth studying.
- **[Tiro (project-tiro)](https://github.com/esagduyu/project-tiro)** (@esagduyu) — Same data-spine architecture (FastAPI + ChromaDB + SQLite + sentence-transformers + MCP) but *curated* input domain (web pages, email newsletters as clean markdown). Architectural twin to MemPalace's auto-mine-everything: same stack, different input shape. Forked at [jphein/project-tiro](https://github.com/jphein/project-tiro).

### Adjacent inference paradigms (different layer than memory)

- **[RLM (Recursive Language Models)](https://github.com/alexzhang13/rlm)** (@alexzhang13, MIT OASYS) — LM offloads context as a REPL variable and recursively decomposes. Targets near-infinite context length. Forked at [jphein/rlm](https://github.com/jphein/rlm); integration example at [`examples/mempalace_demo.py`](https://github.com/jphein/rlm/blob/main/examples/mempalace_demo.py). The [four-layer](#the-four-layers) section's 46.67% / 78.33% finding came from running this fork's SME adapter against the same RLM substrate against Familiar's deterministic pipeline.
- **[ASI-Evolve](https://github.com/GAIR-NLP/ASI-Evolve)** (@GAIR-NLP) — Closed-loop autonomous research agent (Researcher / Engineer / Analyzer). Two parallel memory systems: **Cognition Store** (upfront domain knowledge) and **Experiment Database** (every trial). Validated on neural architecture design (+0.97 over DeltaNet — ~3× recent human gains). [arXiv 2603.29640](https://arxiv.org/abs/2603.29640). Forked at [jphein/ASI-Evolve](https://github.com/jphein/ASI-Evolve). The Cognition Store is exactly the role MemPalace would play.

### MemPalace-orbit projects (peer builds)

Built *on top of* or *alongside* MemPalace by community contributors who use the palace as substrate. The [Convergence with peer systems](#convergence-with-peer-systems) section above narrates the architectural triangulation; this is the inventory.

- **[Familiar](https://github.com/jphein/familiar.realm.watch)** (@jphein) — deterministic retrieval pipeline, local Gemma, 78.33% recall on jp-realm-v0.1.
- **[CampaignGenerator](https://github.com/kostadis/CampaignGenerator)** (@kostadis) — RPG session-prep over 2 TB PDFs + 5etools; 19.82× cost reduction at 0% R@10 loss via hierarchical AAAK pruning.
- **[Kent](https://github.com/kenchambers/kent)** (@kenchambers) — typed async agent runtime with APO training subsystem and recall games.
- **[adaptmem](https://github.com/nakata-app/adaptmem)** (@nakata-app) — encoder fine-tune adapter; orthogonal lift across retrieval modes.
- **[GraphPalace](https://github.com/web3guru888/GraphPalace)** (@web3guru888) — graph-layer build. Forked at [jphein/GraphPalace](https://github.com/jphein/GraphPalace).
- **[mempalace-viz](https://github.com/JoeDoesJits/mempalace-viz)** (@JoeDoesJits) — visualization layer (wings, rooms, tunnels, drawer counts). Forked at [jphein/mempalace-viz](https://github.com/jphein/mempalace-viz).
- **[AutomataArena](https://github.com/astrutt/AutomataArena)** (@astrutt) — multi-agent orchestration substrate. Forked at [jphein/AutomataArena](https://github.com/jphein/AutomataArena).

### Active forks beyond ours

| Fork | Contributor work |
|---|---|
| [jphein/mempalace](https://github.com/jphein/mempalace) | this fork |
| [kostadis/mempalace](https://github.com/kostadis/mempalace) | hierarchical AAAK pruning branch |
| [fuzzymoomoo/cdd-mempalace](https://github.com/fuzzymoomoo/cdd-mempalace) | 10 comment refs; CDD integration layer |
| [potterdigital/mempalace](https://github.com/potterdigital/mempalace) | author of upstream [#1081](https://github.com/MemPalace/mempalace/pull/1081) |
| [vnguyen-lexipol/mempalace](https://github.com/vnguyen-lexipol/mempalace) | author of upstream [#851](https://github.com/MemPalace/mempalace/pull/851) |

## Open upstream PRs

Ten open from this fork as of 2026-05-12. Run `gh pr list --repo MemPalace/mempalace --author jphein --state open` for the live list. Two merged today: [#1459](https://github.com/MemPalace/mempalace/pull/1459) (empty-metadata sentinel) and [#1474](https://github.com/MemPalace/mempalace/pull/1474) (convo_miner bulk pre-fetch).

| PR | Status | Description |
|---|---|---|
| [#660](https://github.com/MemPalace/mempalace/pull/660) | CI green, awaiting review | L1 importance pre-filter |
| [#1005](https://github.com/MemPalace/mempalace/pull/1005) | CI green, Dialectician-acked | Warnings + sqlite BM25 top-up — never silently return fewer results than scope contains |
| [#1024](https://github.com/MemPalace/mempalace/pull/1024) | CI green, qodo-acked | Configurable `chunk_size` / `chunk_overlap` / `min_chunk_size` |
| [#1086](https://github.com/MemPalace/mempalace/pull/1086) | CI green, awaiting review | `mempalace export` CLI wrapper |
| [#1087](https://github.com/MemPalace/mempalace/pull/1087) | CI green, **rewritten 2026-04-26** per @igorls's review | `mempalace purge --wing/--room` via `delete(where=)` (no nuke-and-rebuild) |
| [#1094](https://github.com/MemPalace/mempalace/pull/1094) | CI green, awaiting review | Coerce `None` metadatas to `{}` at `ChromaCollection` boundary |
| [#1142](https://github.com/MemPalace/mempalace/pull/1142) | CI green, @bensig accepted 2026-04-23 | `docs/RELEASING.md` |
| [#1378](https://github.com/MemPalace/mempalace/pull/1378) | CI green | Hoist `CLOSET_RANK_BOOSTS` to module level + record VecRecall ablation finding |
| [#1382](https://github.com/MemPalace/mempalace/pull/1382) | CI green | Benchmarks UTF-8 encoding + ASCII print chrome on Windows |
| [#1484](https://github.com/MemPalace/mempalace/pull/1484) | CI pending | OpenCode source adapter on RFC 002 contract — co-authored with @JakobSachs (originated from his #23 spadework) |

## What's next

Forward-looking, in rough priority order. The substrate work is the biggest single piece of in-flight engineering; everything else is incremental against the existing direction.

- **Complete the pgvector + Apache AGE backend implementation** on `feat/pgvector-age-impl`, composing on upstream #665. The composition stance is documented; the trigger date for switching to a fork-port strategy is 2026-06-08; the live LAN-bound test container is running. See [Substrate](#substrate-postgres--pgvector--apache-age) above.
- **Publish Cat 9 end-to-end results** on the post-migration palace at `notebook/data/cat9-postmigrate-e2e/REPORT.md`, with adapter parity numbers across the verbatim-first cohort once the SME harness lands.
- **Publish the multipass-structural-memory-eval harness** with adapters for MemPalace, Longhand, Celiums, mcp-memory-service so Cat 9 / The Handshake stops being a one-deployment story. The four-layer model needs the cross-system numbers to be more than this fork's read of its own corpus.
- **Land P0 (multi-label tags) and P2 (decay/recency)** — P2 tracked upstream via [#1032](https://github.com/MemPalace/mempalace/pull/1032); P0 is fork-side until upstream wants it.
- **Publish the verbatim-vs-derivative axis as a standalone essay**, distinct from the README. The Auto Dream Dreams-API arrival is the most quotable evidence yet that this axis matters at the vendor-API level; that observation deserves its own writeup.
- **Coordinate with upstream on the multi-collection-by-purpose pattern** — implicit in RFC 001 today, worth naming explicitly so future backends plan for it. The substrate work surfaces this concretely: pgvector + AGE backends need to know about it.
- **Agent-shaped CLI surface.** MCP brings palace data into Claude Code via tool calls; the peer surface is a pipe-friendly CLI with structured-output flags so agents, hooks, or scripts can call `mempalace search ... --json` and route results into context without the MCP roundtrip. Grafana's [GCX CLI](https://www.infoq.com/news/2026/04/grafana-loki-ai-agents/) is the prior art for this pattern in observability — bring the data to where the agent lives, don't force the agent into a separate UI. Today's `mempalace` CLI is operator-shaped (status / mine / repair / search); the next-generation surface should be agent-callable, with first-class JSON output and conventions that compose with shell pipelines and slash commands.
- **First-class support across the AI coding agent ecosystem.** Today's integration is Claude Code-specific (Stop / PreCompact hooks, `~/.claude/projects/*.jsonl` mining). Target the broader set: [Claude Code](https://github.com/anthropics/claude-code), [OpenCode](https://opencode.ai/), [Cursor](https://cursor.com/), [Aider](https://aider.chat/), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [Codex CLI](https://github.com/openai/codex), [Warp](https://www.warp.dev/), and adjacent. Path is upstream's [RFC 002 source-adapter spec](https://github.com/MemPalace/mempalace/pull/990) (tracking [#989](https://github.com/MemPalace/mempalace/issues/989)) — each agent ships a `pip install mempalace-source-<agent>` package mapping its session format onto the canonical drawer shape. Three integration cells: **read** is universal (the MCP server is already agent-agnostic), **mine** is per-agent via RFC 002 adapters, **hook/event** wiring lands wherever the host exposes a hook surface (mining-on-cron is the fallback). Fork unblocks the pattern by helping land RFC 002; per-agent adapter PRs land from their respective authors.

## Setup / Development

```bash
# Setup
git clone https://github.com/jphein/mempalace.git
cd mempalace
uv sync --extra dev                       # recommended; or pip install -e ".[dev]"

# Develop
uv run pytest tests/ -q                   # ~1850 tests (benchmarks deselected)
uv run mempalace status                   # palace health
uv run ruff check . && uv run ruff format --check .

# Doc maintenance (canonical YAML + renderer, see CLAUDE.md)
./scripts/render-docs.py                  # regenerate FORK_CHANGELOG from docs/fork-changes.yaml
./scripts/check-docs.sh                   # lint test count, fork hashes, render parity, upstream PR states

# Deploy fork main → palace-daemon on disks
./scripts/deploy.sh                       # one command: push, sync, restart, health, import-check
```

## Fork change inventory

The full enumeration of fork-ahead changes. For the narrative, see [What this fork ships](#what-this-fork-ships-organized-by-axis) above. This is the inventory for verifying claims, looking up specific commits, or picking a contribution.

The canonical source is [`docs/fork-changes.yaml`](docs/fork-changes.yaml); [`FORK_CHANGELOG.md`](FORK_CHANGELOG.md) is regenerated from it. Run `./scripts/check-docs.sh` to verify everything below resolves to live state.

### Fork-ahead — open or pending

| Area | Change | Status | Files |
|---|---|---|---|
| **Backend** | **Postgres + pgvector + Apache AGE substrate implementation.** Cherry-pick of upstream [#665](https://github.com/MemPalace/mempalace/pull/665) (skuznetsov's PostgreSQL backend on the RFC 001 `BaseBackend` contract). Smoke tests + BaseCollection conformance test added. Live test container on JP's homelab LAN (`10.0.6.120:5433`) running PG16 + pgvector 0.8.2 + AGE 1.6.0. Composition stance: WAIT for #665 to merge; Plan-B trigger 2026-06-08. | In-flight on `feat/pgvector-age-impl` — PR [#21](https://github.com/jphein/mempalace/pull/21). Fork commits `5e90c72` (cherry-pick), `04c6294` (smoke), `1d5486f` (docstring cleanup). | `mempalace/backends/postgres.py`, `mempalace/palace.py`, `tests/test_backends_postgres.py`, `docs/internal/pgvector-665-decision.md` |
| **Reliability** | **Daemon-strict migration completion** (May 7). Closes the last desktop-side write paths that bypassed palace-daemon. `mcp_server.py` gates at the `handle_request` JSON-RPC chokepoint and forwards every method to the daemon's `/mcp` proxy when `PALACE_DAEMON_URL` is set; `cli.py` gates `cmd_status`, `cmd_search`, `cmd_mine` against the same env var. Mirrors the gate `hooks_cli.py` already uses (2026-04-24, drift-incident fix). With the local `mempalace-data/` no longer pinned by `~/.mempalace/config.json`, the canonical palace at `disks.jphe.in:8085` is the single writer for every desktop entry point. CLI `init`/`repair`/`export`/`sweep`/`purge`/`mined`/`wakeup` stay local because they need on-host filesystem access. | Fork-ahead, pitchable upstream as a single-file replacement for `palace-daemon/clients/mempalace-mcp.py`. Fork commits [`41359ba`](https://github.com/jphein/mempalace/commit/41359ba) (mcp_server) + [`22ef562`](https://github.com/jphein/mempalace/commit/22ef562) (CLI). | `mcp_server.py`, `cli.py`, `tests/conftest.py`, `tests/test_mcp_server_daemon.py`, `tests/test_cli_daemon.py` |
| **Search** | **Verbatim-only retrieval** (May 5). Hooks write only verbatim transcript chunks; the dedicated `mempalace_session_recovery` collection and `mempalace_session_recovery_read` MCP tool are retired. `mempalace_search` reaches all session content directly. Replaces the earlier multi-collection split (Apr 25 → May 5). Spec: `docs/superpowers/specs/2026-05-05-verbatim-only-design.md`. | PRs in review — [`#2`](https://github.com/jphein/mempalace/pull/2) (transcript ingest restore), [`#3`](https://github.com/jphein/mempalace/pull/3) (drop checkpoint writes), [`#5`](https://github.com/jphein/mempalace/pull/5) (retire collection); palace-daemon [`#1`](https://github.com/jphein/palace-daemon/pull/1) (path translation) | `hooks_cli.py`, `mcp_server.py`, `palace.py`, `migrate.py`, `cli.py` |
| **Search** | Surface `drawer_id` in `mempalace_search` results and `mempalace_diary_read` entries. ChromaDB primary key was returned but never plumbed into the result-building loop. | PR pending — fork commit [`9a8bb77`](https://github.com/jphein/mempalace/commit/9a8bb77); upstream [#1219](https://github.com/MemPalace/mempalace/pull/1219) (@pepo72) is the narrower searcher-only equivalent. | `searcher.py`, `mcp_server.py`, `tests/...`, `website/reference/mcp-tools.md` |
| **CLI** | `mempalace mined` lists mined source files grouped by wing × source_file; `mempalace purge --source-file` deletes drawers from a specific file. Closes the "removing manually mined data" half of the mining-management ask. | [`#4`](https://github.com/jphein/mempalace/pull/4) | `cli.py`, `tests/test_cli.py` |
| **Performance** | Cherry-picked upstream [#1085](https://github.com/MemPalace/mempalace/pull/1085) (@midweste) — batch ChromaDB inserts in miner. New `_build_drawer()` + `add_drawers()`. Reported 10–30× mining speedup. | Cherry-pick — fork commit [`6be6fff`](https://github.com/jphein/mempalace/commit/6be6fff). **2026-05-16:** #1085 closed by author, superseded by merged upstream [#1185](https://github.com/MemPalace/mempalace/pull/1185) (wider scope: same batching + optional GPU acceleration). Our cherry-pick is now a no-op against develop; drop on next sync. | `mempalace/miner.py` |
| **Reliability** | Coerce None metadatas at chromadb boundary. Closes the per-site-guard family of None-metadata bugs (#999, #1198, #1201) at one site instead of N. Fork-authored; filed upstream as [#1094](https://github.com/MemPalace/mempalace/pull/1094). | Fork commit [`43d728d`](https://github.com/jphein/mempalace/commit/43d728d); upstream PR open | `backends/chroma.py`, `tests/test_backends.py` |
| **CLI** | `mempalace purge --wing/--room` via `collection.delete(where=...)`. Earlier nuke-and-rebuild draft predicated on #521's race; @igorls's review traced the stack. Simpler version preserves embedding fn, no rmtree window, routes through `ChromaBackend`. | [#1087](https://github.com/MemPalace/mempalace/pull/1087), rewritten 2026-04-26 per review | `cli.py`, `tests/test_cli.py` |
| **CLI** | `mempalace export` CLI wrapper for upstream's existing `export_palace()`. | [#1086](https://github.com/MemPalace/mempalace/pull/1086) | `cli.py` |
| **Performance** | L1 importance pre-filter — `importance >= 3` first, full scan fallback. | [#660](https://github.com/MemPalace/mempalace/pull/660) | `layers.py` |
| **Config** | Configurable chunking parameters — `chunk_size` (800), `chunk_overlap` (100), `min_chunk_size` (50) in `config.json`, exposed via `MempalaceConfig`. | [#1024](https://github.com/MemPalace/mempalace/pull/1024) | `config.py`, `miner.py`, `convo_miner.py` |
| **Search** | Warnings + sqlite BM25 top-up when vector underdelivers — `search_memories` returns `warnings: [...]` + `available_in_scope`; fallback hits tagged `matched_via: "sqlite_bm25_fallback"`. The palace never silently returns fewer results than the scope contains. | [#1005](https://github.com/MemPalace/mempalace/pull/1005) | `searcher.py` |
| **Docs** | `docs/RELEASING.md` with `mempalace-mcp` pre-release grep. | [#1142](https://github.com/MemPalace/mempalace/pull/1142), accepted by @bensig 2026-04-23 | `docs/RELEASING.md` |
| **Hooks** | `mempal_save_hook.sh` Python auto-detection (`MEMPAL_PYTHON` → repo venv → system `python3`). Same pattern in `.claude-plugin/`. Replied on [#1049](https://github.com/MemPalace/mempalace/issues/1049) offering autodetect, awaiting maintainer arbitration on [#1069](https://github.com/MemPalace/mempalace/issues/1069). | PR pending after #1069 direction | `hooks/mempal_save_hook.sh`, `.claude-plugin/hooks/...` |
| **Hooks** | Transcript auto-mining with correct defaults + `hook_auto_mine` config flag. Superseded by @sha2fiddy's [#1110](https://github.com/MemPalace/mempalace/pull/1110) for part 1 (opt-out flag); part 2 (`_ingest_transcript` shape change) remains fork-only. | Issue [#1083](https://github.com/MemPalace/mempalace/issues/1083) | `hooks_cli.py` |

### Recently merged into upstream

- **2026-05-06 (in v3.3.5):** [#1377](https://github.com/MemPalace/mempalace/pull/1377) — `_get_collection` retry-once + log-on-failure (~25 LOC + 2 tests; co-authored from this fork via the closed #1286)
- **2026-05-01 (post-v3.3.4):** [#1262](https://github.com/MemPalace/mempalace/pull/1262) (get-then-create at chromadb backend boundary), [#1289](https://github.com/MemPalace/mempalace/pull/1289) (MCP-server-side companion), [#1303](https://github.com/MemPalace/mempalace/pull/1303) (`embedding_function=` plumbing — prevents ONNX SIGSEGV on Py3.14)
- **2026-04-26:** [#1173](https://github.com/MemPalace/mempalace/pull/1173) (`quarantine_stale_hnsw` cold-start gate + integrity sniff), [#1177](https://github.com/MemPalace/mempalace/pull/1177) (`.blob_seq_ids_migrated` marker), [#1198](https://github.com/MemPalace/mempalace/pull/1198) (`_tokenize` None guard), [#1201](https://github.com/MemPalace/mempalace/pull/1201) (`palace_graph` None metadata)
- **2026-04-23:** [#659](https://github.com/MemPalace/mempalace/pull/659) — diary `wing` parameter, hook derives from transcript path
- **2026-04-22:** [#661](https://github.com/MemPalace/mempalace/pull/661) (graph cache), [#673](https://github.com/MemPalace/mempalace/pull/673) (deterministic hook saves), [#1021](https://github.com/MemPalace/mempalace/pull/1021) (Claude Code 2.1.114 stdout fixes)
- **2026-04-21 (in v3.3.2):** [#1000](https://github.com/MemPalace/mempalace/pull/1000) (`quarantine_stale_hnsw`), [#1023](https://github.com/MemPalace/mempalace/pull/1023) (PID file guard), [#681](https://github.com/MemPalace/mempalace/pull/681) (Unicode checkmark)
- **2026-04-18:** [#999](https://github.com/MemPalace/mempalace/pull/999) — None-metadata guards across 8 read paths
- **In v3.3.0:** [#664](https://github.com/MemPalace/mempalace/pull/664), [#682](https://github.com/MemPalace/mempalace/pull/682), [#683](https://github.com/MemPalace/mempalace/pull/683), [#684](https://github.com/MemPalace/mempalace/pull/684), [#635](https://github.com/MemPalace/mempalace/pull/635) (via #667)

### Closed (superseded or withdrawn)

- [#1286](https://github.com/MemPalace/mempalace/pull/1286) (drifted; @igorls closed and re-extracted the surgical fix as #1377 with co-author credit)
- [#1171](https://github.com/MemPalace/mempalace/pull/1171) (cross-process write lock — superseded by #976 + daemon-strict)
- [#1146](https://github.com/MemPalace/mempalace/pull/1146) (duplicate of @igorls's [#1147](https://github.com/MemPalace/mempalace/pull/1147))
- [#1115](https://github.com/MemPalace/mempalace/pull/1115) (premature, withdrew pending [#1069](https://github.com/MemPalace/mempalace/issues/1069) arbitration)
- [#629](https://github.com/MemPalace/mempalace/pull/629), [#632](https://github.com/MemPalace/mempalace/pull/632), [#662](https://github.com/MemPalace/mempalace/pull/662), [#663](https://github.com/MemPalace/mempalace/pull/663), [#738](https://github.com/MemPalace/mempalace/pull/738), [#1036](https://github.com/MemPalace/mempalace/pull/1036) — all superseded; see commit history for context

## Sources

Articles, surveys, and research that shaped the fork's direction.

### Synthesis and research (this fork)

- [**`docs/research/compass_artifact_wf-ad108fcc-…md`**](docs/research/compass_artifact_wf-ad108fcc-3960-4eab-ad5d-234bf365b2f4_text_markdown.md) — "Four layers and a methodology question." Source for the four-layer model and the retrieval-recall vs QA-accuracy distinction.
- [**`docs/research/compass_artifact_wf-28bac4e8-…md`**](docs/research/compass_artifact_wf-28bac4e8-71d9-4175-837a-d4ad563aec8d_text_markdown.md) — "Agent Memory Systems in 2026." Landscape survey: compile-upstream vs verbatim-first, three retrieval patterns, Cat 9 / Handshake, invocation as bottleneck.
- [**`docs/research/three-patterns-for-agent-memory.md`**](docs/research/three-patterns-for-agent-memory.md) — Source for the SME jp-realm-v0.1 46.67% / 78.33% finding and the stacked-architecture proposal (parallel hybrid as retrieval / Familiar's pipeline as fusion / RLM as composition).
- [**`docs/research/three-mempalace-consumers.md`**](docs/research/three-mempalace-consumers.md) — Familiar / CampaignGenerator / Kent triangulation. Convergent design decisions, divergent intelligence-layer bets.
- [**`docs/research/convergent-findings-kostadis-comparison.md`**](docs/research/convergent-findings-kostadis-comparison.md) — Companion to Kostadis's RLM-comparison piece. Articulates why deterministic intermediate compression is a precision discipline, not just a performance optimization.
- [**`docs/research/adaptmem-orthogonal-layers.md`**](docs/research/adaptmem-orthogonal-layers.md) — Encoder fine-tuning as an orthogonal layer; independent reproduction of MemPalace's 0.966 R@5 plus FT-300 lift.
- [**`docs/research/2026-05-06-chunking-strategy-ablation.md`**](docs/research/2026-05-06-chunking-strategy-ablation.md) — A/B/C chunking strategy ablation. The 2026 articles' thesis didn't reproduce on this corpus.
- [**`docs/internal/pgvector-665-decision.md`**](docs/internal/pgvector-665-decision.md) — Composition stance for the substrate work: WAIT for #665, with Plan-B trigger date 2026-06-08.

### External

- [**lhl/agentic-memory**](https://github.com/lhl/agentic-memory) — multi-system analysis. The MemPalace review at [`ANALYSIS-mempalace.md`](https://github.com/lhl/agentic-memory/blob/main/ANALYSIS-mempalace.md) seeded the original 7-item roadmap.
- [**codingwithcody.com — "MemPalace: digital castles on sand"**](https://codingwithcody.com/2026/04/13/mempalace-digital-castles-on-sand/) — TagMem-promotion critique whose hierarchy-causes-bugs argument produced architectural principles 1 and 2.
- [**OSS Insight — Agent Memory Race 2026**](https://ossinsight.io/blog/agent-memory-race-2026) — competitive landscape survey.
- [**InfoQ — Grafana rearchitects Loki with Kafka and ships a CLI to bring observability into coding agents**](https://www.infoq.com/news/2026/04/grafana-loki-ai-agents/) — verbatim-first observability precedent at scale; GCX CLI as agent-bridge prior art; o11y-bench as parallel to multipass-structural-memory-eval.
- [**Anthropic — Dreams (Managed Agents API)**](https://platform.claude.com/docs/en/managed-agents/dreams) — input read-only, output a separate store. The verbatim-vs-derivative axis at the vendor-API level.
- [**Claude Code Auto Dream guide**](https://claudefa.st/blog/guide/mechanics/auto-dream) — research-preview consolidation inside Claude Code; reads memory + JSONL, writes consolidated index.
- [**Microsoft Tech Community — Combining pgvector and Apache AGE: knowledge graph & semantic intelligence in a single engine**](https://techcommunity.microsoft.com/blog/adforpostgresql/combining-pgvector-and-apache-age---knowledge-graph--semantic-intelligence-in-a-/4508781) (Raunak, 2026-04-15) — bridge-pattern reference for the substrate work.
- [**Dave's Garage — "My Custom AI Went Superhuman Yesterday..."**](https://www.youtube.com/watch?v=TdbpoDjIvPk) (Dave Plummer, 2026-02-28) — conceptual reference for why graph structure matters in retrieval: vectors get you "topically nearby"; the graph gets you "actually related."
- [**Phil Karlton's two hard things**](https://martinfowler.com/bliki/TwoHardThings.html) — naming and cache invalidation. Cited in "What this fork has learned" because, even at ~160K drawers, the day-to-day operational work is still mostly these two.
- [**Recursive Language Models**](https://arxiv.org/abs/2512.24601) (Zhang, Kraska, Khattab, 2025) — the RLM paper. The four-layer section's invocation-ceiling finding is measured against this paper's mechanism.
- [**Think, But Don't Overthink: Reproducing Recursive Language Models**](https://arxiv.org/abs/2603.02615) — depth-2 collapse finding behind the recommendation to keep RLM at composition rather than retrieval.

### Systems inspiring roadmap items

- [**Karta**](https://github.com/rohithzr/karta) — contradiction detection, dream-engine feedback loop, foresight signals. Inspires P3/P4/P5; the heavier per-write LLM features are deprioritized.
- [**Codex memory**](https://github.com/openai/codex) — citation-driven retention. Influences P3.
- [**ByteRover CLI**](https://github.com/campfirein/byterover-cli) — 5-tier progressive retrieval. Pattern to consider for context-feeding.
- [**engram**](https://github.com/NickCirv/engram) — Go + SQLite FTS5; file-read interception prototype. Cited in deprioritized FTS5 item and the auto-surfacing problem.
- [**context-engine**](https://github.com/Emmimal/context-engine) — exponential decay implementation that ports directly into P2.
- **Verbatim-first cohort** — Longhand, Celiums, mcp-memory-service. Different scopes, same architectural call.
- **Peer builds on MemPalace** — Familiar, CampaignGenerator, Kent, adaptmem. Convergent architectural decisions, divergent intelligence-layer bets — the empirical evidence for the four-layer model.

### Verification note

Comparison table columns filled 2026-04-14–18, refreshed 2026-05-11; feature status drifts. Cite upstream before treating any row as current. [TagMem](https://codingwithcody.com/2026/04/13/mempalace-digital-castles-on-sand/) is omitted; we couldn't find a public repo for it.

## License

MIT — see [LICENSE](LICENSE).
