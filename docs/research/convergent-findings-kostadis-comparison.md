# Convergent Findings: Two MemPalace-Based RLM Adaptations

*A companion note on Kostadis Roussos's [`rlm_paper_comparison.md`](https://github.com/kostadis/CampaignGenerator/blob/main/docs/rlm_paper_comparison.md) and what it adds to the agent-memory architectural debate.*

## What Kostadis's piece is

Kostadis Roussos published a comparison document in May 2026 contrasting his CampaignGenerator + MemPalace stack — a tabletop RPG session-prep system running against a ~2 TB local PDF library plus the 5etools JSON corpus — against Zhang, Kraska, and Khattab's *Recursive Language Models* paper (arXiv:2512.24601). The document is addressed to the paper's authors and to anyone working on a concrete RLM adaptation in a domain the paper doesn't directly target.

The framing is explicit: CampaignGenerator is **not** an RLM implementation. It "adopts the *idea* and rejects the *mechanism*." Specifically:

- **Adopts:** hierarchical decomposition as the answer to bounded context; symbolic handles over textual ones; pruning at intermediate levels; parameterized `max_depth` reflecting the paper's break-even insight about when sub-calls help.
- **Rejects:** any LLM call in the index or pruning path; LLM-as-architect-executor-renderer in a single REPL session; in-memory prompt-as-corpus framing.

What replaces the LLM mechanism: rank-bucketed AAAK (Aggregate Anchor-Adjacent Keywords) projections at wing and room levels, applied as deterministic compressions of leaf drawer content. The descent runs through a Python function; the LLM is invoked once per query, only at the rendering step that consumes already-retrieved drawers.

## Why it matters for the architectural debate

The main agent-memory writeup centered an empirical finding from SME: RLM-with-Qwen-7B and RLM-with-Llama-70B both score 46.67% recall on the jp-realm-v0.1 corpus while Familiar's deterministic pipeline scores 78.33%. The interpretation — hedged for sample size — was that invocation discipline, not retrieval infrastructure or reasoning capacity, may dominate.

Kostadis's piece provides a second, much harder empirical data point in the same direction:

> "the hierarchical path is **19.82× cheaper than flat search at 0% recall@10 loss**"
> — `tests/benchmarks/test_hierarchical_aaak_gate1.py`, 281-entry benchmark fixture

A measured 19.82× cost reduction at 0% recall@10 loss is real evidence. It's a different benchmark, a different corpus, a different domain (RPG session prep vs. coding/dev memory), and a different team. The shape of the finding — deterministic hierarchical pruning matches flat retrieval on recall while massively reducing cost — supports the same architectural conclusion that Familiar's deterministic pipeline supports against RLM orchestration.

Two independent practitioners, working in different domains on top of the same underlying memory substrate (MemPalace), have converged on the same design pattern: take RLM's hierarchical-decomposition insight, drop the LLM-in-the-loop mechanism for intermediate steps, get the cost reduction without the format-collapse and invocation-discipline failure modes documented in the RLM reproduction paper.

This is the kind of convergent evidence the field has been short on. Most agent-memory comparisons are vendor-published or framework-promotional. Kostadis's piece is neither — it's a working-engineer note explaining concrete tradeoffs against a paper that inspired the system but didn't dictate its mechanism.

## Specific additions to the synthesis

Four patterns Kostadis surfaces that the main writeup either didn't cover or covered in less concrete form.

### 1. Deterministic intermediate compression as a precision discipline

The main writeup framed deterministic pipelines as "good preprocessing + good ranking" without articulating why determinism *itself* is load-bearing. Kostadis's piece does:

> "If an LLM summarizes a wing's contents, two failure modes appear: drift (same wing scored against the same query at two different times produces different rankings) and recall loss at the prune step (if the wing-index summary drops a token that drawers under the wing actually carry, the wing gets pruned at step 1 and the relevant drawer is unreachable at step 3)."

The argument: pruning was supposed to be a cheap accelerator over an exhaustive baseline. With LLM intermediate compression it becomes a precision-losing step you can't audit. The deterministic projection is auditable — the wing index for query `q` is a pure function of the leaf closets under that wing, and the leaf closets are a pure function of the drawer text. If a wing prune removes the right answer, you can trace which leaf token caused it.

This is a clearer articulation of why the deterministic-pipeline architectural pattern wins in hard-recall domains than anything in the main writeup. It also explains why systems that bolt LLM summarization onto vector stores often see retrieval quality regress — the summarization step is invisible to the audit trail.

### 2. Retrieve/render isolation as a mechanical CI invariant

The main writeup treated the consumption problem (high retrieval recall but low end-to-end task accuracy) as a behavioral/training problem. Kostadis surfaces an engineering answer:

> "tests/test_retrieve_render_isolation.py walks every Python file in the repo and fails if a function body contains both a retrieval call (retrieve, search_hierarchical, rpg_search, ...) and a render call (stream_api, call_api, ...). The check passes for 48 cases at the time of writing."

This is a static-analysis CI check enforcing the separation between LLM-extract and LLM-render stages. It's closer to a mechanical answer to Cat 9 / "The Handshake" than anything in the main writeup's three categories (tool-based invocation, declarative expression, push-side surfacing). The pattern: encode the discipline as a test that fails the build when violated, rather than relying on prompt engineering or model training.

For systems trying to enforce a human-in-the-loop checkpoint between retrieval and generation, this is worth replicating. The shape generalizes: any architecture that requires stage A to complete before stage B begins can have its boundary enforced by AST-walking CI rather than by runtime guards.

### 3. Cost-tagged multi-source candidates

Kostadis's retriever returns a merged tier list across three awareness sources:

> "Hits are merged into one ranked tiered list with a `kind / cost` discriminator (`drawer | statblock | candidate(cost: cheap | expensive)`). Cheap candidates carry a one-line `fivetools_ingest` command; expensive candidates carry a `pdf_to_5etools_v2 convert` + `fivetools_ingest` command pair."

The novelty: the retriever doesn't decide whether to materialize uningested content. It surfaces both as candidates with their costs explicit, and the human picks. This extends RLM's decomposition framing past the within-corpus question ("how do I break this down?") into a *should I materialize this part of the corpus at all?* question.

None of the OSS memory systems in the main writeup expose this surface. Cognee, Letta, Mem0, Graphiti — all treat ingest as a synchronous upfront step. Kostadis's pattern treats it as a per-query decision the agent or human makes based on cost-tagged candidate lists.

For domains with large unconverted content (legal corpora, scientific PDFs, code archives at scale), the cost-tagged candidate pattern looks more practical than bulk ingest. It's also a cleaner answer to the "Layer 2 freshness" problem the main writeup raised: rather than maintaining compiled artifacts for the whole corpus, maintain them only for what's been queried.

### 4. Schema-aware indexing as a bridge between paradigms

CampaignGenerator's substrate is bridged by 5etools — a typed JSON schema covering ~30 entity wrappers (`monster`, `spell`, `item`, `class`, etc.). This isn't just metadata; it's what makes the deterministic intermediate compression work:

> "AAAK projections are token frequency operations; the rank-bucketing assumes the leaf content has *meaningful tokens*. Untyped prose (the paper's setting) has tokens, but they're unlabeled. Typed schema content has the ability to carry per-entity facets (CR, level, school, rarity, edition) into Chroma metadata, which means our `where`-clause filters during the hierarchical descent are not just over wing/room IDs but over filterable entity attributes."

This is a clean articulation of why typed schemas matter for hierarchical retrieval over verbatim storage. Untyped drawer content forces vector-similarity-only pruning; typed content enables structural pruning before vector search runs. A query for "fey forest encounter mid-level" can prune wings AND facet-filter to `creature_type=fey, environment=forest, CR ∈ [5,10]` before any embedding comparison.

The dependency is real: content that doesn't fit 5etools is invisible to the retriever until converted. But for domains with existing typed schemas (SEC filings, ICD codes, FHIR resources, legal citations), the schema-aware indexing pattern looks like the right shape. It's also a useful framing for the question "what does Layer 2 look like over verbatim storage?" — perhaps less "compiled artifacts" and more "facet metadata for filterable hierarchical descent."

## What's still unresolved

Kostadis is candid about the limitations his domain choices impose — schema dependency, ingest discipline as UX cost, persistent corruption, no ad-hoc decomposition for queries the wing taxonomy doesn't cover. The main writeup raised similar tradeoffs for the deterministic-pipeline pattern generally.

Two questions both pieces leave open:

- **When does ad-hoc decomposition pay off?** Kostadis's open question — "an RLM-style ad-hoc decomposition for the 'I don't know what wing this fits' case" — is the same question the main writeup raised about hybrid architectures: when should deterministic pipelines fall back to LLM-orchestrated retrieval? Neither piece has an answer; both note it as the most interesting unexplored direction.
- **What does training look like for the deterministic descent path?** Kostadis mentions training a small model on `retrieve()` traces to predict optimal `max_depth` and awareness-source selection per query. The main writeup mentioned post-trained RLM-Qwen3-8B's 28.3% improvement. These point at the same thing from different sides: deterministic systems can be tuned by ML even if the runtime path itself doesn't call models. Nobody has published much on this yet.

## The convergence itself

Two independent practitioners, working on different domains, building on the same underlying memory substrate (MemPalace), arriving at the same architectural pattern (deterministic hierarchical pruning, LLM at the leaves only, human-in-the-loop between retrieve and render), with concrete benchmark numbers supporting the design — is not nothing.

It's also not proof. Different domains, different scales, different workloads might reverse the conclusion. The RLM paper's BrowseComp-Plus and OOLONG-Pairs results (where RLM with GPT-5 reaches 91.33% versus 0% baseline) show LLM-orchestrated decomposition winning decisively on tasks the paper specifically targets. Both architectures have their domains.

What the convergence does suggest: for verbatim-first memory systems with hard recall requirements operating against persistent stores, the deterministic-pipeline pattern is more developed than the OSS landscape coverage might indicate. Two production-shaped instances (Familiar v0.3.9 in jphein's stack, CampaignGenerator + kostadis/mempalace in Kostadis's stack) are arriving at compatible designs from different starting points. The field would benefit from explicit benchmarking of these patterns side-by-side against RLM-orchestrated retrieval on a shared corpus — neither piece has that yet, but both have the substrate to make it possible.

## References

- Roussos, K. (2026). *Comparing CampaignGenerator + MemPalace to "Recursive Language Models."* [github.com/kostadis/CampaignGenerator/blob/main/docs/rlm_paper_comparison.md](https://github.com/kostadis/CampaignGenerator/blob/main/docs/rlm_paper_comparison.md)
- [kostadis/CampaignGenerator](https://github.com/kostadis/CampaignGenerator) — orchestration, retrieval, render pipeline
- [kostadis/mempalace](https://github.com/kostadis/mempalace) — verbatim memory palace fork, hierarchical retrieval branch
- `mempalace/tests/benchmarks/test_hierarchical_aaak_gate1.py` — Gate 1 (recall@10 = 1.0 at 19.82× cost reduction vs. flat)
- `CampaignGenerator/tests/benchmarks/test_rlm_benchmark_rpg_gate2.py` — Gate 2 (top-3 ≥ 90% on 15 RPG queries)
- Zhang, A. L., Kraska, T., Khattab, O. (2025). *Recursive Language Models.* [arXiv:2512.24601](https://arxiv.org/abs/2512.24601)
- Wang, D. (2026). *Think, But Don't Overthink: Reproducing Recursive Language Models.* [arXiv:2603.02615](https://arxiv.org/abs/2603.02615)

---

*This companion note was prepared in response to Kostadis's piece appearing during write-up of the main agent-memory synthesis. Both pieces operate on the MemPalace substrate but address different domains. Neither should be read as advocacy for one architecture over another — the convergence is interesting, the divergent failure modes are real, and the workload dictates which pattern wins.*
