# Multi-encoder RRF — research feature

**Date:** 2026-05-15
**Tracking:** [techempower-org/mempalace#82](https://github.com/techempower-org/mempalace/issues/82)
**Status:** RESEARCH — default off, opt-in via env flag.
**Code:** `mempalace/rrf.py`, `mempalace/multi_encoder.py`, hook in `mempalace/searcher.py`.
**Eval:** `scripts/eval_multi_encoder_rrf.py`.

## Why this exists

HyDE (Hypothetical Document Embeddings) was the cheap candidate for
bridging the query/drawer vocabulary gap. A controlled probe on
katana+qwen2.5:14b ([familiar.realm.watch#6][hyde-issue]) showed
**0 rescues / 3 regressions** at top-10 over 15 paraphrase probes.
Root cause writeup: the LLM writes textbook-conventional hypothetical
documents while our drawers describe specific-implementation choices,
so the hypothesis vector steers retrieval *away* from the target.
HyDE stays off in production.

The next lever is **multi-encoder retrieval with Reciprocal Rank
Fusion**: query the same corpus through N different encoders, RRF-fuse
the rank lists. No LLM call per query; lift comes from the orthogonal
errors of N independent encoders.

The 2026-05-15 reproduction at n=200 (issue #82, raw-vector path
via `scripts/verify_rrf_ftcode5k.py` + nakata-app's 3-way replication)
measured:

| Encoder | Solo MRR | Recall@10 |
|---|---:|---:|
| default ONNX MiniLM | 0.4260 | 49.5% |
| FT-Code-1000 (adaptmem) | 0.4229 | 53.5% |
| FT-Code-5000 (adaptmem) | 0.3972 | 50.0% |
| **3-way RRF fusion (raw vector)** | **0.5101** | **59.5%** |

That's **+0.0841 MRR vs. best solo** at the raw chromadb-vector
layer. This was the motivating result and the reason this code exists.

## What we found in production

Running the same 200-probe set through the full ``search_memories``
pipeline — the path real callers hit, which has closet boost + hybrid
BM25 rerank running on top of vector retrieval — gives a very
different headline:

| Path | MRR | Recall@5 | Recall@10 | qps |
|---|---:|---:|---:|---:|
| Single-encoder baseline (default) | 0.4042 | 46.5% | 49.0% | 3.85 |
| 3-way RRF (default + ft-1k + ft-5k) | 0.4033 | 45.5% | 49.5% | 2.78 |
| **Δ** | **−0.0008** | **−1.00 pp** | **+0.50 pp** | **0.72x** |

Per-probe breakdown across 200 probes:

* **2 rescued** — was a miss in top-10, now a hit
* **1 regressed** — was a hit, now a miss
* **5 improved rank** — still a hit, ranked better
* **10 worsened rank** — still a hit, ranked worse
* **82 tied hits** — same rank in both
* **100 tied misses** — still not in top-10

**The +0.0841 raw-vector lift does not survive the hybrid pipeline.**
3-way RRF is statistically flat through ``search_memories`` and costs
~3x query latency.

The most likely explanation: the encoder-orthogonality signal that
makes RRF work on raw vector retrieval is largely already captured
by the production path's closet boost + BM25 rerank. Whatever the
encoders *disagree* on, BM25 catches as "terminology that matches
verbatim". So the lift available *over the production path* is much
smaller than the lift available *over raw vector retrieval*.

This is the same shape of finding as the HyDE diagnosis
([familiar.realm.watch#6][hyde-issue]) — both techniques target
vocabulary-bridging, and the existing hybrid path is already doing
significant vocabulary-bridging work along the BM25 axis. Tools
that improve raw vector retrieval don't automatically improve
``search_memories`` retrieval.

Implication: do *not* flip ``PALACE_USE_MULTI_ENCODER_RRF`` on in
production. The code lands as a research artifact — reproducible,
inspectable, gated — but the measured lift on real call paths
doesn't justify the 3x latency or the Nx storage.

## What this PR ships

A query-time fusion path, gated behind `PALACE_USE_MULTI_ENCODER_RRF=1`.

* `mempalace/rrf.py` — pure RRF math (`rrf_scores`, `rrf_fuse`,
  `explain_fusion`). Dependency-free; tested at the math level in
  `tests/test_rrf.py`.
* `mempalace/multi_encoder.py` — encoder roster + fan-out + fusion
  glue. Reads `PALACE_RRF_ENCODERS`, `PALACE_RRF_ENCODER_PATHS`,
  `PALACE_RRF_PALACES` from env. Encoder cache is process-wide; loads
  each `SentenceTransformer` once (~1.5s, ~100MB). Defensive — one bad
  encoder palace doesn't sink the query.
* `mempalace/searcher.py` — one hook at the `drawers_col.query(...)`
  call site. When the env flag is unset (default), the call path is
  byte-identical to before. When set, the single chromadb query is
  replaced by `multi_encoder.fused_query`, returning the same
  chromadb-shaped dict downstream code already consumes. Closet
  boost, hybrid rerank, BM25 fallback all stay untouched.
* `scripts/eval_multi_encoder_rrf.py` — eval harness that mines N
  temp palaces, runs the production code path twice (single-encoder
  baseline vs multi-encoder fused), reports MRR + Recall@5/@10.

## What this PR does *not* ship

* **Multi-encoder ingest.** Production users who flip this on need to
  maintain N palaces, one per encoder. Mining infrastructure for that
  isn't in scope here — the eval harness mines temp palaces; real
  users will need either an external orchestration layer or a
  follow-up PR that teaches the miner to ingest into N palaces in a
  single pass.
* **A second vector column in postgres.** Issue #82 sketches that as
  the "real" productized shape. This PR is the lighter "do we
  reproduce the lift end-to-end?" deliverable. Same-palace,
  swap-encoder mode is a research lever, not a ship-it default.
* **A flip-the-default decision.** Storage cost (Nx), ingest cost
  (Nx), and query latency (≈Nx) make this an opt-in. The decision to
  default it on lives behind another round of evals against
  user-style queries (commit-subject-shaped probes are a starting
  point, not the end).

## Operator surface

```bash
# Roster
export PALACE_USE_MULTI_ENCODER_RRF=1
export PALACE_RRF_ENCODERS=default,ft-code-1000,ft-code-5000
export PALACE_RRF_ENCODER_PATHS="ft-code-1000=/models/ft1000,ft-code-5000=/models/ft5k"
export PALACE_RRF_PALACES="default=/var/lib/palace/main,ft-code-1000=/var/lib/palace/ft1k,ft-code-5000=/var/lib/palace/ft5k"

# Tunables (optional)
export PALACE_RRF_K=60           # Cormack 2009 default
export PALACE_RRF_OVERFETCH=3    # per-encoder pool = n_results * overfetch
```

`default` is the built-in ONNX MiniLM; it doesn't need a path. Any
non-default encoder must have an entry in `PALACE_RRF_ENCODER_PATHS`.

If an encoder has no `palace_path`, the query path falls back to the
caller's palace and emits a one-shot warning — useful for benchmarks
on a single mined palace, **not** useful in production (the
non-default encoder will be encoding queries against an
ONNX-encoded index and the cosine values will be noise).

## Cost

| Axis | Cost vs. single-encoder |
|---|---|
| Ingest CPU | Nx (mine the same corpus N times) |
| Storage | Nx (one palace per encoder) |
| Query latency | ≈Nx (sequential encoder calls, parallel-able) |
| Process RAM | +100MB per SentenceTransformer loaded |
| Model files on disk | ~85MB per non-default encoder |

The ~Nx query latency is the most user-facing cost. SentenceTransformer
queries on CPU run ~50ms each for these MiniLM-based models, so a 3-way
fusion adds ~100ms over baseline single-encoder retrieval. Encoders
could be fanned-out concurrently in a follow-up; not done here to keep
this PR composable and inspectable.

## Eval

The harness mines `mempalace/` (this repo's package directory) once
per encoder into a temp palace, then runs the 200-probe git-derived
set through `search_memories` twice — once with the env flag off
(baseline single-encoder), once with it on (3-way RRF).

Eval reports MRR@K, Recall@5, Recall@10 for both runs and the deltas.
Per-probe JSON dump available with `--out`.

```bash
python scripts/eval_multi_encoder_rrf.py \
  --probes scripts/probes_v2_git_derived.json \
  --encoders default,ft-code-1000,ft-code-5000 \
  --model-paths ft-code-1000=/path/to/ft1000/model,ft-code-5000=/path/to/ft5k/model \
  --out /tmp/rrf_eval.json
```

Reproducible on any machine with the three encoder model directories.
SentenceTransformer uses CUDA when available (the FT encoders do this
automatically); ONNX MiniLM is CPU-only in the current install.

## Open questions

1. **Does the raw-vector lift survive a leaner pipeline?** The
   +0.0841 raw-vector lift collapses to ~0 once closet boost +
   hybrid BM25 rerank run on top. An eval that disables the closet
   boost (and / or `candidate_strategy="vector"` strict) and reruns
   would isolate which downstream layer is absorbing the
   orthogonality signal. If it's the BM25 rerank, that's a
   *positive* finding about the existing hybrid path.
2. **Does the lift hold on user-style queries?** Issue #82 raises
   this directly. The probe set is derived from commit subjects,
   which look like docstring-shaped natural language; real user
   queries may be more terse / question-shaped. A second eval
   against a user-query probe set would resolve whether the flat
   3-way RRF result here is corpus-specific.
3. **2-way vs 3-way.** Issue #82 notes the largest 2-way fusion
   lift on raw vector retrieval comes from `default + FT-Code-5000`
   (+0.0631), not from adding more encoders. The eval harness
   supports arbitrary roster size; collecting 2-way numbers
   alongside 3-way is one script invocation. May be worth a
   follow-up to confirm 2-way is also flat through hybrid.
4. **Distillation.** Train a single encoder to mimic the ensemble's
   behavior on our corpus. Cuts query cost back to baseline at the
   price of training infrastructure. adaptmem's methodology is the
   obvious starting point. Out of scope for this PR — and given
   the through-hybrid result, lower-priority than it was before
   this eval.

## Related

* [techempower-org/mempalace#82][issue-82] — tracking issue.
* [familiar.realm.watch#6][hyde-issue] — HyDE diagnosis that gates
  this work.
* [`scripts/verify_rrf_ftcode5k.py`](../../scripts/verify_rrf_ftcode5k.py) —
  the surrogate-RRF probe that produced the original +0.0841 number.
  This PR's eval reruns the experiment end-to-end through the
  production code path.

[hyde-issue]: https://github.com/techempower-org/familiar.realm.watch/issues/6
[issue-82]: https://github.com/techempower-org/mempalace/issues/82
