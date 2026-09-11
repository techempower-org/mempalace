# `mempalace.backends.postgres`

Source: [`mempalace/backends/postgres.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/postgres.py)

Optional PostgreSQL-backed MemPalace storage backend.

The backend prefers ``pg_sorted_heap`` when available and falls back to
``pgvector``. Optional dependencies are imported lazily so the default Chroma
install remains zero-config.

## Classes

### `class PostgresCollection(BaseCollection)`

PostgreSQL collection adapter implementing the RFC 001 collection contract.

#### `__init__`

```python
def __init__(self, dsn: str, table_name: str = 'mempalace_drawers')
```

#### `add`

```python
def add(self, *, documents: list[str], ids: list[str], metadatas: Optional[list[dict[str, Any]]] = None, embeddings: Optional[list[list[float]]] = None) -> None
```

#### `upsert`

```python
def upsert(self, *, documents: list[str], ids: list[str], metadatas: Optional[list[dict[str, Any]]] = None, embeddings: Optional[list[list[float]]] = None) -> None
```

#### `set_kg_writethrough_batch`

```python
def set_kg_writethrough_batch(self, hook) -> None
```

Register a callable invoked ONCE per ``_insert_rows`` batch.

Hook signature: ``hook(drawers: list[dict])``, each dict carrying
``drawer_id`` / ``document`` / ``metadata`` — the same values the
per-drawer hook receives, handed over together.

Takes precedence over :meth:`set_kg_writethrough`; only one of the
two ever runs, so registering both does not write the graph twice.
Set to ``None`` to fall back to the per-drawer hook.

This exists because the per-drawer contract made batching
impossible: the hook could not know a batch existed, so every
mention committed on its own (palace-daemon#265). Exceptions are
caught and logged exactly as the per-drawer hook's are — the drawer
rows have already committed by the time the hook runs, and KG
enrichment is opportunistic, not mandatory.

#### `set_kg_writethrough`

```python
def set_kg_writethrough(self, hook) -> None
```

Register a callable invoked after each successful drawer write.

Hook signature: ``hook(drawer_id: str, document: str, metadata: dict)``.
Called once per drawer in ``_insert_rows`` after the row commits.
Exceptions inside the hook are caught + logged; they never propagate.

Set to ``None`` to disable. The default (no hook registered) is
zero overhead — vector-only write path matches the pre-Phase-2
behavior byte-identically.

Typical use: configure an entity-extracting hook that populates
the AGE KG. See ``mempalace.kg_writethrough.make_age_writethrough``
for the canonical implementation.

#### `query`

```python
def query(self, *, query_texts: Optional[list[str]] = None, query_embeddings: Optional[list[list[float]]] = None, n_results: int = 10, where: Optional[dict] = None, where_document: Optional[dict] = None, include: Optional[list[str]] = None) -> QueryResult
```

#### `get`

```python
def get(self, *, ids: Optional[list[str]] = None, where: Optional[dict] = None, where_document: Optional[dict] = None, limit: Optional[int] = None, offset: Optional[int] = None, include: Optional[list[str]] = None) -> GetResult
```

#### `delete`

```python
def delete(self, *, ids: Optional[list[str]] = None, where: Optional[dict] = None) -> None
```

#### `set_kg_deletethrough`

```python
def set_kg_deletethrough(self, hook) -> None
```

Register a callable invoked after each successful drawer delete.

Hook signature: ``hook(drawer_ids: list[str])``. Called once per
``delete`` call with the list of ids actually removed (resolved
before the DELETE so ``where``-based deletes also propagate).
Exceptions inside the hook are caught + logged.

Set to ``None`` to disable. Pair with ``set_kg_writethrough`` —
running one without the other drifts the graph out of sync with
the relational table.

#### `update`

```python
def update(self, *, ids: list[str], documents: Optional[list[str]] = None, metadatas: Optional[list[dict]] = None, embeddings: Optional[list[list[float]]] = None) -> None
```

#### `rename_wing`

```python
def rename_wing(self, *, from_wing: str, to_wing: str, batch_size: int = 500) -> dict
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

### `class PostgresBackend(BaseBackend)`

Factory for optional PostgreSQL collections.

#### `__init__`

```python
def __init__(self, dsn: Optional[str] = None)
```

#### `get_collection`

```python
def get_collection(self, *, palace: PalaceRef, collection_name: str, create: bool = False, options: Optional[dict] = None) -> PostgresCollection
```

#### `close_palace`

```python
def close_palace(self, palace: PalaceRef) -> None
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
