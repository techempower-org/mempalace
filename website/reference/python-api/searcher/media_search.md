# `mempalace.searcher.media_search`

Source: [`mempalace/searcher/media_search.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/searcher/media_search.py)

Opt-in retrieval across compatible text/code and local media vectors.

## Functions

### `search_with_media`

```python
def search_with_media(query: str, palace_path: str, *, include_media: bool = False, query_task: str = 'search', n_results: int = 5, wing = None, room = None, source_file = None, since = None, before = None, max_distance: float = 0.0, collection_name = None) -> dict
```

Merge candidates by cosine distance in one verified embedding space.

This experimental path uses vector ranking throughout. Drawer-only
default search retains its existing hybrid ranking and result schema.
Missing asset files remain searchable references with ``available=False``.
