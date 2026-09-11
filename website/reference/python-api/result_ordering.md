# `mempalace.result_ordering`

Source: [`mempalace/result_ordering.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/result_ordering.py)

Curated-over-transcript ordering for near-duplicate search hits.

A paraphrased question ranks session transcripts above the curated card that
answers it. Measured on the production palace (wing ``2g``, limit 20, both
interactive routes): the ``CLAUDE.md`` chunk carrying a REFUTED banner came
back at rank 13 on ``bm25-fast`` and rank 14 on hybrid, while a transcript
quoting the same claim sat at rank 1 — so a reader at the default
``--limit 10`` never reaches the correction. The reporting librarian named the
class **FAIL-D: right file, right chunk, right markers, ranked below the cut**
(techempower-org/mempalace#451 item F). Item G is its sibling: palace diary
summaries rank first with no citable source at all.

The preference here is deliberately BOUNDED. A curated hit is promoted only
over a hit it is a *near-duplicate* of — one that quotes substantially the
same text. Where no curated document says the same thing, the ranker's order
is left exactly as it came back, so genuinely transcript-only recall is never
swamped by curated files that merely share a topic.

**Why the overlap coefficient over shingles, and not Jaccard.** Measured over
134 cross-kind hit pairs on production:

===========================  ==========================================
bare token-set overlap       0.63 even for merely same-topic chunks —
                             does not discriminate at all
3-gram shingle Jaccard       peaks at 0.16 for TRUE quoting pairs; any
                             workable threshold would be dangerously low
3-gram shingle overlap       0.373 / 0.473 / 0.491 for the three genuine
 coefficient  (chosen)       buried-card pairs, <= 0.323 for same-topic
                             non-quoting pairs, 0.000 for unrelated files
===========================  ==========================================

A transcript quotes a card chunk and surrounds it with conversation, so the
two differ wildly in length; the overlap coefficient (``|A n B| / min(|A|,
|B|)``) asks "is most of the shorter one inside the longer one?", which is the
actual question. Jaccard divides by the union and is therefore punished by
exactly the extra conversation that makes a quote a quote. Shingles rather
than bare tokens because same-topic chunks share vocabulary but not word
order. No embeddings: this runs on every interactive search.

## Functions

### `similarity`

```python
def similarity(a, b) -> float
```

Overlap coefficient over word 3-grams: ``|A n B| / min(|A|, |B|)``.

1.0 means the shorter text's word order is wholly contained in the longer
one — which is what a transcript quoting a document looks like. 0.0 when
either side is empty or too short to shingle.

### `is_near_duplicate`

```python
def is_near_duplicate(a, b, threshold: Optional[float] = None) -> bool
```

Do these two texts carry substantially the same content?

``False`` whenever either side is too short to be evidence (fewer than
:data:`_MIN_SHINGLES` shingles), so a one-line stub never drags an
unrelated document up the list.

### `prefer_curated`

```python
def prefer_curated(results, threshold: Optional[float] = None)
```

Order curated hits above the near-duplicates that quote them, in place.

Each hit rises to the earliest position it has **earned**: the index of the
topmost hit it directly duplicates, searching no further back than the
nearest earlier hit it does not outrank by kind (curated document →
transcript → diary). A hit that earns nothing keeps the ranker's index, so
hits in no duplicate relationship — the overwhelming majority — keep the
ranker's order exactly.

Two properties this guarantees, both of them learned the hard way:

* **Nothing ever passes a hit it does not outrank.** The rank test guards
  every hit *passed*, not merely the hit earned from. Guarding only the
  latter let a transcript that duplicated both a diary below it and a card
  above it earn the diary's index and overtake the card on the way — the
  curated hit demoted below a copy quoting it, the exact inverse of this
  function's purpose.
* **The result is a fixed point.** Passes repeat until the order settles,
  so calling this twice changes nothing. A single pass is not enough: a
  reorder creates new adjacencies and can manufacture fresh earns.

Collateral overtaking WITHIN those bounds is real and intended: a promoted
hit passes any hit it outranks that happens to lie between it and the copy
it earned, whether or not it duplicates that one too. That is unavoidable
for a single-list ordering — the alternative is to leave the curated hit
buried — and it is bounded by the rank barrier above.

Idempotent, tolerant of non-dict items and a non-list argument, and never
raises: this runs on the return path of a search the caller already paid
for.
