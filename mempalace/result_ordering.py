"""Curated-over-transcript ordering for near-duplicate search hits.

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
"""

import logging
import re
from typing import Optional

from .provenance import source_kind

logger = logging.getLogger(__name__)

__all__ = [
    "NEAR_DUP_THRESHOLD",
    "is_near_duplicate",
    "prefer_curated",
    "similarity",
]

# Word 3-grams: long enough that shared word order means shared sentences,
# short enough to survive a chunk boundary landing mid-sentence.
_SHINGLE_N = 3

# Below this many shingles there is not enough text to call anything a
# duplicate — a stub would otherwise match another stub at 1.0.
_MIN_SHINGLES = 5

# Calibrated on production; sits in the measured gap between same-topic pairs
# (<= 0.323) and genuine quoting pairs (>= 0.373). See the module docstring.
NEAR_DUP_THRESHOLD = 0.35

# The comparison is O(n^2) in the hit count. Interactive limits are 5-50; a
# caller asking for thousands gets the ranker's order rather than a stall.
_MAX_PAIRWISE_RESULTS = 200

# Passes are repeated until the order stops changing, so the value returned is
# always a fixed point and a second call is a no-op. Termination is not merely
# hoped for: a hit only ever moves up past hits it strictly outranks, which
# strictly increases sum(rank * position), and that sum is bounded.
#
# The cap is the belt-and-braces stop, and 8 was too low. Measured on a dense
# stress generator at n=40 — the widest window ``_WIDEN_CAP`` can fetch, and
# far denser than a real search window — over 600 inputs:
#
#     cap   8 -> 591/600 non-fixed-points      cap  32 -> 2/600
#     cap  48 -> 0/600                         cap 128 -> 0/600
#
# Below the cap the loop returned a NON-fixed-point silently, contradicting
# the promise above. A higher cap is free: every row above ran in the same
# 1.22s, because the loop exits on the first pass that moves nothing. 48 is
# the smallest value that measured zero here, and exhausting it is logged
# rather than swallowed.
_MAX_STABILISE_PASSES = 48

# Lower sorts earlier. Curated documents a human maintains outrank a quoted
# copy; a palace diary summary sorts last because it carries no citable
# source at all (item G).
_KIND_RANK = {
    "memory": 0,
    "file": 0,
    "transcript": 1,
    "unknown": 1,
    "diary": 2,
}
_DEFAULT_KIND_RANK = 1

_WORD_RE = re.compile(r"[^0-9a-z]+")


def _tokens(text) -> list:
    if not isinstance(text, str):
        return []
    return [w for w in _WORD_RE.split(text.lower()) if w]


def _shingles(text) -> set:
    words = _tokens(text)
    if len(words) < _SHINGLE_N:
        return set()
    return {tuple(words[i : i + _SHINGLE_N]) for i in range(len(words) - _SHINGLE_N + 1)}


def similarity(a, b) -> float:
    """Overlap coefficient over word 3-grams: ``|A n B| / min(|A|, |B|)``.

    1.0 means the shorter text's word order is wholly contained in the longer
    one — which is what a transcript quoting a document looks like. 0.0 when
    either side is empty or too short to shingle.
    """
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def is_near_duplicate(a, b, threshold: Optional[float] = None) -> bool:
    """Do these two texts carry substantially the same content?

    ``False`` whenever either side is too short to be evidence (fewer than
    :data:`_MIN_SHINGLES` shingles), so a one-line stub never drags an
    unrelated document up the list.
    """
    sa, sb = _shingles(a), _shingles(b)
    if len(sa) < _MIN_SHINGLES or len(sb) < _MIN_SHINGLES:
        return False
    thr = NEAR_DUP_THRESHOLD if threshold is None else threshold
    return (len(sa & sb) / min(len(sa), len(sb))) >= thr


def _kind_rank(hit) -> int:
    kind = hit.get("source_kind") or source_kind(hit)
    return _KIND_RANK.get(kind, _DEFAULT_KIND_RANK)


def _stabilise_once(order: list, ranks: dict, shingles: dict, thr: float):
    """One ordering pass. Returns the new order, or None when nothing moved.

    ``order`` is a list of result indices in their CURRENT order. Each entry
    is given the earliest position it has earned, then the list is re-sorted.

    The backward scan stops at the nearest earlier hit this one does not
    outrank. That barrier is the bound: without it a hit could earn the index
    of something it outranks far above and, in landing there, sail past a hit
    in between that it does NOT outrank — which is how a curated card ended up
    below a transcript that quotes it.
    """
    n = len(order)
    earned = list(range(n))
    for pos in range(n):
        i = order[pos]
        si = shingles[i]
        if len(si) < _MIN_SHINGLES:
            continue
        # Nearest earlier hit this one does not outrank; it may not pass it.
        lo = 0
        for q in range(pos - 1, -1, -1):
            if ranks[order[q]] <= ranks[i]:
                lo = q + 1
                break
        for q in range(lo, pos):
            sj = shingles[order[q]]
            if len(sj) < _MIN_SHINGLES:
                continue
            if len(si & sj) / min(len(si), len(sj)) >= thr:
                earned[pos] = q  # ascending, so the first match is the topmost
                break
    if all(earned[pos] == pos for pos in range(n)):
        return None
    ranked = sorted(range(n), key=lambda pos: (earned[pos], ranks[order[pos]], pos))
    return [order[pos] for pos in ranked]


def prefer_curated(results, threshold: Optional[float] = None):
    """Order curated hits above the near-duplicates that quote them, in place.

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
    """
    if not isinstance(results, list) or len(results) < 2:
        return results
    if len(results) > _MAX_PAIRWISE_RESULTS:
        return results

    indexed = [(i, h) for i, h in enumerate(results) if isinstance(h, dict)]
    if len(indexed) < 2:
        return results

    thr = NEAR_DUP_THRESHOLD if threshold is None else threshold
    shingles = {i: _shingles(h.get("text")) for i, h in indexed}
    ranks = {i: _kind_rank(h) for i, h in indexed}
    order = [i for i, _ in indexed]
    original = list(order)

    settled = False
    for _ in range(_MAX_STABILISE_PASSES):
        nxt = _stabilise_once(order, ranks, shingles, thr)
        if nxt is None:
            settled = True
            break
        order = nxt
    if not settled:
        # The returned order is usable but is NOT a fixed point, so a second
        # call could still move it. Never silent: an under-settled result
        # would otherwise look identical to a settled one.
        logger.warning(
            "result ordering did not settle within %d passes for %d hits; "
            "returning the last order (not a fixed point)",
            _MAX_STABILISE_PASSES,
            len(order),
        )

    if order == original:
        return results

    moved = iter([results[i] for i in order])
    results[:] = [next(moved) if isinstance(h, dict) else h for h in results]
    return results
