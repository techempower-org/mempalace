# Bibliography & Documentation Index

Complete index of fork documentation and external references.

## Fork documentation

### Architecture & design

- [**ARCHITECTURE.md**](ARCHITECTURE.md) — four-layer model, thesis (operational principles), design principles, two memory layers
- [**ECOSYSTEM.md**](ECOSYSTEM.md) — peer systems, companion tools, evaluation frameworks, active forks
- [**CLOSETS.md**](CLOSETS.md) — closets (the searchable index layer) and how they relate to drawers
- [**postgres_backend.md**](postgres_backend.md) — PostgreSQL backend overview (for larger/team deployments)
- [**schema.sql**](schema.sql) — SQLite knowledge graph schema (legacy, pre-AGE)

### Research

- [**compass_artifact_wf-ad108fcc-…md**](research/compass_artifact_wf-ad108fcc-3960-4eab-ad5d-234bf365b2f4_text_markdown.md) — "Four layers and a methodology question." Source for the four-layer model and the retrieval-recall vs QA-accuracy distinction.
- [**compass_artifact_wf-28bac4e8-…md**](research/compass_artifact_wf-28bac4e8-71d9-4175-837a-d4ad563aec8d_text_markdown.md) — "Agent Memory Systems in 2026." Landscape survey: compile-upstream vs verbatim-first, three retrieval patterns, Cat 9 / Handshake, invocation as bottleneck.
- [**three-patterns-for-agent-memory.md**](research/three-patterns-for-agent-memory.md) — SME jp-realm-v0.1 46.67% / 78.33% finding and the stacked-architecture proposal.
- [**three-mempalace-consumers.md**](research/three-mempalace-consumers.md) — Familiar / CampaignGenerator / Kent triangulation. Convergent design decisions, divergent intelligence-layer bets.
- [**convergent-findings-kostadis-comparison.md**](research/convergent-findings-kostadis-comparison.md) — Why deterministic intermediate compression is a precision discipline.
- [**adaptmem-orthogonal-layers.md**](research/adaptmem-orthogonal-layers.md) — Encoder fine-tuning as an orthogonal layer; independent reproduction of MemPalace's 0.966 R@5.
- [**verbatim-vs-derivative-axis.md**](research/verbatim-vs-derivative-axis.md) — Standalone reference for the foundational architectural call.
- [**2026-05-06-chunking-strategy-ablation.md**](research/2026-05-06-chunking-strategy-ablation.md) — A/B/C chunking strategy ablation. The 2026 articles' thesis didn't reproduce on this corpus.
- [**2026-05-15-multi-encoder-rrf.md**](research/2026-05-15-multi-encoder-rrf.md) — Multi-encoder RRF research feature.
- [**2026-05-15-rrf-eval-3way.json**](research/2026-05-15-rrf-eval-3way.json) — RRF evaluation benchmark data (3-way).
- [**uncertainty-aware-retrieval.md**](research/uncertainty-aware-retrieval.md) — Uncertainty-aware retrieval analysis.
- [**2026-05-24-true-memory-comparison.md**](research/2026-05-24-true-memory-comparison.md) — True Memory (arXiv:2605.04897) vs MemPalace: six-vs-four layers, benchmark comparison, convergent verbatim-first design.
- [**2026-05-24-memory-system-benchmarks.md**](research/2026-05-24-memory-system-benchmarks.md) — Landscape benchmark survey: LongMemEval/LoCoMo/BEAM scores for 20+ systems, methodology concerns, multipass SME gap analysis.

### Design docs

- [**multi-palace-separation.md**](designs/multi-palace-separation.md) — Multi-palace separation: curated vs auto-mined (design, not implemented).
- [**scope-collection-filter.md**](designs/scope-collection-filter.md) — Scope/collection filter on `mempalace_search` ([#76](https://github.com/techempower-org/mempalace/issues/76)).

### RFCs

- [**002-source-adapter-plugin-spec.md**](rfcs/002-source-adapter-plugin-spec.md) — Source adapter plugin specification (draft).

### Specs (implementation designs)

- [**auto-query-integration.md**](specs/auto-query-integration.md) — Auto-query integration spec (draft).
- [**2026-04-10-tool-output-mining-design.md**](superpowers/specs/2026-04-10-tool-output-mining-design.md) — Tool output capture in conversation mining.
- [**2026-04-25-checkpoint-collection-split.md**](superpowers/specs/2026-04-25-checkpoint-collection-split.md) — Checkpoint collection split design (historical — retired May 5).
- [**2026-04-26-downstream-eval-findings.md**](superpowers/specs/2026-04-26-downstream-eval-findings.md) — What Familiar v0.2.1 + multipass surfaced.
- [**2026-05-05-verbatim-only-design.md**](superpowers/specs/2026-05-05-verbatim-only-design.md) — Verbatim-only mempalace design.
- [**2026-05-10-pgvector-age-migration-design.md**](superpowers/specs/2026-05-10-pgvector-age-migration-design.md) — Postgres + pgvector + Apache AGE substrate migration design.
- [**2026-05-21-power-resilience-design.md**](superpowers/specs/2026-05-21-power-resilience-design.md) — Power-event resilience design.

### Implementation plans

- [**2026-04-10-tool-output-mining.md**](superpowers/plans/2026-04-10-tool-output-mining.md) — Tool output mining implementation plan.
- [**2026-04-25-checkpoint-collection-split-impl.md**](superpowers/plans/2026-04-25-checkpoint-collection-split-impl.md) — Checkpoint collection split implementation plan (historical).
- [**2026-05-10-pgvector-age-migration-impl.md**](superpowers/plans/2026-05-10-pgvector-age-migration-impl.md) — Postgres + pgvector + Apache AGE implementation plan.

### Operator docs

- [**pgvector-cutover-runbook.md**](operators/pgvector-cutover-runbook.md) — Operator-driven pgvector + AGE cutover runbook.
- [**2026-05-15-drop-canonical-room-fk.sql**](operators/2026-05-15-drop-canonical-room-fk.sql) — Migration SQL for dropping canonical room FK.

### Internal decisions

- [**pgvector-665-decision.md**](internal/pgvector-665-decision.md) — Composition stance: WAIT for upstream #665, with Plan-B trigger date 2026-06-08.
- [**sh-shim-strategy.md**](fork-decisions/sh-shim-strategy.md) — `.sh` shim delegation strategy (active fork-ahead decision).

### Integrations

- [**opencode.md**](integrations/opencode.md) — OpenCode + MemPalace integration (fork-routed via palace-daemon).

### Investigations & recovery

- [**metadata-reshape-root-cause.md**](investigations/metadata-reshape-root-cause.md) — Metadata reshape root-cause investigation (#32).
- [**index-metadata-recovery.md**](recovery/index-metadata-recovery.md) — Recovery for chromadb segment quarantined with `dimensionality: None`.

### Reference & operational

- [**HISTORY.md**](HISTORY.md) — Post-launch corrections, public notices, and retractions (upstream).
- [**RELEASING.md**](RELEASING.md) — Release checklist for mempalace-mcp pre-release.
- [**format-coverage.md**](format-coverage.md) — Format coverage for `mempalace mine --mode extract` (shipped in 3.3.6).
- [**virtual-line-numbering.md**](virtual-line-numbering.md) — Virtual line numbering for drawers (proposed).
- [**mempalace-config.yaml.example**](mempalace-config.yaml.example) — Example mining config.
- [**fork-changes/**](fork-changes/README.md) — Canonical source for fork-ahead changes, one file per entry (renders to FORK_CHANGELOG.md).

### Benchmarks

- [**2026-05-14-search-bench-hybrid-cutover.json**](benchmarks/2026-05-14-search-bench-hybrid-cutover.json) — Search benchmark data from hybrid cutover.
- [**2026-05-15-remaining-benches.json**](benchmarks/2026-05-15-remaining-benches.json) — Remaining bench suite results (8/9 pass on postgres).

---

## External references

- [**lhl/agentic-memory**](https://github.com/lhl/agentic-memory) — multi-system analysis. The MemPalace review at [`ANALYSIS-mempalace.md`](https://github.com/lhl/agentic-memory/blob/main/ANALYSIS-mempalace.md) seeded the original 7-item roadmap.
- [**codingwithcody.com — "MemPalace: digital castles on sand"**](https://codingwithcody.com/2026/04/13/mempalace-digital-castles-on-sand/) — TagMem-promotion critique whose hierarchy-causes-bugs argument produced architectural principles 1 and 2.
- [**OSS Insight — Agent Memory Race 2026**](https://ossinsight.io/blog/agent-memory-race-2026) — competitive landscape survey.
- [**InfoQ — Grafana rearchitects Loki with Kafka**](https://www.infoq.com/news/2026/04/grafana-loki-ai-agents/) — verbatim-first observability precedent at scale; GCX CLI as agent-bridge prior art.
- [**Anthropic — Dreams (Managed Agents API)**](https://platform.claude.com/docs/en/managed-agents/dreams) — input read-only, output a separate store. The verbatim-vs-derivative axis at the vendor-API level.
- [**Claude Code Auto Dream guide**](https://claudefa.st/blog/guide/mechanics/auto-dream) — research-preview consolidation inside Claude Code.
- [**Microsoft — Combining pgvector and Apache AGE**](https://techcommunity.microsoft.com/blog/adforpostgresql/combining-pgvector-and-apache-age---knowledge-graph--semantic-intelligence-in-a-/4508781) (Raunak, 2026-04-15) — bridge-pattern reference for the substrate work.
- [**Dave's Garage — "My Custom AI Went Superhuman Yesterday..."**](https://www.youtube.com/watch?v=TdbpoDjIvPk) (Dave Plummer, 2026-02-28) — why graph structure matters in retrieval.
- [**Phil Karlton's two hard things**](https://martinfowler.com/bliki/TwoHardThings.html) — naming and cache invalidation.
- [**Recursive Language Models**](https://arxiv.org/abs/2512.24601) (Zhang, Kraska, Khattab, 2025) — the RLM paper.
- [**Think, But Don't Overthink: Reproducing RLM**](https://arxiv.org/abs/2603.02615) — depth-2 collapse finding.

## Systems inspiring roadmap items

- [**Karta**](https://github.com/rohithzr/karta) — contradiction detection, dream-engine feedback loop, foresight signals.
- [**Codex memory**](https://github.com/openai/codex) — citation-driven retention.
- [**ByteRover CLI**](https://github.com/campfirein/byterover-cli) — 5-tier progressive retrieval.
- [**engram**](https://github.com/NickCirv/engram) — Go + SQLite FTS5; file-read interception prototype.
- [**context-engine**](https://github.com/Emmimal/context-engine) — exponential decay implementation.
- **Verbatim-first cohort** — Longhand, Celiums, mcp-memory-service.
- **Peer builds on MemPalace** — Familiar, CampaignGenerator, Kent, adaptmem.
