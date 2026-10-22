# `mempalace.palace.collection`

Source: [`mempalace/palace/collection.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace/collection.py)

## Classes

### `class CollectionNameMismatchError(ValueError)`

Raised when ``get_collection(collection_name=...)`` names a collection the
palace wrapper does not own.

The wrapper (``mempalace.palace.get_collection``) front-loads exactly two
collections: the configured drawers collection (default
``mempalace_drawers``, overridable in config) and ``mempalace_closets`` (the
searchable index layer). Opening any other name would create or touch a
store the rest of MemPalace never reads — so data upserted through it would
be invisible to search, MCP, and repair — instead of the caller discovering
the miss once their reads come back empty.

Subclass of :class:`ValueError` so callers that already catch the generic
type for a bad string keep working, while ``except
CollectionNameMismatchError`` gives the specific signal. This is a distinct
failure from :class:`mempalace.backends.CollectionNotInitializedError`
(palace + DB present, collection simply not bootstrapped yet): this one means
the *name* is not one MemPalace routes reads/writes through.

#### `__init__`

```python
def __init__(self, requested: str, allowed: List[str], palace_path: Optional[str] = None)
```

## Functions

### `clear_validated_embedder_identity`

```python
def clear_validated_embedder_identity(palace_path: Optional[str] = None) -> None
```

Drop cached embedder-identity verdicts so the next open re-checks.

Read-only opens of an empty collection can mark a key as validated without
recording identity on disk (``create=False``). When MCP later promotes that
reader to a writable owner, the writable open must re-run enforcement so
the first drawers still get labelled with the active model.

### `get_collection`

```python
def get_collection(palace_path: str, collection_name: Optional[str] = None, create: bool = True, backend: Optional[str] = None, read_only: bool = False, _skip_identity_check: bool = False, _skip_name_check: bool = False)
```

Get a first-class MemPalace collection (drawers or closets).

The wrapper front-loads exactly two collections and is the public surface
MCP, miners, the search layer, and the CLI use to open a palace:

* the **drawers** collection — the verbatim document store. Its name is
  configurable (default ``mempalace_drawers``); read it via
  :func:`mempalace.config.get_configured_collection_name` rather than
  hard-coding the string.
* the **closets** collection — the searchable index layer. Always named
  :data:`mempalace.palace.CLOSETS_COLLECTION_NAME`` ("mempalace_closets");
  open it through :func:`mempalace.palace.get_closets_collection`.

Any other name points to a store the rest of MemPalace never reads, so data
upserted through it stays invisible to search/CLI/MCP. ``get_collection``
therefore raises :class:`CollectionNameMismatchError` (a ``ValueError``)
naming both the offending and allowed strings, instead of silently creating
that orphan collection (issue ``#2347``). Passing ``collection_name=None``
resolves to the configured drawers name and always succeeds.

``read_only=True`` asks local backends to open storage without schema
initialization, migrations, or metadata writes. Backends that support a
genuine read-only mode receive it through the backend ``options`` mapping.

`collection_name=None` means "the configured drawers collection" and resolves through
`mempalace.config.get_configured_collection_name()` (env `MEMPALACE_COLLECTION_NAME` overrides the config file).
An explicit name must be one the wrapper owns (upstream #2347): the configured drawers name or the closets
collection. An explicit "mempalace_drawers" is therefore rejected when a custom name is configured (the
silent-miss case #2347 exists for). Callers that mean the drawers collection pass None. (The fork used to
map an explicit default name onto the configured one, the #665 shim; nothing relies on it after the 2026-10
upstream sync.)

``backend`` explicitly selects a backend (CLI ``--backend`` / RFC 001);
when omitted, resolution follows ``resolve_backend_name`` (config, env,
detected artifacts, chroma default) with mismatch protection.

``_skip_identity_check`` bypasses the embedder-identity enforcement so the
``set-embedder`` override path can open a palace whose recorded model
differs from the current one (the very state it exists to repair).

``_skip_name_check`` is the maintenance escape hatch for tools like
``mempalace_repair_encoding --collection NAME`` that must be able to open a
legacy ad-hoc collection name deliberately created before this check
existed. Callers on this path take the risk of a mismatched-name orphan;
they are explicit and self-aware. The common programmatic-API misuse this
check exists to prevent never uses this flag and still fails loudly.

### `set_palace_embedder_identity`

```python
def set_palace_embedder_identity(palace_path: str, model: Optional[str] = None, *, force: bool = False, backend: Optional[str] = None, collection_name: Optional[str] = None)
```

Record (or force-override) a palace collection's embedder identity (RFC 001).

Backs ``mempalace palace set-embedder``. Returns ``(old, new)`` identities.
Without ``force``, refuses to overwrite an existing identity that names a
different model (the user must confirm they know the vectors are
compatible). Opens with the identity check skipped so a mismatched palace —
the exact state being repaired — can be opened at all.

### `get_closets_collection`

```python
def get_closets_collection(palace_path: str, create: bool = True, backend: Optional[str] = None, *, read_only: bool = False)
```

Get the closets collection — the searchable index layer.
