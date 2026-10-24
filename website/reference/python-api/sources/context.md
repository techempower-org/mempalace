# `mempalace.sources.context`

Source: [`mempalace/sources/context.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/sources/context.py)

``PalaceContext`` facade passed to source adapters (RFC 002 §9).

Bundles the palace-side surface an adapter needs during :meth:`ingest`:
drawer collection, closet collection, knowledge graph, palace config, and
progress hooks. Adapters receive a ``PalaceContext`` instance and MUST NOT
import ``mempalace.palace`` directly — that coupling is what the facade
exists to prevent.

This module publishes the shape third-party adapters target. Core's mine
loop will construct a concrete ``PalaceContext`` and pass it to adapters
when the filesystem/conversations miners are migrated onto ``BaseSourceAdapter``
in a follow-up PR; until then, no in-tree code constructs one, but the
contract is stable.

## Classes

### `class PalaceContext`

Per-mine-invocation facade passed to :meth:`BaseSourceAdapter.ingest`.

Fields:
    drawer_collection: The palace's drawer collection (via RFC 001 backend).
    closet_collection: The palace's closet collection, or ``None`` if the
        palace has no closets yet. Adapters should not write to this
        directly; core builds closets post-step (RFC 002 §1.7).
    knowledge_graph: The palace's SQLite knowledge graph. Adapters
        advertising ``supports_kg_triples`` call ``add_triple`` on it.
    palace_path: Filesystem root of the palace (convenience; same as
        ``backend.PalaceRef.local_path``).
    config: Palace config object (hall keywords, rooms list, privacy
        floor, etc.). Shape is the existing :class:`MempalaceConfig`.
    adapter_name: Name of the adapter currently ingesting; populated by
        core so drawers can carry ``metadata["adapter_name"]``.
    adapter_version: Version of the adapter currently ingesting.
    progress_hooks: Optional callables core invokes on progress events.

Methods are intentionally thin wrappers so the concrete mine loop in
core can swap implementations without changing adapter code.

#### `upsert_drawer`

```python
def upsert_drawer(self, record: DrawerRecord) -> None
```

Persist a ``DrawerRecord`` to the drawer collection.

Applies the spec-mandated ``adapter_name`` and ``adapter_version``
metadata stamps (§5.1), which an adapter may set for itself, and the
``source_dir_ino`` stamp core records for every drawer that names a
source file (§5.1, #2320), which it may not: that one is a reading of
this machine's filesystem, and a value from anywhere else would be a
number ``sync`` weighs a removal against.

#### `skip_current_item`

```python
def skip_current_item(self) -> None
```

Signal to core that the current ``SourceItemMetadata`` is up-to-date
and no drawers should be emitted for it. Core resets the flag after
advancing past the item.

#### `is_skip_requested`

```python
def is_skip_requested(self) -> bool
```

Return whether :meth:`skip_current_item` was called since the last
time core advanced past an item. Adapters check this between the
``SourceItemMetadata`` yield and the cost of processing the item — if
core has signaled skip (because :meth:`is_current` returned True), the
adapter can bail out of expensive work (SQL queries, chunking, etc.)
rather than waiting for core to drop drawers downstream. Core resets
the flag on its own; adapters MUST NOT clear it.

#### `emit`

```python
def emit(self, event: str, **details: Any) -> None
```

Invoke each registered progress hook with ``(event, **details)``.
