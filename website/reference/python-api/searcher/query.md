# `mempalace.searcher.query`

Source: [`mempalace/searcher/query.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/searcher/query.py)

## Functions

### `search_memories`

```python
def search_memories(query: str, palace_path: str, wing: str = None, room: str = None, tags: list = None, source_file: str = None, since: str = None, before: str = None, n_results: int = 5, max_distance: float = 0.0, vector_disabled: bool = False, candidate_strategy: str = 'vector', fusion_mode: str = 'convex', collection_name: str = None, lang: Optional[str] = None) -> dict
```

Programmatic search — returns a dict instead of printing.

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
