"""Reciprocal Rank Fusion — combine ranked lists from N retrievers.

RRF is a classical fusion algorithm (Cormack, Clarke & Buettcher 2009):
given N ranked lists over the same item universe, score each item by
``sum(1/(k + rank_i))`` across the lists where it appears. Items absent
from a list contribute 0. ``k=60`` is the conventional smoothing
constant — large enough that the top-1 advantage (1/61 vs 1/62) is
small relative to the gap between top-K and absent.

We use this to fuse vector-search rankings from multiple encoders. The
existing :func:`mempalace.searcher._hybrid_rank` already fuses BM25
with vector cosine via a convex combination on shared candidates; RRF
solves a different problem — fusing *ranked lists* whose underlying
score scales are not comparable. Different encoders' cosine spaces
have different distributions, so direct distance-averaging is
unprincipled; RRF only requires the rank ordering.

This module is pure and dependency-free; the multi-encoder retrieval
glue lives in :mod:`mempalace.multi_encoder`.

Used by
-------

* :func:`mempalace.multi_encoder.fused_query` — query-time fusion
  across N encoder-bound palaces (research feature, gated by
  ``PALACE_USE_MULTI_ENCODER_RRF``).
* ``scripts/eval_multi_encoder_rrf.py`` — evaluation harness.

References
----------

* Cormack et al. 2009, "Reciprocal Rank Fusion outperforms Condorcet
  and individual rank learning methods", SIGIR '09.
* ``scripts/verify_rrf_ftcode5k.py`` — the surrogate-RRF probe that
  motivated this implementation. It only knows rank-of-expected, so
  fuses with ``min(rank)``; this module does proper score-based
  fusion across the full ranked lists.
"""

from __future__ import annotations

from typing import Any, Callable, Hashable, Sequence, TypeVar

T = TypeVar("T")

DEFAULT_K = 60


def rrf_scores(
    rank_lists: Sequence[Sequence[T]],
    key: Callable[[T], Hashable] | None = None,
    k: int = DEFAULT_K,
) -> dict[Hashable, float]:
    """Compute RRF scores for items across ``rank_lists``.

    Parameters
    ----------
    rank_lists
        Sequence of ranked lists. Each inner sequence is ordered best
        → worst. The rank an item contributes is its 1-indexed
        position within its containing list.
    key
        Function mapping an item to a hashable identity used for
        cross-list aggregation. Defaults to the item itself, which
        works for strings/ints/tuples but not dicts.
    k
        RRF smoothing constant. Default 60 per Cormack et al.

    Returns
    -------
    dict
        ``{identity: aggregate_score}`` where score is
        ``sum(1/(k + rank_i))`` over the lists where the identity
        appears.

    Notes
    -----
    Within a single list, only the *first* occurrence of an identity
    counts toward the rank. This matters when a downstream caller
    feeds a list with duplicates (e.g. two chunks from the same
    source file collapsed by source-file identity); the better-ranked
    duplicate wins, the worse-ranked is ignored.
    """
    if k <= 0:
        raise ValueError(f"RRF k must be > 0, got {k}")
    _key = key if key is not None else (lambda x: x)
    scores: dict[Hashable, float] = {}
    for ranked in rank_lists:
        seen_in_list: set[Hashable] = set()
        for rank0, item in enumerate(ranked):
            ident = _key(item)
            if ident in seen_in_list:
                continue
            seen_in_list.add(ident)
            rank1 = rank0 + 1
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + rank1)
    return scores


def rrf_fuse(
    rank_lists: Sequence[Sequence[T]],
    key: Callable[[T], Hashable] | None = None,
    k: int = DEFAULT_K,
    representative: Callable[[Sequence[T]], T] | None = None,
) -> list[tuple[Hashable, float, T]]:
    """Return RRF-fused items sorted best-first.

    Parameters
    ----------
    rank_lists
        Same as :func:`rrf_scores`.
    key
        Same as :func:`rrf_scores`. Required when items are not
        natively hashable (e.g. dicts).
    k
        RRF smoothing constant.
    representative
        Given the list of all occurrences of one identity (across
        all input lists, in input-order), return the item to surface
        in the fused output. Defaults to the first occurrence —
        which corresponds to the best-ranked-list/highest-rank
        version of the item. Useful when one list carries more
        metadata than another and you want to prefer that copy.

    Returns
    -------
    list of (identity, rrf_score, representative_item)
        Sorted descending by ``rrf_score``. Ties broken by
        first-seen order (stable sort).
    """
    _key = key if key is not None else (lambda x: x)
    scores = rrf_scores(rank_lists, key=_key, k=k)
    # Collect all occurrences per identity, preserving input order so
    # the default representative is the best-ranked-list/highest-rank
    # copy.
    occurrences: dict[Hashable, list[T]] = {}
    for ranked in rank_lists:
        for item in ranked:
            ident = _key(item)
            occurrences.setdefault(ident, []).append(item)
    fused: list[tuple[Hashable, float, T]] = []
    for ident, score in scores.items():
        items = occurrences[ident]
        rep = representative(items) if representative is not None else items[0]
        fused.append((ident, score, rep))
    fused.sort(key=lambda triple: triple[1], reverse=True)
    return fused


def explain_fusion(
    rank_lists: Sequence[Sequence[T]],
    list_names: Sequence[str] | None = None,
    key: Callable[[T], Hashable] | None = None,
    k: int = DEFAULT_K,
) -> list[dict[str, Any]]:
    """Diagnostic — return per-identity rank breakdown for debugging.

    Useful when an item appears in the fused top-K and you want to
    see which encoders ranked it where. Not used in the hot path.
    """
    _key = key if key is not None else (lambda x: x)
    names = (
        list(list_names)
        if list_names is not None
        else [f"list_{i}" for i in range(len(rank_lists))]
    )
    if len(names) != len(rank_lists):
        raise ValueError(f"list_names length {len(names)} != rank_lists length {len(rank_lists)}")
    # Per identity: {list_name: rank_1_indexed_or_None}
    per_ident: dict[Hashable, dict[str, int | None]] = {}
    for name, ranked in zip(names, rank_lists):
        seen_in_list: set[Hashable] = set()
        for rank0, item in enumerate(ranked):
            ident = _key(item)
            if ident in seen_in_list:
                continue
            seen_in_list.add(ident)
            per_ident.setdefault(ident, {n: None for n in names})[name] = rank0 + 1
    scores = rrf_scores(rank_lists, key=_key, k=k)
    out: list[dict[str, Any]] = []
    for ident, ranks in per_ident.items():
        out.append({"identity": ident, "ranks": ranks, "rrf_score": scores[ident]})
    out.sort(key=lambda r: r["rrf_score"], reverse=True)
    return out


__all__ = ["rrf_scores", "rrf_fuse", "explain_fusion", "DEFAULT_K"]
