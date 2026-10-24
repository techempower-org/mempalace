# Loaded into mempalace.searcher via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.searcher":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.searcher")


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
        _print_also_in(hit)
        print(f"      Match:  bm25={bm25}  (vector disabled)")
        print()
        for line in (hit.get("text", "") or "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()


def _cli_hit_source(hit: dict):
    return (hit.get("metadata") or {}).get("source_file")


def _cli_hit_ref(hit: dict) -> dict:
    meta = hit.get("metadata") or {}
    source = meta.get("source_file")
    return {
        "source_file": Path(source).name if source else "?",
        "source_path": source,
        "wing": meta.get("wing"),
        "room": meta.get("room"),
        "chunk_index": meta.get("chunk_index"),
    }


def _print_also_in(hit: dict) -> None:
    """Name the other files holding this exact passage (folded copies)."""
    copies = hit.get("also_in") or []
    if not copies:
        return
    names = ", ".join(c.get("source_file") or "?" for c in copies[:5])
    more = f" and {len(copies) - 5} more" if len(copies) > 5 else ""
    print(f"      Also in: {names}{more}")


def _cli_ranked_hits(
    col, query, *, fetch, n_results, where, wing, room, window, metric, stop_words
):
    """``(hits, fetched)``: the top ``fetch`` drawers, re-ranked and folded.

    ``hits`` is not cut to ``n_results``; the caller widens ``fetch`` when
    folded copies leave it short.
    """
    try:
        kwargs = {
            "query_texts": [query],
            # The window is a post-filter (ChromaDB can't range-compare
            # string metadata), and copies folded below free their slots,
            # so fetch a pool and trim back after both.
            "n_results": fetch,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        results = _query_drawers_with_filter_fallback(col, kwargs, query, n_results, wing, room)
    except Exception as e:
        if _model_error_class(e) is not None:
            # The configured embedding function could not be built (C): the
            # message names the fix, so print it plainly.
            import sys

            print(f"mempalace: {e}", file=sys.stderr)
            raise SearchError(str(e)) from e
        print(f"\n  Search error: {e}")
        raise SearchError(f"Search error: {e}") from e

    docs = _first_or_empty(results, "documents")
    metas = _first_or_empty(results, "metadatas")
    dists = _first_or_empty(results, "distances")
    fetched = len(docs)
    if window is not None:
        # Keep the whole in-window pool: the hybrid re-rank must see every
        # survivor before the display cut, or a BM25-strong drawer deep in the
        # pool could never surface.
        kept = [
            (doc, meta, dist)
            for doc, meta, dist in zip(docs, metas, dists)
            if filed_at_in_window((meta or {}).get("filed_at"), *window)
        ]
        docs = [k[0] for k in kept]
        metas = [k[1] for k in kept]
        dists = [k[2] for k in kept]

    # Pure-cosine retrieval on the CLI path was missing lexical matches: a
    # drawer whose text contains every query term can still score distance
    # >= 1.0 when it is a mechanical artifact (directory listing, diff, log
    # fragment). Hybridize with BM25 as `mempalace_search` does.
    hits = [
        {"text": doc or "", "distance": float(dist), "metadata": meta or {}}
        for doc, meta, dist in zip(docs, metas, dists)
    ]
    vector_weight, bm25_weight = _resolve_hybrid_rank_weights()
    hits = _hybrid_rank(
        hits,
        query,
        vector_weight=vector_weight,
        bm25_weight=bm25_weight,
        metric=metric,
        stop_words=stop_words,
    )
    return _fold_copies_across_sources(hits, _cli_hit_source, _cli_hit_ref), fetched


def search(*args, **kwargs):
    """The CLI search (see :func:`_search_delegating`). A model error raised on the way (the fork's pre-open
    probes open the collection outside upstream's ``_open_collection_or_explain``) prints upstream's single
    ``mempalace: <message>`` line on stderr and raises :class:`SearchError`, never a traceback."""
    from ..embedding import _model_error_class

    try:
        return _search_delegating(*args, **kwargs)
    except SearchError:
        raise
    except Exception as e:
        if _model_error_class(e) is None:
            raise
        import sys

        print(f"mempalace: {e}", file=sys.stderr)
        raise SearchError(str(e)) from e


def _search_delegating(  # noqa: C901 — fork delegation + upstream window fence on one CLI entry
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    tags: list = None,
    n_results: int = 5,
    since: str = None,
    before: str = None,
    collection=None,
    include_media: bool = False,
    query_task: str = "search",
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
    if include_media or query_task != "search":
        result = search_memories(
            query,
            palace_path,
            wing=wing,
            room=room,
            n_results=n_results,
            since=since,
            before=before,
            include_media=include_media,
            query_task=query_task,
        )
        if result.get("error"):
            if result.get("error_class"):
                # A model error: the details carry the explanation and the fix.
                import sys

                print(f"mempalace: {result['details']}", file=sys.stderr)
            else:
                print(result["error"])
            raise SearchError(result["error"])
        for index, hit in enumerate(result["results"], 1):
            kind = hit.get("media_type") or "text/code"
            print(f"{index}. [{kind}] {hit['source_file']}  similarity={hit['similarity']:.3f}")
            print(f"   {hit['source_path']}")
            if hit.get("result_type") == "asset":
                if not hit.get("available"):
                    print("   File is missing; the indexed reference is retained.")
                if hit.get("drawer_id"):
                    print(f"   Related drawer: {hit['drawer_id']}")
            else:
                print(hit["text"])
        if not result["results"]:
            print("No results found.")
        return
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
            col = _open_collection_or_explain(palace_path, opener=get_collection, read_only=True)
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
            # read_only (upstream 5a2abffe): a CLI search must not take a writer lock.
            get_collection(palace_path, create=False, read_only=True)
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
        if result.get("error_class"):
            # A model error (upstream C): one ``mempalace: <details>`` line on stderr, as upstream's CLI prints it.
            import sys

            print(f"mempalace: {result.get('details') or result['error']}", file=sys.stderr)
            raise SearchError(result["error"])
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
        _print_also_in(hit)
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
