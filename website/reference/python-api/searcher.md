# `mempalace.searcher`

Source: [`mempalace/searcher/__init__.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/searcher/__init__.py)

searcher.py — Find anything. Exact words.

Hybrid search: BM25 keyword matching + vector semantic similarity. The
drawer query is the floor — always runs — and closet hits add a rank-based
boost when they agree. Closets are a ranking *signal*, never a gate, so
weak closets (regex extraction on narrative content) can only help, never
hide drawers the direct path would have found.

## Classes

### `class SearchError(Exception)`

Raised when search cannot proceed (e.g. no palace found).
