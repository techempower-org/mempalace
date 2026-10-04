# `mempalace.backends.base`

Source: [`mempalace/backends/base.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/base.py)

Storage backend contract for MemPalace (RFC 001).

This module defines the surface every storage backend must implement:

* ``BaseCollection`` — the per-collection read/write interface, kwargs-only.
* ``BaseBackend`` — the per-palace factory, addressed by ``PalaceRef``.
* ``QueryResult`` / ``GetResult`` — typed result dataclasses that replace the
  Chroma dict shape as the canonical return type.
* Error classes + ``HealthStatus`` — uniform across backends.

This is the v1 cleanup from RFC 001 §10: full typed results, ``PalaceRef``,
registry-ready ABC. Embedder injection, maintenance hooks, and the full
conformance suite land in follow-up PRs.

## Classes

### `class BackendError(Exception)`

Base class for every storage-backend error raised by core.

### `class PalaceNotFoundError(BackendError, FileNotFoundError)`

Raised when ``get_collection(create=False)`` is called on a missing palace.

Subclass of ``FileNotFoundError`` so legacy callers that catch the latter
(pre-#413 seam) keep working unchanged.

### `class CollectionNotInitializedError(PalaceNotFoundError)`

Raised when the palace exists on disk but the requested collection has
never been created (e.g. ``init`` ran but ``mine`` has not).

Distinct from :class:`PalaceNotFoundError`: the palace dir and DB are
present and valid, only the collection has not been bootstrapped yet.
Subclass of :class:`PalaceNotFoundError` (and therefore
:class:`FileNotFoundError`) so legacy callers catching either parent
keep working unchanged.

### `class BackendClosedError(BackendError)`

Raised when a backend method is called after ``close()``.

### `class UnsupportedFilterError(BackendError)`

Raised when a where-clause uses an operator the backend does not implement.

Silent dropping of unknown operators is forbidden by spec (RFC 001 §1.4).

### `class UnsupportedCapabilityError(BackendError)`

Raised when a backend does not implement an optional capability.

### `class UnsupportedMaintenanceKindError(BackendError)`

Raised when ``run_maintenance(kind)`` is called with an unadvertised kind.

A backend MUST advertise a kind in ``maintenance_kinds`` before it accepts
it (RFC 001). Advertising a kind it does not implement is a conformance
failure; a kind it has no analogue for MUST be omitted, not no-op'd.

### `class BackendMismatchError(BackendError)`

Raised when a selected backend does not match existing palace artifacts.

### `class DimensionMismatchError(BackendError)`

Raised when the embedding dimension on write does not match the collection.

### `class EmbedderIdentityMismatchError(BackendError)`

Raised when the stored embedder model name differs from the current one.

### `class EmbedderIdentityUnknownWarning(UserWarning)`

Emitted on first open of a collection with no recorded embedder identity.

Legacy palaces created before identity tracking carry no model name. Per
RFC 001 the right behavior is warn-not-fail: the identity is recorded on
the next write and subsequent opens become strict.

### `class PalaceRef`

A handle to a palace, consumed by backends.

``id`` is always present and is the key backends use to cache handles.
``local_path`` is populated for filesystem-rooted palaces.
``namespace`` is used by server-mode backends for tenant / prefix routing.

Isolation contract (RFC 001 §2.1, conformance: ``tests/test_backend_conformance.py``)
-----------------------------------------------------------------------------------
``id`` is the *required* isolation key. Within a single backend instance:

    A record written for one ``PalaceRef.id`` MUST NOT be returned,
    modified, or deleted by an operation issued for a different
    ``PalaceRef.id``. Cross-palace access is a spec violation.

``namespace`` is *additional* partitioning, honored only by backends that
advertise the ``supports_namespace_isolation`` capability. For those
backends the same guarantee extends to namespaces:

    A record written under one ``namespace`` MUST NOT be returned,
    modified, or deleted by an operation issued under a different
    ``namespace`` within the same backend instance. Cross-namespace
    access is a spec violation.

Backends that do not advertise ``supports_namespace_isolation`` (e.g.
path-rooted ``chroma`` / ``sqlite_exact``) MUST NOT silently accept and
ignore a populated ``namespace`` — they MUST raise
:class:`UnsupportedCapabilityError` (same spirit as
:class:`UnsupportedFilterError`). Callers targeting those backends MUST
leave ``namespace`` as ``None``. Isolation conformance lives in
``tests/_backend_conformance.py`` (cross-id arm for every backend;
same-id / different-namespace arm for advertisers only).

### `class EmbedderIdentity`

Identity of the embedder that produced a collection's vectors (RFC 001).

``model_name`` is the stable identity persisted alongside a collection and
checked on subsequent opens. ``dimension`` is the vector width. A
``dimension`` of ``0`` means *unknown / not probed* — comparisons treat it
as "no dimension signal" rather than a real zero-width vector, so a cheap
read-path check can compare model names without loading the model.

### `class MaintenanceResult`

Observable outcome of ``run_maintenance(kind)`` (RFC 001).

Maintenance is *not* fire-and-forget: a backend MUST serialize concurrent
same-kind runs and report the outcome so a caller can learn it must not
re-trigger. ``status`` is one of:

* ``"ran"`` — this call performed the maintenance.
* ``"already_running"`` — another caller holds the work; this call did
  nothing and the caller MUST NOT re-trigger (the production index-build
  wedge: concurrent writers each issuing the build stacked exclusive locks).
* ``"noop"`` — nothing needed doing (e.g. the index already exists).

``stats`` is free-form per kind (rows analyzed, bytes reclaimed, index
build time) for benchmark/operator reporting.

### `class Embedder(Protocol)`

Minimal embedder contract (RFC 001, normative for identity checking).

The fuller embedder RFC (batching/async/pooling) is additive; identity
enforcement depends only on these three members.

#### `embed`

```python
def embed(self, texts: list[str]) -> list[list[float]]
```

### `class HealthStatus`

#### `healthy`

```python
def healthy(cls, detail: str = '') -> 'HealthStatus'
```

#### `unhealthy`

```python
def unhealthy(cls, detail: str) -> 'HealthStatus'
```

### `class QueryResult(_DictCompatMixin)`

Typed return from ``BaseCollection.query``.

Outer list dimension = number of query vectors / texts.
Inner list dimension = hits per query (may be zero).

Fields not in ``include=`` at the call site are populated with empty lists
of the correct outer shape (never ``None``), except ``embeddings`` which
is ``None`` when not requested.

#### `empty`

```python
def empty(cls, num_queries: int = 1, embeddings_requested: bool = False) -> 'QueryResult'
```

Construct an all-empty result preserving outer dimension.

When ``embeddings_requested`` is True, ``embeddings`` preserves the outer
query dimension with empty hit lists (matching the spec's rule that fields
requested via ``include=`` carry the outer shape even when empty). When
False, ``embeddings`` stays ``None`` to signal the field was not requested.

### `class GetResult(_DictCompatMixin)`

Typed return from ``BaseCollection.get``.

#### `empty`

```python
def empty(cls) -> 'GetResult'
```

### `class LexicalHit`

One hit from backend lexical candidate search.

### `class LexicalResult`

Typed return from ``BaseCollection.lexical_search``.

### `class BaseCollection(ABC)`

Per-collection read/write surface every backend must implement.

#### `add`

```python
def add(self, *, documents: list[str], ids: list[str], metadatas: Optional[list[dict]] = None, embeddings: Optional[list[list[float]]] = None) -> None
```

#### `upsert`

```python
def upsert(self, *, documents: list[str], ids: list[str], metadatas: Optional[list[dict]] = None, embeddings: Optional[list[list[float]]] = None) -> None
```

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

#### `count`

```python
def count(self) -> int
```

#### `estimated_count`

```python
def estimated_count(self) -> int
```

#### `count_where`

```python
def count_where(self, where: Optional[dict] = None) -> Optional[int]
```

Rows matching ``where``, counted without reading them, or ``None``.

``None`` means the backend has no filtered count, and the caller must
page through ``get`` instead (ChromaDB's ``count()`` takes no
``where``). A backend that can count in one statement should override
this: the searcher's in-scope count otherwise reads every drawer in the
scope, 6.9 s for a 57K-drawer wing on postgres (measured 2026-10-04).
Wrappers must forward it explicitly, since this default shadows
``__getattr__``.

#### `close`

```python
def close(self) -> None
```

#### `health`

```python
def health(self) -> HealthStatus
```

#### `distance_metric`

```python
def distance_metric(self) -> str
```

The space this collection's ``distances`` are reported in.

Defaults to the owning backend's declared metric (cosine for all
in-tree backends). Collections that can vary per-collection — e.g. a
legacy Chroma palace built without ``hnsw:space=cosine`` — override
this to report their actual space so core ranking converts correctly.

#### `get_stored_embedder_identity`

```python
def get_stored_embedder_identity(self) -> Optional[EmbedderIdentity]
```

Return the embedder identity recorded for this collection, if any.

Returns ``None`` when nothing is recorded — a legacy collection, or a
backend that does not yet persist identity. Core treats ``None`` as the
``unknown`` state (warn, do not fail). Backends override this and
:meth:`set_embedder_identity` against their own metadata store.

#### `set_embedder_identity`

```python
def set_embedder_identity(self, identity: EmbedderIdentity) -> None
```

Persist this collection's embedder identity. Default: no-op.

A backend without an identity slot inherits the no-op default and so
stays permanently ``unknown`` (safe — it simply never enforces). The
enforcement choke point calls this when recording on first write or
on an explicit, forced model swap.

#### `effective_embedder_identity`

```python
def effective_embedder_identity(self) -> Optional[EmbedderIdentity]
```

The identity of the embedder this collection actually uses.

For ``server_embedder`` backends that ignore the injected embedder,
this reports the server-side embedder so the same identity rules apply
(RFC 001). Defaults to ``None`` — the collection is embedded by the
injected/core embedder, and the caller supplies the current identity.

#### `get_all_metadata`

```python
def get_all_metadata(self, where: Optional[dict] = None) -> list[dict]
```

Return every matching record's metadata in one logical pass (#1796).

Default implementation pages through :meth:`get` using
``limit``/``offset`` -- correct for backends with a real server-side
cursor (e.g. Chroma's SQL OFFSET), and the same shape callers already
relied on before this method existed.

Backends whose ``get(limit=, offset=)`` is implemented by fully
materializing a result set and then Python-slicing it (no true
server-side cursor) MUST override this method to walk their native
cursor exactly once instead. Calling the default implementation on
such a backend is O(n^2) in collection size: each page re-walks the
entire collection just to discard everything outside the requested
slice. See issue #1796.

#### `facet_counts`

```python
def facet_counts(self, field: str, where: Optional[dict] = None, limit: int = 1000) -> dict[str, int]
```

Return counts for each distinct value of a metadata field.

#### `maintenance_state`

```python
def maintenance_state(self) -> dict
```

Return a structured snapshot of this collection's maintenance state.

Free-form per backend (e.g. row count, whether a vector index exists,
last-analyze age). Used by benchmark harnesses to record state
alongside each latency/recall measurement so an un-analyzed store is
not compared against a settled one (RFC 001). Backends should include
a JSON-compatible ``consistency_token`` when they can cheaply detect
writes that preserve the row count. Defaults to empty.

#### `run_maintenance`

```python
def run_maintenance(self, kind: str) -> 'MaintenanceResult'
```

Run a maintenance ``kind`` and return an observable result (RFC 001).

Backends advertise supported kinds in ``BaseBackend.maintenance_kinds``
and override this. The default supports nothing, so every kind raises
:class:`UnsupportedMaintenanceKindError`. Implementations MUST serialize
concurrent same-kind runs and report ``already_running`` rather than
stacking the work.

#### `lexical_search`

```python
def lexical_search(self, *, query: str, n_results: int = 10, where: Optional[dict] = None) -> LexicalResult
```

#### `update`

```python
def update(self, *, ids: list[str], documents: Optional[list[str]] = None, metadatas: Optional[list[dict]] = None, embeddings: Optional[list[list[float]]] = None) -> None
```

Default non-atomic update: get + merge + upsert.

Backends advertising ``supports_update`` MUST override with an atomic
single-round-trip implementation.

#### `rename_wing`

```python
def rename_wing(self, *, from_wing: str, to_wing: str, batch_size: int = 500) -> dict
```

Rename all drawers from one wing to another.

Default implementation iterates in batches using metadata-only
``update()`` calls.  Backends with native bulk-update support
(e.g. PostgreSQL) should override with an atomic implementation.

Returns ``&#123;"renamed": int, "errors": int}``.

### `class BaseBackend(ABC)`

Long-lived factory serving many palaces (RFC 001 §2).

Instances are lightweight on construction — no I/O, no network. All
connection work is deferred to ``get_collection``. Instances are thread-
safe for concurrent ``get_collection`` calls across different palaces.

Every backend MUST satisfy the per-``PalaceRef.id`` isolation guarantee in
:class:`PalaceRef`. Backends that additionally isolate by
``PalaceRef.namespace`` (multi-tenant / hosted deployments) MUST advertise
the ``supports_namespace_isolation`` capability token; doing so is a
promise to satisfy the cross-namespace guarantee and to pass the namespace
arm of the conformance suite. Backends without the token MUST raise
:class:`UnsupportedCapabilityError` when ``PalaceRef.namespace`` is
non-``None`` rather than silently accept-and-ignore (RFC 001 §4.4).

#### `require_namespace_support`

```python
def require_namespace_support(self, palace: PalaceRef) -> None
```

Raise if ``palace.namespace`` is set but this backend does not isolate by it.

Call at the start of ``get_collection`` (and any other entry that
accepts a :class:`PalaceRef`) so non-advertising backends never
silently drop a tenant namespace (RFC 001 §4.4).

#### `get_collection`

```python
def get_collection(self, *, palace: PalaceRef, collection_name: str, create: bool = False, options: Optional[dict] = None) -> BaseCollection
```

#### `close_palace`

```python
def close_palace(self, palace: PalaceRef) -> None
```

Evict cached handles for a single palace. Default: no-op.

#### `close`

```python
def close(self) -> None
```

Shut down the entire backend. Default: no-op.

#### `health`

```python
def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus
```

#### `detect`

```python
def detect(cls, path: str) -> bool
```

## Functions

### `check_embedder_identity`

```python
def check_embedder_identity(stored: Optional[EmbedderIdentity], current: Optional[EmbedderIdentity], *, force_model_swap: bool = False) -> str
```

Three-state embedder-identity check (RFC 001).

Returns the resolved state and raises on a hard, unforced conflict:

* ``"unknown"`` — no identity recorded yet (legacy collection), or the
  current embedder is nameless. The caller warns and records on write.
* ``"known_match"`` — stored name (and dimension, when both known) equal
  the current embedder. Proceed normally.
* ``"known_mismatch"`` — names or dimensions differ. Without
  ``force_model_swap`` this raises (:class:`EmbedderIdentityMismatchError`
  for a model swap, :class:`DimensionMismatchError` for a width change,
  which is checked first because mismatched vectors are physically
  unusable). With ``force_model_swap`` it returns the state so the caller
  can re-record the identity and log the swap.

A ``dimension`` of ``0`` on either side means "unknown" and is skipped, so
a model-name-only check (cheap read path) still works.
