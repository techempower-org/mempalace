# Four layers and a methodology question: a synthesis across MemPalace contributors

This is an attempt to map what has emerged across multiple contributors' work on MemPalace and related memory architectures over the past month. It's descriptive rather than advocacy — I'm trying to present the layered model and empirical findings as they've appeared from work across the community, not to land a position about which architecture is correct.

The synthesis needs to start with the methodology question raised in [#747](https://github.com/MemPalace/mempalace/discussions/747) by @rohitg00 and seconded by @terrizoaguimor, because it's load-bearing for everything that follows. MemPalace's published 96.6% R@5 on LongMemEval is a retrieval-only metric (recall_any@5), not the end-to-end QA accuracy that appears on the LongMemEval public leaderboard. The leaderboard reports QA accuracy — retrieve, generate answer, invoke GPT-4o judge. An independent tester running the full pipeline ([Issue #39](https://github.com/MemPalace/mempalace/issues/39)) got 82.6% QA accuracy. Still competitive, but a substantively different number. This distinction — between finding the right information and reasoning correctly about it — runs through all the work below.

## The four composable layers

What's emerged from contributors working independently on different domains is a four-layer architecture where improvements at each layer compose. This isn't what any single contributor proposed; it's what multiple practitioners arrived at while building on the same substrate.

**Storage** is the first layer: verbatim drawers holding original text. MemPalace upstream uses ChromaDB; my production fork has a Postgres + pgvector + Apache AGE migration in progress. The substrate question — transactional consistency, graph queries in-database, concurrent writes under load — is real but is a separate debate from the layers above. What everyone agrees on: no LLM in the index path, verbatim storage as base layer.

**Encoder** is the second layer: the embedding model itself. This layer is often invisible in architectural discussions because most systems treat it as a vendor decision. @nakata-app's work on adaptmem operates here via contrastive fine-tuning on hard negatives mined from retrieval errors. The finding that encoder fine-tuning lifts retrieval independently of retrieval-layer changes is one of the clearest pieces of evidence for layer orthogonality.

**Retrieval** is the third layer: how vectors are queried, combined, reranked. Semantic search, BM25, hybrid combinations, cross-encoder reranking, hierarchical pruning all live here. MemPalace's hybrid_v4 operates at this layer. @kostadis's hierarchical AAAK pruning — deterministic projection-based indexing that achieved 19.82× cost reduction at 0% recall@10 loss on a 281-entry RPG benchmark fixture — is a retrieval-layer optimization. External references like Hindsight's 4-way parallel hybrid (semantic + BM25 + graph + temporal with RRF fusion) and HyMem's dynamic two-tier scheduling (lightweight module for simple queries, LLM-based deep module selectively activated for complex queries) demonstrate different production patterns at this layer.

**Consumption** is the fourth layer: what happens after retrieval. This is where the QA-accuracy gap lives. High retrieval recall is necessary but not sufficient for high QA accuracy. @terrizoaguimor's Celiums benchmarking demonstrates this structurally: retrieval rate 100%, but QA accuracy with Opus 4.6 only 62.3%. The synthesis difficulty is in generation, not retrieval. Contributors are taking three different bets about where intelligence above retrieval should live:

- **Algorithm** (Familiar's deterministic pipeline): rerank, temporal decay, extractive compression, grounding directives. Always-on pipeline that sidesteps the invocation problem by never not-calling.
- **Human-in-the-loop** (@kostadis's retrieve/render isolation CI invariant): cost-tagged multi-source candidates, schema-aware indexing via 5etools entity wrappers, static-analysis enforcement that render functions can't call LLMs. The human decides; the system surfaces options.
- **Trained policy** (@kenchambers's Kent APO subsystem): training memory invocation policies through recall games (A: recall@k, B: scope, C: closet fidelity, D: tunnel utility), wake-up + compaction refresh patterns, heartbeat agent, channel-as-wing automatic routing. Let the agent learn when and how to invoke memory.

These are different answers to the same consumption problem and may not be mutually exclusive.

## Empirical findings with metric labels

The table below presents findings from across contributors. Every metric is labeled explicitly because R@k retrieval-recall, QA accuracy, embedding similarity, and cost reduction measure different things, and mixing them is a category error.

| Finding | Metric Type | Result | Source |
|---------|-------------|--------|--------|
| MemPal raw default on LongMemEval-S | Retrieval R@5 | 0.966 | @nakata-app reproduction, matches published |
| MemPal + adaptmem FT-300 encoder fine-tune | Retrieval R@5 | 0.980 (+1.4pp) | @nakata-app |
| MemPal hybrid_v4 + adaptmem FT-300 | Retrieval R@5 / R@1 | 0.990 / 0.916 | @nakata-app |
| Hierarchical AAAK pruning on RPG corpus | Cost reduction at 0% R@10 loss | 19.82× cheaper | @kostadis hierarchical_aaak_gate1 |
| Familiar v0.3.9 deterministic pipeline | Retrieval recall (substring-on-filename) | 78.33% (23/30) | jphein SME jp-realm-v0.1 |
| RLM + Qwen 2.5 7B orchestrator | Retrieval recall (substring-on-filename) | 46.67% (14/30, 25 zero-call) | jphein SME jp-realm-v0.1 |
| RLM + Llama 3.3 70B orchestrator | Retrieval recall (substring-on-filename) | 46.67% (14/30, 22 zero-call) | jphein SME jp-realm-v0.1 |
| MemPalace full E2E pipeline | QA accuracy | 82.6% | Independent tester, Issue #39 |
| Celiums + Opus 4.6 (retrieval rate 100%) | QA accuracy | 62.3% | @terrizoaguimor |
| Kent APO drawer-aware queries | Embedding similarity | avg 0.323 vs 0.027 unrelated | @kenchambers Round 01 |

**Key findings:**

1. **Orthogonal stacking** (@nakata-app): Encoder fine-tuning and hybrid retrieval stack independently. Raw → raw+FT-300 adds 1.4pp R@5. Hybrid_v4 + FT-300 reaches 0.990 R@5, 0.916 R@1. Whether this lifts QA accuracy is unmeasured, but the retrieval-layer evidence is clean.

2. **Invocation as bottleneck** (jphein SME, 30-question jp-realm-v0.1 corpus, beta instrumentation): Both RLM runs ceiling at orchestrator willingness to invoke memory, not retrieval quality. 25/30 and 22/30 questions get zero memory calls despite relevant memories existing. Familiar's always-on pipeline sidesteps this entirely: 78.33% recall. Defensible findings are deltas under identical conditions; absolute numbers are decoration. Substring-on-filename scoring is a weak rubric but sufficient for comparing retrieval patterns.

3. **The R@k → QA gap**: 96.6% retrieval recall (Issue #39 reproduction) drops to 82.6% QA accuracy when generation and judging are added. @terrizoaguimor's data shows this gap persists across models: 100% retrieval rate, but Opus 4.6 peaks at 62.3% QA, DeepSeek R1 70B at 51.6%, Sonnet 4.6 at 51.1%. The LongMemEval paper (ICLR 2025) reports retrieval R@5 with Stella V5 at 64-73% and QA accuracy with GPT-4o oracle at 87-92%. High retrieval is necessary but not sufficient.

4. **APO training signal** (@kenchambers): Drawer-aware queries scored avg sim 0.323 vs 0.027 for unrelated (3/3 pairwise wins). APO Round 01 completes with v0=0.866 wins vs edited v1=0.778. Algorithm phase works; multi-round improvement validation in progress. This measures embedding-similarity signal, not task performance.

5. **Hierarchical pruning cost reduction** (@kostadis): 19.82× cheaper than flat search with 0% recall@10 degradation on 281-entry RPG corpus. Deterministic AAAK projections, no LLM in index. Domain-specific finding; generalization to broader corpora unmeasured.

The methodology question from [#747](https://github.com/MemPalace/mempalace/discussions/747) matters because it clarifies what each layer's improvements actually demonstrate. Retrieval-recall metrics measure the first three layers (storage, encoder, retrieval). QA-accuracy metrics measure all four layers including consumption. Both matter, but they're not interchangeable.

## Where the bets diverge: the consumption layer

The consumption layer is where work is most uneven across contributors and where the R@k → QA accuracy gap shows up most starkly. Three different architectural bets have emerged:

**Algorithm: Familiar's deterministic pipeline** treats memory retrieval as a data structure problem with a known-good traversal order. The v0.3.9 pipeline: rerank by relevance, apply temporal decay (fresher memories score higher), extract grounding directives (quoted phrases, entity mentions, temporal markers), compress to context budget, surface. No LLM in the pipeline; no invocation decision to make. The always-on design sidesteps the invocation bottleneck entirely — the SME jp-realm-v0.1 runs showed 78.33% recall vs 46.67% for both RLM configurations, where the gap was orchestrator willingness to call memory at all. The deterministic bet: if you know what good retrieval looks like, codify it. The limitation: algorithms don't adapt to novel query patterns without manual updates.

**Human-in-the-loop: @kostadis's retrieve/render isolation** enforces a static-analysis CI invariant: render functions cannot call LLMs. The system retrieves multi-source candidates, tags each with cost and provenance, then presents options. The human decides what to synthesize. In the CampaignGenerator stack over 2TB local PDFs + 5etools JSON, this pattern prevents runaway LLM costs during session prep while maintaining quality through human judgment. Schema-aware indexing via 5etools entity wrappers (monsters, spells, items with type-safe fields) enables precise filtering that pure text embeddings miss. The human-in-the-loop bet: synthesis quality comes from human expertise, not LLM reasoning. The limitation: doesn't scale to fully autonomous agents.

**Trained policy: @kenchambers's Kent APO subsystem** trains memory invocation as a learned behavior. The recall games framework defines four categories of memory fitness: A (recall@k), B (scope accuracy), C (closet fidelity — does compressed memory preserve key facts), D (tunnel utility — does memory help multi-turn reasoning). APO (Automatic Prompt Optimization, from Microsoft Agent Lightning) generates candidate invocation policies, runs them through recall games, scores fitness, iterates. Round 01 completed with drawer-aware queries showing 0.323 avg similarity vs 0.027 for unrelated (3/3 pairwise wins). Wake-up + compaction refresh pattern: agent wakes with slim context, retrieves on-demand, compacts after each turn. Channel-as-wing: Discord channels automatically map to memory scopes. The trained-policy bet: let agents learn when and how to invoke memory through reinforcement. The limitation: training infrastructure overhead, sample efficiency, and generalization across domains remain open questions.

Each approach surfaces patterns the others don't. Familiar demonstrates that deterministic pipelines can outperform LLM orchestration when the task structure is knowable. @kostadis's CI invariant shows that static analysis can enforce cost boundaries while preserving flexibility through human oversight. Kent's recall games provide a framework for measuring memory fitness across multiple dimensions simultaneously. These may not be mutually exclusive — a system could use deterministic pipelines for high-frequency queries, trained policies for complex multi-hop reasoning, and human oversight for high-stakes decisions.

## What everyone agrees on

Across contributors working on different domains — general agent use, RPG session prep, Discord-bot memory, encoder optimization, alternative memory systems — several architectural conclusions have converged:

**Verbatim storage as base layer.** No one is advocating for LLM-summarized storage upstream. The core MemPalace insight — raw verbatim text with good embeddings beats LLM extraction — holds across implementations. @rohitg00's agentmemory compresses to structured facts for context injection, but stores verbatim text in the retrieval layer. The storage layer keeps originals.

**No LLM in the index path.** Write-time LLM calls don't pay for themselves at scale. Deterministic chunking, regex classification, schema-aware entity extraction all appear across contributors. When LLMs appear, they're at consumption time (generation, reasoning, synthesis), not indexing time.

**Wings as scope routing, not classification.** The spatial metaphor (wings, rooms, drawers) provides human-navigable structure and metadata filtering that improves retrieval precision. This is validated across forks and alternative architectures. The mechanism is straightforward — directory paths or channel IDs map to metadata tags, retrieval filters on tags before vector search — but the UX benefit is real.

**The consumption problem is real.** High retrieval recall doesn't automatically produce high task performance. The 96.6% → 82.6% gap (retrieval R@5 → QA accuracy) and @terrizoaguimor's 100% retrieval → 62.3% QA finding demonstrate this structurally. The field hasn't solved synthesis at scale. Contributors are exploring different solutions (algorithm, human, training), but everyone acknowledges the problem.

**Recall floor matters.** Production systems need high retrieval recall before optimizing synthesis. @kostadis's 0% recall@10 loss constraint, @nakata-app's encoder fine-tuning lifting R@1 from 0.816 to 0.916, Familiar's 78.33% baseline — all start from "get the right documents first." Synthesis can't fix retrieval failures.

**Concurrent writes under load remain unsolved.** File locking workarounds, HNSW mtime detection, graph cache TTL, single-writer HTTP wrappers — these are all documented friction points. The Postgres + pgvector + Apache AGE migration in the jphein fork is partly meant to address this with transactional consistency and connection pooling. But the upstream ChromaDB + file-based architecture has real limitations at production scale that multiple contributors have hit independently.

## Open questions and where the conversation goes next

**Methodology standardization.** [Discussion #747](https://github.com/MemPalace/mempalace/discussions/747) and the seven issues it references (#27, #29, #39, #125, #314, #333, #367, #875) represent a community-wide push toward clearer metric labeling. The fact that @rohitg00 (agentmemory), @terrizoaguimor (Celiums), @dial481, and independent testers engaged seriously with benchmark methodology — providing specific corrections, running independent reproductions, publishing E2E QA numbers — is the community working through how to standardize comparisons. The question: how do we label retrieval-recall vs QA accuracy vs embedding similarity vs cost reduction so cross-system benchmarks can be honest?

**Cross-system benchmarking on shared corpora.** LongMemEval is one fixture. The jp-realm-v0.1 corpus (30 questions, substring-on-filename scoring, admittedly weak but sufficient for adapter comparison) is another. @kostadis's 281-entry RPG fixture serves domain-specific needs. The question: what shared corpora with clearly-labeled metrics would let contributors compare architectural choices fairly? Especially at the consumption layer where the evidence gap is widest.

**Whether orthogonality generalizes.** @nakata-app's finding that encoder fine-tuning and hybrid retrieval stack independently on LongMemEval is clean evidence for layer orthogonality. Whether this holds outside LongMemEval, on domain-specific corpora, with different embedding models, and from retrieval-recall to QA accuracy is unmeasured. If it generalizes, it validates the four-layer model architecturally. If it doesn't, the layers may couple in ways we don't yet understand.

**When ad-hoc decomposition pays off.** RLM-style sub-calls for queries where deterministic descent fails is an open question. The SME jp-realm-v0.1 runs showed RLM bottlenecking on invocation willingness, not retrieval quality, but that's with beta instrumentation on a 30-question sample. Recursive decomposition may have a place for complex multi-hop queries even if it underperforms on simple retrieval. The question: what query patterns benefit from decomposition vs deterministic pipelines?

**The need for an open Layer 2 standard.** Pinecone Nexus's KnowQL is the proprietary version — declarative query language with six primitives (intent, filter, provenance, output shape, confidence, budget) that compiles queries into persistent artifacts. If compiled views are the right abstraction, the OSS ecosystem needs a portable format so agents can interoperate across memory systems without vendor lock-in. What would a KnowQL equivalent look like as an open standard?

**The push-side surfacing problem.** Engram's file-read interception (replacing raw file reads with pre-computed structural summaries from AST, Git, and session miners) is the only credible OSS direction for the invocation problem at agent-tool boundaries. If agents can't be trusted to invoke memory reliably, intercept the file reads and inject context automatically. This is the extreme form of always-on: no invocation decision at all. The question: how do push-side patterns compose with retrieval patterns, and where does the balance tip from pull to push?

**Concurrent writes at production scale.** The single-writer HTTP wrapper (palace-daemon) and the Postgres + pgvector + Apache AGE migration are both attempts to address transactional consistency under concurrent writes. The ChromaDB + file-based upstream has documented limitations. The question: what's the right substrate for production multi-agent deployments where writes are frequent, agents are concurrent, and memory must remain consistent?

These are the questions the community is working through. The convergence on storage / encoder / retrieval layers and the divergence at the consumption layer suggest the field is settling some questions while actively exploring others.

## Contributors and their work

This synthesis draws from work by: @rohitg00 (agentmemory, methodology critique in [#747](https://github.com/MemPalace/mempalace/discussions/747)), @terrizoaguimor (Celiums, E2E QA benchmarking), @dial481 (issue cross-references and documentation audit), @nakata-app (adaptmem encoder fine-tuning, orthogonality findings), @kostadis (CampaignGenerator RPG stack, hierarchical AAAK pruning, retrieve/render isolation CI invariant), @kenchambers (Kent APO subsystem, recall games framework), @M0nkeyFl0wer (upstream SME fork parent), and the MemPalace upstream maintainers. My own contributions: jphein/mempalace production fork (Postgres + pgvector + AGE migration in progress), jphein/familiar.realm.watch (deterministic retrieval pipeline), jphein/palace-daemon (single-writer HTTP wrapper), jphein/multipass-structural-memory-eval (SME nine-category test menu with adapter comparison), and upstream PRs #659 (diary wing parameter), #660 (L1 importance pre-filter), #661 (graph cache with write-invalidation), #673 (deterministic hook saves), #681 (Unicode → ASCII for Windows compatibility).

The broader landscape context — Pinecone Nexus with KnowQL, Recursive Language Models (Zhang, Kraska, Khattab, arXiv:2512.24601), Cognee/Letta/Mem0/Graphiti compile-upstream systems, HyMem (arXiv 2602.13933) dual-granular storage, Hindsight 4-way parallel hybrid with connection-pool bottleneck findings, engram (NickCirv/engram) file-read interception — provides reference points but isn't the focus. This synthesis is about what the MemPalace community has built and is building.

The four-layer model and the methodology question aren't solved problems. They're where the conversation stands as of early May 2026, based on work that's been shared openly. The fact that multiple contributors are publishing reproducible numbers, engaging seriously with methodology critiques, and converging on architectural patterns despite working independently is the strength. The consumption layer remains the frontier.

—jphein