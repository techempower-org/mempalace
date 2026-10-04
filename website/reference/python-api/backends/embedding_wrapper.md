# `mempalace.backends.embedding_wrapper`

Source: [`mempalace/backends/embedding_wrapper.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/embedding_wrapper.py)

Core-side embedding adapter for explicit-vector backends.

## Classes

### `class EmbeddingCollection(BaseCollection)`

Wrap a collection that requires explicit vectors.

Backends opt in with the ``requires_explicit_embeddings`` capability.
Core callers can keep using ``documents=`` and ``query_texts=``; this
wrapper computes vectors locally before delegating to the backend.

#### `__init__`

```python
def __init__(self, inner: BaseCollection)
```

#### `distance_metric`

```python
def distance_metric(self) -> str
```

#### `get_stored_embedder_identity`

```python
def get_stored_embedder_identity(self)
```

#### `set_embedder_identity`

```python
def set_embedder_identity(self, identity) -> None
```

#### `effective_embedder_identity`

```python
def effective_embedder_identity(self)
```

#### `maintenance_state`

```python
def maintenance_state(self) -> dict
```

#### `run_maintenance`

```python
def run_maintenance(self, kind: str)
```

#### `add`

```python
def add(self, *, documents, ids, metadatas = None, embeddings = None)
```

#### `upsert`

```python
def upsert(self, *, documents, ids, metadatas = None, embeddings = None)
```

#### `query`

```python
def query(self, *, query_texts: Optional[list[str] | str] = None, query_embeddings: Optional[list[list[float]]] = None, n_results: int = 10, where: Optional[dict] = None, where_document: Optional[dict] = None, include: Optional[list[str]] = None)
```

#### `get`

```python
def get(self, *, ids = None, where = None, where_document = None, limit = None, offset = None, include = None)
```

#### `delete`

```python
def delete(self, *, ids = None, where = None)
```

#### `count`

```python
def count(self) -> int
```

#### `estimated_count`

```python
def estimated_count(self) -> int
```

#### `close`

```python
def close(self) -> None
```

#### `health`

```python
def health(self)
```

#### `lexical_search`

```python
def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None)
```

#### `facet_counts`

```python
def facet_counts(self, field: str, where: Optional[dict] = None, limit: int = 1000) -> dict[str, int]
```

#### `count_where`

```python
def count_where(self, where: Optional[dict] = None) -> Optional[int]
```

#### `get_all_metadata`

```python
def get_all_metadata(self, where: Optional[dict] = None) -> list[dict]
```

#### `update`

```python
def update(self, *, ids, documents = None, metadatas = None, embeddings = None)
```

#### `rename_wing`

```python
def rename_wing(self, *, from_wing: str, to_wing: str, batch_size: int = 500) -> dict
```
