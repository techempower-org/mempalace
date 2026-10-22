# `mempalace.searcher.filters`

Source: [`mempalace/searcher/filters.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/searcher/filters.py)

## Functions

### `build_where_filter`

```python
def build_where_filter(wing: str = None, room: str = None, tags: list = None, source_file: str = None) -> dict
```

Build ChromaDB-style where filter for wing/room/tag/source_file filtering.

``tags`` requires drawers to carry EVERY listed tag (AND logic). On the
postgres backend the filter is pushed down via the ``$contains_all``
JSONB operator; for chroma it's stripped here and applied as a
post-filter by the caller (see ``search_memories``). ChromaDB needs a
``$and`` only when ≥2 clauses are present; a single clause is returned
bare and zero clauses yield an empty filter (#1815).
