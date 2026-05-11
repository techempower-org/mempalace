# Three Patterns for Agent Memory Retrieval

*Notes on comparing deterministic pipelines, LLM-orchestrated retrieval, and parallel hybrid against a verbatim-first memory substrate.*

## TL;DR

- Agent memory benchmarks measure what an isolated retrieval pipeline can do. Production systems also need to measure whether the agent reaches the pipeline at all. Those are different numbers.
- First live readings from [SME](https://github.com/jphein/multipass-structural-memory-eval) on the `jp-realm-v0.1` corpus (30 questions): `rlm + Qwen 2.5 7B` and `rlm + Llama 3.3 70B` both score **46.67%** recall despite a 4× difference in tool-invocation rate. Familiar's deterministic pipeline on the same task: **78.33%**.
- The two RLM runs ceiling at the orchestrator's willingness to call the tool, not at retrieval quality. **Invocation, not expression, is the dominant bottleneck.**
- This breaks part of the prevailing narrative — including [Pinecone Nexus's KnowQL pitch](https://www.pinecone.io/blog/knowledge-infrastructure-for-agents/) that a more declarative query language is the fix. A better query language doesn't help if the agent never issues a query.
- Three retrieval architectures co-exist (deterministic, LLM-orchestrated, parallel hybrid). They are not mutually exclusive; the right deployment stacks them. Specific stacking recommendation and three SME adapter proposals below.

## The architectural debate

Agent memory in 2026 has two opposing structural bets.

**Compile-upstream** (Pinecone Nexus): reasoning costs are high at inference, so do the work once when data changes. Agents get typed task-specific artifacts via a declarative query language (KnowQL). The bet is that agents should never see raw documents.

**Verbatim-on-write** (MemPalace): preservation costs are low — just store raw — so defer all interpretation. Agents get raw text plus good retrieval. The bet is that derived artifacts lose the exact command/error/snippet you actually need to recover.

The OSS landscape has heavily explored the compile-upstream side — Cognee, Letta, Mem0, Graphiti, LightRAG, GraphRAG — almost all of which transform on write. Verbatim-first storage with MCP exposure is rare. MemPalace is the most production-tested instance we know of, at 134K+ drawers across 60+ rooms.

Both bets fail on the same problem from different sides: **the agent doesn't know what to ask for.** Nexus compensates by making the query language structured enough to force intent declaration. MemPalace's [README](https://github.com/jphein/mempalace) explicitly flags this as the open problem it has not solved.

## A three-layer model

A complete agent memory system needs three layers, and most products today bundle one of them as if it were the whole system.

1. **Archive (system of record)** — verbatim, append-only, source of truth. Vector + BM25 retrieval. MemPalace lives here.
2. **Compiled views (system of knowledge)** — task-shaped derived artifacts: decision logs, error→fix pairs, command histories, entity graphs with temporal validity. Generated as background processes from Layer 1. Versioned. Traceable. Nexus sells this layer, bundled with its own Layer 1. The OSS parts to build it exist (DSPy for typed declarations, GraphRAG for extraction, Apache AGE for graph queries, Postgres for unified substrate) but no one has packaged them as a coherent layer with a public contract.
3. **Surfacing (system of attention)** — push (file-read interception, PreToolUse hooks, context injection), pull (typed tool schemas tight enough to force correct invocation), and feedback (rate, decay, prune). This is where systems live or die. Every memory system regardless of architecture has this problem.

The SME data point is empirical evidence that Layer 3 is the dominant bottleneck. Layer 1 and Layer 2 improvements have diminishing returns past good retrieval. Layer 3 improvements have step-function effects on end-to-end task performance.

## The empirical finding

From SME's first live readings on the `jp-realm-v0.1` corpus (30 questions):

| Run | Mean recall | Tool-call distribution |
|---|---|---|
| `rlm` + Qwen 2.5 7B Q5_K_M | 46.67% | 25/30 zero-call, 2/30 used tool |
| `rlm` + Llama 3.3 70B | 46.67% | 22/30 zero-call, 8/30 used tool |
| `familiar` v0.3.9 (deterministic) | 78.33% | n/a |

Two RLM runs at identical recall despite a 4× difference in tool-invocation rate. Model capability doesn't fix invocation; it just makes confidently-not-calling marginally less common. The 30+ point gap to the deterministic pipeline is what Layer 3 looks like when measured.

This is the data behind SME's [Cat 9a invocation-rate issue](https://github.com/M0nkeyFl0wer/multipass-structural-memory-eval/issues/3) and the engram-2 critique (0.984 R@5 but 17% E2E QA accuracy) that motivated Cat 9 in the first place.

**Calibration:** 30 questions, beta-level instrumentation, substring-on-filename scoring. The defensible findings are deltas under identical conditions; absolute numbers are decoration. The shape of the result — invocation ceiling — generalizes; the exact percentages do not.

## Three architectural patterns

Each pattern is a distinct hypothesis about what makes retrieval work.

### 1. Deterministic pipeline (Familiar)

Good preprocessing plus good ranking on a single always-on pipeline. [Familiar](https://github.com/jphein/familiar.realm.watch) v0.3.9: rerank, temporal decay, extractive compression, grounding directives. No LLM-side invocation decision — pipeline runs by default, agent consumes.

- **Strength:** sidesteps the invocation problem entirely.
- **Weakness:** does not compose well over multi-hop questions; cannot decide when more aggressive reasoning is needed.

### 2. LLM-orchestrated ([RLM](https://github.com/alexzhang13/rlm))

Root LM gets a Python REPL with the long input as a persistent variable. Inspects, decomposes, and recursively calls itself over snippets. From the [reproduction work](https://arxiv.org/abs/2603.02615): depth-1 RLMs dramatically improve complex reasoning but hurt simple retrieval; depth-2 collapses format and explodes latency.

- **Strength:** programmatic inspection of long contexts; strong on multi-hop composition.
- **Weakness:** the orchestrator decides when to invoke retrieval. In settings where the model does not know if memory has anything relevant, it does not ask. The SME table is exactly this failure mode.

### 3. Parallel hybrid ([Hindsight](https://hindsight.vectorize.io/blog/2026/03/27/parallel-hybrid-search) / [MiroFish](https://openflows.org/currency/currents/mirofish/) / [HyMem](https://arxiv.org/abs/2602.13933))

Multiple retrieval strategies fired in parallel, fused with RRF, optionally reranked with a cross-encoder. MiroFish's three-tier (depth via sub-query decomposition, breadth via full-scope fan-out, perspective via different angles) is one shape. Hindsight's four-way (semantic + BM25 + temporal + graph) is another. HyMem adds dynamic scheduling between lightweight and deep modules per query complexity.

- **Strength:** strategy diversity captures complementary recall; sidesteps single-pipeline brittleness.
- **Weakness:** cost scales with branches; fusion of heterogeneous outputs is non-trivial; the shared connection pool can become the contention point before the queries do.

## A stacked architecture

Treat these as layers, not competitors.

- **Retrieval layer:** parallel hybrid. Semantic + BM25 + AGE graph + temporal, optionally fanned out across query variants, RRF-fused. Runs unconditionally on every turn.
- **Fusion / ranking layer:** Familiar's existing pipeline. Rerank, temporal decay, extractive compression, grounding directives applied to the fused candidate set.
- **Composition layer:** RLM, invoked only for questions classified as multi-hop. RLM as a reasoning strategy over a known-relevant retrieval, not as a dispatcher to memory.

This collapses the "should I retrieve" decision (parallel hybrid always runs) and reserves expensive inspection (RLM) for the cases where it actually helps. It is the architecture the SME data implies, with each layer addressing a different failure mode.

## SME adapters to write

Three adapters that would test the layered architecture empirically.

| Adapter | Tests | Predicted Cat 1 | Predicted Cat 4/5 |
|---|---|---|---|
| `parallel-hybrid` | Strategy diversity hypothesis | 80–85% | ≈ familiar |
| `three-tier` (MiroFish-shaped) | Perspective tier value (entity fan-out across AGE) | ≈ familiar | > familiar |
| `stacked` (full pipeline) | Composed architecture | ≥ 85% | best of all |

The `perspective` tier in `three-tier` is the genuinely novel piece for a MemPalace-shaped system. The AGE graph has the structure to fan out queries to different entity neighborhoods or wings simultaneously — no other published memory system has the underlying graph to do this cleanly. The hypothesis: perspective-tier parallelism finds different evidence than scoring-function parallelism, and the union beats either alone.

Predictions are deliberately falsifiable. If `parallel-hybrid` lands at 60% on Cat 1, the strategy-diversity hypothesis is weaker than the deterministic-pipeline hypothesis. If `stacked` only matches `familiar` on Cat 4/5, the composition layer is not earning its cost. Either result is informative.

## Practical caveats

**Postgres connection pool is the real bottleneck under parallel load.** From Hindsight's writeup: `max_conn_wait` can spike past 200 ms because branches wait on the pool, not because queries are slow. Semantic + BM25 + temporal on a shared connection sometimes beats fully-parallel across the pool. PgBouncer or pgcat config matters once you are firing 4+ branches per query. Log connection-acquisition time, not just query time.

**RLM is a reasoning strategy, not a retrieval strategy.** Putting RLM in parallel with BM25 negates the efficiency win — you pay RLM's cost on every query. RLM as final composition over a fused candidate set is the right architectural placement. The framework should distinguish retrieval adapters from composition adapters cleanly.

**Parallel hybrid does not fix Cat 9.** Pushing Cat 1–8 numbers up does not help if the agent never reaches the pipeline. Parallel hybrid is most useful as an *unconditional* always-running stage. If parallel hybrid is gated behind LLM tool-call decisions like RLM is, the same 46.67% ceiling applies regardless of how good the retrieval gets. The SME Cat 9 delta between gated and ungated parallel-hybrid would prove this empirically and is the more important measurement than Cat 1 recall.

## Open questions

1. **Invocation calibration is non-uniform.** Cat 9b broken down by question category would show whether LLMs fail to invoke more on certain shapes (factual vs synthesis vs ontology). The fix is different depending.
2. **Forced-invocation ablation.** If a system prompt that mandates a MemPalace check on every turn closes the 46.67% → 78.33% gap, orchestrator willingness really is the only bottleneck. If it does not, there is a query-formulation problem hiding underneath that is worth isolating separately.
3. **Push-side surfacing.** engram-style file-read interception is the most underinvested OSS direction for Layer 3. It addresses invocation by removing the decision entirely.
4. **Layer 2 as an open standard.** The OSS parts to build the compiled-views layer exist; nobody has packaged them with a public contract. Whoever ships an open Layer 2 standard with Layer 1 portability and Layer 3 surfacing as constraints shapes what the stack looks like.

## References

- [Pinecone Nexus announcement](https://www.pinecone.io/blog/knowledge-infrastructure-for-agents/)
- [Recursive Language Models (Zhang et al., MIT, late 2025)](https://www.primeintellect.ai/blog/rlm)
- [Think, But Don't Overthink: Reproducing Recursive Language Models](https://arxiv.org/abs/2603.02615)
- [Hindsight 4-way parallel hybrid search writeup](https://hindsight.vectorize.io/blog/2026/03/27/parallel-hybrid-search)
- [MiroFish memory OS (Openflows)](https://openflows.org/currency/currents/mirofish/)
- [HyMem: Hybrid Memory Architecture with Dynamic Retrieval Scheduling](https://arxiv.org/abs/2602.13933)
- [MemPalace (jphein fork)](https://github.com/jphein/mempalace)
- [SME — multipass-structural-memory-eval](https://github.com/jphein/multipass-structural-memory-eval)
- [Familiar](https://github.com/jphein/familiar.realm.watch)
- [palace-daemon](https://github.com/jphein/palace-daemon)

## License

MIT.
