# `mempalace.hallways`

Source: [`mempalace/hallways.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/hallways.py)

Hallways — within-wing entity-to-entity connectors.

A **hallway** is a connection between two entities (people, projects,
concepts, interests) inside one wing, materialized from their
co-occurrence across that wing's drawers. Conceptually:

    WING → has DRAWERS (each tagged with entities)
            entities → connected to other entities by HALLWAYS
                       (within-wing, built from drawer co-occurrence)
                       hallways → are the primitive
                                   tunnels → use hallways to spawn
                                             cross-wing connections

If Aya and Lumi are both mentioned in 47 drawers across the diary,
letters, and ideas rooms, there's a hallway between them. If Aya
and "consciousness" co-occur in 19 drawers, there's a hallway between
them too. The hallway *is* the structural fact of "these two entities
travel together inside this wing."

Mempalace's tunnel primitive in ``palace_graph.py`` connects rooms
across wings. This module fills the within-wing gap with an
entity-centric (not room-centric) model: hallways are about *who/what
relates to whom/what*, not *which rooms relate to which*. A planned
follow-up PR will refactor ``_compute_topic_tunnels_for_wing`` to
build cross-wing tunnels from hallway data (Wing → Drawer-entities →
Hallway → Tunnel).

Persistence mirrors ``palace_graph._TUNNEL_FILE``: a JSON file under
``~/.mempalace/`` so the records survive across mines and are
inspectable / editable by hand if needed.

## Functions

### `compute_hallways_for_wing`

```python
def compute_hallways_for_wing(wing: str, col = None, min_count: int = 2, config = None) -> list[dict]
```

Compute entity-pair hallways for one wing.

Algorithm:
  1. Query drawers for ``wing`` from ``col``.
  2. For each drawer with entities, every pair of distinct entities in
     that drawer is one co-occurrence. Increment a counter for each
     pair; also record the room the drawer lives in.
  3. For each (entity_a, entity_b) pair whose co-occurrence count is
     ``>= min_count``, materialize a hallway record. The record
     carries the pair, the count, and the set of rooms where they
     co-occurred (useful context for navigation).
  4. Persist the full hallway list (records for other wings preserved,
     this wing's records replaced) and return the just-computed list.

Args:
    wing: wing name to scan.
    col: ChromaDB collection — must support ``.count()`` and paginated
        ``.get(limit=..., offset=..., include=...)``. The fetch is filtered
        to ``wing`` client-side rather than via ``.get(where=&#123;"wing": ...})``,
        which binds one SQL variable per matched id and overflows SQLite's
        ``SQLITE_MAX_VARIABLE_NUMBER`` on large wings (#1619). Fake
        collections and alternate backends must implement this shape.
        If ``None``, returns ``[]`` (caller didn't supply a backing
        store, so nothing to compute against). Tests pass a controlled
        MagicMock.
    min_count: minimum co-occurrence count required to materialize a
        hallway between two entities. Default 2 — single co-occurrences
        are noise (entities mentioned together once in one drawer);
        two or more is a real signal. Clamped to ``>=1``.
    config: Optional ``MempalaceConfig`` selecting the palace-scoped
        hallway sidecar. Callers using an explicit palace path must pass
        the matching config so derived graph state cannot leak into the
        default palace.

Returns:
    List of hallway dicts created for this wing. Records for other
    wings already on disk are preserved.

### `list_hallways`

```python
def list_hallways(wing: Optional[str] = None, config = None, limit: Optional[int] = None, offset: Optional[int] = None) -> list[dict]
```

List hallway records, optionally filtered by ``wing`` and paginated.

On the postgres store the wing filter and the pagination are both SQL, so
a wing-scoped call reads only that wing. On the JSON store the filter is
still applied after the full load — same behaviour as before, which is
why the postgres cutover is the actual fix rather than this signature.

``limit``/``offset`` are new and optional: 797K records is not a sane
payload at any speed, and the daemon needs a bounded page to be able to
answer ``mempalace_list_hallways`` at all (palace-daemon#255).

### `delete_hallway`

```python
def delete_hallway(hallway_id: str, config = None) -> bool
```

Remove one hallway record by id. Returns True if a record was removed.
