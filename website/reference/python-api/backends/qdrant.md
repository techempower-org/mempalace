# `mempalace.backends.qdrant`

Source: [`mempalace/backends/qdrant.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/qdrant.py)

Qdrant REST backend for MemPalace.

Qdrant is an opt-in external-service backend. Chroma remains the default; this
adapter only runs when the user explicitly selects ``qdrant`` via config, env,
or CLI/MCP flag. Embeddings are still produced locally by MemPalace through the
core embedding wrapper before vectors are sent to Qdrant.

## Classes

### `class QdrantCollection(BaseCollection)`

#### `__init__`

```python
def __init__(self, *, backend: 'QdrantBackend', client: _QdrantRESTClient, config: _QdrantConfig, palace: PalaceRef, collection_name: str, remote_collection: str)
```

#### `get_stored_embedder_identity`

```python
def get_stored_embedder_identity(self)
```

#### `set_embedder_identity`

```python
def set_embedder_identity(self, identity) -> None
```

#### `add`

```python
def add(self, *, documents, ids, metadatas = None, embeddings = None)
```

#### `upsert`

```python
def upsert(self, *, documents, ids, metadatas = None, embeddings = None)
```

#### `update`

```python
def update(self, *, ids, documents = None, metadatas = None, embeddings = None)
```

#### `query`

```python
def query(self, *, query_texts = None, query_embeddings = None, n_results = 10, where = None, where_document = None, include = None) -> QueryResult
```

#### `get`

```python
def get(self, *, ids = None, where = None, where_document = None, limit = None, offset = None, include = None) -> GetResult
```

#### `get_all_metadata`

```python
def get_all_metadata(self, where: Optional[dict] = None) -> list[dict]
```

Return every matching record's metadata in one cursor pass (#1796).

Overrides the default offset-paginated implementation, which would
call self.get(limit=, offset=) in a loop -- and since self.get() is
backed by a full _scroll_all() materialization, each page of that
loop would re-walk the entire collection from the start just to
discard everything outside its slice (O(n^2) over collection size).

Delegates to self._rows(), the same single-scroll-plus-local-filter
helper that backs get()/delete(). With ids=None and
where_document=None, _rows() reduces to exactly one _scroll_all()
pass followed by an unconditional _matches_where() re-check on every
row -- the same filter logic get(), delete(), and lexical_search()
already use, so this can't independently drift from those call
sites. (Maintainer review on #1832: avoid duplicating the filter
dance inline.)

#### `get_all_rows`

```python
def get_all_rows(self, where: Optional[dict] = None, include: Optional[list[str]] = None) -> GetResult
```

Every matching row's id plus the requested fields in one cursor pass.

Same reasoning as :meth:`get_all_metadata` (#1796): the default
implementation would call ``get(limit=, offset=)`` in a loop, and each
of those calls re-walks the collection from the start (#2452 measured
275 s for ``list_drawers`` on 126k drawers). ``_rows()`` already has
every id in hand, so hand them back instead of discarding them.

#### `facet_counts`

```python
def facet_counts(self, field: str, where: Optional[dict] = None, limit: int = 1000) -> dict[str, int]
```

#### `delete`

```python
def delete(self, *, ids = None, where = None)
```

#### `count`

```python
def count(self) -> int
```

#### `lexical_search`

```python
def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None)
```

#### `close`

```python
def close(self) -> None
```

#### `health`

```python
def health(self) -> HealthStatus
```

### `class QdrantBackend(BaseBackend)`

#### `__init__`

```python
def __init__(self)
```

#### `get_collection`

```python
def get_collection(self, *args, **kwargs) -> QdrantCollection
```

#### `close_palace`

```python
def close_palace(self, palace: PalaceRef | str) -> None
```

#### `close`

```python
def close(self) -> None
```

#### `health`

```python
def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus
```

#### `detect`

```python
def detect(cls, path: str) -> bool
```

#### `create_collection`

```python
def create_collection(self, palace_path: str, collection_name: str) -> QdrantCollection
```

#### `get_or_create_collection`

```python
def get_or_create_collection(self, palace_path: str, collection_name: str)
```

#### `delete_collection`

```python
def delete_collection(self, palace_path: str, collection_name: str) -> None
```
