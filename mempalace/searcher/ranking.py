# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


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


def _resolve_hybrid_rank_weights() -> tuple:
    """Resolve the vector/BM25 weights for the hybrid re-rank (#2298).

    Reads ``MempalaceConfig().hybrid_rank_vector_weight`` /
    ``.hybrid_rank_bm25_weight`` — which themselves resolve env var
    (``MEMPALACE_HYBRID_VECTOR_WEIGHT`` / ``MEMPALACE_HYBRID_BM25_WEIGHT``)
    > ``config.json`` > the built-in defaults (0.6 / 0.4). Any failure — a
    config that cannot be read or a setting that failed validation — falls
    back to the same defaults, so a bad config value never takes a search
    down. Leaving the settings unset reproduces the previously-hardcoded
    blend byte-for-byte.
    """
    try:
        cfg = MempalaceConfig()
        vector_weight = float(cfg.hybrid_rank_vector_weight)
        bm25_weight = float(cfg.hybrid_rank_bm25_weight)
        return vector_weight, bm25_weight
    except Exception:
        logger.debug("hybrid rank weight resolution failed, using defaults", exc_info=True)
        return 0.6, 0.4


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
        from ..backends.embedding_wrapper import _embed_texts

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

    from ..rrf import DEFAULT_K, rrf_fuse

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
