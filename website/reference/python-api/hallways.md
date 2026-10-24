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

### `entity_spelling_key`

```python
def entity_spelling_key(entity: str) -> str
```

Basename without a known code extension, lower-cased.

``src/main.zig``, ``main.zig`` and ``/Users/x/proj/src/main.zig`` all key
to ``main``; ``mcp_server`` and ``mcp_server.py`` both key to
``mcp_server``. The soft key groups candidate aliases; unequal explicit
extensions of the same stem (``parser.c`` / ``parser.h``) still key
together here and are separated by :func:`same_file_spelling` and
:func:`_spelling_clusters`. Used by the miner to skip self-pairs and by
``mempalace audit`` / ``mempalace hallways --prune-self-links`` to find
the ones older mines already wrote.

### `is_generic_entity`

```python
def is_generic_entity(name: str) -> bool
```

A name that identifies no project: a short lower-case word (``content``,
``thinking``), a harness tool (``WebFetch``), a generic noun (``Server``),
a file every repo has (``compose.yml``, ``app.js``) or a library
reference (``pathlib.Path``).

Symbols (``ChatStore``), qualified project names (``store.baseURL``) and
project names pass; the boundary with a short lower-case project name is
fuzzy by construction. Cross-wing ubiquity is judged separately, where
the wing counts are known.

### `same_file_spelling`

```python
def same_file_spelling(a: str, b: str) -> bool
```

True when two spellings can only be the same file.

They must share a basename key *and* one path must be a suffix of the
other: ``main.zig`` and ``src/main.zig`` are one file, while
``src/models/user.py`` and ``tests/models/user.py`` are two files that
happen to share a name. Keying on the basename alone merged their
associations and let ``--prune-spellings`` delete one of them.

Unequal explicit code extensions stay distinct: ``src/parser.c`` and
``src/parser.h`` share a stem and a directory, but they are two files.
An extensionless spelling may still alias a single extension
(``mcp_server`` / ``mcp_server.py``).

### `canonical_spelling`

```python
def canonical_spelling(cluster: list[str]) -> str
```

The spelling records carry for one entity: its most qualified path first.

A path is what tells ``src/models/user.py`` from ``tests/fixtures/user.py``
once a record leaves its wing, so the spelling with the most directory
segments wins; keeping the shortest threw that away and let two unrelated
files meet as bare ``user.py`` in the cross-wing tunnel builder. Among
spellings with no directory the shortest reads best (``ChatStore`` over
``ChatStore.swift``). Ties break on the text, so the choice is stable.

### `is_self_link`

```python
def is_self_link(record) -> bool
```

True when a hallway record joins two spellings of one entity.

### `canonical_entities`

```python
def canonical_entities(entities: list[str]) -> list[str]
```

One spelling per entity, in first-seen order.

The structural extractor records a file as both its path and its
basename, so a drawer's entity list holds ``src/main.zig`` and
``main.zig`` side by side. Pairing those raw spellings wrote a hallway
from the entity to itself and four copies of every real association
(``ChatStore`` × ``RootView`` under each spelling combination). Each
entity keeps its :func:`canonical_spelling`, so hallways read
``ChatStore ↔ RootView``.

### `compute_hallways_for_wing`

```python
def compute_hallways_for_wing(wing: str, col = None, min_count: int | None = None, config = None) -> list[dict]
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
    col: ChromaDB collection. A Chroma collection is read in one pass
        over ``chroma.sqlite3`` (see :func:`_wing_metadata_from_sqlite`).
        Any other must support paginated
        ``.get(where=&#123;"wing": ...}, limit=..., offset=..., include=...)``.
        The fetch is scoped to ``wing`` server-side AND paginated: an
        unbounded ``.get(where=...)`` binds one SQL variable per matched
        id and overflows SQLite's ``SQLITE_MAX_VARIABLE_NUMBER`` on wings
        above ~32k drawers (#1619), while an unscoped page walk costs
        O(total palace drawers) on every mine, pegging the CPU for
        minutes on large palaces (#2466). A bounded page never binds more
        than ``batch_size`` ids. Fake collections and alternate backends
        must implement this shape. If ``None``, returns ``[]`` (caller
        didn't supply a backing store, so nothing to compute against).
        Tests pass a controlled MagicMock.
    min_count: minimum co-occurrence count required to materialize a
        hallway between two entities. Pass an explicit ``None`` (the
        default) — or omit the arg — to resolve it from config:
        ``MEMPALACE_KG_HALLWAY_MIN_COUNT`` env first, then the
        ``hallway_min_count`` config-file value, then the historical
        default of ``2`` (single co-occurrences are noise — entities named
        together once in one drawer; two or more is a real signal). An
        explicitly supplied integer is honoured verbatim, clamped to
        ``>=1``.
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

### `iter_hallways`

```python
def iter_hallways(config = None) -> Iterator[dict]
```

Yield every hallway record without materializing the palace's list (#551).

For callers that make one pass over all wings, such as the entity-tunnel
step of a projects-mode mine. ``list_hallways()`` with no wing builds a
list of every record. On the JSON store at 2.6M records that is more than
8 GB. This streams it instead. Both stores stream: the JSON store through
the ``raw_decode`` reader, the postgres store through a server-side cursor.
A store without ``iter()`` falls back to ``list()``.

A JSON file that turns out to be truncated part-way through stops the
stream with a warning instead of raising. The records already yielded
stand. This is derived-graph input, and the old whole-file reader returned
nothing at all for a corrupt file.

### `prune_spelling_hallways`

```python
def prune_spelling_hallways(config = None, apply: bool = False) -> dict
```

Find (and with ``apply``) remove hallways that older mines wrote per spelling.

Two defects, one cause: before :func:`canonical_entities` the miner paired
raw spellings, so every code wing has ``main.zig ↔ src/main.zig``
(an entity joined to itself) and four copies of ``ChatStore ↔ RootView``
(one per spelling combination). Self-links are dropped; of each variant
group the record with the highest co-occurrence count survives under
its canonical spellings and the rest are dropped. Only the sidecar file
is touched, never a drawer. ``removed`` is 0 on a dry run.

### `association_groups`

```python
def association_groups(records: list[dict]) -> list[list[dict]]
```

Group hallway records that are one association, per wing.

Each endpoint is mapped to the file it names using the wing's own
spellings (:func:`_spelling_clusters`), so two files that share a
basename are two entities, and a bare name that could be either of them
belongs to no file. A record with such an ambiguous endpoint is a group
of its own: comparing it pairwise let it match both files and bridge
their records into one group, and the prune then deleted a real
association. Self-links are expected to be filtered out first.

Shared by ``--prune-spellings`` and ``mempalace audit`` so the audit never
reports a duplicate the prune would keep.

### `delete_hallway`

```python
def delete_hallway(hallway_id: str, config = None) -> bool
```

Remove one hallway record by id. Returns True if a record was removed.
