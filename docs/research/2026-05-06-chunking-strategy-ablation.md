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

## Run 2 — curated mixed corpus (markdown + code)

First attempt was on the full repo root (266 files); abandoned
after 22 minutes wall time only got through file 102/266 of
strategy 1 — too much filler (test files, generated bench
results, etc.) for the signal we actually wanted to test.

Pivoted to a **24-file curated corpus** with deliberate diversity:
- 9 markdown files: `CLAUDE.md`, `FORK_CHANGELOG.md`, `README.md`,
  `MISSION.md`, `SECURITY.md`, `RELEASING.md`, the two checkpoint /
  verbatim spec docs, this file, and the MCP tool reference.
- 15 representative Python files spanning size + complexity:
  `searcher.py`, `miner.py`, `convo_miner.py`, `hooks_cli.py`,
  `mcp_server.py`, `palace.py`, `config.py`, `normalize.py`,
  `knowledge_graph.py`, `backends/chroma.py`, `palace_graph.py`,
  `spellcheck.py`, `layers.py`, `dialect.py`, `dedup.py`.

Probe set: 20 questions (15 original + 5 markdown-targeted)
covering file-targets that *all* live in the curated corpus.

**Results — partial reproduction.**

| strategy           | drawers | MRR  | md-only MRR | code-only MRR | R@5 | R@10 |
|--------------------|--------:|-----:|------------:|--------------:|----:|-----:|
| A_paragraph_aware  |   1,054 | 0.470 | 0.240       | 0.547         | 60% | 70%  |
| **B_heading_aware_md** | 1,085 | **0.478** | **0.267**     | 0.548         | 60% | 70%  |
| C_plus_ast_python  |   1,129 | 0.446 | 0.250       | 0.512         | 60% | 70%  |

**B wins markdown MRR by ~10% relative** (0.267 vs 0.240) without
hurting code-probe MRR (essentially tied with A: 0.548 vs 0.547).
Aggregate MRR improvement (+1.7% relative) is real but small
because only 5 of 20 probes target markdown.

**C still loses on code probes** (0.512 vs 0.547 = -6.4% relative).
Article 2's AST-aware win (Context Precision 0.78 vs 0.57) does
not reproduce on this corpus. Confirms Run 1's finding:
mempalace-style code (short, well-named functions) loses
module-level context when AST-split.

### Per-probe deltas (only where strategies disagree)

| Probe                                      | A | B | C | Type |
|--------------------------------------------|---|---|---|------|
| How does the stop hook save diary entries? | 7 | 6 | 6 | .py  |
| BM25 tokenization with None-document safety | 1 | 1 | 3 | .py |
| How is a wing name normalized?             | 4 | 4 | 5 | .py  |
| Spellcheck user prose                      | 3 | 3 | 2 | .py  |
| Hook silent_save vs block-mode             | 5 | **3** | 4 | **.md** |

The .md probe where B wins (`Hook silent_save vs block-mode`) is
exactly the case heading-aware splitting was designed to help:
`CLAUDE.md` has a `## Hook Save Architecture` section with `###
Silent mode` and `### Block mode` subsections. A's
paragraph-aware splitter doesn't know about the heading boundary
and chunks `### Silent mode` content together with adjacent
unrelated paragraphs. B isolates the silent-vs-block discussion
into its own chunk → tighter embedding → better rank.

### Probes that all 3 strategies missed

Three of 5 markdown probes were not in top-10 for any strategy:

| Probe                                     | Expected file |
|-------------------------------------------|---|
| Verbatim-only Phase 2 architecture        | `2026-05-05-verbatim-only-design.md` |
| Pre-release grep checklist for `mempalace-mcp` | `RELEASING.md` |
| Fork-ahead row inventory + upstream PR table | `CLAUDE.md` |

These are abstract "what's the architecture of X?" questions where
the target doc has many chunks, none of which lexically overlap
strongly with the query. The embedding doesn't fire on any chunk
distinctively enough to enter top-10. **This is a query-side
failure, not a chunking-side failure** — B's heading-aware split
doesn't change the fundamental problem that "Verbatim-only Phase
2 architecture" is a *summary* query with no precise lexical
anchor in the doc body.

That's a separate retrieval-quality concern (corpora benefit from
title/heading-as-doc-summary embedding tricks the articles also
discussed) and not addressable by chunking strategy alone.

## What this means for #1024 / fork-ahead direction

  - **Heading-aware-md is a defensible (but small-win) PR
    direction.** B beat A on markdown MRR by ~10% relative without
    hurting code-probe performance — a clean Pareto improvement
    on this corpus. The aggregate impact is small (+1.7% MRR)
    because most probes target code, but markdown queries
    consistently benefit. Filing as a small upstream PR with the
    empirical evidence below is reasonable; pre-filing a
    Discussions or RFC issue may be the more polite path given
    the win is modest.
  - **AST-aware Python is NOT a defensible PR direction.** C
    consistently loses on code probes across both runs. Article
    2's win was on bigger codebases where function bodies have
    more text; small-function corpora like mempalace's hurt under
    AST split because module-level context is stripped.
  - **The `chunk_size` debate (issue #390) remains the dominant
    lever.** All three strategies miss the same abstract markdown
    probes (Verbatim-only Phase 2, RELEASING.md, CLAUDE.md) — those
    are query-side failures not chunking-side failures. The
    256-token MiniLM truncation also limits how much per-chunk
    context a strategy can preserve.
  - **#1024 stays the right minimal fix.** Configurable
    `chunk_size` / `chunk_overlap` / `min_chunk_size` lets users
    tune for their model. The heading-aware-md PR (if filed) is
    additive, not a substitute.

### Proposed PR shape (if/when filed)

```python
def chunk_text(content: str, source_file: str, chunk_size, chunk_overlap, min_chunk_size) -> list:
    if source_file.lower().endswith((".md", ".markdown", ".rst")):
        return _chunk_markdown_heading_aware(content, source_file,
            chunk_size, chunk_overlap, min_chunk_size)
    return _chunk_paragraph_aware(content, source_file,
        chunk_size, chunk_overlap, min_chunk_size)
```

One new helper (~50 LOC), file-extension dispatch, paragraph-aware
fallback for oversized sections. No AST, no semantic chunking, no
new dependencies. Tests + ablation reproducer attached.

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
