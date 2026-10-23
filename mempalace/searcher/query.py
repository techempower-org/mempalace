# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def _open_search_collection(palace_path: str, collection_name: str):
    try:
        return get_collection(
            palace_path, collection_name=collection_name, create=False, read_only=True
        ), None
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
    from ..config import MempalaceConfig

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

    from ..calibration import Calibrator

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
    from ..config import MempalaceConfig

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


# Folding copies can leave fewer distinct passages than asked for while the
# vector pool was full, so more candidates exist. The search then repeats with
# a pool four times larger, up to _MAX_POOL_SCALE times the normal pool and
# never past _MAX_FOLD_POOL candidates.
_MAX_POOL_SCALE = 16
_MAX_FOLD_POOL = 500


def _scaled_pool_limits(limits: tuple, scale: int) -> tuple:
    if scale <= 1:
        return limits
    pool_size, pre_enrichment_limit = limits
    return (
        min(pool_size * scale, max(_MAX_FOLD_POOL, pool_size)),
        min(pre_enrichment_limit * scale, max(_MAX_FOLD_POOL, pre_enrichment_limit)),
    )


def _copies_left_results_short(hits, n_results, candidates_fetched, pool_size) -> bool:
    """Whether folded copies left the page short while the pool was full."""
    return (
        len(hits) < n_results
        and candidates_fetched >= pool_size
        and any(hit.get("also_in") for hit in hits)
    )


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
    _pool_scale: int = 1,
) -> dict:
    """Programmatic search — returns a dict instead of printing.

    Used by the MCP server and other callers that need data.

    Hybrid search: BM25 keyword matching + vector semantic similarity.
    The drawer query is the floor — always runs — and closet hits add a
    rank-based boost when they agree.

    Each hit exposes ``filed_at`` (also retained as ``created_at``), plus
    the legacy ``authored_at`` value and ``authored_at_source`` indicating
    whether it came from stored ``authored_at``, the ``filed_at`` fallback,
    or is ``unknown``. ``content_date`` is a separate inferred date with
    ``content_date_source`` (filename/frontmatter/body/mtime when recorded,
    otherwise ``unknown``). Neither inference nor filing proves authorship.

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
    pool_size, pre_enrichment_limit = _scaled_pool_limits(
        _candidate_pool_limits(candidate_strategy, n_results, date_window_active),
        _pool_scale,
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
    from .. import multi_encoder as _mc

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
        closets_col = get_closets_collection(palace_path, create=False, read_only=True)
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
            **_result_date_fields(meta),
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
        from .. import cross_encoder_rerank as _cer

        hits = _cer.rerank(
            query,
            hits,
            model_name=_cer_cfg["model"],
            top_n=_cer_cfg["top_n"],
        )

    # The candidates arrive folded (upstream #2593: _finalize_candidate_hits lists a passage's other copies under
    # ``also_in``). Counted with those copies, this is what the vector pool actually ranked.
    unfolded = len(hits) + sum(len(h.get("also_in") or ()) for h in hits)
    # Folding can leave the page short while the vector pool came back full: widen the pool and search again
    # (upstream #2593), before the sqlite fallback below can fill those slots with weaker keyword matches.
    candidates_fetched = len(_first_or_empty(drawer_results, "documents"))
    copies_left_short = _copies_left_results_short(hits, n_results, candidates_fetched, pool_size)
    if copies_left_short and _pool_scale < _MAX_POOL_SCALE and pool_size < _MAX_FOLD_POOL:
        return search_memories(
            query,
            palace_path,
            wing=wing,
            room=room,
            tags=tags,
            source_file=source_file,
            since=since,
            before=before,
            n_results=n_results,
            max_distance=max_distance,
            vector_disabled=vector_disabled,
            candidate_strategy=candidate_strategy,
            fusion_mode=fusion_mode,
            collection_name=collection_name,
            lang=lang,
            _pool_scale=_pool_scale * 4,
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
        from ..calibration import apply_calibrator

        for h in hits:
            conf = apply_calibrator(_cal, h.get("similarity"))
            if conf is not None:
                h["confidence"] = conf

    # Track whether the VECTOR path was the degraded layer, separate from
    # the final hit count. This lets the "more in scope than we could rank"
    # warning fire correctly even when the BM25 fallback happened to fill
    # the request — the vector index still underdelivered, which is the
    # real signal pointing at `mempalace repair`.
    # Counted before the fold: copies folded into one hit are not the vector index underdelivering.
    vector_hit_count = min(unfolded, n_results)
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
        candidates_fetched=candidates_fetched,
        pool_size=pool_size,
        date_window_active=date_window_active,
    )
    # Fork-only response fields upstream's envelope doesn't carry.
    result["available_in_scope"] = available_in_scope
    result["warnings"] = warnings
    if copies_left_short:                                        # could not widen any further
        result["distinct_results_truncated"] = True
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
