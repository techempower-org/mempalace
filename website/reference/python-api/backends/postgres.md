# `mempalace.backends.postgres`

Source: [`mempalace/backends/postgres.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/postgres.py)

Optional PostgreSQL-backed MemPalace storage backend.

The backend prefers ``pg_sorted_heap`` when available and falls back to
``pgvector``. Optional dependencies are imported lazily so the default Chroma
install remains zero-config.

## Classes

### `class LookupIndexesBusy(RuntimeError)`

Another ensure_lookup_indexes run holds this table's advisory lock.

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

#### `count_where`

```python
def count_where(self, where: Optional[dict] = None) -> Optional[int]
```

Exact count of rows matching ``where``, in one statement.

Uses ``_where_to_sql``, the same translation ``get()`` uses, so this
equals the length of a full paged ``get(where=where)``. ``wing`` and
``room`` filters are served by their btree indexes.

#### `estimated_count`

```python
def estimated_count(self) -> int
```

#### `manifest_get`

```python
def manifest_get(self, extract_mode: str, source_files: Optional[list] = None) -> dict
```

``&#123;source_file: &#123;"source_mtime", "chunk_total", "normalize_version", "convo_chunker_version"}}``.

#### `manifest_put`

```python
def manifest_put(self, source_file: str, extract_mode: str, source_mtime: Optional[float], chunk_total: int, normalize_version: int, convo_chunker_version: Optional[int] = None) -> None
```

#### `close`

```python
def close(self) -> None
```

#### `has_source_file_index`

```python
def has_source_file_index(self) -> bool
```

True when a valid, non-partial btree leads with ``metadata->>'source_file'``.

That index is what makes a per-file lookup one indexed query at any list
size (#567). It is built by ``_create_table`` for a new palace, but an
older palace only has it if an operator built it, so ask ``pg_index``
rather than assume it from the backend type. Only True is cached: a
palace whose index is built later is found on the next check.

#### `ensure_lookup_indexes`

```python
def ensure_lookup_indexes(self, *, dry_run: bool = False, plan_check_min_pages: int = 1000) -> list[dict]
```

Build every missing lookup index (LOOKUP_INDEX_KEYS) on an existing palace, online.

All on a side connection with ``statement_timeout`` lifted for that session only (the role's own timeout
would cancel the build), holding a per-table advisory lock for the whole run, so two runs never drop or build
under each other; every state read of a run that changes anything happens under it (a dry run reads without
it). Raises LookupIndexesBusy when another run holds it. The lock protects lookup-indexes runs from each
other, not from an index someone builds by hand at the same moment. The table is the one the collection's
own statements reach through search_path, resolved once to its oid.

For each key without a valid index: drop an INVALID leftover of our own build under the lookup name, ``CREATE
INDEX CONCURRENTLY``, confirm it is valid. A failed build is reported (state "failed", ``error``,
``leftover``), not raised. Then ``ANALYZE`` the table, on every run that is not a dry run: an expression index
has no statistics until then, and the planner keeps its default guess and the old plan (2026-11-21: both
judged-parent indexes went unused until ANALYZE), so a palace whose indexes exist without statistics is
repaired by a rerun. Then ``EXPLAIN (FORMAT JSON)`` (nothing executes) of the statement get() sends for the
judged-parent lookup (on parent_drawer_id and parent_entry_id; source_file's lookups have other shapes, so its
plan is not judged with this one) records whether its plan uses one of the key's valid indexes, twice: the
per-value plan (plan_uses_index) and the generic plan a prepared statement can be given
(generic_plan_uses_index). The key is inlined in that statement (_field_filter_to_sql), which is what lets the
generic plan use the index.

Returns one dict per key: key, index, state (present | built | failed | missing in a dry run), and, only
where they apply:
- valid_indexes: on present and built rows;
- error and leftover: on failed rows (leftover only when a leftover or a name conflict was found);
- plan_uses_index: on present and built rows of a run that is not a dry run. None with a plan_note when the
  plan is not judged: "no judged-parent lookup on this key" (source_file), or "small table" below
  ``plan_check_min_pages``, where a scan is cheaper than any index;
- generic_plan_uses_index: only where plan_uses_index is True or False.
Read the optional ones with ``row.get()``.

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

## Functions

### `lookup_index_name`

```python
def lookup_index_name(table_name: str, key: str) -> str
```

source_file keeps its original name; the others are ``&lt;table>_meta_&lt;key>_idx``. A name longer than 63 bytes
becomes ``&lt;prefix>_&lt;hash of the full name>_idx``, so long table names still get distinct names per key.
