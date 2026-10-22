# `mempalace.searcher.cli_search`

Source: [`mempalace/searcher/cli_search.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/searcher/cli_search.py)

## Functions

### `search`

```python
def search(query: str, palace_path: str, wing: str = None, room: str = None, tags: list = None, n_results: int = 5, since: str = None, before: str = None, collection = None)
```

Search the palace. Returns verbatim drawer content.
Optionally filter by wing (project) or room (aspect), and/or narrow to
drawers whose ``filed_at`` falls in the ``[since, before)`` window —
same semantics as ``search_memories``/``list_drawers`` (#1128/#463).
Optionally filter by wing (project) or room (aspect).

Delegates to ``search_memories`` so CLI and MCP callers share the same
hybrid ranking, sqlite-BM25 fallback, and scope-aware warnings.
