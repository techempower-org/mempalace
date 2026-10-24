# Uncertainty-aware retrieval — research analysis

**Date:** 2026-05-22
**Tracking:** [techempower-org/mempalace#84](https://github.com/techempower-org/mempalace/issues/84)
**Status:** RESEARCH — analysis only, no code change proposed in this PR.
**Trigger:** MIT CSAIL press release [*Teaching AI models to say "I'm not sure"*][csail-news]
([MIT News mirror][mit-news]) referencing Damani et al., *Beyond Binary Rewards:
Training LMs to Reason About Their Uncertainty* ([arXiv:2507.16806][arxiv], ICLR 2026).

[csail-news]: https://www.csail.mit.edu/news/teaching-ai-models-say-im-not-sure
[mit-news]: https://news.mit.edu/2026/teaching-ai-models-to-say-im-not-sure-0422
[arxiv]: https://arxiv.org/abs/2507.16806

## TL;DR — recommendation

**Defer the headline technique (RLCR) — it does not apply.** RLCR is an
RL training method for *generative reasoning models*; mempalace does not
train models and the retrieval pipeline does not generate answers. The
press-release framing ("teach AI to say I'm not sure") collapses two
distinct ideas — *model* uncertainty in generation and *retrieval*
uncertainty in ranking — and only the first is what the paper addresses.

That said, the paper's two **transferable** ideas are real and worth
naming so future PRs can land them deliberately rather than by
accidental rediscovery:

1. **Calibrated confidence as a first-class search-result field**
   (cheap, additive, no model training). A `confidence` score
   surfaced alongside each hit, calibrated empirically against
   human-rated relevance on our corpus, would let agents threshold
   "stop reading lower-confidence drawers" without us guessing
   thresholds for them.
2. **Brier-score evaluation of our ranking, not just MRR/Recall@k**.
   Our existing eval harness ([`scripts/eval_multi_encoder_rrf.py`][eval]
   etc.) measures rank quality. It does not measure whether the
   `similarity` field we return is *calibrated* (i.e., whether
   `similarity=0.8` hits are correct 80% of the time). Adding a Brier
   score to the eval requires only a labeled probe set we already have.

Concrete proposal: a 1-week spike that (a) adds a `confidence` field to
`search_memories` results, (b) calibrates it via isotonic regression
on the n=200 git-derived probe set, (c) reports Brier score in the
eval harness. **Storage cost zero, query cost a few µs, retraining cost
zero — orthogonal to multi-encoder / HyDE work.** Defer the headline
"train an LM to verbalize uncertainty" application until and unless we
ship an LLM-mediated answer surface (RAG endpoint) — at that point RLCR
becomes directly relevant for the *answerer*, not the retriever.

[eval]: ../../scripts/eval_multi_encoder_rrf.py

## 1. What the paper actually proposes

The paper introduces **RLCR (Reinforcement Learning with Calibration
Rewards)**, a training-time modification to RL fine-tuning of
reasoning LMs. The model is asked to emit both an answer `y` and a
numerical confidence `q ∈ [0,1]`. The standard binary-correctness
reward (RLVR) is augmented with a Brier-score penalty:

```
R_RLCR(y, q, y*) = 𝟙[y ≡ y*] − (q − 𝟙[y ≡ y*])²
```

The general form is `R(c, q) = λ·c − S(q, c)` where `S` is any
**bounded proper scoring rule**. The paper proves that any bounded
proper scoring rule yields a reward function whose Bayes-optimal
policy is both correct-maximizing *and* calibrated. The boundedness
matters — log-loss does not satisfy this, which is why prior work using
it broke calibration on out-of-domain tasks.

### Key results (Qwen2.5-7B base, paper §5)

| Setting | Method | Accuracy | ECE ↓ | Brier ↓ |
|---|---|---:|---:|---:|
| HotpotQA in-domain | RLVR | 63.0% | 0.37 | 0.37 |
| HotpotQA in-domain | **RLCR** | 62.1% | **0.03** | **0.21** |
| HotpotQA OOD (6 datasets) | RLVR | 53.9% | 0.46 | 0.46 |
| HotpotQA OOD (6 datasets) | **RLCR** | **56.2%** | **0.21** | **0.21** |
| Math in-domain (3 datasets) | RLVR | 72.9% | 0.26 | 0.28 |
| Math in-domain | **RLCR** | 72.7% | **0.10** | 0.17 |
| Math in-domain | **SFT+RLCR** | 72.2% | **0.08** | **0.14** |

Headline: **up to ~90% reduction in ECE with no accuracy loss**, and on
out-of-domain (HotpotQA → 6 other QA datasets) RLCR *outperforms* RLVR
on raw accuracy too — calibration training acts as a regularizer.

The other consumer-side findings the press release glosses:

* **Verbalized confidence is informative for test-time scaling.**
  Confidence-weighted majority vote outperforms vanilla majority vote.
  Ensembling multiple analysis CoTs further reduces Brier score.
* **Self-reflective reasoning is itself a useful classifier feature.**
  Classifiers trained on (answer, reasoning-about-uncertainty) inputs
  beat classifiers trained on (answer)-only — especially for smaller
  models. The uncertainty reasoning carries information beyond the
  final answer.

### What the paper *doesn't* propose

* It does not propose anything about retrieval, search ranking, or
  vector similarity scores. The word "retrieval" does not appear in
  the abstract.
* It does not propose changes to embedding models, BM25 weights, or
  reranker training. (It does mention training a post-hoc classifier
  to assign confidence — see §3.3 — and finds RLCR beats it. Even that
  is a generation-side comparison.)
* It is silent on RAG. The closest gesture is the "confidence-weighted
  scaling" application, which is about combining multiple answers from
  one model — not about combining one answer with retrieved evidence.

## 2. Mapping to mempalace's retrieval pipeline

mempalace's current scoring stack (per [`mempalace/searcher/`](../../mempalace/searcher/)):

1. **Vector retrieval** — pgvector / chromadb HNSW cosine distance.
   Returns `distance ∈ [0, 2]` per candidate. Default n_results=5 with
   `pull_size = n_results * 3` overfetch.
2. **Closet boost** — best-per-source closet hit subtracts a rank-based
   constant from `distance` (`CLOSET_RANK_BOOSTS = [0.40, 0.25, 0.15,
   0.08, 0.04]`) capped at `CLOSET_DISTANCE_CAP = 1.5`.
3. **Hybrid BM25 re-rank** — `_hybrid_rank` computes Okapi-BM25 over
   the candidate documents and combines `vector_weight=0.6 * cos_sim
   + bm25_weight=0.4 * bm25_norm`.
4. **Candidate strategy** — `vector` (default), `union` (BM25 augment),
   or `hybrid` (BM25 + AGE graph expansion).
5. **Final field surfaced** — `similarity = round(max(0, 1 -
   effective_distance), 3)` plus `bm25_score` and `matched_via`.

The two relevant questions, then:

### 2a. Is `similarity` already a confidence score?

**No.** `similarity` is a *transformed cosine distance* — it has the
units and range of a probability ([0, 1]) but no statistical guarantee
of being calibrated to a binary "this hit is relevant" event. We have
never measured: *across n queries, of the hits returned with
similarity=s, what fraction are truly relevant?*

This is exactly the calibration question the paper formalizes. We can
compute it without doing RL training: take a labeled probe set, bin by
`similarity`, compute relevance frequency per bin, and look at the
reliability diagram. The expected calibration error (ECE) is a
one-number summary:

```
ECE = Σ_m |B_m|/N · |acc(B_m) − conf(B_m)|
```

If our `similarity` is well-calibrated, ECE is near zero. If not, we
can apply post-hoc calibration (Platt scaling or isotonic regression)
to produce a calibrated `confidence` field — without changing the
underlying retrieval at all. Storage cost: a 2-parameter or step-function
calibrator loaded at process start. Query cost: one floating-point
transform per hit.

### 2b. Does RLCR apply to hybrid scoring?

**Not directly, but the proper-scoring-rule framing is informative.**
Our hybrid score `0.6·cos_sim + 0.4·bm25_norm` was set by convention,
not by minimizing a proper scoring rule against ground truth. If we
treat `final_score ∈ [0, 1]` as a calibrated probability that a hit is
relevant, then the right way to set those weights is to optimize Brier
(or log-loss) on a labeled set — exactly the principled-calibration
move the paper makes for generative LMs. This is regression with a
proper loss, not RL training, but the underlying mathematical claim
("bounded proper scoring rules yield calibrated estimators") is the
same theorem.

## 3. Confidence scores on search results — the cheap path

The question issue #84 actually wants answered: *could we surface a
reliability score on each hit?* Yes, and the plumbing is small.

### Minimum viable integration

```python
# in mempalace/calibration.py (new file, ~150 LOC)
def fit_calibrator(labeled_hits: list[tuple[float, bool]]) -> Calibrator:
    """Fit isotonic regression: similarity → P(relevant)."""

def apply_calibrator(cal: Calibrator, similarity: float) -> float:
    """Return calibrated confidence ∈ [0, 1]."""
```

Wire it into `search_memories` after `_hybrid_rank`:

```python
# at the bottom of search_memories(), before returning
for h in hits:
    h["confidence"] = apply_calibrator(_CAL, h["similarity"])
```

The calibrator is loaded from a small file (~1 KB) at process start.
If missing or stale, `confidence` is omitted, not faked.

### Source of labels

We have three plausible sources, in increasing fidelity:

1. **Synthetic** — the existing 200-probe git-derived set
   ([`scripts/probes_v2_git_derived.json`](../../scripts/probes_v2_git_derived.json))
   has known-relevant-doc-id pairs. Treat top-k membership as the
   label. Cheap, but probe set is commit-subject-shaped, not user-query-shaped.
2. **Human-rated** — a small set of (query, hit, label) tuples
   collected via a one-time labeling pass over ~500 hits from real
   logs. Higher fidelity, costs a person-day.
3. **Click-through / acceptance signal** — drawer-id-level signal from
   Claude Code sessions ("agent searched, then read that drawer"). The
   palace-daemon already has the data on disks; the schema for
   plumbing it back is not yet in place.

For a 1-week spike, source (1) is sufficient. The calibrator will be
biased toward the probe distribution; that's a known limitation to
document in the output schema (`confidence_source: "git_probes_v2"`)
so downstream agents don't over-trust it.

## 4. Threshold-based filtering — should low-confidence results be suppressed?

**Tempted to say yes, but the right answer is "no, surface the score
and let the agent decide."** Three reasons:

1. mempalace already has `max_distance` (default 1.5 in MCP, 0.0 = off
   in CLI). It is filter-by-vector-distance which is *not* a calibrated
   threshold. Stacking another filter on top of it without first
   characterizing what each is doing would compound the issue, not
   solve it.
2. The fork's philosophy is **closets-as-signal-not-gate** (see
   `searcher.py` module docstring): even strong-signal layers like
   closet matching only *boost*, never *suppress*. A confidence
   threshold that hides results would invert this — first time we've
   introduced a gate on a non-error path.
3. Different consumers want different thresholds. An interactive
   search ("show me what we know about X") wants recall-leaning; an
   automated answer-grounding loop ("only use sources I'm sure of")
   wants precision-leaning. The cheap and correct interface is to
   surface `confidence` and let the caller filter — which they already
   can: `results = [h for h in r['results'] if h['confidence'] > 0.6]`.

What we *could* add, without becoming a gate, is a `warnings` entry
when all returned hits fall below a known-empirically-bad threshold —
analogous to the existing "more in scope than we could rank" warning.
That stays informational, not load-bearing.

## 5. Interaction with hybrid search (vector + BM25 + graph)

This is where naive uncertainty-on-search proposals tend to fall over.
Each candidate source has a different score scale and a different
*kind* of evidence:

| Source | Score domain | Calibration target |
|---|---|---|
| Vector hit | cosine distance ∈ [0, 2] | P(relevant \| semantic similarity) |
| BM25-postgres / sqlite | `ts_rank_cd` or normalized Okapi | P(relevant \| keyword overlap) |
| Graph (AGE) seeded | binary (in expanded set or not) | P(relevant \| shares entities with seeds) |
| Graph (NER) | binary | P(relevant \| mentions query entity) |
| Closet boost | bounded constant subtract | P(relevant \| source-level closet hit) |

A single `confidence` field on the final reranked hit hides this
heterogeneity. Two options:

### Option A — calibrate the *final* score only

Treat `_hybrid_rank` as a black box and calibrate its output. Simple,
preserves the existing API, and is what the MVP above does.
Limitation: `matched_via="bm25_postgres"` hits and `matched_via="drawer"`
hits get treated by the same calibration curve, even though they
have different reliability profiles.

### Option B — per-source calibration, then mix

Fit one calibrator per `matched_via` channel, apply it before combining.
The final `confidence` is then a weighted mixture (with weights also
fit on the labeled set). This is more faithful to the paper's
proper-scoring-rule framing — each evidence source contributes a
calibrated probability, and the combination is a calibrated weighted
average.

Option A is the spike. Option B is the followup if Option A's ECE on
real queries is unacceptable. The eval would need to break down ECE by
`matched_via` to know which path is miscalibrated.

### Note on the graph paths

Graph-expanded candidates (`graph_seeded`, `graph_ner`) currently
carry `similarity=None, distance=None, bm25_score=0.0,
closet_boost=0.05` (line 1462-1473 of `searcher.py`). They are scored
on the BM25 contribution alone in `_hybrid_rank`, which then trims to
`n_results`. A calibrated `confidence` for a graph-source hit means
*P(drawer is relevant | it shares an entity with the query or with a
vector-top-5 seed)*. That's a meaningful probability — but it's
roughly orthogonal to the cosine-distance probability, and combining
them is exactly what RRF or a calibrated mixture is for.

## 6. Practical implementation path — MVP

Concrete deliverable for a follow-up PR, *if* this analysis is
accepted as the integration plan:

* `mempalace/calibration.py` — `Calibrator` class wrapping
  `sklearn.isotonic.IsotonicRegression` or a 2-param Platt sigmoid.
  Loads/saves from a JSON file. No new heavy dependencies; sklearn is
  already pulled in transitively by sentence-transformers in the
  `[gpu]` extra. Fall back to identity if unavailable.
* `mempalace/searcher.py` — one hook in `search_memories` after
  `_hybrid_rank`: `h["confidence"] = _CAL.apply(h["similarity"])`.
  The calibrator file path comes from config; missing file → no
  `confidence` field (agents already handle missing keys).
* `scripts/fit_calibrator.py` — train script consuming the existing
  probe set, writes the calibrator file. Reports ECE before/after and
  per-source breakdown.
* `scripts/eval_multi_encoder_rrf.py` — extend to report **Brier
  score** alongside MRR/Recall, so future retrieval changes can be
  evaluated on calibration too. This is the lasting benefit even if
  we ship nothing else: we currently can't tell if a retrieval change
  broke calibration because we don't measure it.
* MCP surface: `confidence` becomes an optional field on the
  `mempalace_search` response. Documented as "calibrated probability
  the hit is relevant, from isotonic regression on the
  `git_probes_v2` set; absent if no calibrator is configured."

Total: ~400 LOC plus tests, ~half a person-week of work. Gated behind
a config flag (`search.calibration.enabled`) for the first release so
default behavior is unchanged.

## 7. Cost analysis

### Compute overhead

* **Training** the calibrator: seconds. Isotonic regression on n≤10k
  labeled pairs is millisecond-scale; even with 5-fold CV for ECE
  estimation it's well under a minute. One-time, offline.
* **Inference** per query: one floating-point transform per hit
  (typically ≤5 hits). Negligible — sub-microsecond.
* **Storage**: calibrator file ~1-10 KB depending on representation.

### Model requirements

**None.** The CSAIL paper requires an LLM and RL infrastructure
because it modifies the LM's training. Our analog operates entirely
on retrieval scores we already produce. No GPU, no new model
artifacts, no extra inference passes.

This is the asymmetry that makes calibration cheap on the retrieval
side and expensive on the generation side. The paper's contribution
is making it tractable for *generation*; for *retrieval* the methods
have been standard since Platt 1999 and we just haven't applied them
yet.

### Eval overhead

Adding Brier score to the existing eval harness: ~20 LOC and a single
new column in the eval JSON. Each eval run gains a calibration number
alongside the existing MRR/Recall numbers. No additional query passes.

## 8. What does *not* transfer from the paper

Naming these explicitly so they don't get rediscovered as good ideas:

* **RL training of the embedding model**. The paper's method is
  PPO/GRPO over an LM's reasoning chain. Embedding models don't have
  a reasoning chain; the optimization surface is different. Contrastive
  fine-tuning of embeddings against a relevance signal is a known
  technique (sentence-transformers) and is what issue #82's
  adaptmem-trained encoders already do — that is the embedding-side
  analog, not RLCR.
* **Asking an LM to verbalize confidence in a search hit**. We could
  pipe each result through an LLM that emits a confidence string. The
  paper shows this works *for the LM's own answer*; doing it for an
  external retrieved doc is the same as the post-hoc-classifier
  baseline RLCR was shown to beat. Worse fidelity, far higher cost
  (~one LLM call per hit), no clear win.
* **Confidence-weighted majority vote across retrievals**. Suggestive,
  but our `candidate_strategy="hybrid"` already does a weighted
  combination across vector, BM25, and graph; the marginal value of
  framing it as "confidence-weighted vote" rather than "calibrated
  hybrid score" is presentational, not functional.

## 9. When this should be revisited

* **When mempalace ships an LLM answer surface.** A RAG endpoint that
  generates natural-language answers grounded in retrieved drawers
  would put the paper's actual technique back on the table — for the
  *answerer*, not the retriever. The answerer is the generative LM
  whose calibration RLCR is designed to fix. RLCR would then be a
  candidate for the answerer's training recipe (if we trained one) or
  the existing models' selection criterion (if we chose between
  available models based on calibration benchmarks).
* **When we have user-acceptance labels at scale.** Right now we have
  synthetic probes. The minute we have drawer-level
  "agent-read-this-after-searching" telemetry from palace-daemon, the
  calibrator gains a much better label source and the Option B
  per-source calibration becomes worth the engineering.
* **When `candidate_strategy="hybrid"` ships as default.** Right now
  hybrid is opt-in and the production default is `vector`. The graph
  paths' contribution to final ranking is small in current usage. If
  hybrid becomes the default, the per-source calibration question
  (§5) gets sharper and we'll need answers.

## 10. Recommendation summary

* **Reject** the framing "integrate RLCR into mempalace's retrieval
  pipeline." RLCR is a generative-LM training technique; mempalace
  retrieval is not generative-LM-mediated.
* **Accept** the framing "integrate calibrated confidence on search
  results, evaluate with Brier score." This is the transferable kernel
  of the paper applied to our actual stack.
* **Spike, don't ship yet.** A 1-week implementation behind a config
  flag, with ECE numbers and a per-source breakdown, would give us
  the evidence to decide whether to default-on. Default-off until the
  numbers are good.
* **Always ship the Brier-score eval column.** Even if the spike
  doesn't ship, having calibration measurement in `eval_multi_encoder_rrf.py`
  is a permanent value-add that improves how we evaluate every future
  retrieval change. This is the safe lower bound on uptake.

## Related

* [techempower-org/mempalace#84][issue-84] — this tracking issue.
* [techempower-org/mempalace#82][issue-82] — multi-encoder retrieval
  (the embedding-side analog of "use more signals"; orthogonal to
  calibration).
* [2026-05-15 multi-encoder RRF analysis](2026-05-15-multi-encoder-rrf.md)
  — same "raw lift vs production-pipeline lift" methodology this doc
  recommends for calibration.
* [`mempalace/searcher/`](../../mempalace/searcher/) — the
  retrieval pipeline this would extend.
* [`scripts/eval_multi_encoder_rrf.py`](../../scripts/eval_multi_encoder_rrf.py)
  — the eval harness that would gain a Brier-score column.

[issue-84]: https://github.com/techempower-org/mempalace/issues/84
[issue-82]: https://github.com/techempower-org/mempalace/issues/82
