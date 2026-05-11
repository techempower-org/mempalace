# adaptmem: Encoder Fine-Tuning as an Orthogonal Layer

*Companion note to the three-way MemPalace consumer comparison. Adds a fourth contributor that doesn't fit the consumer framing because it operates at a different layer entirely, and a benchmark finding that suggests the architectural layers compose more cleanly than the earlier synthesis implied.*

## What adaptmem is

[`adaptmem`](https://github.com/nakata-app/adaptmem), by nakata-app, is a 200-line library that wraps SentenceTransformers with hard-negative mining and a contrastive fine-tune step. The encoder family is MiniLM-L6 (~90 MB). The contribution is a domain-adaptive encoder that any consumer of MemPalace (or any other vector store using a swappable embedding model) can drop in.

This is structurally different from the three consumers (Familiar, CampaignGenerator, Kent) discussed in the previous companion piece. Familiar, CampaignGenerator, and Kent all sit *above* the retrieval layer — they take retrieved candidates and add their own intelligence to what happens next (algorithmic pipeline, human checkpoint, trained policy). adaptmem sits *below* it, at the encoder layer, and modifies what gets retrieved in the first place by changing the vector representation.

## The benchmark finding

nakata-app published numbers in [MemPalace discussion #1249](https://github.com/MemPalace/mempalace/discussions/1249), evaluated through MemPalace's own `longmemeval_bench.py` with a monkey-patched encoder swap (zero changes to eval logic, same dataset, same encoder family). Three rows worth pulling out:

| System | R@1 | R@5 | R@10 | n |
|---|---|---|---|---|
| MemPal raw default | 0.806 | 0.966 | 0.982 | 500 |
| MemPal raw + adaptmem FT-300 | 0.862 | 0.980 | 0.994 | 500 |
| MemPal hybrid_v4 + adaptmem FT-300 | **0.916** | **0.990** | **0.998** | 500 |

Two findings to flag:

**Independent reproduction.** The raw-baseline R@5 of 0.966 exactly matches MemPalace's published number. nakata-app didn't tune the protocol; they swapped the encoder and re-ran. That's clean external validation of MemPalace's eval reproducibility, and the kind of measurement the broader OSS memory landscape needs more of.

**Fine-tune and hybrid retrieval stack orthogonally.** FT-300 on raw mode: +5.6pt R@1, +1.4pt R@5. FT-300 + hybrid_v4: +11pt R@1, +2.4pt R@5. The gains compose — fine-tuning at the encoder layer and hybrid retrieval at the retrieval layer are operating on different failure modes and adding independent lift. R@1 is where fine-tuning moves the needle most (the model learns to rank the right session *first*), R@5 moves with hybrid retrieval (more right items in top-5).

## Why this changes the synthesis framing

The previous three-way comparison framed Familiar / CampaignGenerator / Kent as competing bets about where intelligence lives — algorithm, human-in-the-loop, or trained policy. That framing undersold a different observation: those three live at the same layer, not at different ones. They're all above retrieval, deciding what to do with candidates after they're retrieved.

The cleaner architecture is four composable layers, each independently improvable:

1. **Storage** — verbatim drawers. ChromaDB in upstream MemPalace, Postgres + pgvector + Apache AGE in jphein's fork. The substrate question is real (transactional consistency, graph queries in-database, etc.) but it's a different debate from the retrieval debate.
2. **Encoder** — the embedding model. adaptmem operates here via contrastive fine-tuning on hard negatives. The encoder is normally invisible in agent-memory discussions because most systems treat it as a vendor decision (OpenAI's `text-embedding-3-large`, sentence-transformers/all-MiniLM-L6, etc.). adaptmem makes the case that domain-adaptive fine-tuning at this layer is a real lever and worth measuring separately from architectural choices above it.
3. **Retrieval** — how vectors are queried, combined, and reranked. Semantic + BM25 + hybrid_v4 + cross-encoder reranking. MemPalace's hybrid_v4, Hindsight's 4-way parallel, HyMem's two-tier dynamic scheduling all live here.
4. **Consumption** — what happens after retrieval. Familiar's pipeline, Kostadis's retrieve/render isolation, Kent's APO-trained policies all sit here. This is where the three-way comparison framed the architectural debate; it's one of four layers, not the whole stack.

The orthogonality finding from adaptmem is the empirical evidence that these layers compose. The 0.916 R@1 / 0.990 R@5 result comes from stacking encoder fine-tune (layer 2) + hybrid_v4 retrieval (layer 3). Adding a Familiar-style consumption layer on top would in principle add another independent lift — though nobody has published that combined number yet.

## What this implies

Three implications worth thinking about.

**The "intelligence layer" might be plural.** The earlier writeup framed the consumption problem as needing *an* intelligence layer above the verbatim substrate. The adaptmem data suggests every layer can have its own intelligence, and the right system stacks them. An encoder fine-tuned for the domain plus a hybrid retrieval pipeline plus a deterministic post-retrieval pipeline plus a trained query-rewriter is not architectural overkill — it's four independent improvements composing.

**The encoder layer is underexplored.** Most agent-memory comparisons hold the encoder constant (almost always either `text-embedding-3-large` from OpenAI or `all-MiniLM-L6` from sentence-transformers) and compare downstream architectures. adaptmem points out that domain-adaptive encoder fine-tuning is a 200-line wrapper around well-understood machinery (hard-negative mining + contrastive loss) that produces measurable lift across retrieval modes. If the encoder layer is this cheap to improve and adds orthogonal value, the under-exploration is surprising.

**Benchmark infrastructure that supports encoder-swapping matters more than it looks.** The fact that nakata-app could plug a different encoder into MemPalace's `longmemeval_bench.py` with a monkey-patch and zero changes to eval logic is what made the orthogonality finding measurable. Systems that bake the encoder choice deeply into their stack make this kind of comparison much harder. The clean separation between encoder and retrieval logic in MemPalace's bench harness is doing useful work for the broader field even though it wasn't its primary design goal.

## Open questions

A few things nakata-app's thread doesn't yet resolve:

**Whether the lift generalizes outside LongMemEval.** All three result JSONs in adaptmem's `benchmarks/` are LongMemEval runs. Whether the same encoder fine-tuning produces equivalent orthogonal lift on different benchmarks (LOCOMO, MemoryAgentBench, MemoryArena, SME's `jp-realm-v0.1`) is unknown. The contrastive fine-tune target is conversational memory; whether the gains hold on agentic-task benchmarks (where invocation discipline matters more than ranking quality) is an open question. This is the same critique the consumption-problem framing made of retrieval-only benchmarks generally.

**Whether the FT data requirements scale.** FT-100 and FT-300 in the result file names presumably refer to training set size. Domain adaptation on 100-300 labelled query pairs is small enough to feel practical; whether the lift continues at 1000 or whether the diminishing-returns curve is steep is unclear from the data published in the thread.

**Whether jphein's MemPalace fork's hybrid_v4 + Postgres+pgvector+AGE migration changes the orthogonality.** The MemPalace v3.3.4 storage optimization (referenced by nakata-app in their May 1 follow-up question) and jphein's substrate migration are both moving targets. The 0.990 R@5 number was measured against an earlier release; whether it holds against current substrates is exactly the rerun nakata-app asked about and jphein hasn't yet posted results for. Worth following the discussion thread for the answer.

## A note on status

This is an active in-flight discussion, not a settled artifact. nakata-app posted the original numbers on April 28, 2026, asked about v3.3.4 protocol equivalence on May 1, and jphein replied "I'm reading your work! Excited to learn more" on May 11, 2026. The integration shape proposed in the thread — adaptmem as an encoder-side adapter applied at MemPalace's config load time, no changes to the MemPalace API surface — is still in discussion. Citing the specific 0.916 / 0.990 numbers in any writeup should attribute to nakata-app's thread post and link the discussion directly. The orthogonality finding is more durable than the absolute percentages.

## References

- nakata-app, [Domain-adaptive fine-tune as orthogonal R@5 lift on top of MemPal raw](https://github.com/MemPalace/mempalace/discussions/1249), MemPalace Discussion #1249, April 28, 2026
- [adaptmem](https://github.com/nakata-app/adaptmem) — hard-negative mining + contrastive fine-tune wrapper around SentenceTransformers
- [MemPalace upstream](https://github.com/MemPalace/mempalace) — the substrate being measured
- [jphein/mempalace](https://github.com/jphein/mempalace) — Postgres+pgvector+AGE fork
- LongMemEval — the benchmark used in the orthogonality measurement

---

*This addendum supplements the [three-way MemPalace consumer comparison](three-mempalace-consumers.md). The framing of four composable layers (storage / encoder / retrieval / consumption) supersedes the earlier framing that treated the consumption layer as the whole architectural debate.*
