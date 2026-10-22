# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


def _result_date_fields(meta: dict) -> dict:
    """Expose stored dates and disclose the legacy authored-at fallback.

    ``created_at`` and ``authored_at`` retain their historical values;
    the source identifies the field supplying ``authored_at``, not a
    guarantee of authorship. Content-date provenance is only reported
    when stored, never reconstructed from a legacy drawer's path or text.
    """
    filed_at = meta.get("filed_at", "unknown")
    authored_at = meta.get("authored_at", filed_at)
    authored_at_source = "authored_at" if "authored_at" in meta else "filed_at"
    if not authored_at or authored_at == "unknown":
        authored_at_source = "unknown"
    content_date = meta.get("content_date")
    content_date_source = meta.get("content_date_source") or "unknown"
    if not content_date or content_date == "unknown":
        content_date_source = "unknown"
    return {
        "created_at": filed_at,
        "filed_at": filed_at,
        "authored_at": authored_at,
        "authored_at_source": authored_at_source,
        "content_date": content_date,
        "content_date_source": content_date_source,
    }


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
        # A backend that can count a filtered scope in one statement says so.
        # Only a non-negative int is trusted; None (can't), an error, or any
        # other answer falls through to paging. Paging read every drawer in
        # the scope: 6.9 s of a 7.0 s postgres search for a 57K wing (measured 2026-10-04).
        try:
            direct = drawers_col.count_where(where)
        except Exception:
            direct = None
        if type(direct) is int and direct >= 0:
            return direct
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
                **_result_date_fields(meta),
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
        from ..config import get_configured_collection_name

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
        conn = connect_sqlite_read(db_path)
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
    from ..tags import metadata_matches_all_tags, normalise_tags

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
                **_result_date_fields(meta),
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
        from ..config import MempalaceConfig

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
                    **_result_date_fields(meta),
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
        from ..config import MempalaceConfig

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
