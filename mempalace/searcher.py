#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Hybrid search: BM25 keyword matching + vector semantic similarity. The
drawer query is the floor — always runs — and closet hits add a rank-based
boost when they agree. Closets are a ranking *signal*, never a gate, so
weak closets (regex extraction on narrative content) can only help, never
hide drawers the direct path would have found.
"""

import functools
import logging
import math
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Optional

from .backends import (
    BackendError,
    BackendMismatchError,
    CollectionNotInitializedError,
    PalaceNotFoundError,
    UnsupportedCapabilityError,
)
from .config import MempalaceConfig, sqlite_read_uri
from .date_window import filed_at_in_window, parse_window
from .i18n import _canonical_lang, get_stopwords
from .miner import _open_collection_or_explain
from .palace import (
    get_closets_collection,
    get_collection,
    resolve_backend_name,
)
from .provenance import annotate as _annotate_provenance
from .result_ordering import prefer_curated as _prefer_curated
from .ratings import net_rating, rating_distance_adjustment
from .recency import RECENCY_HALFLIFE_DAYS, recency_distance_adjustment

# Closet pointer line format: "topic|entities|→drawer_id_a,drawer_id_b"
# Multiple lines may join with newlines inside one closet document.
_CLOSET_DRAWER_REF_RE = re.compile(r"→([\w,]+)")


logger = logging.getLogger("mempalace_mcp")


class SearchError(Exception):
    """Raised when search cannot proceed (e.g. no palace found)."""


_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)


def _first_or_empty(results, key: str) -> list:
    """Return the first inner list of a query result field, or [].

    Accepts both the typed :class:`QueryResult` (attribute access) and the
    pre-typed chroma dict shape; this polymorphism is retained so test mocks
    still work and callers mid-migration do not crash. Preserves the empty-
    collection semantics from issue #195: when no queries returned hits, the
    outer list may be empty and indexing ``[0]`` would raise.
    """
    outer = getattr(results, key, None) if not isinstance(results, dict) else results.get(key)
    if not outer:
        return []
    return outer[0] or []


def _aligned_query_ids(results, document_count: int) -> list:
    """Return query IDs padded to match the document result column.

    Production backends return an ID for every document. Some legacy test
    mocks omit IDs, so pad with ``None`` instead of letting ``zip`` discard
    otherwise valid mocked results.
    """
    ids = list(_first_or_empty(results, "ids"))
    if len(ids) < document_count:
        ids.extend([None] * (document_count - len(ids)))
    return ids[:document_count]


def _result_drawer_id(meta, stored_drawer_id):
    """Return the ID that round-trips through ``mempalace_get_drawer``.

    Chunk metadata carries the logical-group id under ``parent_drawer_id``
    (``tool_add_drawer``) or ``parent_entry_id`` (``tool_diary_write``);
    resolving both means a hit on a chunked diary entry reports the id that
    fetches the WHOLE entry rather than the one chunk that matched (#2185).
    Kept in sync with ``mcp_server._PARENT_ID_KEYS``.
    """
    meta = meta or {}
    return meta.get("parent_drawer_id") or meta.get("parent_entry_id") or stored_drawer_id


def _tokenize(text: str, stop_words: frozenset = frozenset()) -> list:
    """Lowercase + strip to alphanumeric tokens of length ≥ 2.

    Tolerates ``None`` documents — Chroma can return ``None`` in the
    ``documents`` field for drawers without text content, which would
    otherwise raise ``AttributeError`` mid-rerank.

    When ``stop_words`` is non-empty, filters tokens that match any entry.
    The set is expected to already be lowercased so callers can share one
    instance across query + document tokenization.
    """
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text.lower())
    if stop_words:
        return [t for t in tokens if t not in stop_words]
    return tokens


@functools.lru_cache(maxsize=16)
def _stopwords_for_canonical(canonical_lang: str) -> frozenset:
    """Cached stop-word set keyed by a canonical locale code.

    Splitting canonicalization out of the cache key avoids thrashing when
    callers pass equivalent variants (``"EN"``, ``"en"``, ``"en-US"``) —
    they all hit the same cache slot.
    """
    return frozenset(get_stopwords(canonical_lang))


def _stopwords_for_lang(lang: str) -> frozenset:
    """Resolve raw ``lang`` to its canonical form before cache lookup.

    Kept as the public-shaped helper (callers and tests reach for this
    name) while the lru_cache lives on ``_stopwords_for_canonical`` to
    keep the cache key normalized.
    """
    canonical = _canonical_lang(lang) or lang.lower()
    return _stopwords_for_canonical(canonical)


def _resolve_stop_words(lang: Optional[str]) -> frozenset:
    """Return the BM25 stop-word set for ``lang`` as an opt-in feature.

    When ``lang`` is an explicit string, loads that locale's stop words.
    When ``lang`` is ``None``, resolution order is:

    1. ``MEMPALACE_LANG`` / ``MEMPAL_LANG`` environment variable.
    2. ``MempalaceConfig().lang_explicit`` (which itself reads the env vars
       first, then ``config.json["lang"]``).

    The env-var fast path avoids constructing ``MempalaceConfig`` (which
    reads ``config.json`` from disk) on the hot search path when the user
    has set the env var — the common case for explicit-locale palaces.
    Palaces that never configured a language get an empty set, preserving
    pre-PR scoring byte-for-byte.
    """
    if lang is None:
        env_val = os.environ.get("MEMPALACE_LANG") or os.environ.get("MEMPAL_LANG")
        if env_val and env_val.strip():
            lang = env_val.strip()
        else:
            try:
                lang = MempalaceConfig().lang_explicit
            except Exception:
                logger.debug("lang resolution failed, skipping stop-word filter", exc_info=True)
                return frozenset()
        if lang is None:
            return frozenset()
    return _stopwords_for_lang(lang)


def _bm25_scores(
    query: str,
    documents: list,
    k1: float = 1.5,
    b: float = 0.75,
    stop_words: frozenset = frozenset(),
) -> list:
    """Compute Okapi-BM25 scores for ``query`` against each document.

    IDF is computed over the *provided corpus* using the Lucene/BM25+
    smoothed formula ``log((N - df + 0.5) / (df + 0.5) + 1)``, which is
    always non-negative. This is well-defined for re-ranking a small
    candidate set returned by vector retrieval — IDF then reflects how
    discriminative each query term is *within the candidates*, exactly
    what's needed to reorder them.

    Parameters mirror Okapi-BM25 conventions:
        k1 — term-frequency saturation (1.2-2.0 typical, 1.5 default)
        b  — length normalization (0.0 = none, 1.0 = full, 0.75 default)

    Returns a list of scores in the same order as ``documents``.
    """
    n_docs = len(documents)
    query_terms = set(_tokenize(query, stop_words))
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs

    tokenized = [_tokenize(d, stop_words) for d in documents]
    doc_lens = [len(toks) for toks in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0

    # Document frequency: how many docs contain each query term?
    df = {term: 0 for term in query_terms}
    for toks in tokenized:
        seen = set(toks) & query_terms
        for term in seen:
            df[term] += 1

    idf = {term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1) for term in query_terms}

    scores = []
    for toks, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf: dict = {}
        for t in toks:
            if t in query_terms:
                tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores


def _distance_to_similarity(distance, metric: str = "cosine") -> float:
    """Map a backend-reported ``distance`` to a [0, 1]-ish similarity.

    The backend contract for the ``distances`` field is *lower = closer*
    regardless of metric (RFC 001, backend metric declaration), so every
    mapping here is monotonic decreasing in ``distance``. The output stays
    bounded so it is
    commensurable with the min-max-normalized BM25 term in
    :func:`_hybrid_rank`.

    * ``cosine`` — distance ∈ [0, 2], 0 = identical: ``max(0, 1 - d)``.
    * ``l2`` — Euclidean ∈ [0, ∞): ``1 / (1 + d)`` (1 at d=0, →0 as d→∞).
    * ``ip`` — inner-product distance (e.g. pgvector ``<#>`` = -dot, lower =
      closer), unbounded and signed: logistic squash ``1 / (1 + e^d)``.
      Provisional until a real ip backend exercises it; no in-tree backend
      uses ip today.

    ``distance is None`` (vector-unknown, e.g. a BM25-only candidate) maps to
    0.0 so the candidate scores on its BM25 contribution alone.
    """
    if distance is None:
        return 0.0
    m = (metric or "cosine").lower()
    if m == "l2":
        return 1.0 / (1.0 + max(0.0, distance))
    if m == "ip":
        # Clamp the exponent so a large positive distance can't overflow.
        return 1.0 / (1.0 + math.exp(min(60.0, distance)))
    # cosine (default)
    return max(0.0, 1.0 - distance)


def _metric_for_collection(col) -> str:
    """Resolve a collection's declared distance metric, defaulting to cosine.

    Reads the ``distance_metric`` exposed by the backend collection (the
    RFC 001 backend metric declaration). ``EmbeddingCollection`` delegates the
    attribute to its inner collection; legacy Chroma palaces report their
    actual ``hnsw:space``.
    Any failure falls back to ``"cosine"`` — the value all in-tree backends
    use and the only metric MemPalace created palaces with historically.
    """
    try:
        metric = getattr(col, "distance_metric", "cosine")
    except Exception:
        return "cosine"
    metric = str(metric or "cosine").lower()
    return metric if metric in ("cosine", "l2", "ip") else "cosine"


def _vector_distance(
    query_vector: list[float], candidate_vector: list[float], metric: str
) -> float:
    """Return a backend-style distance between two already-normalized vectors."""
    if not query_vector or not candidate_vector or len(query_vector) != len(candidate_vector):
        raise ValueError("embedding dimensions do not match")

    if metric == "l2":
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(query_vector, candidate_vector)))

    dot = sum(a * b for a, b in zip(query_vector, candidate_vector))
    if metric == "ip":
        return -dot

    q_norm = math.sqrt(sum(a * a for a in query_vector))
    c_norm = math.sqrt(sum(b * b for b in candidate_vector))
    if q_norm == 0.0 or c_norm == 0.0:
        raise ValueError("zero-norm embedding")
    return 1.0 - (dot / (q_norm * c_norm))


def _lexical_hit_vector_distances(drawers_col, query: str, lexical_hits: list, metric: str) -> dict:
    """Compute vector distances for lexical hits when stored embeddings are available."""
    ids = [hit.id for hit in lexical_hits if getattr(hit, "id", None)]
    if not ids:
        return {}

    try:
        from .backends.embedding_wrapper import _embed_texts

        query_vector = _embed_texts([query])[0]
        stored = drawers_col.get(ids=ids, include=["embeddings"])
    except Exception:
        logger.debug(
            "candidate_strategy=union: failed to load lexical hit embeddings", exc_info=True
        )
        return {}

    stored_ids = getattr(stored, "ids", None) if not isinstance(stored, dict) else stored.get("ids")
    embeddings = (
        getattr(stored, "embeddings", None)
        if not isinstance(stored, dict)
        else stored.get("embeddings")
    )
    if not stored_ids or not embeddings:
        return {}

    distances = {}
    for doc_id, candidate_vector in zip(stored_ids, embeddings):
        try:
            distances[doc_id] = _vector_distance(query_vector, candidate_vector, metric)
        except Exception:
            logger.debug(
                "candidate_strategy=union: failed to score lexical hit %s", doc_id, exc_info=True
            )
    return distances


def _hybrid_rank(
    results: list,
    query: str,
    vector_weight: float | None = None,
    bm25_weight: float | None = None,
    metric: str = "cosine",
    stop_words: frozenset = frozenset(),
) -> list:
    """Re-rank ``results`` by a convex combination of vector similarity and BM25.

    * Vector similarity is derived from each candidate's backend-reported
      ``distance`` via :func:`_distance_to_similarity`, interpreted in the
      collection's declared ``metric`` (per RFC 001) rather than assuming
      cosine. Absolute (not relative-to-max) means adding/removing a
      candidate can't reshuffle the others.
    * BM25 is real Okapi-BM25 with corpus-relative IDF over the candidates
      themselves. Since the absolute scale is unbounded, BM25 is min-max
      normalized within the candidate set so weights are commensurable.

    Candidates with ``distance=None`` are treated as vector-unknown
    (no vector signal available) and scored on BM25 contribution alone.
    Used by candidate-union mode to merge BM25-only candidates that the
    vector index didn't surface.

    When ``vector_weight``/``bm25_weight`` are left ``None`` (the production
    dispatch path), the weights come from :func:`_hybrid_weights` so the #111
    sweep can tune them via env without a code change. Explicit kwargs (used
    by unit tests) bypass the env so tests stay deterministic.

    Mutates each result dict to add ``bm25_score`` and reorders the list
    in place. Returns the same list for convenience.
    """
    if not results:
        return results

    if vector_weight is None or bm25_weight is None:
        env_vw, env_bw = _hybrid_weights()
        if vector_weight is None:
            vector_weight = env_vw
        if bm25_weight is None:
            bm25_weight = env_bw

    docs = [r.get("text", "") for r in results]
    bm25_raw = _bm25_scores(query, docs, stop_words=stop_words)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    bm25_norm = [s / max_bm25 for s in bm25_raw] if max_bm25 > 0 else [0.0] * len(bm25_raw)

    scored = []
    for r, raw, norm in zip(results, bm25_raw, bm25_norm):
        vec_sim = _distance_to_similarity(r.get("distance"), metric)
        # Tokenizer disagreement guard: BM25 search backends (postgres
        # tsvector, sqlite FTS5) tokenize on punctuation including
        # underscores, so `ts_rank_cd` splits into 3 tokens. Local
        # `_tokenize` uses `\w{2,}` which keeps underscores, treating
        # `ts_rank_cd` as one token. Candidates surfaced via BM25
        # search would get `raw=0` from local recompute even though
        # they're genuinely strong BM25 matches in the source backend.
        # Treat the BM25-surfaced signal as "already vetted" — give
        # them max BM25 contribution rather than re-judging with the
        # weaker local tokenizer.
        matched_via = r.get("matched_via", "")
        if matched_via in ("bm25_postgres", "bm25_sqlite"):
            effective_norm = max(norm, 0.9)  # near-max; tiebreak still possible via vec_sim
            r["bm25_score"] = round(max(raw, 0.9), 3)
        else:
            effective_norm = norm
            r["bm25_score"] = round(raw, 3)
        scored.append((vector_weight * vec_sim + bm25_weight * effective_norm, r))

    # Break exact score ties toward the more recently authored drawer so equal-score
    # candidates rank chronologically instead of in arbitrary backend order. ISO-8601
    # ``authored_at`` strings sort chronologically; missing dates sort oldest.
    # authored_at lives at the top level on the search_memories path and nested under
    # "metadata" on the candidate-union path; check both so the tie-break works for each.
    scored.sort(
        key=lambda pair: (
            pair[0],
            pair[1].get("authored_at") or pair[1].get("metadata", {}).get("authored_at") or "",
        ),
        reverse=True,
    )
    results[:] = [r for _, r in scored]
    return results


def _candidate_identity(r: dict):
    """Stable per-candidate identity for cross-list RRF fusion.

    Chunk-precise: ``(source_file_full, chunk_index)`` when present so two
    files sharing a basename don't collide and distinct chunks of one file
    stay distinct. Falls back to the drawer ``id`` then ``source_file`` for
    legacy rows missing the richer metadata. Mirrors the dedup key used by
    the candidate mergers so RRF fuses on the same identity the union/hybrid
    strategies dedup on.
    """
    full = r.get("_source_file_full")
    ci = r.get("_chunk_index")
    if full and ci is not None:
        return (full, ci)
    return r.get("id") or r.get("source_file") or id(r)


def _rrf_rank(
    results: list,
    query: str,
    k: int = None,
) -> list:
    """Re-rank ``results`` by Reciprocal Rank Fusion of vector + BM25 lists.

    The alternative to :func:`_hybrid_rank`'s convex combination. Instead of
    blending normalized scores on a shared scale, RRF fuses two *rank
    orderings*:

    * **vector list** — candidates with a real ``distance``, ordered best
      (smallest distance) first. ``distance=None`` candidates (BM25-only or
      graph-surfaced) carry no vector signal and are absent from this list.
    * **bm25 list** — all candidates ordered by descending BM25 score. The
      same tokenizer-disagreement guard as ``_hybrid_rank`` applies:
      candidates surfaced by a BM25 search backend (``matched_via`` in
      ``bm25_postgres``/``bm25_sqlite``) are floored to a near-max BM25 score
      so the weaker local tokenizer can't demote a genuine backend match.

    RRF (Cormack et al. 2009) only requires the orderings, not commensurable
    score scales — which is exactly the case here, since cosine similarity
    and Okapi-BM25 live on incomparable scales. Items absent from a list
    contribute 0; an item strong in both lists fuses above an item strong in
    only one.

    Mutates each result dict to add ``bm25_score`` (parity with
    ``_hybrid_rank`` so downstream display is uniform) and reorders the list
    in place. Returns the same list for convenience.
    """
    if not results:
        return results

    from .rrf import DEFAULT_K, rrf_fuse

    fuse_k = DEFAULT_K if k is None else k

    docs = [r.get("text", "") for r in results]
    bm25_raw = _bm25_scores(query, docs)
    for r, raw in zip(results, bm25_raw):
        matched_via = r.get("matched_via", "")
        if matched_via in ("bm25_postgres", "bm25_sqlite"):
            r["bm25_score"] = round(max(raw, 0.9), 3)
        else:
            r["bm25_score"] = round(raw, 3)

    # Vector list: real-distance candidates, best (smallest distance) first.
    vector_list = sorted(
        (r for r in results if r.get("distance") is not None),
        key=lambda r: r["distance"],
    )
    # BM25 list: every candidate, highest BM25 first.
    bm25_list = sorted(results, key=lambda r: r["bm25_score"], reverse=True)

    fused = rrf_fuse(
        [vector_list, bm25_list],
        key=_candidate_identity,
        k=fuse_k,
    )
    for ident, score, rep in fused:
        rep["rrf_score"] = round(score, 6)
    results[:] = [rep for _ident, _score, rep in fused]
    return results


# Closet-boost ranking constants. Hoisted to module level so they can be
# tuned from the outside (env var, config flag, or in-process patch for
# A/B benchmarking) without touching `search_memories`. The ordinal signal
# — "which closet matched best for this source" — is more reliable than
# absolute distance on narrative content, where closet distances cluster
# in 1.2–1.5 regardless of match quality.
#
# Empirical note (A/B ablation 2026-04-27 on the 151K canonical palace,
# 12-probe set covering recent fork-side work + transcript content):
# boost fires on ~20% of result rows, concentrated in queries whose
# answer lives in mined files; closets are sparse on chat-transcript
# queries (most fork-side decisions). When the boost did fire, it
# re-ordered chunks within a single source file rather than displacing
# right answers with wrong ones — i.e., VecRecall's critique
# (https://github.com/MemPalace/mempalace/discussions/1129, "org-layer
# in retrieval path drops R@5") didn't reproduce here. Kept as a
# rare-but-cheap signal; ablation script lived in /tmp, not committed.
CLOSET_RANK_BOOSTS = [0.40, 0.25, 0.15, 0.08, 0.04]
CLOSET_DISTANCE_CAP = 1.5  # cosine dist > 1.5 = too weak to use as signal


def _rating_boost_enabled() -> bool:
    """Whether the feedback-rating ranking signal (#159) is active.

    On by default; set ``PALACE_RATING_BOOST=0`` to disable (A/B, debugging).
    Read live from the environment so the daemon picks up the toggle without
    a restart, mirroring the multi-encoder RRF gate.
    """
    return os.environ.get("PALACE_RATING_BOOST", "1").strip() != "0"


def _recency_boost_enabled() -> bool:
    """Whether the recency ranking signal (#158) is active.

    Off by default — recency is an experimental tilt we A/B against our own
    corpus before trusting it, so it ships dark. Set ``PALACE_RECENCY_BOOST=1``
    to enable. Read live from the environment so the daemon picks it up
    without a restart, mirroring the rating gate.
    """
    return os.environ.get("PALACE_RECENCY_BOOST", "0").strip() == "1"


def _recency_halflife_days() -> float:
    """Half-life (days) for the recency decay, from the environment.

    Falls back to ``RECENCY_HALFLIFE_DAYS`` when unset or unparseable. A
    non-positive value disables the signal in ``recency_distance_adjustment``.
    """
    raw = os.environ.get("PALACE_RECENCY_HALFLIFE_DAYS", "").strip()
    if not raw:
        return RECENCY_HALFLIFE_DAYS
    try:
        return float(raw)
    except ValueError:
        return RECENCY_HALFLIFE_DAYS


# Convex-fusion weights for ``_hybrid_rank`` (techempower-org/mempalace, SME
# #111). The hybrid candidate strategy unions BM25/graph candidates into the
# vector pool, then re-ranks the whole pool by a convex combination of vector
# similarity and BM25. Because BM25 IDF is recomputed corpus-relative to the
# candidate set, the *act* of widening the pool shifts every drawer's BM25
# score and can reshuffle near-tied vector hits — which is how the
# candidate-strategy ablation (baselines/candidate-strategy-2026-05-28.json)
# saw hybrid win tail recall (R@5 0.92→1.00) while demoting a rank-1 vector
# match (MRR -2.3pp). The weights are the lever that trades those off, so we
# expose them as a live env knob (read per call, no restart needed) mirroring
# the rating/recency gates. Defaults reproduce the historical 0.6/0.4 blend.
HYBRID_VECTOR_WEIGHT = 0.6
HYBRID_BM25_WEIGHT = 0.4


def _hybrid_weights() -> tuple[float, float]:
    """Return ``(vector_weight, bm25_weight)`` for the convex hybrid blend.

    Overridable via ``PALACE_HYBRID_VECTOR_WEIGHT`` / ``PALACE_HYBRID_BM25_WEIGHT``
    so the #111 weight sweep can A/B against the live daemon without a code
    change per point. Unparseable or unset values fall back to the module
    defaults. The two weights are independent (not forced to sum to 1) — the
    convex score is a monotone blend, so only their *ratio* changes ranking.
    """

    def _read(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            v = float(raw)
        except ValueError:
            return default
        return v if v >= 0.0 else default

    return (
        _read("PALACE_HYBRID_VECTOR_WEIGHT", HYBRID_VECTOR_WEIGHT),
        _read("PALACE_HYBRID_BM25_WEIGHT", HYBRID_BM25_WEIGHT),
    )


def build_where_filter(
    wing: str = None,
    room: str = None,
    tags: list = None,
    source_file: str = None,
) -> dict:
    """Build ChromaDB-style where filter for wing/room/tag/source_file filtering.

    ``tags`` requires drawers to carry EVERY listed tag (AND logic). On the
    postgres backend the filter is pushed down via the ``$contains_all``
    JSONB operator; for chroma it's stripped here and applied as a
    post-filter by the caller (see ``search_memories``). ChromaDB needs a
    ``$and`` only when ≥2 clauses are present; a single clause is returned
    bare and zero clauses yield an empty filter (#1815).
    """
    from .tags import normalise_tags

    parts: list[dict] = []
    if wing:
        parts.append({"wing": wing})
    if room:
        parts.append({"room": room})
    normalised_tags = normalise_tags(tags) if tags else []
    if normalised_tags:
        parts.append({"tags": {"$contains_all": normalised_tags}})
    if source_file:
        parts.append({"source_file": source_file})

    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}


def _extract_drawer_ids_from_closet(closet_doc: str) -> list:
    """Parse all `→drawer_id_a,drawer_id_b` pointers out of a closet document.

    Preserves order and dedupes.
    """
    seen: dict = {}
    for match in _CLOSET_DRAWER_REF_RE.findall(closet_doc):
        for did in match.split(","):
            did = did.strip()
            if did and did not in seen:
                seen[did] = None
    return list(seen.keys())


def _scoped_source_filter(source_file: str, parent_drawer_id=None) -> dict:
    """Build a Chroma ``where`` clause that scopes a query to ``source_file``,
    additionally constrained by ``parent_drawer_id`` when one is supplied.

    Two unrelated oversized ``tool_add_drawer`` writes (chunked path from
    #1539) can pass the same ``source_file`` (e.g. two pastes tagged
    ``"chat.log"``); each call stores its own ``parent_drawer_id`` group
    of chunks but the bare ``source_file`` filter pulls chunks from both
    groups as if they were siblings (#1580). When the matched chunk
    carries a ``parent_drawer_id`` the filter narrows to that logical
    group. Otherwise (pre-#1539 drawers, single-chunk writes, and
    ``diary_ingest`` drawers grouped by real file path) the original
    file-global shape is preserved. Mirrors the conditional-``$and``
    precedent in ``build_where_filter``.
    """
    if parent_drawer_id:
        return {
            "$and": [
                {"source_file": source_file},
                {"parent_drawer_id": parent_drawer_id},
            ]
        }
    return {"source_file": source_file}


def _expand_with_neighbors(drawers_col, matched_doc: str, matched_meta: dict, radius: int = 1):
    """Expand a matched drawer with its ±radius sibling chunks in the same source file.

    Motivation — "drawer-grep context" feature: a closet hit returns one
    drawer, but the chunk boundary may clip mid-thought (e.g., the matched
    chunk says "here's a breakdown:" and the actual breakdown lives in the
    next chunk). Fetching the small neighborhood around the match gives
    callers enough context without forcing a follow-up ``get_drawer`` call.

    Returns a dict with:
        ``text``            combined chunks in chunk_index order
        ``drawer_index``    the matched chunk's index in the source file
        ``total_drawers``   total drawer count for the source file (or None)

    On any ChromaDB failure or missing metadata, falls back to returning the
    matched drawer alone so search never breaks because neighbor expansion
    failed.
    """
    src = matched_meta.get("source_file")
    chunk_idx = matched_meta.get("chunk_index")
    if not src or not isinstance(chunk_idx, int):
        return {"text": matched_doc, "drawer_index": chunk_idx, "total_drawers": None}

    # Narrow by ``parent_drawer_id`` when present so chunks from unrelated
    # logical drawers sharing ``source_file`` do not stitch (#1580). See
    # ``_scoped_source_filter`` for the contract.
    parent_id = matched_meta.get("parent_drawer_id")
    target_indexes = [chunk_idx + offset for offset in range(-radius, radius + 1)]
    neighbor_clauses = [
        {"source_file": src},
        {"chunk_index": {"$in": target_indexes}},
    ]
    if parent_id:
        neighbor_clauses.append({"parent_drawer_id": parent_id})
    try:
        neighbors = drawers_col.get(
            where={"$and": neighbor_clauses},
            include=["documents", "metadatas"],
        )
    except Exception:
        return {"text": matched_doc, "drawer_index": chunk_idx, "total_drawers": None}

    indexed_docs = []
    for doc, meta in zip(neighbors.documents, neighbors.metadatas):
        ci = meta.get("chunk_index")
        if isinstance(ci, int):
            indexed_docs.append((ci, doc))
    indexed_docs.sort(key=lambda pair: pair[0])

    if not indexed_docs:
        combined_text = matched_doc
    else:
        combined_text = "\n\n".join(doc for _, doc in indexed_docs)

    # Cheap total_drawers lookup. When ``parent_drawer_id`` is present the
    # count is scoped to that group so the returned number matches the
    # text the caller gets back. Without a parent id, the legacy
    # file-global count is preserved.
    total_drawers = None
    try:
        all_meta = drawers_col.get(
            where=_scoped_source_filter(src, parent_id),
            include=["metadatas"],
        )
        total_drawers = len(all_meta.ids) if all_meta.ids else None
    except Exception:
        logger.debug("total_drawers lookup failed for %s", src, exc_info=True)

    return {
        "text": combined_text,
        "drawer_index": chunk_idx,
        "total_drawers": total_drawers,
    }


def _warn_if_legacy_metric(col) -> None:
    """Print a one-line notice if the palace was created without
    ``hnsw:space=cosine``.

    ChromaDB's default is L2 (Euclidean), under which cosine-based
    similarity interpretation falls apart — distances routinely exceed
    1.0 and the display ``max(0, 1 - dist)`` floors every result to 0.
    Legacy palaces (mined before this metadata was consistently set)
    need ``mempalace repair`` to rebuild with the correct metric.

    The warning fires only for palaces that clearly have the wrong
    metric; palaces with no metadata table at all (empty dict) also
    fall under this check since that is the signal of a pre-metadata
    palace.
    """
    try:
        meta = getattr(col, "metadata", None)
    except Exception:
        return
    if not isinstance(meta, dict):
        return
    space = meta.get("hnsw:space")
    if space == "cosine":
        return
    # Either missing or set to something else — both are suspect.
    import sys as _sys

    detail = f"hnsw:space={space!r}" if space else "no hnsw:space metadata"
    print(
        f"\n  NOTICE: this palace was created without cosine distance ({detail}).\n"
        "          Semantic similarity scores will not be meaningful.\n"
        "          Run `mempalace repair` to rebuild the index with the correct metric.",
        file=_sys.stderr,
    )


def _hnsw_capacity_diverged(palace_path: str) -> bool:
    """Return True if HNSW divergence is severe enough to crash ChromaDB.

    Thin, exception-safe wrapper around
    :func:`mempalace.backends.chroma.hnsw_capacity_status`. Used by the
    CLI search path to short-circuit to the BM25-only fallback before
    opening a Chroma client. Client construction and collection identity
    checks can themselves touch the damaged index, so guarding only
    ``col.query()`` is too late (#1222 covers the MCP path via the module-level
    ``_vector_disabled`` flag; this covers the CLI path).

    A probe that raises falls through to ``False`` so the caller proceeds
    to the normal vector path — the underlying query then either succeeds
    (probe was a false negative) or raises its own diagnostic error. The
    probe itself must never be the thing that crashes search.
    """
    try:
        from .backends.chroma import hnsw_capacity_status
        from .config import get_configured_collection_name

        info = hnsw_capacity_status(palace_path, get_configured_collection_name())
        return bool(info.get("diverged"))
    except Exception:
        logger.debug("HNSW capacity probe raised; proceeding to vector path", exc_info=True)
        return False


def _print_search_results_bm25_only(
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    n_results: int,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> None:
    """CLI fallback printer for when HNSW divergence fences off vector search.

    Mirrors the vector-path output shape so users get lexical matches in
    the format they expect, plus a clear notice pointing at
    ``mempalace repair``. Replaces the silent SIGBUS users otherwise hit
    when the CLI called ``col.query()`` against a diverged segment.

    ``stop_words`` reaches the BM25 scorer here for the same reason
    :func:`_vector_disabled_search` forwards it on the MCP side: this path
    still ranks by BM25, so dropping the filter would rank a diverged
    palace by different rules than a healthy one.

    An active ``[since_dt, before_dt)`` window is forwarded to the BM25
    reader, which post-filters on it. A diverged index degrades the
    ranking; it must never widen the result set past the window the
    caller asked for.
    """
    result = _bm25_only_via_sqlite(
        query=query,
        palace_path=palace_path,
        wing=wing,
        room=room,
        n_results=n_results,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
    )
    hits = result.get("results", [])

    print(
        "\n  NOTICE: vector search disabled — HNSW index has diverged from SQLite.\n"
        "          Showing BM25-only results. Run `mempalace repair` to restore "
        "vector search.\n"
    )
    print(f"{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    print(f"{'=' * 60}\n")

    if not hits:
        print(f'  No results found for: "{query}"')
        return

    for i, hit in enumerate(hits, 1):
        bm25 = hit.get("bm25_score", 0.0)
        wing_name = hit.get("wing", "?")
        room_name = hit.get("room", "?")
        source = Path(hit.get("source_file", "?")).name

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Source: {source}")
        print(f"      Match:  bm25={bm25}  (vector disabled)")
        print()
        for line in (hit.get("text", "") or "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()


def search(  # noqa: C901 — fork delegation + upstream window fence on one CLI entry
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    tags: list = None,
    n_results: int = 5,
    since: str = None,
    before: str = None,
    collection=None,
):
    """
    Search the palace. Returns verbatim drawer content.
    Optionally filter by wing (project) or room (aspect), and/or narrow to
    drawers whose ``filed_at`` falls in the ``[since, before)`` window —
    same semantics as ``search_memories``/``list_drawers`` (#1128/#463).
    Optionally filter by wing (project) or room (aspect).

    Delegates to ``search_memories`` so CLI and MCP callers share the same
    hybrid ranking, sqlite-BM25 fallback, and scope-aware warnings.
    """
    # Filesystem-first checks distinguish State A / State B before reaching
    # chromadb (upstream #1498). PersistentClient lazily creates
    # chroma.sqlite3 on first open of an empty palace dir, so without these
    # checks State B collapses into the "initialized but empty" State C
    # message and mutates the dir as a side effect of a read-only search.
    # Fork then delegates to ``search_memories`` so CLI and MCP callers share
    # the same hybrid-rerank + BM25-sqlite fallback + legacy-metric warnings.
    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        raise SearchError(f"No palace found at {palace_path}")
    if not os.path.isfile(os.path.join(palace_path, "chroma.sqlite3")):
        print(f"\n  Palace dir at {palace_path} exists but has no chroma.sqlite3 yet.")
        print("  Run: mempalace mine <dir>")
        raise SearchError(f"No palace database at {palace_path}")
    # Parse the window before probing the palace: an inverted or malformed
    # bound is a caller error and must raise identically whether or not the
    # index turns out to be diverged (upstream #463). The parsed bounds are
    # needed by the BM25 fence below; search_memories re-parses the raw
    # strings on the delegated path.
    try:
        since_dt, before_dt = parse_window(since, before)
    except ValueError as e:
        print(f"\n  {e}")
        raise SearchError(str(e)) from e

    # Upstream CLI safety fence: a diverged HNSW segment can segfault
    # ChromaDB's native bindings during client construction, so probe BEFORE
    # any get_collection call and route to the BM25-only sqlite fallback.
    # Backend-resolution errors delegate to _open_collection_or_explain's
    # state-specific diagnostics instead of the fence.
    # Upstream v3.9 lets a caller hand in an already-open ``collection`` (a
    # hub's warm instance); the fork delegates retrieval to search_memories,
    # so the handed-in collection only means the palace is provably open and
    # every pre-open probe below can be skipped.
    stop_words = _resolve_stop_words(None)
    if collection is None:
        try:
            backend_name = resolve_backend_name(palace_path)
        except (BackendMismatchError, KeyError):
            col = _open_collection_or_explain(palace_path, opener=get_collection)
            if col is None:
                raise SearchError(f"No palace found at {palace_path}")
            backend_name = None
        if backend_name == "chroma" and _hnsw_capacity_diverged(palace_path):
            return _print_search_results_bm25_only(
                query,
                palace_path,
                wing,
                room,
                n_results,
                stop_words=stop_words,
                since_dt=since_dt,
                before_dt=before_dt,
            )
        try:
            # Probe-only call — distinguishes State C (initialized but empty) from
            # State D (corrupt) before search_memories blurs them into a generic
            # "No palace found".
            get_collection(palace_path, create=False)
        except CollectionNotInitializedError as e:
            print(f"\n  Palace at {palace_path} is initialized but empty (no drawers yet).")
            print("  Run: mempalace mine <dir>")
            raise SearchError(f"Palace at {palace_path} is initialized but empty") from e
        except PalaceNotFoundError as e:
            # Backend filesystem-race fallback: dir was deleted between our
            # check above and the backend call. Same message as State A.
            print(f"\n  No palace found at {palace_path}")
            print("  Run: mempalace init <dir> then mempalace mine <dir>")
            raise SearchError(f"No palace found at {palace_path}") from e

    try:
        metric = _metric_for_collection(get_collection(palace_path, create=False))
    except Exception:
        metric = "cosine"
    result = search_memories(
        query,
        palace_path,
        wing=wing,
        room=room,
        tags=tags,
        n_results=n_results,
        since=since,
        before=before,
    )
    if "error" in result and not result.get("results"):
        # Preserve the palace path in the printed error so the user sees
        # which palace the search tried to open (a common source of
        # confusion when more than one palace is in play). The structured
        # error payload from search_memories is intentionally path-agnostic.
        error_message = result["error"]
        if error_message == "No palace found":
            error_message = f"{error_message} at {palace_path}"
        print(f"\n  {error_message}")
        if "hint" in result:
            print(f"  {result['hint']}")
        raise SearchError(error_message)

    warnings = result.get("warnings") or []
    hits = result.get("results") or []

    if not hits:
        print(f'\n  No results found for: "{query}"')
        for w in warnings:
            print(f"  ! {w}")
        return

    # Hits are already built and hybrid-reranked by search_memories(); the
    # delegate path centralizes retrieval, BM25-sqlite fallback, and
    # legacy-metric warning so CLI and MCP callers share a single source
    # of truth. (Upstream's #1179 added an inline rebuild + _warn_if_legacy_metric
    # call here; the fork keeps the warning live by calling it from
    # search_memories instead — see the wired call below.)

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    if result.get("available_in_scope") is not None:
        print(f"  Scope has: {result['available_in_scope']} drawers matching filter")
    if warnings:
        for w in warnings:
            print(f"  ! {w}")
    if since:
        print(f"  Since: {since}")
    if before:
        print(f"  Before: {before}")
    print(f"{'=' * 60}\n")

    for i, hit in enumerate(hits, 1):
        wing_name = hit.get("wing", "?")
        room_name = hit.get("room", "?")
        source = hit.get("source_file", "?")
        similarity = hit.get("similarity")
        bm25 = hit.get("bm25_score")
        matched_via = hit.get("matched_via", "drawer")

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Source: {source}")
        if similarity is not None and bm25 is not None:
            print(f"      Match:  {metric}_sim={similarity}  bm25={bm25}")
        elif similarity is not None:
            print(f"      Match:  {similarity}")
        elif bm25 is not None:
            print(f"      BM25:   {bm25}  (matched_via: {matched_via})")
        else:
            print(f"      (matched_via: {matched_via})")
        print()
        for line in (hit.get("text") or "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()


def _window_sql_prefilters(since_dt, before_dt) -> list:
    """(operator, bound-string) pairs for the SQL date-window narrowing.

    A SQL-side *narrowing* on the ISO ``filed_at`` string, kept at
    whole-DAY granularity so it is provably wider than the window for
    every ISO-8601 spelling that shares the YYYY-MM-DD prefix (bare date,
    space separator, minute precision, Z/offset suffixes) — a
    full-isoformat bound would sort after some of those on the boundary
    day and drop an in-window row at the SQL layer, where the
    authoritative Python re-filter (offset drop, unparseable exclusion —
    mirroring the wing/room double-check) can't recover it. Day
    granularity costs at most one extra day of candidates per bound;
    Python decides the exact window.
    """
    prefilters = []
    if since_dt is not None:
        prefilters.append((">=", since_dt.date().isoformat()))
    if before_dt is not None:
        try:
            upper = (before_dt + timedelta(days=1)).date().isoformat()
        except OverflowError:
            # before at the calendar ceiling ("9999-12-31" as an open-ended
            # sentinel): there is no next day to bound by, so skip the SQL
            # narrowing entirely — the Python re-filter stays authoritative
            # and such a window is effectively unbounded above anyway.
            upper = None
        if upper is not None:
            prefilters.append(("<", upper))
    return prefilters


def _count_in_scope(drawers_col, where: dict) -> Optional[int]:
    """Return the total number of drawers matching ``where``.

    When ``where`` is empty (unfiltered scope), uses ``Collection.count()``
    which is O(1). Otherwise paginates ``get(include=[])`` — ChromaDB's
    ``count()`` does not accept a ``where`` filter. Pagination keeps each
    query well under the #950 "too many SQL variables" limit.

    Returns ``None`` if the count could not be computed (e.g., filter
    planner error).
    """
    try:
        if not where:
            raw = drawers_col.count()
            return int(raw) if isinstance(raw, (int, float)) else None
        PAGE = 5000
        offset = 0
        total = 0
        while True:
            batch = drawers_col.get(limit=PAGE, offset=offset, include=[], where=where)
            batch_ids = batch.get("ids") or []
            if not batch_ids:
                break
            total += len(batch_ids)
            if len(batch_ids) < PAGE:
                break
            offset += len(batch_ids)
    except Exception:
        return None
    return total


def _sqlite_fallback_and_scope(
    drawers_col,
    query: str,
    where: dict,
    hits: list,
    n_results: int,
    vector_underdelivered: bool,
    allow_fallback: bool,
    since_dt=None,
    before_dt=None,
) -> tuple:
    """Compute the sqlite-authoritative in-scope count and, if enabled, top
    up the hits list with BM25-ranked sqlite candidates when the vector
    path returned fewer than ``n_results``.

    ``vector_underdelivered`` is independent from ``len(hits) < n_results``
    after this function mutates ``hits``, so callers can gate the "more in
    scope than we could rank" warning on whether the *vector path* was the
    degraded layer, rather than on the final hit count after BM25 top-up.

    Returns ``(available_in_scope, warnings)``. Mutates ``hits`` in place
    when it adds fallback entries.
    """
    warnings: list[str] = []

    # Sqlite-authoritative scope count (paginated, independent of the pool
    # we read for BM25 ranking). None on failure — caller treats that as
    # "unknown" rather than crashing.
    available_in_scope = _count_in_scope(drawers_col, where)

    if not allow_fallback or not vector_underdelivered:
        return available_in_scope, warnings

    shortfall = n_results - len(hits)
    if shortfall <= 0:
        return available_in_scope, warnings

    # Fetch a bounded BM25 candidate pool. Cap keeps #950 at bay and a
    # pool 20x the request is plenty for keyword-rank top-up.
    try:
        pool_kwargs: dict = {"include": ["documents", "metadatas"]}
        if where:
            pool_kwargs["where"] = where
        pool_kwargs["limit"] = max(n_results * 20, 100)
        pool = drawers_col.get(**pool_kwargs)
    except Exception as e:
        warnings.append(f"sqlite fallback unavailable: {e}")
        return available_in_scope, warnings

    pool_ids = pool.get("ids") or []
    pool_docs = pool.get("documents") or []
    pool_metas = pool.get("metadatas") or []
    if not pool_docs:
        return available_in_scope, warnings
    # Pad ids when fixtures omit them (see vector path above).
    if not pool_ids:
        pool_ids = [None] * len(pool_docs)

    seen_texts = {h.get("text") for h in hits if h.get("text")}
    candidate_ids: list = []
    candidate_docs: list = []
    candidate_metas: list = []
    window_active = since_dt is not None or before_dt is not None
    for i, d, m in zip(pool_ids, pool_docs, pool_metas):
        if d in seen_texts:
            continue
        # The date window applies to every candidate source (upstream #463):
        # a keyword-strong drawer outside [since, before) must not enter
        # through the fallback top-up when the vector pool was windowed.
        if window_active and not filed_at_in_window((m or {}).get("filed_at"), since_dt, before_dt):
            continue
        candidate_ids.append(i)
        candidate_docs.append(d)
        candidate_metas.append(m or {})

    if not candidate_docs:
        return available_in_scope, warnings

    bm25 = _bm25_scores(query, candidate_docs)
    ranked = sorted(
        zip(candidate_ids, candidate_docs, candidate_metas, bm25),
        key=lambda t: t[3],
        reverse=True,
    )
    added = 0
    for drawer_id, doc, meta, score in ranked:
        if added >= shortfall:
            break
        if score <= 0.0:
            # No query term present — skip rather than pad with arbitrary
            # content, so the warning stays accurate.
            break
        src = meta.get("source_file", "") or ""
        hits.append(
            {
                "drawer_id": drawer_id,
                "text": doc,
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "topic": meta.get("topic"),
                "source_file": Path(src).name if src else "?",
                "source_path": src,
                "created_at": meta.get("filed_at", "unknown"),
                "authored_at": meta.get("authored_at", meta.get("filed_at", "unknown")),
                "similarity": None,
                "distance": None,
                "bm25_score": round(score, 3),
                "matched_via": "sqlite_bm25_fallback",
            }
        )
        added += 1
    if added > 0:
        vector_count = len(hits) - added
        warnings.append(
            f"vector search returned {vector_count} of {n_results} "
            f"requested; filled {added} from sqlite+BM25 keyword match"
        )
    return available_in_scope, warnings


def _bm25_only_via_sqlite(  # noqa: C901 — fork tag/scope filters atop upstream window
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    tags: list = None,
    source_file: str = None,
    n_results: int = 5,
    max_candidates: int = 500,
    _include_internal: bool = False,
    collection_name: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> dict:
    """BM25-only search reading drawers directly from chroma.sqlite3.

    Used when HNSW is diverged or unloadable (#1222). Bypasses chromadb's
    Python client entirely so a corrupt vector segment can't segfault the
    MCP server. Routes through chromadb's own FTS5 trigram index
    (``embedding_fulltext_search``) for candidate selection, then re-ranks
    with the same Okapi-BM25 used in :func:`_hybrid_rank` so the result
    shape matches the vector path.

    The query is split into ≥3-char trigram-tokens and OR-joined for the
    FTS5 MATCH — chromadb writes the index with ``tokenize='trigram'``,
    so single-character tokens never match. When no usable token survives
    (e.g. "is a"), candidate selection falls back to the most-recent
    ``max_candidates`` rows so we still return *something* rather than
    nothing.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return _search_error_result(
            "No palace found",
            hint="Run: mempalace init <dir> && mempalace mine <dir>",
        )
    if collection_name is None:
        from .config import get_configured_collection_name

        collection_name = get_configured_collection_name()

    def _metadata_filter_sql(row_id_expr: str) -> tuple[str, list[str]]:
        clauses = []
        params = []
        for key, value in (("wing", wing), ("room", room), ("source_file", source_file)):
            if not value:
                continue
            clauses.append(
                f"""
                AND EXISTS (
                    SELECT 1
                    FROM embedding_metadata mf
                    WHERE mf.id = {row_id_expr}
                      AND mf.key = ?
                      AND COALESCE(
                        mf.string_value,
                        CAST(mf.int_value AS TEXT),
                        CAST(mf.float_value AS TEXT),
                        CAST(mf.bool_value AS TEXT)
                      ) = ?
                )
                """
            )
            params.extend([key, value])
        for op, sql_bound in _window_sql_prefilters(since_dt, before_dt):
            clauses.append(
                f"""
                AND EXISTS (
                    SELECT 1
                    FROM embedding_metadata mf
                    WHERE mf.id = {row_id_expr}
                      AND mf.key = 'filed_at'
                      AND mf.string_value {op} ?
                )
                """
            )
            params.append(sql_bound)
        return "".join(clauses), params

    try:
        conn = sqlite3.connect(sqlite_read_uri(db_path), uri=True)
    except sqlite3.Error as e:
        return _search_error_result(f"sqlite open failed: {e}")

    window_active = since_dt is not None or before_dt is not None
    try:
        # FTS5 MATCH expects whitespace-separated tokens. Drop tokens
        # shorter than 3 chars (trigram tokenizer can't match them).
        tokens = [t for t in _tokenize(query) if len(t) >= 3]
        candidate_ids: list[int] = []
        use_recency_fallback = not tokens
        if tokens:
            fts_query = " OR ".join(tokens)
            filter_sql, filter_params = _metadata_filter_sql("embedding_fulltext_search.rowid")
            try:
                rows = conn.execute(
                    f"""
                    SELECT embedding_fulltext_search.rowid
                    FROM embedding_fulltext_search
                    JOIN embeddings e ON e.id = embedding_fulltext_search.rowid
                    JOIN segments s ON e.segment_id = s.id
                    JOIN collections c ON s.collection = c.id
                    WHERE embedding_fulltext_search MATCH ?
                      AND c.name = ?
                    {filter_sql}
                    LIMIT ?
                    """,
                    (fts_query, collection_name, *filter_params, max_candidates),
                ).fetchall()
                candidate_ids = [r[0] for r in rows]
            except sqlite3.Error:
                # FTS5 tokenizer mismatch or syntax error — fall through
                # to the recency-window selector below.
                logger.debug("FTS5 MATCH failed; using recency fallback", exc_info=True)
                use_recency_fallback = True

        if not candidate_ids and use_recency_fallback:
            # No usable FTS tokens, or FTS itself failed — pull the most
            # recent rows for the drawers segment so we can BM25-rank
            # something rather than return empty-handed. A clean FTS miss
            # must stay empty, especially after wing/room filtering, because
            # recency fallback would return unrelated scoped drawers.
            # Wrapped in try/except because the schema may differ on legacy
            # palaces (older chromadb without ``created_at``, missing
            # ``segments`` rows after partial restore, etc.); on schema
            # mismatch we fall back to ordering by primary-key id and finally
            # to an empty result rather than letting search raise.
            try:
                filter_sql, filter_params = _metadata_filter_sql("e.id")
                rows = conn.execute(
                    f"""
                    SELECT e.id
                    FROM embeddings e
                    JOIN segments s ON e.segment_id = s.id
                    JOIN collections c ON s.collection = c.id
                    WHERE c.name = ?
                    {filter_sql}
                    ORDER BY e.created_at DESC
                    LIMIT ?
                    """,
                    (collection_name, *filter_params, max_candidates),
                ).fetchall()
                candidate_ids = [r[0] for r in rows]
            except sqlite3.Error:
                logger.debug(
                    "recency-window query failed; trying id-ordered fallback",
                    exc_info=True,
                )
                try:
                    filter_sql, filter_params = _metadata_filter_sql("e.id")
                    rows = conn.execute(
                        f"""
                        SELECT e.id
                        FROM embeddings e
                        JOIN segments s ON e.segment_id = s.id
                        JOIN collections c ON s.collection = c.id
                        WHERE c.name = ?
                        {filter_sql}
                        ORDER BY e.id DESC
                        LIMIT ?
                        """,
                        (collection_name, *filter_params, max_candidates),
                    ).fetchall()
                    candidate_ids = [r[0] for r in rows]
                except sqlite3.Error:
                    logger.debug("id-ordered fallback also failed", exc_info=True)
                    candidate_ids = []

        # A full candidate page means rows beyond it never got a chance to
        # match the window — mirror the vector path's truncation honesty
        # (``date_filter_pool_truncated``) instead of a silently thin result.
        window_pool_truncated = window_active and len(candidate_ids) >= max_candidates

        if not candidate_ids:
            return {
                "query": query,
                "filters": {
                    "wing": wing,
                    "room": room,
                    "tags": list(tags) if tags else None,
                    "source_file": source_file,
                },
                "total_before_filter": 0,
                "results": [],
                "fallback": "bm25_only_via_sqlite",
            }

        placeholders = ",".join(["?"] * len(candidate_ids))
        meta_rows = conn.execute(
            f"""
            SELECT m.id, e.embedding_id, m.key, m.string_value, m.int_value
            FROM embedding_metadata AS m
            JOIN embeddings AS e ON e.id = m.id
            WHERE m.id IN ({placeholders})
            """,
            candidate_ids,
        ).fetchall()
    finally:
        conn.close()

    # Group metadata rows into per-drawer dicts.
    drawers: dict[int, dict] = {}
    for emb_id, stored_drawer_id, key, sval, ival in meta_rows:
        d = drawers.setdefault(
            emb_id,
            {
                "_id": emb_id,
                "_stored_drawer_id": stored_drawer_id,
                "metadata": {},
                "text": "",
            },
        )
        if key == "chroma:document":
            d["text"] = sval or ""
        else:
            d["metadata"][key] = sval if sval is not None else ival

    # Apply wing/room filters in Python (FTS5 candidates may include
    # entries from other wings).
    from .tags import metadata_matches_all_tags, normalise_tags

    normalised_tags = normalise_tags(tags) if tags else []
    candidates = []
    for d in drawers.values():
        meta = d["metadata"]
        if wing and meta.get("wing") != wing:
            continue
        if room and meta.get("room") != room:
            continue
        if normalised_tags and not metadata_matches_all_tags(meta, normalised_tags):
            continue
        if source_file and meta.get("source_file") != source_file:
            continue
        if window_active and not filed_at_in_window(meta.get("filed_at"), since_dt, before_dt):
            continue
        full_source = meta.get("source_file", "") or ""
        candidates.append(
            {
                "drawer_id": _result_drawer_id(meta, d["_stored_drawer_id"]),
                "text": d["text"],
                "wing": meta.get("wing", "unknown"),
                "room": meta.get("room", "unknown"),
                "source_file": Path(full_source).name if full_source else "?",
                "source_path": full_source,
                "created_at": meta.get("filed_at", "unknown"),
                "authored_at": meta.get("authored_at", meta.get("filed_at", "unknown")),
                # No vector distance available in BM25-only mode.
                "similarity": None,
                "distance": None,
                "matched_via": "bm25_sqlite",
                # Internal: full path + chunk_index let callers (notably
                # candidate_strategy="union") dedupe at chunk granularity
                # rather than basename — two files in different directories
                # may share a basename, and one source_file is split across
                # multiple chunks. Stripped before this helper returns.
                "_source_file_full": full_source,
                "_chunk_index": meta.get("chunk_index"),
            }
        )

    # Local BM25 over the candidate set.
    docs = [c["text"] for c in candidates]
    bm25_raw = _bm25_scores(query, docs, stop_words=stop_words)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    for c, raw in zip(candidates, bm25_raw):
        c["bm25_score"] = round(raw, 3)
        c["_score"] = (raw / max_bm25) if max_bm25 > 0 else 0.0
    candidates.sort(key=lambda c: c["_score"], reverse=True)
    hits = candidates[:n_results]
    for h in hits:
        h.pop("_score", None)
        # Strip internal fields by default so the public BM25-only fallback
        # response stays clean. Callers that need chunk-precise dedup
        # (notably the union-merge path) opt in via _include_internal.
        if not _include_internal:
            h.pop("_source_file_full", None)
            h.pop("_chunk_index", None)

    result = {
        "query": query,
        "filters": {
            "wing": wing,
            "room": room,
            "tags": normalised_tags or None,
            "source_file": source_file,
        },
        "total_before_filter": len(candidates),
        "results": _annotate_provenance(hits),
        "fallback": "bm25_only_via_sqlite",
        "fallback_reason": "vector_search_disabled",
    }
    if window_pool_truncated:
        result["date_filter_pool_truncated"] = True
    return result


def _bm25_only_via_postgres(
    query: str,
    dsn: str,
    wing: str = None,
    room: str = None,
    n_results: int = 5,
    table_name: str = "mempalace_drawers",
    _include_internal: bool = False,
) -> dict:
    """BM25-only search reading drawers directly from postgres tsvector.

    Postgres backend equivalent of ``_bm25_only_via_sqlite``. Uses
    ``plainto_tsquery`` for parsing (AND-of-tokens semantics — what
    users actually want for keyword search over code identifiers) and
    ``ts_rank_cd`` for cover-density ranking.

    Why not ``websearch_to_tsquery``: it interprets identifiers with
    underscores (e.g. ``ts_rank_cd``, ``websearch_to_tsquery``) as
    PHRASE queries (``'ts' <-> 'rank' <-> 'cd'``). When the same
    identifier appears in a drawer, the tokenizer may insert position
    gaps due to surrounding code punctuation, so the phrase doesn't
    match — even though all three tokens are present. ``plainto_tsquery``
    treats them as AND-of-tokens which is the right behavior for
    keyword identifier search. Trade-off: users lose web-search syntax
    (quoted phrases, ``-exclude``, ``OR``), but those weren't usable
    for code identifiers anyway.

    The ``doc_tsv`` generated column auto-populates from ``document``
    (truncated to 100KB to stay under tsvector's 1MB limit); the GIN
    index makes lookup ~microseconds even at 100k+ rows.

    Result shape matches ``_bm25_only_via_sqlite``: each hit has text,
    wing, room, source_file, distance (None for BM25-only), bm25_score,
    matched_via="bm25_postgres". When ``_include_internal=True``, the
    ``_source_file_full`` + ``_chunk_index`` keys survive so the
    candidate-union dedup logic can match the upstream pattern.
    """
    try:
        import psycopg as psycopg2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "BM25 postgres search requires the psycopg driver. "
            'Install with: pip install "mempalace[postgres]"'
        ) from exc

    # Compose WHERE — wing/room optional. plainto_tsquery handles
    # empty / nonsense queries by returning empty tsquery; we treat that
    # as no-match (return empty results) rather than scanning the table.
    conditions = ["doc_tsv @@ q"]
    params: list = [query]
    if wing:
        conditions.append("wing = %s")
        params.append(wing)
    if room:
        conditions.append("room = %s")
        params.append(room)
    where = " AND ".join(conditions)

    # Identifier-aware boost: when the query contains tokens with
    # underscores (e.g. ts_rank_cd, websearch_to_tsquery), postgres's
    # tsvector parser splits on `_` so the search devolves into
    # AND-of-token-stems — surfacing scattered-token matches alongside
    # the literal. Union an ILIKE substring search on the identifier
    # tokens so genuine literal matches always rise to the top.
    import re as _re

    ident_tokens = [
        t for t in _re.findall(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+", query) if len(t) >= 5
    ]

    if ident_tokens:
        # Build an OR of ILIKEs over the identifier tokens. The literal-
        # substring match is unioned with the tsvector match; a CASE
        # boost in ORDER BY floats genuine literal matches to the top.
        ilike_clauses = " OR ".join(["document ILIKE %s"] * len(ident_tokens))
        wing_room_filters = []
        wing_room_params = []
        if wing:
            wing_room_filters.append("wing = %s")
            wing_room_params.append(wing)
        if room:
            wing_room_filters.append("room = %s")
            wing_room_params.append(room)
        wing_room_clause = (" AND " + " AND ".join(wing_room_filters)) if wing_room_filters else ""

        sql = f"""
            SELECT id, wing, room, document, metadata,
                   ts_rank_cd(doc_tsv, q) +
                   CASE WHEN ({ilike_clauses}) THEN 10.0 ELSE 0.0 END AS rank
            FROM {table_name}, plainto_tsquery('english', %s) q
            WHERE (doc_tsv @@ q OR ({ilike_clauses}))
                  {wing_room_clause}
            ORDER BY rank DESC
            LIMIT %s
        """
        # Param order: CASE ILIKEs, plainto query, WHERE ILIKEs, wing, room, limit
        like_patterns = [f"%{t}%" for t in ident_tokens]
        sql_params = (
            like_patterns  # CASE WHEN clauses
            + [query]  # plainto_tsquery
            + like_patterns  # OR-fallback in WHERE
            + wing_room_params
            + [n_results]
        )
    else:
        sql = f"""
            SELECT id, wing, room, document, metadata,
                   ts_rank_cd(doc_tsv, q) AS rank
            FROM {table_name}, plainto_tsquery('english', %s) q
            WHERE {where}
            ORDER BY rank DESC
            LIMIT %s
        """
        sql_params = [query] + params[1:] + [n_results]

    results = []
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, sql_params)
                rows = cur.fetchall()
    except Exception:
        logger.debug("_bm25_only_via_postgres failed", exc_info=True)
        return {
            "query": query,
            "filters": {"wing": wing, "room": room},
            "total_before_filter": 0,
            "results": [],
            "fallback": "bm25_only_via_postgres",
            "error": "query failed; see daemon log",
        }

    for row in rows:
        drawer_id, drawer_wing, drawer_room, document, metadata, rank = row
        # Metadata may be a dict (jsonb) or a JSON string; normalize.
        if isinstance(metadata, str):
            import json as _json

            try:
                metadata = _json.loads(metadata)
            except Exception:
                metadata = {}
        elif metadata is None:
            metadata = {}

        full_source = (metadata or {}).get("source_file", "") or ""
        entry = {
            "id": drawer_id,
            "text": document,
            "wing": drawer_wing,
            "room": drawer_room,
            "source_file": full_source.rsplit("/", 1)[-1] if full_source else "?",
            "created_at": (metadata or {}).get("added_at")
            or (metadata or {}).get("filed_at", "unknown"),
            # No vector distance available in BM25-only mode.
            "similarity": None,
            "distance": None,
            "bm25_score": round(float(rank), 4),
            "matched_via": "bm25_postgres",
            # Internal — needed for candidate-union dedup. Stripped below
            # when caller didn't request them.
            "_source_file_full": full_source,
            "_chunk_index": (metadata or {}).get("chunk_index"),
        }
        if not _include_internal:
            entry.pop("_source_file_full", None)
            entry.pop("_chunk_index", None)
        results.append(entry)

    return {
        "query": query,
        "filters": {"wing": wing, "room": room},
        "total_before_filter": len(results),
        "results": _annotate_provenance(results),
        "fallback": "bm25_only_via_postgres",
    }


def _graph_expand_from_seeds(
    seed_drawer_ids: list[str],
    dsn: str,
    max_entities: int = 10,
    max_drawers_per_entity: int = 10,
) -> list[str]:
    """Find drawers connected to seed drawers via the AGE knowledge graph.

    Phase 3 of the hybrid-search-taxonomy initiative — adds a third
    retrieval mode to the union strategy. Steps:

      1. For each seed drawer, find the Entity nodes whose RELATION edges
         have source=seed_drawer_id. Returns the set of entities those
         drawers "talk about".
      2. For each surfaced entity, find drawers that *other* RELATION
         edges name as source — i.e., other drawers about the same
         entities.

    Returns the deduped drawer-id list. Caller's responsibility to
    fetch full drawer content + merge into the candidate pool.

    Fan-out caps (max_entities, max_drawers_per_entity) keep the AGE
    query bounded. The Cypher inlines literals (no $1 parameters)
    because AGE's parameter binding requires PG-prepared statements
    that psycopg2's %s substitution doesn't produce — see
    knowledge_graph_age._run_cypher's docstring for the upstream issue.

    Returns empty list on any error — graph expansion is value-add,
    never blocks hybrid retrieval.
    """
    if not seed_drawer_ids:
        return []
    try:
        import psycopg as psycopg2
    except ImportError:
        return []

    # Inline-literal Cypher (safe — seed_drawer_ids come from internal
    # vector hits, not user input; if that changes, sanitize first)
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "\\'")

    seeds = [s for s in seed_drawer_ids if isinstance(s, str)][:20]
    if not seeds:
        return []
    seeds_clause = "[" + ", ".join(f"'{_esc(s)}'" for s in seeds) + "]"

    # Issue #291: AGE compiles bidirectional ``-[r:RELATION]-`` to an
    # OR-of-AND join filter that postgres can't hash — falls back to a
    # nested loop over (RELATION × all-vertex-labels), 200s+ on the
    # production graph (1.76M edges). Splitting into two directional
    # queries lets the planner use a Parallel Hash Join (~2s wall-clock,
    # ~100× speedup). We union the entity names because the RELATION
    # schema is (Entity)->(Entity); an entity can appear as subject OR
    # object of a relation extracted from a seed drawer — both are
    # valid hits and direction-collapsing would silently drop half of
    # them.
    #
    # mempalace#335: the open endpoint is bound to ``:Entity`` rather than
    # left anonymous (``()``). An anonymous node matches *any* vertex label,
    # so AGE builds a Parallel Append over every label table (Entity + Drawer
    # + Room + Wing + _ag_label_vertex, ~1.58M rows on prod) and nested-loops
    # to validate the endpoint exists — materializing the union and spilling
    # to /dev/shm. RELATION is always (Entity)->(Entity), so binding the open
    # end to ``:Entity`` is semantically identical (verified row-for-row on a
    # 300K-edge AGE graph) and collapses the Append to a single Entity scan.
    entity_cypher_outbound = f"""
        MATCH (e:Entity)-[r:RELATION]->(:Entity)
        WHERE r.source IN {seeds_clause}
        RETURN DISTINCT e.name AS name
        LIMIT {max_entities}
    """
    entity_cypher_inbound = f"""
        MATCH (:Entity)-[r:RELATION]->(e:Entity)
        WHERE r.source IN {seeds_clause}
        RETURN DISTINCT e.name AS name
        LIMIT {max_entities}
    """

    expanded_drawers: set = set()
    try:
        with psycopg2.connect(dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute('SET search_path = ag_catalog, "$user", public')
                # Find entities mentioned in seed drawers — subjects + objects
                entities_set: set = set()
                for cyp in (entity_cypher_outbound, entity_cypher_inbound):
                    cur.execute(f"SELECT * FROM cypher('mempalace_kg', $${cyp}$$) AS (name agtype)")
                    for (name_agtype,) in cur.fetchall():
                        # agtype renders strings as '"..."'; strip quotes
                        raw = str(name_agtype)
                        if raw.startswith('"') and raw.endswith('"'):
                            raw = raw[1:-1]
                        entities_set.add(raw)
                entities = list(entities_set)[:max_entities]

                # For each entity, find drawers mentioning it (via their
                # RELATION source ids). Direction-safe per #291: r.source
                # is the drawer where the relation was extracted, identical
                # for either endpoint of the edge — drawer attribution
                # doesn't depend on whether this entity is the subject or
                # the object.
                for ent in entities:
                    ent_safe = _esc(ent)
                    expand_cypher = f"""
                        MATCH (a:Entity {{name: '{ent_safe}'}})-[r:RELATION]->(:Entity)
                        RETURN DISTINCT r.source AS source
                        LIMIT {max_drawers_per_entity}
                    """
                    cur.execute(
                        f"SELECT * FROM cypher('mempalace_kg', $${expand_cypher}$$) AS (source agtype)"
                    )
                    for (source_agtype,) in cur.fetchall():
                        raw = str(source_agtype)
                        if raw.startswith('"') and raw.endswith('"'):
                            raw = raw[1:-1]
                        if raw and raw != "null" and raw not in seeds:
                            expanded_drawers.add(raw)
    except Exception:
        logger.debug("_graph_expand_from_seeds failed", exc_info=True)
        return []

    return list(expanded_drawers)


def _graph_expand_from_entities(
    entity_names: list[str],
    dsn: str,
    max_drawers_per_entity: int = 10,
) -> list[str]:
    """Find drawers mentioning the given entities, via AGE.

    Companion to _graph_expand_from_seeds. Used when callers have entity
    names directly (from query NER or a static project-name catalog)
    rather than seed drawer ids. Returns deduped drawer-id list.
    """
    if not entity_names:
        return []
    try:
        import psycopg as psycopg2
    except ImportError:
        return []

    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "\\'")

    expanded_drawers: set = set()
    try:
        with psycopg2.connect(dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute('SET search_path = ag_catalog, "$user", public')
                for ent in entity_names[:10]:
                    if not ent or len(ent) < 3:
                        continue
                    if ent.lower() in _QUERY_NER_STOPWORDS:
                        continue
                    ent_safe = _esc(ent)
                    # Exact match only. The previous `a.name =~ '(?i).*X.*'`
                    # fallback caused production-scale (100K+ vertex) seq
                    # scans that wedged the daemon for tens of minutes per
                    # query. The fuzzy/case-insensitive variant is tracked
                    # for re-introduction via pg_trgm + functional index.
                    #
                    # Issue #291: directional ``->`` instead of bidirectional
                    # ``-`` lets AGE compile to a Parallel Hash Join rather
                    # than a nested loop on an un-hashable OR predicate
                    # (~100× faster on the 1.76M-edge production graph).
                    # Safe for this projection — ``r.source`` is the drawer
                    # where the edge was extracted and doesn't depend on
                    # which endpoint binds to ``a``.
                    #
                    # mempalace#335: the open endpoint is bound to ``:Entity``
                    # (not anonymous ``()``) so AGE scans only the Entity label
                    # table instead of a Parallel Append over every vertex
                    # label. RELATION is always (Entity)->(Entity); verified
                    # row-equivalent. See _graph_expand_from_seeds for the full
                    # rationale.
                    expand_cypher = f"""
                        MATCH (a:Entity)-[r:RELATION]->(:Entity)
                        WHERE a.name = '{ent_safe}'
                        RETURN DISTINCT r.source AS source
                        LIMIT {max_drawers_per_entity}
                    """
                    # Defensive: 3s per-Cypher cap so a misfire can't wedge
                    # the daemon. Wrap SET LOCAL + cypher() in an explicit
                    # transaction — with conn.autocommit=True, each
                    # standalone execute() runs in its own implicit txn,
                    # so a bare `SET LOCAL statement_timeout` ends
                    # immediately and never applies to the next execute().
                    # `conn.transaction()` opens a real BEGIN/COMMIT around
                    # both statements, so the LOCAL setting actually
                    # scopes the cypher call (verified empirically:
                    # docs/operators/2026-05-26-age-statement-timeout.sql).
                    try:
                        with conn.transaction():
                            cur.execute("SET LOCAL statement_timeout = '3s'")
                            cur.execute(
                                f"SELECT * FROM cypher('mempalace_kg', $${expand_cypher}$$) AS (source agtype)"
                            )
                            for (source_agtype,) in cur.fetchall():
                                raw = str(source_agtype)
                                if raw.startswith('"') and raw.endswith('"'):
                                    raw = raw[1:-1]
                                if raw and raw != "null":
                                    expanded_drawers.add(raw)
                    except Exception:
                        continue  # bad regex / missing entity / timeout — keep going
    except Exception:
        logger.debug("_graph_expand_from_entities failed", exc_info=True)
        return []

    return list(expanded_drawers)


_ENTITY_REGEX = re.compile(r"\b([A-Z][a-zA-Z0-9_]+(?:\s+[A-Z][a-zA-Z0-9_]+)*)\b")

# Common capitalized sentence-starters and interrogatives that match the
# NER regex but aren't real entities. Filtering them here prevents
# downstream AGE consumers (_graph_expand_from_entities) from issuing
# fuzzy-regex Cypher against a multi-million-vertex Entity table —
# scans like `a.name =~ '(?i).*What.*'` collapse to seq-scans and wedge
# the daemon's Postgres pool for tens of minutes.
_QUERY_NER_STOPWORDS = frozenset(
    {
        "what",
        "which",
        "where",
        "when",
        "why",
        "who",
        "whom",
        "whose",
        "how",
        "the",
        "this",
        "that",
        "these",
        "those",
        "there",
        "their",
        "they",
        "them",
        "and",
        "but",
        "for",
        "from",
        "are",
        "you",
        "your",
        "yours",
        "can",
        "could",
        "would",
        "should",
        "shall",
        "will",
        "may",
        "might",
        "must",
        "have",
        "has",
        "had",
        "does",
        "did",
        "any",
        "all",
        "some",
        "one",
        "two",
        "yes",
        "not",
        "now",
    }
)


def _ner_from_query(query, known_entities=None):
    # type: (str, Optional[set]) -> list
    # Py3.9 compat: avoid `set[str] | None` PEP 604 union syntax.
    """Cheap NER for hybrid retrieval — capitalized multi-word phrases
    plus matches against a known-entity set (e.g. project names from the
    catalog).

    Not a real NER model. Catches the common cases for our corpus —
    project names, person names, system names — without paying a model
    inference per query. The hybrid retrieval can survive missed
    entities (vector + BM25 cover that ground); the NER is purely
    additive signal.

    Returns up to 8 distinct entity candidates.
    """
    candidates: list[str] = []
    seen = set()
    for m in _ENTITY_REGEX.finditer(query):
        token = m.group(1).strip()
        if token in seen or len(token) < 3:
            continue
        # Filter on the leading word — _ENTITY_REGEX greedily captures
        # multi-word capitalized phrases, so "Which Drawer" arrives as a
        # single token. Rejecting the whole phrase when it starts with a
        # stopword is simpler than splitting it apart, and vector + BM25
        # still cover "Drawer" via their own paths.
        first_word = token.split(None, 1)[0].lower()
        if first_word in _QUERY_NER_STOPWORDS:
            continue
        candidates.append(token)
        seen.add(token)

    if known_entities:
        # Add lowercase substring matches against known entities (catches
        # "palace_daemon" in lowercased queries).
        q_lower = query.lower()
        for ent in known_entities:
            if ent and ent in q_lower and ent not in seen:
                candidates.append(ent)
                seen.add(ent)

    return candidates[:8]


def _merge_bm25_union_candidates(
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float = 0.0,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> None:
    """Append top-K backend lexical candidates into ``hits`` in place.

    Used by ``search_memories(..., candidate_strategy="union")`` to widen
    the rerank pool's *source* (not just its size) — vector-only candidate
    selection skips docs whose embeddings are far from the query even when
    BM25 signal is strong.

    Dedup is chunk-precise: the key is ``(_source_file_full, _chunk_index)``
    so two files sharing a basename in different directories don't collide,
    and a vector hit on chunk N of a file doesn't block BM25 from
    contributing chunk M of the same file. Falls back to ``source_file``
    only when full-path/chunk metadata is absent.

    BM25-only additions carry ``distance=None`` unless a strict
    ``max_distance`` threshold is set. Under a threshold, union mode loads
    stored embeddings for lexical hits and computes their vector distance
    before admitting them, preserving the same distance guarantee as the
    vector-only path.
    """
    # Backend-aware dispatch: postgres backend uses tsvector/GIN via
    # _bm25_only_via_postgres; default (chroma) path keeps the
    # sqlite/FTS5 implementation. The decision is driven by env config
    # rather than introspecting the live collection so the merger stays
    # decoupled from the per-call collection object.
    # (Upstream af7bca77 removed the old ``max_distance > 0.0: return``
    # early-exit — lexical hits are now admitted under a threshold by
    # computing their stored-embedding vector distance below.)
    use_postgres = False
    dsn = None
    try:
        from .config import MempalaceConfig

        cfg = MempalaceConfig()
        use_postgres = getattr(cfg, "backend", None) == "postgres"
        if use_postgres:
            dsn = getattr(cfg, "postgres_dsn", None) or os.environ.get("MEMPALACE_POSTGRES_DSN")
    except Exception:
        logger.debug("candidate_strategy=union: backend probe failed", exc_info=True)

    bm25_extra: list = []
    lexical = None
    try:
        if use_postgres and dsn:
            bm25_extra = _bm25_only_via_postgres(
                query,
                dsn,
                wing=wing,
                room=room,
                n_results=n_results * 3,
                _include_internal=True,
            ).get("results", [])
            if since_dt is not None or before_dt is not None:
                # Same window contract as the capability path below: postgres
                # BM25 entries are already final-shape, so filter on the
                # created_at they carry (the drawer's filed_at).
                bm25_extra = [
                    r
                    for r in bm25_extra
                    if filed_at_in_window(r.get("created_at"), since_dt, before_dt)
                ]
        else:
            # RFC 001 capability path: the collection's own lexical_search
            # (chroma BM25, sqlite_exact FTS, qdrant, pgvector). Backends
            # without the capability raise UnsupportedCapabilityError, which
            # must propagate so _finalize_candidate_hits reports it.
            where = build_where_filter(wing, room, source_file=source_file)
            lexical = drawers_col.lexical_search(
                query=query,
                n_results=n_results * 3,
                where=where or None,
            )
    except UnsupportedCapabilityError:
        raise
    except Exception:
        logger.debug("candidate_strategy=union: lexical fetch failed", exc_info=True)
        return

    # ``lexical`` is only bound on the capability path — the postgres
    # branch already produced ``bm25_extra`` in final entry shape.
    if lexical is not None:
        metric = _metric_for_collection(drawers_col)
        lexical_distances = (
            _lexical_hit_vector_distances(drawers_col, query, lexical.hits, metric)
            if max_distance > 0.0
            else {}
        )
        for hit in lexical.hits:
            meta = hit.metadata or {}
            # The window applies to every candidate source (upstream #463): a
            # lexically strong drawer outside [since, before) must not enter
            # through this side door — the vector candidates are filtered
            # upstream of the merge.
            if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
                meta.get("filed_at"), since_dt, before_dt
            ):
                continue
            full_source = meta.get("source_file", "") or ""
            distance = lexical_distances.get(hit.id)
            if max_distance > 0.0:
                if distance is None or distance > max_distance:
                    continue
                distance = round(distance, 4)
            bm25_extra.append(
                {
                    "drawer_id": _result_drawer_id(meta, hit.id),
                    "text": hit.document or "",
                    "wing": meta.get("wing", "unknown"),
                    "room": meta.get("room", "unknown"),
                    "source_file": Path(full_source).name if full_source else "?",
                    "source_path": full_source,
                    "created_at": meta.get("filed_at", "unknown"),
                    "authored_at": meta.get("authored_at", meta.get("filed_at", "unknown")),
                    "similarity": (
                        None
                        if distance is None
                        else round(_distance_to_similarity(distance, metric), 3)
                    ),
                    "distance": distance,
                    "effective_distance": distance,
                    "closet_boost": 0.0,
                    "matched_via": "bm25_backend",
                    "bm25_score": round(float(hit.score), 3),
                    "_source_file_full": full_source,
                    "_chunk_index": meta.get("chunk_index"),
                }
            )
    elif max_distance > 0.0:
        # The postgres tsvector arm carries no vector distance and has no
        # stored-embedding access here, so upstream's admit-under-threshold
        # path (af7bca77) can't apply. Preserve the original guarantee
        # instead: no BM25-only candidate may bypass the vector-distance
        # bound.
        bm25_extra = []

    def _dedup_key(entry: dict):
        full = entry.get("_source_file_full")
        ci = entry.get("_chunk_index")
        if full and ci is not None:
            return (full, ci)
        # Fall back to basename only when richer metadata is missing —
        # avoids silently dropping candidates on legacy data while still
        # giving chunk-precise dedup whenever the metadata is present.
        return entry.get("source_file")

    seen = {_dedup_key(h) for h in hits}
    for bh in bm25_extra:
        key = _dedup_key(bh)
        if not key or key == "?" or key in seen:
            continue
        bh["closet_boost"] = 0.0
        hits.append(bh)
        seen.add(key)


def _vector_underdelivered_warning(available_in_scope, vector_hit_count):
    # type: (int, int) -> str
    """Explain a thin vector result without misdiagnosing the backend.

    The "rebuild the HNSW index" hint is a ChromaDB diagnosis (a diverged
    HNSW segment really does make drawers unreachable). On postgres/pgvector
    the same shape almost always means the query has no semantic neighbour
    inside the distance threshold — e.g. an identifier-soup query scoring
    0.36 on a healthy 86K-drawer wing (2026-09-03) — and telling the reader
    to run ``mempalace repair`` sends them into a long, pointless rebuild
    under the write lock.
    """
    try:
        from .config import MempalaceConfig

        backend = getattr(MempalaceConfig(), "backend", None) or "chroma"
    except Exception:
        backend = "chroma"
    if backend == "chroma":
        return (
            f"{available_in_scope} drawers match this scope in sqlite; "
            f"vector ranked {vector_hit_count} — the rest are only reachable "
            f"by keyword match. Run `mempalace repair` to rebuild the HNSW "
            f"index for full semantic recall."
        )
    return (
        f"{available_in_scope} drawers in scope; vector ranked only {vector_hit_count} "
        f"within the distance threshold — the query has few semantic neighbours "
        f"here. Try exact identifiers (BM25 matches them), broader phrasing, or a "
        f"wider --max-distance. (Not an index fault on the {backend} backend.)"
    )


def _candidate_pool_size(n_results: int, date_window_active: bool) -> int:
    """Rerank-pool size for the drawer vector query.

    Without a date window this is the historical ``n_results * 3``
    over-fetch. With one, the window filters the pool AFTER retrieval
    (ChromaDB rejects string operands for ``$gte``/``$lt``, so ``filed_at``
    can't be range-filtered server-side), and a narrow window over a large
    palace would starve a 3x pool even though matching drawers exist —
    recall is the design requirement. Widen to ``n_results * 15``, capped
    at 500 (the ceiling the filter-fallback path already uses) — except
    the pool never drops below ``n_results`` itself, or an oversized
    request could return fewer rows than an unfiltered query would.
    ``date_filter_pool_truncated`` in the response flags a full pool so a
    capped result is never silent.
    """
    if not date_window_active:
        return n_results * 3
    return max(min(n_results * 15, 500), n_results)


def _merge_hybrid_candidates(
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float = 0.0,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> None:
    """Three-mode hybrid merger: BM25 + graph (vector-seeded + NER).

    Phase 4 of the hybrid-search-taxonomy initiative. Extends the union
    pattern from #1306 with a graph source: drawers reachable from the
    seeded entities in AGE get added to the candidate pool, with
    distance=None so the hybrid reranker scores them on their other
    signals only.

    NOTE: explicitly ignores ``max_distance``. The union strategy honors
    it (BM25 candidates have no vector distance, so injecting them
    breaks the strict-bound guarantee). Hybrid is the opposite: we
    *want* BM25 candidates regardless of vector-distance threshold —
    that's the whole point of the strategy. The hybrid reranker scores
    BM25 candidates on BM25-only contribution; if the caller asked for
    a vector-distance bound, hybrid honors it for vector hits and
    augments with BM25/graph candidates that bypass it.

    Steps:
      1. Inject BM25 candidates directly via _bm25_only_via_postgres
         (sidesteps the max_distance short-circuit in
         _merge_bm25_union_candidates which is correct for union but
         wrong for hybrid).
      2. Take vector hits' drawer IDs as seeds; AGE-expand to find
         drawers about the same entities. Add as graph-source candidates.
      3. Run cheap NER on the query; AGE-expand any matched entities.
         Add as graph-source candidates.

    The graph-source candidates get `matched_via="graph_postgres"` so
    debug traces can attribute which source surfaced each hit. They
    carry the same shape as BM25-only hits (distance=None) so the
    hybrid reranker handles them uniformly.

    Only postgres backend (the AGE graph lives there). Falls through
    to BM25-only union for chroma.
    """
    # Step 1: BM25 candidates (delegates to the existing merger which is
    # already backend-aware). Pass max_distance=0.0 to *force* BM25
    # injection regardless of the caller's vector-distance bound —
    # hybrid retrieval explicitly wants BM25 candidates that vector
    # missed, even when a strict vector threshold would normally filter
    # them out.
    _merge_bm25_union_candidates(
        hits,
        drawers_col,
        query,
        wing,
        room,
        n_results,
        max_distance=0.0,
        source_file=source_file,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
    )

    # Step 2 + 3: graph expansion requires postgres backend
    dsn = None
    try:
        from .config import MempalaceConfig

        cfg = MempalaceConfig()
        if getattr(cfg, "backend", None) == "postgres":
            dsn = getattr(cfg, "postgres_dsn", None) or os.environ.get("MEMPALACE_POSTGRES_DSN")
    except Exception:
        logger.debug("hybrid merger: backend probe failed", exc_info=True)
    if not dsn:
        return

    # Collect existing dedup keys before graph expansion so we don't
    # re-add what BM25/vector already surfaced.
    def _dedup_key(entry):
        full = entry.get("_source_file_full")
        ci = entry.get("_chunk_index")
        if full and ci is not None:
            return (full, ci)
        return entry.get("source_file") or entry.get("id")

    seen = {_dedup_key(h) for h in hits}
    seen_ids = {h.get("id") for h in hits if h.get("id")}

    # Step 2: vector-seeded graph expansion. Use top-5 vector hits (those
    # with a real distance, not distance=None BM25-only entries).
    seed_ids = [h.get("id") for h in hits[:5] if h.get("distance") is not None and h.get("id")]
    seed_expanded = _graph_expand_from_seeds(seed_ids, dsn) if seed_ids else []

    # Step 3: NER-based graph expansion
    ner_candidates = _ner_from_query(query)
    ner_expanded = _graph_expand_from_entities(ner_candidates, dsn) if ner_candidates else []

    # Fetch full drawer content for any new graph-surfaced IDs in one
    # batched query (vs N round-trips). Skip the ones we've already
    # surfaced via vector or BM25.
    new_ids = [did for did in (seed_expanded + ner_expanded) if did and did not in seen_ids]
    new_ids = list(dict.fromkeys(new_ids))  # dedup, preserve order
    if not new_ids:
        return

    try:
        import psycopg as psycopg2

        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, wing, room, document, metadata
                    FROM mempalace_drawers
                    WHERE id = ANY(%s)
                    LIMIT %s
                    """,
                    (new_ids, max(n_results * 2, 20)),
                )
                rows = cur.fetchall()
    except Exception:
        logger.debug("hybrid merger: drawer fetch failed", exc_info=True)
        return

    import json as _json

    for drawer_id, drawer_wing, drawer_room, document, metadata in rows:
        # Honor wing/room filters
        if wing and drawer_wing != wing:
            continue
        if room and drawer_room != room:
            continue
        if isinstance(metadata, str):
            try:
                metadata = _json.loads(metadata)
            except Exception:
                metadata = {}
        elif metadata is None:
            metadata = {}
        full_source = (metadata or {}).get("source_file", "") or ""
        entry_key = (full_source, (metadata or {}).get("chunk_index"))
        if entry_key in seen or drawer_id in seen_ids:
            continue
        # Determine which graph channel surfaced this for trace purposes
        via = "graph_seeded" if drawer_id in seed_expanded else "graph_ner"
        hit = {
            "id": drawer_id,
            "text": document,
            "wing": drawer_wing,
            "room": drawer_room,
            "source_file": full_source.rsplit("/", 1)[-1] if full_source else "?",
            "created_at": (metadata or {}).get("added_at")
            or (metadata or {}).get("filed_at", "unknown"),
            "similarity": None,
            "distance": None,
            "effective_distance": None,
            "closet_boost": 0.05,  # small graph-presence boost in hybrid rerank
            "bm25_score": 0.0,
            "matched_via": via,
            "_source_file_full": full_source,
            "_chunk_index": (metadata or {}).get("chunk_index"),
        }
        # The date window applies to every candidate source (upstream #463):
        # graph expansion must not smuggle in out-of-window drawers either.
        if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
            (metadata or {}).get("filed_at"), since_dt, before_dt
        ):
            continue
        hits.append(hit)
        seen.add(entry_key)
        seen_ids.add(drawer_id)


_CLOSET_RESULT_POOL_MULTIPLIER = 4
_MAX_HYDRATION_CHARS = 10000


def _candidate_pool_limits(
    candidate_strategy: str,
    n_results: int,
    date_window_active: bool = False,
) -> tuple[int, int]:
    """Return vector-query and pre-enrichment candidate limits.

    Date-window searches retain the wider current-develop pool because the
    timestamp constraint is applied after retrieval. The normal vector path
    also remains at least four times wide through closet enrichment. Union
    mode keeps only the requested top vector candidates before lexical merge.
    """
    base_query_limit = _candidate_pool_size(
        n_results,
        date_window_active,
    )

    if candidate_strategy == "union":
        return base_query_limit, n_results

    widened = max(
        base_query_limit,
        n_results * _CLOSET_RESULT_POOL_MULTIPLIER,
    )
    return widened, widened


def _enrich_closet_hits(
    hits: list,
    drawers_col,
    query: str,
    stop_words: frozenset = frozenset(),
) -> list:
    """Hydrate closet-boosted hits and memoise each source/group fetch."""
    query_terms = set(
        _tokenize(
            query,
            stop_words,
        )
    )
    source_cache: dict = {}

    for hit in hits:
        if hit.get("matched_via") == "drawer":
            continue

        full_source = hit.get("_source_file_full") or ""

        if not full_source:
            continue

        parent_drawer_id = hit.get("_parent_drawer_id") or None
        cache_key = (
            full_source,
            parent_drawer_id,
        )

        if cache_key not in source_cache:
            try:
                source_drawers = drawers_col.get(
                    where=(
                        _scoped_source_filter(
                            full_source,
                            parent_drawer_id,
                        )
                    ),
                    include=[
                        "documents",
                        "metadatas",
                    ],
                )
            except Exception:
                logger.debug(
                    "Neighbor fetch failed for %s",
                    full_source,
                    exc_info=True,
                )
                source_cache[cache_key] = None
            else:
                source_cache[cache_key] = (
                    list(
                        getattr(
                            source_drawers,
                            "documents",
                            None,
                        )
                        or []
                    ),
                    list(
                        getattr(
                            source_drawers,
                            "metadatas",
                            None,
                        )
                        or []
                    ),
                )

        cached = source_cache[cache_key]

        if cached is None:
            continue

        docs, metadatas = cached

        if len(docs) <= 1:
            continue

        indexed = []

        for index, (
            document,
            metadata,
        ) in enumerate(
            zip(
                docs,
                metadatas,
            )
        ):
            chunk_index = (
                metadata.get(
                    "chunk_index",
                    index,
                )
                if isinstance(
                    metadata,
                    dict,
                )
                else index
            )

            if not isinstance(
                chunk_index,
                int,
            ):
                chunk_index = index

            indexed.append(
                (
                    chunk_index,
                    document or "",
                )
            )

        indexed.sort(key=lambda pair: pair[0])
        ordered_docs = [document for _, document in indexed]

        best_index = 0
        best_score = -1

        for index, document in enumerate(ordered_docs):
            lowered = document.lower()
            score = sum(1 for term in query_terms if term in lowered)

            if score > best_score:
                best_score = score
                best_index = index

        start = max(
            0,
            best_index - 1,
        )
        end = min(
            len(ordered_docs),
            best_index + 2,
        )
        expanded = "\n\n".join(ordered_docs[start:end])

        if len(expanded) > _MAX_HYDRATION_CHARS:
            expanded = expanded[:_MAX_HYDRATION_CHARS] + (
                f"\n\n[...truncated. "
                f"{len(ordered_docs)} total drawers. "
                "Use mempalace_get_drawer "
                "for full content.]"
            )

        hit["text"] = expanded
        hit["drawer_index"] = best_index
        hit["total_drawers"] = len(ordered_docs)

    return hits


def _dedupe_rendered_hits(
    hits: list,
) -> list:
    """Drop repeated closet-rendered passages while preserving rank order.

    Plain drawer hits retain their historical behavior, including legitimate
    exact repeats at different source positions. A duplicate is removed only
    when either occurrence came through closet enrichment.
    """
    unique = []
    first_by_key: dict = {}

    for hit in hits:
        source = hit.get("_source_file_full") or hit.get("source_path") or hit.get("source_file")
        text = hit.get("text")

        if not source or not isinstance(
            text,
            str,
        ):
            unique.append(hit)
            continue

        key = (
            source,
            text,
        )
        previous = first_by_key.get(key)

        if previous is None:
            first_by_key[key] = hit
            unique.append(hit)
            continue

        previous_is_closet = previous.get("matched_via") == "drawer+closet"
        current_is_closet = hit.get("matched_via") == "drawer+closet"

        if previous_is_closet or current_is_closet:
            continue

        unique.append(hit)

    return unique


# Strategy dispatch — keeps search_memories' branch count under the
# project's complexity ceiling (C901 max-complexity=25). New strategies
# register here.
_CANDIDATE_MERGERS = {
    "vector": None,  # default no-op
    "union": _merge_bm25_union_candidates,
    "hybrid": _merge_hybrid_candidates,  # BM25 + graph (Phase 4)
}


# Fusion-mode dispatch — how the merged candidate pool is finally ranked.
# Both rankers share the (results, query) signature and rank in place.
_FUSION_RANKERS = {
    "convex": _hybrid_rank,
    "rrf": _rrf_rank,
}


def _validate_fusion_mode(mode: str) -> None:
    """Raise ``ValueError`` for unknown fusion modes."""
    if mode not in _FUSION_RANKERS:
        raise ValueError(f"fusion_mode must be one of {tuple(_FUSION_RANKERS)}, got {mode!r}")


def _validate_candidate_strategy(strategy: str) -> None:
    """Raise ``ValueError`` for unknown strategies.

    Called eagerly at the top of ``search_memories`` so invalid values
    fail consistently regardless of whether the call routes through the
    vector path, the BM25-only fallback, or returns an early error dict.
    """
    if strategy not in _CANDIDATE_MERGERS:
        raise ValueError(
            f"candidate_strategy must be one of {tuple(_CANDIDATE_MERGERS)}, got {strategy!r}"
        )


def _apply_candidate_strategy(
    strategy: str,
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float = 0.0,
    source_file: str = None,
    since_dt=None,
    before_dt=None,
) -> None:
    """Dispatch to the registered merger for ``strategy``.

    Strategy validity is assumed (``_validate_candidate_strategy`` runs
    earlier); ``"vector"`` is a no-op.
    """
    merger = _CANDIDATE_MERGERS[strategy]
    if merger is not None:
        merger(
            hits,
            drawers_col,
            query,
            wing,
            room,
            n_results,
            max_distance=max_distance,
            source_file=source_file,
            since_dt=since_dt,
            before_dt=before_dt,
        )


def _finalize_candidate_hits(
    *,
    candidate_strategy: str,
    hits: list,
    drawers_col,
    query: str,
    wing: str,
    room: str,
    n_results: int,
    max_distance: float,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> tuple:
    try:
        _apply_candidate_strategy(
            candidate_strategy,
            hits,
            drawers_col,
            query,
            wing,
            room,
            n_results,
            max_distance=max_distance,
            source_file=source_file,
            since_dt=since_dt,
            before_dt=before_dt,
        )
    except UnsupportedCapabilityError:
        return [], _search_error_result(
            "candidate_strategy='union' requires a backend with lexical_search support",
            unsupported_capability="supports_lexical_search",
            hint=(
                "Use candidate_strategy='vector' or select a backend that supports lexical search."
            ),
        )

    ranked = _hybrid_rank(
        hits,
        query,
        metric=_metric_for_collection(drawers_col),
        stop_words=stop_words,
    )
    hits = _dedupe_rendered_hits(ranked)[:n_results]

    for hit in hits:
        hit.pop("_sort_key", None)
        hit.pop("_source_file_full", None)
        hit.pop("_chunk_index", None)
        hit.pop("_parent_drawer_id", None)

    return hits, None


def _search_error_result(error: str, **extra) -> dict:
    """Error envelope for programmatic search callers.

    Always includes ``results: []`` so callers can safely index
    ``result["results"]`` without a KeyError when the palace failed to
    open or the query raised mid-flight (Windows CI flake surface).
    """
    out = {"error": error, "results": []}
    out.update(extra)
    return out


def _backend_mismatch_result(error: BackendMismatchError) -> dict:
    return _search_error_result(
        "Backend mismatch",
        details=str(error),
        hint="Select the matching backend or use a fresh palace directory.",
    )


def _unknown_backend_result(error: KeyError) -> dict:
    return _search_error_result(
        "Unknown backend",
        details=str(error),
        hint="Check MEMPALACE_BACKEND or the configured backend name.",
    )


def _search_result_envelope(
    *,
    query: str,
    wing,
    room,
    source_file,
    since,
    before,
    hits: list,
    candidates_fetched: int,
    pool_size: int,
    date_window_active: bool,
) -> dict:
    """Assemble the ``search_memories`` response dict.

    When a date window is active and the widened candidate pool came back
    full, drawers beyond the pool never got a chance to match the window —
    ``date_filter_pool_truncated`` flags it so a thin result under a date
    filter is never mistaken for "that's all there was".
    """
    result = {
        "query": query,
        "filters": {
            "wing": wing,
            "room": room,
            "source_file": source_file,
            "since": since,
            "before": before,
        },
        "total_before_filter": candidates_fetched,
        # Curated documents are ordered above the near-duplicate transcripts
        # that quote them (#451 item F) — bounded to near-duplicates, so
        # transcript-only recall keeps the ranker's order.
        #
        # Provenance is stamped here so every caller of ``search_memories``
        # — including MCP ``mempalace_search``, which is what the fleet
        # actually reads — sees which kind of source a hit came from. Hits
        # from this path carry ``source_path`` (the full path) beside the
        # display basename, so staleness resolves here too, against THIS
        # host's filesystem. Under the daemon that is the palace host, which
        # is the copy the miner read — the right mtime to compare. See
        # provenance.py for the cross-host rule.
        "results": _prefer_curated(_annotate_provenance(hits)),
    }
    if date_window_active and candidates_fetched >= pool_size:
        result["date_filter_pool_truncated"] = True
    return result


def _window_and_fallback_gate(
    since,
    before,
    vector_disabled: bool,
    *,
    query: str,
    palace_path: str,
    wing,
    room,
    n_results: int,
    collection_name,
    source_file,
    tags: list = None,
    stop_words: frozenset = frozenset(),
):
    """Front gate for ``search_memories``: parse the window, route the fallback.

    Returns ``(since_dt, before_dt, active, short_circuit)``.
    ``short_circuit`` is a complete response to return verbatim — the
    ``{"error": ...}`` payload for an invalid/inverted window, or the
    BM25-only fallback result when ``vector_disabled`` is set — and ``None``
    when the vector path should proceed. Extracted so the window plumbing
    doesn't push ``search_memories`` over the C901 complexity ceiling.
    """
    try:
        since_dt, before_dt = parse_window(since, before)
    except ValueError as e:
        return None, None, False, {"error": str(e)}
    active = since_dt is not None or before_dt is not None
    if vector_disabled:
        return (
            since_dt,
            before_dt,
            active,
            _vector_disabled_with_window(
                query=query,
                palace_path=palace_path,
                wing=wing,
                room=room,
                n_results=n_results,
                collection_name=collection_name,
                source_file=source_file,
                tags=tags,
                since=since,
                before=before,
                since_dt=since_dt,
                before_dt=before_dt,
                stop_words=stop_words,
            ),
        )
    return since_dt, before_dt, active, None


def _candidate_out_of_scope(dist, meta, max_distance, since_dt, before_dt) -> bool:
    """True when a drawer candidate fails the distance or date-window gate.

    Distance is checked on the raw value before rounding to avoid precision
    loss (pre-existing behavior); the date window applies whenever a bound
    is set, with the shared ``[since, before)`` semantics.
    """
    if max_distance > 0.0 and dist > max_distance:
        return True
    if (since_dt is not None or before_dt is not None) and not filed_at_in_window(
        meta.get("filed_at"), since_dt, before_dt
    ):
        return True
    return False


def _vector_disabled_with_window(
    *,
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    n_results: int,
    collection_name: str,
    source_file: str,
    since: str,
    before: str,
    since_dt,
    before_dt,
    tags: list = None,
    stop_words: frozenset = frozenset(),
) -> dict:
    """Run the BM25-only route and echo the raw window strings.

    The fallback helper takes parsed bounds; the caller's raw ``since``/
    ``before`` strings are stitched into the ``filters`` envelope here so
    both search paths report the same shape.
    """
    result = _vector_disabled_search(
        query=query,
        palace_path=palace_path,
        wing=wing,
        room=room,
        n_results=n_results,
        collection_name=collection_name,
        source_file=source_file,
        tags=tags,
        since_dt=since_dt,
        before_dt=before_dt,
        stop_words=stop_words,
    )
    if "filters" in result:
        result["filters"]["since"] = since
        result["filters"]["before"] = before
    return result


def _vector_disabled_search(
    *,
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    tags: list = None,
    n_results: int,
    collection_name: str,
    source_file: str = None,
    stop_words: frozenset = frozenset(),
    since_dt=None,
    before_dt=None,
) -> dict:
    try:
        backend_name = resolve_backend_name(palace_path)
    except BackendMismatchError as e:
        return _backend_mismatch_result(e)
    except KeyError as e:
        return _unknown_backend_result(e)
    if backend_name != "chroma":
        return _search_error_result(
            "vector_disabled fallback is Chroma-only",
            unsupported_capability="chroma_hnsw_fallback",
            backend=backend_name,
            hint="Disable vector_disabled for non-Chroma backends.",
        )
    return _bm25_only_via_sqlite(
        query,
        palace_path,
        wing=wing,
        room=room,
        tags=tags,
        source_file=source_file,
        n_results=n_results,
        collection_name=collection_name,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
    )


def _open_search_collection(palace_path: str, collection_name: str):
    try:
        return get_collection(palace_path, collection_name=collection_name, create=False), None
    except BackendMismatchError as e:
        return None, _backend_mismatch_result(e)
    except KeyError as e:
        return None, _unknown_backend_result(e)
    except (CollectionNotInitializedError, PalaceNotFoundError) as e:
        logger.error("No palace found at %s: %s", palace_path, e)
        return None, _search_error_result(
            "No palace found",
            hint="Run: mempalace init <dir> && mempalace mine <dir>",
        )
    except BackendError as e:
        logger.error("Backend error opening palace at %s: %s", palace_path, e)
        return None, _search_error_result(
            "Backend error",
            details=str(e),
            hint="Check the selected backend configuration and availability.",
        )
    except Exception as e:
        logger.error("No palace found at %s: %s", palace_path, e)
        return None, _search_error_result(
            "No palace found",
            hint="Run: mempalace init <dir> && mempalace mine <dir>",
        )


def _query_drawers_with_filter_fallback(
    drawers_col, dkwargs, query, n_results, wing, room, source_file=None
):
    """Run the filtered drawer query, falling back to an unfiltered query plus a
    Python-side post-filter when ChromaDB raises on the filtered query.

    A ChromaDB HNSW/SQLite index mismatch makes filtered queries fail with
    "Error finding id" even when unfiltered search works fine — it happens when
    drawers are ingested via two different paths (e.g. bulk import vs MCP tool
    calls), leaving the vector index inconsistent with the metadata store. We
    retry unfiltered (over-fetching) and re-apply the wing/room/source_file filter in Python.
    See #1245 / #1035.
    """
    where = dkwargs.get("where")
    try:
        return drawers_col.query(**dkwargs)
    except Exception as filter_err:
        if not where:
            raise
        logger.warning(
            "Filtered search failed (%s); falling back to unfiltered + post-filter",
            filter_err,
        )
        raw = drawers_col.query(
            query_texts=[query],
            n_results=min(n_results * 15, 500),
            include=["documents", "metadatas", "distances"],
        )
        raw_docs = _first_or_empty(raw, "documents")
        raw_ids = _aligned_query_ids(raw, len(raw_docs))
        fids, fdocs, fmetas, fdists = [], [], [], []
        for stored_drawer_id, doc, meta, dist in zip(
            raw_ids,
            raw_docs,
            _first_or_empty(raw, "metadatas"),
            _first_or_empty(raw, "distances"),
        ):
            meta = meta or {}
            if wing and meta.get("wing") != wing:
                continue
            if room and meta.get("room") != room:
                continue
            if source_file and meta.get("source_file") != source_file:
                continue
            fids.append(stored_drawer_id)
            fdocs.append(doc)
            fmetas.append(meta)
            fdists.append(dist)
        return {
            "ids": [fids],
            "documents": [fdocs],
            "metadatas": [fmetas],
            "distances": [fdists],
        }


# ── confidence calibration (techempower-org/mempalace#167) ───────────────────
#
# A fitted calibrator maps the raw ``similarity`` field (a transformed cosine
# distance) to a calibrated P(relevant). It is loaded lazily from the path in
# config (``calibration_path``) and cached per-path so a config change in a
# long-lived process is picked up without a restart. Missing/unconfigured →
# no ``confidence`` field is emitted (never faked). See
# docs/research/uncertainty-aware-retrieval.md.

_CALIBRATOR_CACHE: dict = {}


def _load_calibrator():
    """Return the configured :class:`~mempalace.calibration.Calibrator`, or None.

    Cached by resolved path and modification time. ``None`` (unconfigured) and a
    missing/unreadable file both yield ``None`` so the search path omits
    ``confidence`` rather than faking it. Keying on mtime lets a long-lived
    process (the daemon) pick up an in-place re-fit without a restart.
    """
    from .config import MempalaceConfig

    path = MempalaceConfig().calibration_path
    if not path:
        return None

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None

    cached = _CALIBRATOR_CACHE.get(path)
    if cached is not None:
        cached_cal, cached_mtime = cached
        if cached_mtime == mtime:
            return cached_cal

    from .calibration import Calibrator

    cal = Calibrator.load(path) if mtime is not None else None
    _CALIBRATOR_CACHE[path] = (cal, mtime)
    return cal


# ── optional cross-encoder rerank (techempower-org/mempalace#179) ────────────
#
# Off by default. When the operator sets ``MEMPALACE_RERANK_CROSS_ENCODER=1``
# or ``"cross_encoder_rerank": true`` in config.json, the rerank stage fires
# between fusion and result return. Resolves config at search time so the
# daemon picks up toggles without a restart (mirrors the calibrator pattern).


def _cross_encoder_rerank_config() -> "dict | None":
    """Return ``{model, top_n}`` if cross-encoder rerank is enabled, else ``None``.

    Resolved fresh on each call so the daemon picks up config or env
    changes without a restart. The model itself is cached inside
    ``mempalace.cross_encoder_rerank``, so a hot palace with rerank on
    pays this resolution cost (microseconds) per query, not the model
    load cost.
    """
    from .config import MempalaceConfig

    cfg = MempalaceConfig()
    if not cfg.cross_encoder_rerank:
        return None
    return {"model": cfg.cross_encoder_model, "top_n": cfg.cross_encoder_top_n}


def _backend_capabilities(col) -> frozenset:
    backend = getattr(col, "_backend", None)
    if backend is None:
        inner = getattr(col, "_inner", None)
        backend = getattr(inner, "_backend", None) if inner is not None else None
    caps = getattr(backend, "capabilities", None) if backend is not None else None
    return caps if isinstance(caps, (set, frozenset)) else frozenset()


def _closet_boosts(closets_col, *, query: str, n_results: int, where: dict) -> dict:
    """Best-per-source closet hits used as a rank boost, never a gate.

    sqlite_exact (and any lexical backend) uses FTS instead of a second
    exact-cosine scan over the closet collection — closets are pointer
    lines, so BM25 is the better signal anyway.
    """
    boosts: dict = {}
    n_hits = max(1, n_results * 2)
    if "supports_lexical_search" in _backend_capabilities(closets_col):
        result = closets_col.lexical_search(query=query, n_results=n_hits, where=where or None)
        hits = getattr(result, "hits", None) or []
        for rank, hit in enumerate(hits):
            meta = hit.metadata or {}
            source = meta.get("source_file", "")
            if source and source not in boosts:
                preview = (hit.document or "")[:200]
                boosts[source] = (rank, 0.0, preview)
        return boosts

    ckwargs = {
        "query_texts": [query],
        "n_results": n_hits,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        ckwargs["where"] = where
    closet_results = closets_col.query(**ckwargs)
    for rank, (cdoc, cmeta, cdist) in enumerate(
        zip(
            _first_or_empty(closet_results, "documents"),
            _first_or_empty(closet_results, "metadatas"),
            _first_or_empty(closet_results, "distances"),
        )
    ):
        cmeta = cmeta or {}
        source = cmeta.get("source_file", "")
        if source and source not in boosts:
            boosts[source] = (rank, cdist, (cdoc or "")[:200])
    return boosts


def search_memories(  # noqa: C901 — fork-only fallback orchestration; complexity above ceiling is the cost of the BM25-top-up + warnings + closet-boost branches
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    tags: list = None,
    source_file: str = None,
    since: str = None,
    before: str = None,
    n_results: int = 5,
    max_distance: float = 0.0,
    vector_disabled: bool = False,
    candidate_strategy: str = "vector",
    fusion_mode: str = "convex",
    collection_name: str = None,
    lang: Optional[str] = None,
) -> dict:
    """Programmatic search — returns a dict instead of printing.

    Used by the MCP server and other callers that need data.

    Hybrid search: BM25 keyword matching + vector semantic similarity.
    The drawer query is the floor — always runs — and closet hits add a
    rank-based boost when they agree.

    Args:
        query: Natural language search query.
        palace_path: Path to the ChromaDB palace directory.
        wing: Optional wing filter.
        room: Optional room filter.
        source_file: Optional exact source_file filter. Matches the full
            stored source_file value verbatim (#1815).
        since: Optional inclusive ISO date/datetime lower bound on a
            drawer's ``filed_at`` (ingest time, the ``created_at`` shown in
            results) — ``[since, before)`` window semantics shared with
            ``list_drawers`` (#1128): wall-clock naive comparison, drawers
            with missing/unparseable ``filed_at`` excluded while a bound is
            active. Filtering happens after retrieval (ChromaDB rejects
            string operands for ``$gte``/``$lt``), so the candidate pool is
            widened via ``_candidate_pool_size`` — see
            ``date_filter_pool_truncated`` in the response.
        before: Optional exclusive ISO upper bound; see ``since``.
        n_results: Max results to return.
        max_distance: Max cosine distance threshold. The palace collection uses
            cosine distance (hnsw:space=cosine) — 0 = identical, 2 = opposite.
            Results with distance > this value are filtered out. A value of
            0.0 disables filtering. Typical useful range: 0.3–1.0.
        vector_disabled: When True, route to the sqlite-only BM25 fallback
            (#1222). Set by the MCP server when the HNSW capacity probe
            detects a divergence that would segfault chromadb on segment
            load.
        candidate_strategy: How candidates for the hybrid re-rank are gathered.

            * ``"vector"`` (default) — preserves historical behavior: top
              ``n_results * 4`` rows from the vector index are the rerank pool.
              Cheap; works well when query and target docs agree in the
              embedding space.
            * ``"union"`` — also pull top ``n_results * 3`` lexical candidates
              through the backend's ``lexical_search`` capability and merge
              them into the rerank pool (deduped by source_file). Catches docs
              with strong BM25 signal that are vector-distant from the query.
              Perf depends on the selected backend; opt in until the cost is
              characterized.

              When ``max_distance > 0.0`` is also set, BM25-only candidates
              are admitted only if their stored embeddings can be loaded and
              their computed vector distance satisfies that threshold.
        lang: Locale code for BM25 stop-word filtering (opt-in). When
            omitted, reads ``MempalaceConfig().lang_explicit`` — returns an
            empty set unless the user has set ``MEMPALACE_LANG`` /
            ``MEMPAL_LANG`` or ``config.json["lang"]``. Palaces without an
            explicit language skip filtering entirely, preserving pre-PR
            byte-identical scoring.
        fusion_mode: How the final candidate pool is ranked.

            * ``"convex"`` (default) — historical behavior: a weighted blend
              of normalized vector similarity and BM25 (``_hybrid_rank``).
            * ``"rrf"`` — Reciprocal Rank Fusion of the vector ordering and
              the BM25 ordering (``_rrf_rank``). Score-scale agnostic; only
              the rank orderings matter. Selectable for the #162 A/B study.
    """
    # Validate the strategy eagerly so invalid values fail the same way
    # regardless of whether the call routes through the vector path or
    # the BM25-only fallback below.
    _validate_candidate_strategy(candidate_strategy)
    _validate_fusion_mode(fusion_mode)

    # Resolve stop words once up-front so every BM25 site (the vector path's
    # `_hybrid_rank`, the `vector_disabled` fallback, and the union-merge
    # candidate gather) tokenizes against the same locale.
    stop_words = _resolve_stop_words(lang)

    since_dt, before_dt, date_window_active, short_circuit = _window_and_fallback_gate(
        since,
        before,
        vector_disabled,
        query=query,
        palace_path=palace_path,
        wing=wing,
        room=room,
        n_results=n_results,
        collection_name=collection_name,
        source_file=source_file,
        tags=tags,
        stop_words=stop_words,
    )
    if short_circuit is not None:
        # The vector-disabled BM25 fallback is a public ``search_memories``
        # return too, so it gets the same curated-first ordering.
        _prefer_curated(short_circuit.get("results") if isinstance(short_circuit, dict) else None)
        return short_circuit

    drawers_col, open_error = _open_search_collection(palace_path, collection_name)
    if open_error:
        return open_error

    metric = _metric_for_collection(drawers_col)
    # Alert if this palace predates hnsw:space=cosine being set on creation —
    # similarity scores will be junk until `mempalace repair` rebuilds the
    # index. Centralized here so both CLI search() and MCP mempalace_search
    # benefit from the warning via the delegate path. (Upstream #1179 added
    # the warning inline in CLI search(); the fork's delegation pattern needs
    # it one layer up so the same warning surface stays live.)
    _warn_if_legacy_metric(drawers_col)

    where = build_where_filter(wing, room, tags=tags, source_file=source_file)

    # Hybrid retrieval: always query drawers directly (the floor), then use
    # closet hits to boost rankings. Closets are a ranking SIGNAL, never a
    # GATE — direct drawer search is always the baseline.
    #
    # This avoids the "weak-closets regression" where narrative content
    # produces low-signal closets (regex extraction matches few topics)
    # and closet-first routing hides drawers that direct search would find.
    pool_size, pre_enrichment_limit = _candidate_pool_limits(
        candidate_strategy,
        n_results,
        date_window_active,
    )
    pull_size = pool_size  # fork paths reuse upstream's window-aware pool
    warnings: list[str] = []
    drawer_results: dict = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    # Over-fetch 3× the requested limit so re-ranking with closet boosts
    # has enough candidates to reorder.

    # RESEARCH FEATURE — multi-encoder RRF, gated by
    # PALACE_USE_MULTI_ENCODER_RRF. When enabled, fan the query out to
    # N encoder-bound palaces and RRF-fuse the rank lists in place of
    # the single chromadb query below. See mempalace.multi_encoder and
    # techempower-org/mempalace#82. Default off; storage cost is Nx so
    # this is not a flip-the-default candidate without further work.
    from . import multi_encoder as _mc

    use_multi_encoder = _mc.is_enabled()

    try:
        if use_multi_encoder:
            drawer_results = _mc.fused_query(
                query=query,
                palace_path=palace_path,
                n_results=pull_size,
                where=where or None,
            )
        else:
            dkwargs = {
                "query_texts": [query],
                "n_results": pull_size,
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                dkwargs["where"] = where
            drawer_results = _query_drawers_with_filter_fallback(
                drawers_col, dkwargs, query, n_results, wing, room, source_file
            )
    except Exception as e:
        # Don't hard-fail: degrade to sqlite fallback below so callers still
        # get the drawers that match the scope, with a warning explaining why
        # vector ranking was unavailable. This covers the #951 filter-planner
        # "Error finding id" failure mode and HNSW runtime errors on drifted
        # indexes.
        warnings.append(f"vector search unavailable: {e}")

    # Gather closet hits (best-per-source) to build a boost lookup.
    closet_boost_by_source: dict = {}  # source_file -> (rank, closet_dist, preview)
    try:
        closets_col = get_closets_collection(palace_path, create=False)
        closet_boost_by_source = _closet_boosts(
            closets_col, query=query, n_results=n_results, where=where
        )
    except Exception:
        # No closets yet — hybrid degrades to pure drawer search.
        logger.debug("Closet collection unavailable; using drawer-only search", exc_info=True)

    scored: list = []
    drawer_docs = _first_or_empty(drawer_results, "documents")
    stored_drawer_ids = _aligned_query_ids(drawer_results, len(drawer_docs))
    for stored_drawer_id, doc, meta, dist in zip(
        stored_drawer_ids,
        drawer_docs,
        _first_or_empty(drawer_results, "metadatas"),
        _first_or_empty(drawer_results, "distances"),
    ):
        meta = meta or {}
        doc = doc or ""
        if _candidate_out_of_scope(dist, meta, max_distance, since_dt, before_dt):
            continue

        meta = meta or {}
        source = meta.get("source_file", "") or ""
        boost = 0.0
        matched_via = "drawer"
        closet_preview = None
        if source in closet_boost_by_source:
            c_rank, c_dist, c_preview = closet_boost_by_source[source]
            if c_dist <= CLOSET_DISTANCE_CAP and c_rank < len(CLOSET_RANK_BOOSTS):
                boost = CLOSET_RANK_BOOSTS[c_rank]
                matched_via = "drawer+closet"
                closet_preview = c_preview

        # Feedback rating adjustment (#159): an explicit useful/not-useful
        # signal stored in drawer metadata nudges the effective distance.
        # Bounded and capped (mempalace.ratings) so it can reorder neighbors
        # but never displace a relevant drawer out of the result set — recall
        # is preserved. Gated by PALACE_RATING_BOOST (default on; set "0" to
        # disable for A/B or debugging).
        rating_adj = 0.0
        if _rating_boost_enabled():
            rating_adj = rating_distance_adjustment(meta)

        # Recency adjustment (#158): newer drawers get a small upward nudge via
        # exponential decay on age. Bounded and capped (mempalace.recency) so
        # it reorders neighbors but never displaces a relevant drawer out of
        # the result set — recall is preserved. Off by default; gated by
        # PALACE_RECENCY_BOOST (set "1" to enable), half-life configurable via
        # PALACE_RECENCY_HALFLIFE_DAYS.
        recency_adj = 0.0
        if _recency_boost_enabled():
            recency_adj = recency_distance_adjustment(meta, halflife_days=_recency_halflife_days())

        # Clamp to the valid cosine-distance range [0, 2]. When a strong
        # closet boost (up to 0.40) exceeds the raw distance, the subtraction
        # can go negative — which (a) yields ``similarity > 1.0`` downstream
        # and (b) makes the sort key land *below* ordinary positive distances,
        # inverting the ranking so the best hybrid matches sort last.
        effective_dist = max(0.0, min(2.0, dist - boost + rating_adj + recency_adj))
        entry = {
            "drawer_id": _result_drawer_id(meta, stored_drawer_id),
            "text": doc,
            "wing": meta.get("wing", "unknown"),
            "room": meta.get("room", "unknown"),
            "topic": meta.get("topic"),
            # source_file is the basename (display); source_path is the full
            # stored value, the round-trippable key for the source_file filter.
            "source_file": Path(source).name if source else "?",
            "source_path": source,
            "created_at": meta.get("filed_at", "unknown"),
            "authored_at": meta.get("authored_at", meta.get("filed_at", "unknown")),
            # Similarity is the raw vector score. Closet boost ranks via
            # effective_distance but must not inflate the advertised score.
            "similarity": round(_distance_to_similarity(dist, metric), 3),
            "distance": round(dist, 4),
            "effective_distance": round(effective_dist, 4),
            "closet_boost": round(boost, 3),
            "rating_score": net_rating(meta),
            "matched_via": matched_via,
            # Internal: retain the full source_file path + chunk_index so the
            # enrichment step below doesn't have to reverse-lookup via
            # basename-suffix matching (which silently collides when two
            # files share a basename across different directories).
            "_sort_key": effective_dist,
            "_source_file_full": source,
            "_chunk_index": meta.get("chunk_index"),
            "_parent_drawer_id": meta.get("parent_drawer_id"),
        }
        if closet_preview:
            entry["closet_preview"] = closet_preview
        scored.append(entry)

    scored.sort(key=lambda h: h["_sort_key"])
    # Under an active date window, keep EVERY in-window survivor for the
    # hybrid re-rank below — the pool was widened precisely so a BM25-strong
    # drawer deep in the vector ordering can surface; trimming here would cut
    # it before fusion ever sees it (upstream #463 review finding). The
    # display cut to n_results happens after the fusion/rerank stages.
    # Outside a window, upstream v3.9 keeps the wider pre-enrichment pool
    # (strategy-aware via _candidate_pool_limits) through closet enrichment.
    hits = scored if date_window_active else scored[:pre_enrichment_limit]

    # Drawer-grep enrichment: retain the wider pool until repeated
    # closet-rendered passages can be replaced by distinct candidates.
    _enrich_closet_hits(
        hits,
        drawers_col,
        query,
        stop_words=stop_words,
    )

    # Candidate strategy hook: optionally widen the rerank pool's *source*
    # before ranking. Default ("vector") is a no-op; "union" merges top-K
    # backend lexical candidates. See `_apply_candidate_strategy`.
    # ``max_distance`` is forwarded so union mode can refuse to inject
    # BM25-only (distance=None) candidates that would silently bypass the
    # caller's strict distance threshold.
    # The helper also runs the final BM25 hybrid re-rank and strips internal
    # dedup fields before returning.
    hits, strategy_error = _finalize_candidate_hits(
        candidate_strategy=candidate_strategy,
        hits=hits,
        drawers_col=drawers_col,
        query=query,
        wing=wing,
        room=room,
        n_results=n_results,
        max_distance=max_distance,
        source_file=source_file,
        stop_words=stop_words,
        since_dt=since_dt,
        before_dt=before_dt,
    )
    if strategy_error:
        return strategy_error

    # BM25 hybrid re-rank within the final candidate set. Without trimming
    # here, ``candidate_strategy="union"`` would carry up to 4× ``n_results``
    # (vector hits + BM25 union pool) into the optional cross-encoder
    # rerank stage below. We keep the full fused pool until *after* the
    # rerank so the rerank gets to see candidates the convex/RRF blend
    # buried — that's the whole point of having a reranker. Trim happens
    # below, after the rerank stage.
    hits = _FUSION_RANKERS[fusion_mode](hits, query)

    # Optional cross-encoder rerank (techempower-org/mempalace#179).
    # Off by default — only fires when the operator has explicitly
    # enabled it via env or config.json. Composes with every
    # ``candidate_strategy`` and every ``fusion_mode`` because it
    # reorders the already-fused candidate list, never replaces fusion.
    _cer_cfg = _cross_encoder_rerank_config()
    if _cer_cfg is not None:
        from . import cross_encoder_rerank as _cer

        hits = _cer.rerank(
            query,
            hits,
            model_name=_cer_cfg["model"],
            top_n=_cer_cfg["top_n"],
        )

    # Apply the result-size contract after every reordering stage has
    # had a chance to influence the top of the list.
    hits = hits[:n_results]
    for h in hits:
        h.pop("_sort_key", None)
        h.pop("_source_file_full", None)
        h.pop("_chunk_index", None)

    # Calibrated confidence (#167): map the raw ``similarity`` to a calibrated
    # P(relevant) when a calibrator is configured. Absent calibrator → no
    # ``confidence`` key (never faked). Hits with ``similarity=None`` (BM25-only
    # or graph-source) carry no vector signal and so get no confidence.
    _cal = _load_calibrator()
    if _cal is not None:
        from .calibration import apply_calibrator

        for h in hits:
            conf = apply_calibrator(_cal, h.get("similarity"))
            if conf is not None:
                h["confidence"] = conf

    # Track whether the VECTOR path was the degraded layer, separate from
    # the final hit count. This lets the "more in scope than we could rank"
    # warning fire correctly even when the BM25 fallback happened to fill
    # the request — the vector index still underdelivered, which is the
    # real signal pointing at `mempalace repair`.
    vector_hit_count = len(hits)
    vector_underdelivered = vector_hit_count < n_results

    # Capture vector hit count before BM25 may extend hits. The scope warning
    # must fire whenever vector underdelivered — even when BM25 fills the
    # request to n_results — because vector is still the degraded layer.
    # BM25 fallback is a reliability mechanism: it fires when the distance
    # threshold is permissive (max_distance=0.0 means "no filtering") OR
    # when a vector error occurred (warnings non-empty at this point). This
    # ensures MCP callers on a drifted palace get fallback coverage even
    # though tool_search passes max_distance=1.5, without firing fallback
    # when a strict distance filter legitimately eliminates all results on
    # a working HNSW index.
    allow_fallback = (max_distance <= 0.0) or bool(warnings)
    available_in_scope, fallback_warnings = _sqlite_fallback_and_scope(
        drawers_col,
        query,
        where,
        hits,
        n_results=n_results,
        vector_underdelivered=vector_underdelivered,
        allow_fallback=allow_fallback,
        since_dt=since_dt,
        before_dt=before_dt,
    )
    warnings.extend(fallback_warnings)

    # Surface unreachable data: the scope in sqlite has more drawers than
    # the vector path could rank. Gate off vector_underdelivered (not final
    # hit count) so the warning still surfaces when BM25 fallback filled
    # the request — vector is still the degraded layer; the fallback is
    # keyword-only and doesn't have semantic recall.
    if (
        vector_underdelivered
        and available_in_scope is not None
        and available_in_scope > vector_hit_count
    ):
        warnings.append(_vector_underdelivered_warning(available_in_scope, vector_hit_count))

    result = _search_result_envelope(
        query=query,
        wing=wing,
        room=room,
        source_file=source_file,
        since=since,
        before=before,
        hits=hits,
        candidates_fetched=len(_first_or_empty(drawer_results, "documents")),
        pool_size=pool_size,
        date_window_active=date_window_active,
    )
    # Fork-only response fields upstream's envelope doesn't carry.
    result["available_in_scope"] = available_in_scope
    result["warnings"] = warnings
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Virtual line numbering — read-time grid for drawers (3.3.6).
#
# Drawers are stored verbatim on disk. The reader applies a line-number grid
# at read time so any drawer — numbered or not — can be sectioned by a closet
# pointer like ``→2026-01-18:L55-L72`` without rewriting the corpus. Pure
# functions, no I/O. Source drawer text is never mutated.
# See docs/virtual-line-numbering.md for the full design rationale.
# ─────────────────────────────────────────────────────────────────────────────


# A line is "already numbered" iff it starts with [<digits>].
_ALREADY_NUMBERED_RE = re.compile(r"^\[\d+\]")


def render_with_line_numbers(text: "str | None", start_line: int = 1) -> str:
    """Prefix each line of ``text`` with ``[N] `` for read-time grid display.

    Lines that already begin with ``[<digits>]`` pass through unchanged,
    but the counter still advances on them so callers can rely on positional
    alignment with the original line indices.

    ``None`` is treated as empty string. Pure function.
    """
    if not text:
        return ""
    out = []
    for i, line in enumerate(text.split("\n"), start=start_line):
        if _ALREADY_NUMBERED_RE.match(line):
            out.append(line)
        else:
            out.append(f"[{i}] {line}")
    return "\n".join(out)


def extract_line_range(text: str, line_start: int, line_end: int) -> str:
    """Return the 1-indexed inclusive slice ``[line_start, line_end]`` rendered with line numbers.

    This is the closet-pointer read path. A pointer like ``→2026-01-18:L55-L72``
    resolves by opening the day-drawer and calling ``extract_line_range(drawer_text, 55, 72)``.
    Out-of-bounds ranges are clamped. Invalid ranges return ``""``.
    """
    if not text:
        return ""
    if line_end < line_start:
        return ""

    lines = text.split("\n")
    effective_start = max(1, line_start)
    effective_end = min(len(lines), line_end)

    if effective_start > effective_end:
        return ""

    section = "\n".join(lines[effective_start - 1 : effective_end])
    return render_with_line_numbers(section, start_line=effective_start)
