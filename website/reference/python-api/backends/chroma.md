# `mempalace.backends.chroma`

Source: [`mempalace/backends/chroma.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/chroma.py)

ChromaDB-backed MemPalace storage backend (RFC 001 reference implementation).

## Classes

### `class MetadataRecoveryPending(sqlite3.OperationalError)`

The persisted metadata projection needs the normal Chroma recovery path.

### `class CandidateRows(list)`

Candidate row ids; ``truncated`` when a read budget ran out first.

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

#### `iter_metadata`

```python
def iter_metadata(self, keys: Optional[Iterable[str]] = None, *, require_key: Optional[str] = None, equals: Optional[dict] = None) -> Optional[Iterator[Optional[dict]]]
```

Stream every drawer's metadata from chroma.sqlite3 in one linear pass.

``keys``, ``require_key`` and ``equals`` narrow the read (see
:func:`_sqlite_iter_metadata`). Returns ``None`` when this collection
has no palace path or no database file, so the caller can page through
:meth:`get` instead. A read error raises from the iterator.

#### `get_all_metadata`

```python
def get_all_metadata(self, where: Optional[dict] = None) -> list[dict]
```

Every drawer's metadata in one pass over chroma.sqlite3 (#1796).

The base implementation pages ``get(limit, offset)``, and Chroma turns
``offset`` into SQL ``OFFSET``, which steps over every skipped row, so
that pass is quadratic: on a 360k-drawer palace one page cost 81 ms at
offset 0 and 717 ms at offset 359k. A ``where`` filter still goes
through the base implementation.

#### `get_recent`

```python
def get_recent(self, *, limit: int, where: Optional[dict] = None, order_field: str = 'filed_at', include: Optional[list[str]] = None) -> GetResult
```

Newest ``limit`` records by ``order_field``, read from chroma.sqlite3.

Chroma's ``get`` loads the collection's whole HNSW segment before it
answers, even for a metadata-only read, so the base implementation's
paged ``get`` loaded every vector in the palace to pick a few recent
drawers: ``mempalace wake-up`` for a ten-drawer wing loaded millions.
This reads the ordered window from the metadata tables instead, which
also makes the window exact rather than whatever ``get`` paged first.
``where`` made of string equalities (what Layer 1 passes) is evaluated
in sqlite; any other filter, or a database the read cannot reach, goes
through the base implementation.

String equalities and an unfiltered read are the true top ``limit``
at any collection size, which is why :class:`ChromaBackend` advertises
``supports_recency_order``. A filter this path cannot evaluate keeps
the base implementation's approximate window.

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

Report HNSW space from legacy metadata or live schema.

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

Probed through :func:`mempalace.backends._magic.has_sqlite_magic`,
which never opens a plain descriptor on the file -- see that module for
why a plain open+close of a live database drops the process's POSIX
locks on it.

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

### `sqlite_wing_source_counts`

```python
def sqlite_wing_source_counts(palace_path: str, collection_name: str) -> Optional[list[tuple]]
```

Grouped ``(wing, source_file, n)`` for transcript-mined drawers from
``chroma.sqlite3``, scoped to ``collection_name``; ``None`` when sqlite
cannot serve it. Same contract as the sqlite_exact reader: only rows
whose ``source_file`` is a Claude Code projects path or a Codex sessions
path, which is what the audit's mixed-wing check and ``wings split`` use.

### `sqlite_metadata_snapshot`

```python
def sqlite_metadata_snapshot(palace_path: str, collection_name: str)
```

A recovered metadata snapshot, or None when callers must use Chroma.

Keep this context open through page selection and document hydration. It
reads no documents itself and never takes a backend write lock.

### `sqlite_list_id_metadata`

```python
def sqlite_list_id_metadata(palace_path: str, collection_name: str, where: Optional[dict] = None, *, _connection = None) -> Optional[tuple[list[str], list[dict]]]
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
def sqlite_documents_for_ids(palace_path: str, collection_name: str, ids: list, *, _connection = None) -> Optional[dict]
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

### `sqlite_diary_rows`

```python
def sqlite_diary_rows(palace_path: str, collection_name: str, *, agent_name: str, wing: str = '', limit: int = 10) -> Optional[tuple[int, list[tuple[str, Optional[str], dict]]]]
```

Count physical diary rows and hydrate only the newest ``limit`` rows.

All reads share one SQLite snapshot. Missing timestamps sort as empty
strings, with storage order breaking ties, matching the diary's paged
``Collection.get`` path. Non-string timestamps, an ambiguous collection,
or an unavailable schema return ``None`` for that path to handle instead.
Queued writes ahead of the metadata watermark also require Chroma's
recovery path. A missing stored document remains ``None`` as it does in
Chroma's get. ``limit`` is bounded to the diary API's range of 1..100.

### `sqlite_drawer_rows`

```python
def sqlite_drawer_rows(palace_path: str, collection_name: str, *, drawer_id: str) -> Optional[tuple[bool, list[tuple[str, Optional[str], dict]]]]
```

Read a direct drawer, or every scalar-parent-linked physical chunk.

The direct physical id wins over a logical group with the same handle.
Groups are complete and deduplicated by physical row, with no page cap.
Missing, unsupported, or unmaterialized records return ``None`` so Chroma
can recover them or preserve metadata types this scalar reader cannot
decode. In particular any selected array metadata requires that fallback.

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

### `chroma_system_generation`

```python
def chroma_system_generation() -> int
```

Generation of the process-wide Chroma System cache.

Increments each time :func:`_clear_chroma_system_cache` drops the cache.
A client opened at an older generation is reading a discarded segment.

### `register_before_system_cache_reset`

```python
def register_before_system_cache_reset(callback) -> None
```

Run ``callback`` before the shared Chroma System cache is dropped.

``clear_system_cache`` forgets Chroma's maps without stopping Systems.
Every client this module does not itself own has to be closed while those
maps still resolve. The callback must not call
:func:`_clear_chroma_system_cache`.

### `chroma_write_serial`

```python
def chroma_write_serial() -> int
```

Number of Chroma writes (add, upsert, update, delete) started in this process.
