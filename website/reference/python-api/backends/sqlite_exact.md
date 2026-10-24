# `mempalace.backends.sqlite_exact`

Source: [`mempalace/backends/sqlite_exact.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/sqlite_exact.py)

SQLite exact-vector backend for MemPalace.

This backend is intentionally simple and local-first. It is a correctness
backend, not a high-throughput ANN backend: vectors are stored as float32
blobs and query uses exact cosine distance over the matching collection.
Unfiltered query() ranks from the embedding column only (vectorized numpy),
hydrates the top-k documents afterwards, and caches the matrix on the
long-lived handle so a hub does not re-read every blob on the next search.

## Classes

### `class SQLiteExactCollection(BaseCollection)`

#### `__init__`

```python
def __init__(self, handle: _SQLiteExactHandle, collection_name: str, backend: Optional[SQLiteExactBackend] = None)
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
def query(self, **kwargs) -> QueryResult
```

#### `get`

```python
def get(self, *, ids = None, where = None, where_document = None, limit = None, offset = None, include = None) -> GetResult
```

#### `delete`

```python
def delete(self, *, ids = None, where = None)
```

#### `count`

```python
def count(self) -> int
```

#### `facet_counts`

```python
def facet_counts(self, field: str, where: Optional[dict] = None, limit: int = 1000) -> dict[str, int]
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

#### `maintenance_state`

```python
def maintenance_state(self) -> dict
```

#### `run_maintenance`

```python
def run_maintenance(self, kind: str)
```

### `class SQLiteExactBackend(BaseBackend)`

#### `__init__`

```python
def __init__(self)
```

#### `get_collection`

```python
def get_collection(self, *args, **kwargs) -> SQLiteExactCollection
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

Return True when ``path`` looks like a sqlite_exact palace.

Verifies the SQLite magic header rather than file presence alone, for
the same reason as :py:meth:`mempalace.backends.chroma.ChromaBackend.detect`:
bare ``sqlite3.connect()`` against a missing path leaves a 0-byte file
behind because the SQLite header is written on the first statement,
not on connection. The 16-byte ``SQLite format 3\x00`` magic prefix
accepts every real palace while rejecting empty / garbage files. See #1893.

The probe goes through :func:`mempalace.backends._magic.has_sqlite_magic`
and never opens a plain descriptor on the file: ``detect()`` runs on
nearly every MCP tool call, and closing a non-SQLite descriptor on the
database this backend already holds a WAL connection to drops every
POSIX lock the process owns on that inode -- including the SHARED lock
that stops an external ``close()`` from checkpointing and unlinking the
live ``-wal`` / ``-shm`` sidecars.

#### `create_collection`

```python
def create_collection(self, palace_path: str, collection_name: str) -> SQLiteExactCollection
```

#### `get_or_create_collection`

```python
def get_or_create_collection(self, palace_path: str, collection_name: str)
```

#### `delete_collection`

```python
def delete_collection(self, palace_path: str, collection_name: str) -> None
```

## Functions

### `sqlite_wing_room_counts`

```python
def sqlite_wing_room_counts(palace_path: str, collection_name: str) -> Optional[tuple[int, dict[str, dict[str, int]]]]
```

Tally drawers by wing/room from ``sqlite_exact.sqlite3`` without paging.

Returns ``(total, &#123;wing: &#123;room: count}})`` or ``None`` when the read
cannot be trusted. ``None``/missing wing-or-room values are stored as
``"?"`` so ``mcp_server._sqlite_taxonomy`` can map them to ``"unknown"``.

### `sqlite_wing_source_counts`

```python
def sqlite_wing_source_counts(palace_path: str, collection_name: str) -> Optional[list[tuple]]
```

Grouped ``(wing, source_file, n)`` for transcript-mined drawers, or ``None``.

Scoped to ``collection_name``: drawers and closets share the
``documents`` table, and counting both would double every project.
Only rows whose ``source_file`` sits under a Claude Code projects
directory or a Codex sessions directory are returned; that is what
``mempalace audit`` needs to see whether one wing mixes several
projects, and what ``wings split`` plans over.

### `sqlite_room_wing_hall_counts`

```python
def sqlite_room_wing_hall_counts(palace_path: str, collection_name: str) -> Optional[list[tuple]]
```

Grouped ``(room, wing, hall, n, last_date)`` rows, or ``None``.

``last_date`` is the newest ``date`` metadata value in the group — enough
for ``find_tunnels``' ``recent`` field without paging every drawer.
