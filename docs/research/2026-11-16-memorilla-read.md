# Memorilla: what it is, and what the palace can use

**Date:** 2026-11-16
**Status:** RESEARCH, read only. No build; one experiment proposed below.
**Sources (primary):** the paper [*Memorilla: Latent Semantic Memory for LLMs*][arxiv] (arXiv 2610.21375, v1
2026-10-15, v3 2026-11-03; Fallahpour, Idehpour, Kothapalli, Mounier, Wu, Pardis, Leskovec); the
[project page][site]; the [code][code] (Apache-2.0); the [checkpoints][hf]. The press coverage that prompted this
read is not used as a source. Every number below is the authors' own, taken from the project page and the model
card; none of it was reproduced here.

[arxiv]: https://arxiv.org/abs/2610.21375
[site]: https://memory.stanford.edu/
[code]: https://github.com/snap-stanford/memorilla
[hf]: https://huggingface.co/memorilla/Memorilla-Qwen3-8B

## What it is

- **The pipeline.** A frozen encoder (Qwen3-Embedding-4B) turns each document (a conversation, a book chapter) into
  one embedding, once. At question time, a trained **memory module** (194M parameters, bf16, per the model card)
  reads the question together with the whole store and writes a fixed number of **latent memory tokens**: 16 by
  default. These go straight into the input embedding space of a **frozen Qwen3-8B**.
- **What is trained.** Only the memory module, in three stages: Wikipedia paraphrase, then general QA and
  summarization, then the benchmarks' training splits. The README reports about 19 h on 4×H100-80GB and about
  310 GiB of data and embeddings.
- **The checkpoints** work only with that exact encoder and decoder pair.
- **Results, 16 tokens** (full context / best baseline / Memorilla):

  | Benchmark | Full context | Best baseline | Memorilla |
  |---|---|---|---|
  | PersonaMem-v2 (accuracy) | 27.92 | 30.90 (xRAG) | 44.60 |
  | PersonalizationV4 (accuracy) | 59.12 | 67.88 (MemoRAG) | 66.65 |
  | PubMedQA (accuracy) | 67.00 | 69.00 (LLMLingua-2) | 59.00 |
  | FactKG (accuracy) | 80.43 | 80.43 (full context) | 84.33 |
  | NarrativeQA (ROUGE-L) | 17.99 | 20.51 (MemoRAG) | 19.31 |
  | TriviaQA (exact match) | 52.60 | 56.60 (MemoRAG) | 57.00 |
  | LaMP-4 (ROUGE-L) | 14.21 | 16.10 (xRAG) | 18.15 |
  | LaMP-7 (ROUGE-L) | 43.71 | 46.84 (xRAG) | 48.37 |

  - It is best on five of eight, with the best mean (49.68).
  - It is clearly worse on PubMedQA (59 against 67 for full context), the precise-fact benchmark.
  - Input is 394× smaller than full context on average, and 4,212× on NarrativeQA.
  - End to end, it takes 0.74 s against 19.9 s for full context: 512 documents, one H100, batch 1. RAG was not
    timed.
  - In a TextWorld agent run, the success rate went from 18.6% to 24.2%.

## What transfers to the palace

1. **Query-dependent selection before injection, never compression.** The palace injects recalled drawers as text:
   a fixed top-k from hybrid search, after a rerank. Memorilla's largest gain is on *personal* memory
   (PersonaMem-v2, +13.7 over the best baseline), which is the palace's kind of memory. It comes from reading the
   store *with the question* instead of injecting a static top-k.
   - Memorilla's output is itself a lossy derivative, and the palace's first principle is verbatim
     ([the verbatim-vs-derivative axis](verbatim-vs-derivative-axis.md)).
   - So what transfers is **choosing which exact words to inject** against the query: which drawers, which
     sentences of them, in what order. Never a condensed or paraphrased stand-in. The verbatim drawer stays the
     answer of record, one id away.
2. **Their evaluation sets.**
   - PersonaMem-v2 and PersonalizationV4 are personal-memory QA with gold answers. PersonalizationV4 is CC BY 4.0;
     the PersonaMem-v2 license is per its dataset card, not checked here.
   - The palace has no outside benchmark for recall quality. Running our hybrid retrieval over their documents
     gives an evidence-recall@k number that does not depend on our own data.
3. **Encode once, read per query.** Drawers are already embedded once. The paper is a data point that a larger
   encoder (Qwen3-Embedding-4B, against our 384-dim MiniLM) pays off on personal memory. That is a candidate for
   the alternative-embedder row, with the full re-embed it costs.
4. **A local fast-recall path** for local models that can take latent tokens, which no hosted model can. It would be
   a derivative layer beside the verbatim one, as in item 1, and never a replacement for it.

## What does not transfer

- **Latent tokens cannot go to Claude** or any API model. They enter a model's embedding layer, and hosted APIs
  take text.
- **It is tied to one model pair.** The module must be retrained for a different decoder or encoder.
- **Training is a GPU project** (about 76 H100-hours, 310 GiB). Inference needs bf16 Qwen3-8B (about 16 GB) plus
  Qwen3-Embedding-4B (about 8 GB) on one card. A measured local box with two 10 GB Pascal cards cannot host it:
  neither card holds the 8B decoder, Pascal has no native bf16, and its 10 GB of host RAM is too small to stage the
  weights.
- **It is weak on precise facts** (the PubMedQA result). Exact recall of commands, ids and dates is much of the
  palace's job.

## One experiment

**The question:** on our own drawers, does Memorilla's 16-token read answer as well as our text retrieval, at a
fraction of the tokens?

- **Two stages, because drawers never leave the user's machine** (local-first): a rented GPU sees only public
  benchmark data.
  - **Stage 1, no GPU, today:** run our hybrid retrieval over PersonaMem-v2's documents and measure evidence
    recall@k against its gold answers.
  - **Stage 2, public data, rented GPU:** arms A–C below on PersonaMem-v2 questions.
  - **Stage 3, our drawers:** the same arms over one wing, on a local card only. Neither of the fleet's GPU boxes
    measured so far fits, so stage 3 waits for one.
- **Questions (stage 3):** 50, each with a gold answer and its gold drawer ids, written from drawers that a
  human-readable diary or doc confirms.
- **Arms, all with Qwen3-8B as the reader:**
  - A: our hybrid search top-k as text;
  - B: the released stage-3 checkpoint over the same documents (in stage 3, the wing's drawers, one document each),
    with no fine-tuning (out of domain is part of what is measured);
  - C: closed book.
- **Measure:** answer accuracy (exact match where possible, otherwise a local judge model in stage 3, so that no
  drawer reaches a hosted judge), input tokens and latency.
- **Cost:**
  - stage 1 costs nothing;
  - stage 2 needs one bf16 card of 40–80 GB for about 4 hours. On-demand A100 or H100 pricing is a few dollars an
    hour (an estimate, not a quote), so about $20, payable from existing cloud credits. Starting the instance is a
    billing step, so it needs the owner's tap;
  - stage 3 needs about 4 agent-hours for the question set, plus a local card of 32 GB or more.
- **Success:**
  - If B is within 2 points of A at ≤ 1/10 of A's input tokens in stage 2, stage 3 is worth a local card. If stage 3
    agrees, a local fast-recall path (a derivative layer) is worth building.
  - Otherwise, the palace keeps text retrieval and adopts only items 1 and 2 above.
