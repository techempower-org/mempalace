# `mempalace.backends.chroma`

Source: [`mempalace/backends/chroma.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/chroma.py)

ChromaDB-backed MemPalace storage backend (RFC 001 reference implementation).

## Classes

### `class ChromaCollection(BaseCollection)`

Thin adapter translating ChromaDB dict returns into typed results.

When ``palace_path`` is set, all write methods (``add``, ``upsert``,
``update``, ``delete``) acquire ``mine_palace_lock(palace_path)`` for the
duration of the underlying chromadb call. This serializes MCP and other
direct-backend writers against ``mempalace mine`` and against each other,
closing the race between concurrent writers that triggers ChromaDB's
multi-threaded HNSW corruption (#974/#965).

The lock is the same primitive used by ``miner.mine()`` so re-entrant
acquisition from inside the mine pipeline (mine -> _mine_body ->
collection.upsert) is short-circuited by the per-thread guard inside
``mine_palace_lock`` — no self-deadlock.

``palace_path=None`` disables the wrapping, preserving the legacy
no-lock behaviour for callers that construct a ``ChromaCollection``
directly without going through ``ChromaBackend``.

#### `__init__`

```python
def __init__(self, collection, palace_path: Optional[str] = None, backend = None)
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

#### `delete`

```python
def delete(self, *, ids = None, where = None)
```

#### `count`

```python
def count(self)
```

#### `lexical_search`

```python
def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None) -> LexicalResult
```

Return lexical BM25 candidates for this collection.

This is the normal healthy-Chroma implementation behind the optional
backend capability. The HNSW-disabled fallback in ``searcher.py`` still
reads ``chroma.sqlite3`` directly and remains Chroma-only.

#### `metadata`

```python
def metadata(self) -> dict
```

Pass-through to the underlying ChromaDB collection's metadata.

Used by the searcher to detect legacy palaces that were created
without ``hnsw:space=cosine`` and therefore silently use L2
distance, which breaks cosine-based similarity interpretation.
Returns ``&#123;}`` when metadata is absent so callers can do a plain
``.get("hnsw:space")`` without None-checks.

#### `distance_metric`

```python
def distance_metric(self) -> str
```

Report this collection's actual space from ``hnsw:space``.

MemPalace sets ``hnsw:space=cosine`` on every creation path, so a
healthy palace reports ``"cosine"``. When the key is absent, empty, or
an unrecognized value, the collection is genuinely using Chroma's HNSW
default — **L2** (Euclidean) — because cosine was never set on it. We
report ``"l2"`` in that case so core ranking maps the distances
correctly; reporting ``"cosine"`` here would reintroduce the
floor-every-result-to-zero misranking this property exists to fix.

#### `get_stored_embedder_identity`

```python
def get_stored_embedder_identity(self)
```

#### `set_embedder_identity`

```python
def set_embedder_identity(self, identity) -> None
```

### `class ChromaBackend(BaseBackend)`

MemPalace's default ChromaDB backend.

Maintains two caches:

* ``self._clients`` — ``palace_path -> PersistentClient`` for callers
  using the ``PalaceRef`` / :meth:`get_collection` path.
* An inode+mtime freshness check absorbed from ``mcp_server._get_client``
  (merged via #757) ensuring a palace rebuild on disk is detected on the
  next :meth:`get_collection` call.

#### `__init__`

```python
def __init__(self)
```

#### `make_client`

```python
def make_client(palace_path: str)
```

Create a fresh ``PersistentClient`` (runs pre-open safety pass first).

Deprecated-ish: exposed for legacy long-lived callers that manage their
own client cache. New code should obtain a collection through
:meth:`get_collection` which manages caching internally.

Quarantines HNSW segments on first open and after any detected
disk change. See :attr:`_quarantined_paths` for the gate logic.

#### `backend_version`

```python
def backend_version() -> str
```

Return the installed chromadb package version string.

#### `get_collection`

```python
def get_collection(self, *args, **kwargs) -> ChromaCollection
```

Obtain a collection for a palace.

Supports two calling conventions during the RFC 001 transition:

* New (preferred): ``get_collection(palace=PalaceRef, collection_name=...,
  create=False, options=None)``.
* Legacy: ``get_collection(palace_path, collection_name, create=False)``
  — still used by callers not yet migrated.

#### `close_palace`

```python
def close_palace(self, palace) -> None
```

Drop cached handles for ``palace`` and release its SQLite file lock.

Accepts ``PalaceRef`` or legacy path str. chromadb's rust-side file
lock is held until ``PersistentClient.close()`` is called, so plain
dict eviction would leave the palace path unreopenable and
unremovable in the same process.

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

Return True when ``path`` looks like a chroma palace.

Verifies the SQLite magic header rather than file presence alone.
Bare ``sqlite3.connect()`` against a missing path leaves a 0-byte
file behind (the SQLite header is written on the first statement,
not on connection), so file-presence alone treats those artifacts
as real chroma palaces and breaks multi-backend resolution. The
16-byte ``SQLite format 3\x00`` magic prefix is written as soon
as chromadb's ``PersistentClient`` does any work, so this check
accepts every real chroma palace while rejecting empty / garbage
files. See #1893.

#### `get_or_create_collection`

```python
def get_or_create_collection(self, palace_path: str, collection_name: str) -> ChromaCollection
```

Legacy shim for ``get_collection(..., create=True)`` by path string.

#### `delete_collection`

```python
def delete_collection(self, palace_path: str, collection_name: str) -> None
```

Delete ``collection_name`` from the palace at ``palace_path``.

#### `create_collection`

```python
def create_collection(self, palace_path: str, collection_name: str, hnsw_space: str = 'cosine') -> ChromaCollection
```

Create (not get-or-create) ``collection_name`` with the given HNSW space.

## Functions

### `quarantine_stale_hnsw`

```python
def quarantine_stale_hnsw(palace_path: str, stale_seconds: float = 300.0) -> list[str]
```

Rename HNSW segment dirs that look unsafe to open.

This catches three classes of HNSW corruption before ChromaDB opens the
native segment reader:

1. stale-by-mtime segments whose ``index_metadata.pickle`` fails the
   existing format sniff-test;
2. structurally impossible HNSW payloads where ``link_lists.bin`` is much
   larger than ``data_level0.bin``.

The second check is intentionally not gated by mtime. A segment with a
300x link/data ratio is unsafe regardless of whether its mtime is recent;
letting Chroma open it can SIGSEGV before Python fallback code runs.

The original directory is renamed, not deleted, so recovery remains
possible if the heuristic ever misfires.

### `reset_hnsw_capacity_cache`

```python
def reset_hnsw_capacity_cache() -> None
```

Forget every cached capacity verdict.

The signature check already picks up on-disk changes on its own; this is
for callers that drop all cached palace state at once (``tool_reconnect``,
``_force_chroma_cache_reset``) and for tests that want a probe to run
unconditionally. Bumps the generation so a probe already running cannot
re-store the entry this call just dropped.

### `hnsw_capacity_status`

```python
def hnsw_capacity_status(palace_path: str, collection_name: str = 'mempalace_drawers') -> dict
```

Compare sqlite embedding count against HNSW element count.

The #1222 failure mode: ``max_elements`` froze at 16 384 while sqlite
accumulated 192 997 embeddings. Every subsequent tool call segfaulted
when chromadb tried to load the undersized HNSW. This probe runs
*before* anything touches the segment so we can warn (or fall back to
BM25) instead of crashing.

Returns a dict with:

* ``segment_id``       — VECTOR segment UUID, or ``None`` if no palace
* ``sqlite_count``     — embeddings present in chroma.sqlite3
* ``hnsw_count``       — elements chromadb's pickle knows about
* ``divergence``       — ``sqlite_count - hnsw_count`` when both known
* ``diverged``         — True when divergence exceeds the threshold
* ``status``           — ``"ok"`` | ``"diverged"`` | ``"unknown"``
* ``message``          — human-readable summary

Never raises — a probe that throws would defeat the point.

A fully-measured verdict is cached per ``(palace_path, collection_name)``
and reused while every file the probe reads is unchanged on disk (#1471).
Each call otherwise costs a ``COUNT(*)`` over the embeddings table and a
full unpickle of the segment metadata — the two dominant costs — plus a few
small sqlite reads, on a path every search, duplicate check and status call
runs through. A verdict the probe could not fully measure (``sqlite_count``
is ``None`` from a locked database, or there is no palace yet) is returned
but never cached, so a transient failure cannot pin a false reading.

Freshness comes from an ``(inode, mtime_ns, size)`` signature rather than a
wall-clock TTL, so an external writer — ``mempalace repair``, a peer mine,
another process — invalidates the verdict as soon as it touches the files,
instead of leaving the #1222 guard blind for a fixed window.
``_CAPACITY_CACHE_MAX_AGE_SECONDS`` caps how long one verdict may be
reused, but only as a backstop for filesystems with coarse timestamps; the
signature is what makes the verdict fresh.

Unlike :meth:`ChromaBackend._client`, which tolerates a 0.01 s mtime
epsilon to avoid rebuilding an expensive client, this compares exactly:
re-running the probe costs milliseconds, whereas serving one stale verdict
can route a query into a diverged segment.

### `sqlite_room_wing_hall_counts`

```python
def sqlite_room_wing_hall_counts(palace_path: str, collection_name: str) -> Optional[list[tuple]]
```

Grouped ``(room, wing, hall, n, last_date)`` from ``chroma.sqlite3``.

``last_date`` is the newest ``date`` metadata value in the group, so
``find_tunnels`` can still report ``recent`` without paging every drawer
(``build_graph`` only ever uses the maximum). Returns ``None`` when sqlite
cannot be trusted, so the caller falls back to the client path.

### `sqlite_list_id_metadata`

```python
def sqlite_list_id_metadata(palace_path: str, collection_name: str, where: Optional[dict] = None) -> Optional[tuple[list[str], list[dict]]]
```

All matching drawer ids + metadata from sqlite, without opening HNSW.

Documents are deliberately excluded: ``chroma:document`` lives in the same
``embedding_metadata`` table, and joining it in would materialize the whole
palace's verbatim text (hundreds of MB on a six-figure palace) just to
render one page of previews. Callers hydrate the page they display via
:func:`sqlite_documents_for_ids`.

``where`` supports equality on ``wing``/``room`` and ``$and`` of those,
matching ``tool_list_drawers``; it is applied in SQL. Returns ``None`` when
sqlite cannot be trusted so the caller can fall back to ``col.get`` paging.

### `sqlite_documents_for_ids`

```python
def sqlite_documents_for_ids(palace_path: str, collection_name: str, ids: list) -> Optional[dict]
```

``&#123;drawer_id: document}`` for ``ids`` only, straight from sqlite.

Hydrates previews for the page being displayed without opening HNSW and
without reading the rest of the palace's text.

Resolved in two indexed steps rather than one join: ``embedding_id`` is
only indexed as part of ``UNIQUE (segment_id, embedding_id)``, so a join
that filters on it alone degenerates into a full scan of
``embedding_metadata`` — 5.7s for a 20-row page on a 165k-drawer palace.
Seeking the segment first, then ``embedding_metadata``'s ``(id, key)``
primary key, keeps both steps on an index.

### `quarantine_invalid_hnsw_metadata`

```python
def quarantine_invalid_hnsw_metadata(palace_path: str) -> list[str]
```

Quarantine segment dirs whose ``index_metadata.pickle`` is unreadable or invalid.

Chroma's persisted HNSW metadata is untrusted disk state. If a segment has
labels but invalid or partial metadata, current Chroma versions can accept
the pickle and crash later in the Rust loader. We rename the entire segment
out of the way before ``PersistentClient`` opens so Chroma can rebuild
cleanly instead of touching known-bad metadata.
