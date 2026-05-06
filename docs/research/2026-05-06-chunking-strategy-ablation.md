# Chunking-strategy ablation — findings

**Date:** 2026-05-06
**Author:** jphein (this fork)
**Reproducer:** `scripts/chunk_strategy_ablation.py`

## Why this exists

Two articles in early 2026 made the case that fixed-size chunking is
strategy-agnostic and that production RAG retrieval improves when the
chunker matches the content type ([TDS — "Your Chunks Failed Your RAG
in Production"][tds]; [dev.to — "I tested chunking on docs, PDFs and
code — the winner changed every time"][devto]). Both reported wins:

- Markdown docs: heading-aware splitting beat sliding-window on MRR
  (0.755 vs 0.687) with 50% fewer chunks.
- Code: AST-based chunking beat recursive char split on Context
  Precision (0.78 vs 0.57) — half the retrieved chunks were noise
  without AST.

This file records what happened when the same A/B/C comparison was
run on the corpus mempalace itself ships with.

## Setup

Three chunking strategies, all running through mempalace's full
mining + retrieval pipeline (chromadb + ONNX MiniLM + hybrid BM25
rerank), monkey-patched at `mempalace.miner.chunk_text`:

  - **A** — current paragraph-aware fixed-size (the baseline already
    in `main`).
  - **B** — A + heading-aware splitting at `#`/`##`/`###` for
    `.md` / `.markdown` / `.rst`. Sections under `chunk_size`
    become their own chunk; oversized sections fall back to A.
  - **C** — B + AST-aware splitting for `.py` via `ast.parse`. Each
    top-level `FunctionDef` / `AsyncFunctionDef` / `ClassDef`
    becomes one chunk (split if oversized); preamble (imports,
    constants, module docstring) keeps the leading lines as the
    first chunk.

`chunk_size = 800`. Probe set: 15 hand-curated queries with
known-good `source_file` basenames spanning fork-side decisions
mentioned in `CLAUDE.md`. Scoring: exact basename match for MRR
and Recall@K.

## Run 1 — package-only corpus

Corpus: `mempalace/` package directory (69 files, all `.py`).

| strategy                    | drawers | mine_secs |   MRR | R@5 | R@10 |
|-----------------------------|--------:|----------:|------:|----:|-----:|
| A_paragraph_aware           |  1,590  |     120.8 | 0.7095 | 80% | 93.3% |
| B_heading_aware_md          |  1,617  |     132.7 | 0.6762 | 80% | 93.3% |
| C_plus_ast_python           |  1,732  |     129.1 | 0.6356 | 80% | 86.7% |

Per-probe rank deltas across the 15 probes — strategies agree on
**11 of 15**. The 4 they differ on:

| Probe                                          | A   | B   | C   |
|------------------------------------------------|----:|----:|----:|
| Validate chunk_size infinite-loop              |   7 |   7 |   5 |
| Pre-PreCompact daemon /mine                    |   2 |   2 |   3 |
| KG entity validation commas                    |   1 |   1 |   — |
| where_filter wing/room                         |   1 |   2 |   1 |

**The articles' thesis didn't reproduce on this corpus.**

  - A wins MRR (0.71 vs 0.68 vs 0.64).
  - All three tie on R@5.
  - C drops one probe entirely from top-10 (KG-entity validation,
    expected `config.py`'s `sanitize_kg_value`).

### Why not (hypothesis)

The two articles tested on **diverse** corpora — HR policies +
financial tables + GitHub code from many repos. Mempalace's own
package is **homogeneous**: Python source with consistent style.
Two structural reasons heading-aware and AST don't help here:

1. The package directory has very little markdown (one or two
   `__init__.py` docstrings, no `.md` files). B has nothing to do
   that A doesn't already do. The 2% drawer-count increase is the
   AST-side capturing a few short class methods that A merged.
2. AST-aware Python *hurts* on small-function corpora because it
   strips the surrounding module-level context (constants, imports,
   adjacent helpers). The `sanitize_kg_value` probe failed under C
   because the function body alone (10 lines, no docstring) doesn't
   surface the "validate KG entity name" intent that the embedder
   was anchoring to in A's longer paragraph chunks. Article 2's R@5
   win for AST chunking (0.78 vs 0.57) was on bigger codebases
   where function bodies have substantially more text.

## Run 2 — repo-root corpus (markdown + code mixed)

Corpus: `~/Projects/memorypalace` (entire repo: 266 files —
`.py` + `.md` including `CLAUDE.md`, `FORK_CHANGELOG.md`,
`docs/superpowers/specs/*.md`, `docs/RELEASING.md`, `README.md`).
Probe set extended with 5 markdown-targeted probes (questions
whose answers only exist in the docs, not in any `.py`).

**Results pending — run in flight, ETA 30 minutes.**

The hypothesis going in: B (heading-aware on `.md`) should now
have something to do — the markdown probes target multi-section
docs where heading boundaries align with topic shifts. If B
substantially outperforms A on the markdown probes specifically,
the article finding holds for *that content type* even though the
code-only corpus didn't reproduce article 2's AST win.

## What this means for #1024 / fork-ahead direction

  - **Don't ship strategy-dispatch in `chunk_text` based on file
    extension yet.** The empirical evidence on mempalace's own code
    doesn't justify it.
  - **The `chunk_size` debate (issue #390) remains the dominant
    lever.** All three strategies miss the same probe (`convo_miner`
    transcript-path regex) — that's the embedding-layer 256-token
    truncation, not the chunking strategy.
  - **#1024 stays the right minimal fix.** Configurable
    `chunk_size` / `chunk_overlap` / `min_chunk_size` lets users
    tune for their model; let strategy stay simple until evidence
    on a mixed corpus warrants more.
  - **If Run 2 shows B winning on markdown probes**, the path
    forward is a small upstream PR adding a heading-aware
    `_chunk_markdown_headings()` helper invoked when the source
    file ends in `.md` — a one-function dispatch, not a strategy
    framework. AST chunking stays out of scope based on Run 1's
    counter-evidence on this corpus.

## Reproducer

```bash
python scripts/chunk_strategy_ablation.py \
    --corpus /path/to/corpus \
    --chunk-sizes 800 \
    --n-results 10 \
    --out scratch/results.json
```

The script monkey-patches `mempalace.miner.chunk_text` per
strategy, builds a temp palace per (strategy × chunk_size), runs
every probe, and reports MRR + Recall@K. No production palace
side effects.

[tds]: https://towardsdatascience.com/your-chunks-failed-your-rag-in-production/
[devto]: https://dev.to/ayanarshad02/i-tested-chunking-on-docs-pdfs-and-code-the-winner-changed-every-time-1lof
