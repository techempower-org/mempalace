# `mempalace.knowledge_graph_age`

Source: [`mempalace/knowledge_graph_age.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/knowledge_graph_age.py)

AGE-backed implementation of KnowledgeGraph (Apache AGE on Postgres).

Companion to `mempalace.knowledge_graph.KnowledgeGraph` (SQLite). Selectable
via `MEMPALACE_KG_BACKEND=age` once the config-routing layer is wired up.
Mirrors the public interface of the SQLite KG so callers can swap backends
without code changes.

The graph itself is `mempalace_kg` registered in AGE's `ag_catalog`. It is
created on first init and reused thereafter — initialization is idempotent.

## Classes

### `class KnowledgeGraphAGE`

Cypher-queryable KG using Apache AGE on a Postgres connection.

Public surface mirrors the SQLite ``KnowledgeGraph``:

- ``add_triple(subject, relation_type, object_, ...)`` — write a triple.
  Validates inputs (sanitize_kg_value, sanitize_iso_temporal) and rejects
  inverted temporal intervals at write time.
- ``query_triples(subject=..., **filters)`` — read triples matching the
  filter. Filter set is intentionally small for now; temporal ``as_of``
  filtering arrives in the as_of-filter feature.
- ``clear()`` — drop + recreate the graph (test isolation).

Routing via ``MempalaceConfig.kg_backend`` arrives in the kg_backend feature.

#### `__init__`

```python
def __init__(self, dsn: str)
```

Open a Postgres connection and ensure `mempalace_kg` exists.

Args:
    dsn: PostgreSQL DSN. Must point at a database where the AGE
        extension is installed (CREATE EXTENSION succeeds). The
        ``apache/age:release_PG16_1.6.0`` image we deploy on the
        homelab already has the .so files baked in; bare-metal
        Postgres requires source-build of AGE first.

#### `close`

```python
def close(self) -> None
```

Close the underlying Postgres connection.

#### `clear`

```python
def clear(self) -> None
```

Drop and recreate the graph. Intended for test isolation only.

Production callers should use targeted deletes; this nukes every
triple in the graph. The graph is re-registered immediately so
the instance remains usable for subsequent writes.

#### `add_triple`

```python
def add_triple(self, subject: str, relation_type: str, object_: str, source: Optional[str] = None, valid_from: Optional[str] = None, valid_to: Optional[str] = None, confidence: float = 1.0, context: Optional[str] = None) -> None
```

Write a triple ``(subject)-[relation_type]->(object_)`` to AGE.

Sanitizes the three positional values via ``sanitize_kg_value`` and
the two temporal fields via ``sanitize_iso_temporal``. Rejects
inverted intervals (``valid_to < valid_from``) at write time so
bad data never reaches the graph.

Entities are MERGE'd (created if absent, reused if present);
the relation itself is always CREATE'd so multiple temporally-
distinct facts between the same entities co-exist as parallel
edges (matches the SQLite KG semantics — see knowledge_graph.py).

``context`` is the SPOC fourth-axis: a free-form anchor naming
where this fact was witnessed (e.g. ``drawer:abc123``,
``conversation:2026-05-28``). Distinct from ``source`` (which
carries provenance for human-curated edits) in that ``context`` is
meant to be set by the extraction pipeline on every auto-derived
triple. Stored as an optional property and surfaced through every
read path. See techempower-org/mempalace#161.

#### `query_triples`

```python
def query_triples(self, subject: Optional[str] = None, as_of: Optional[str] = None, **_filters) -> list
```

Return triples matching ``subject`` and active ``as_of`` a date.

``as_of`` filters to triples whose temporal interval contains the
given date: ``valid_from <= as_of <= valid_to``, with NULL on
either end interpreted as open (NULL valid_from = active since
forever; NULL valid_to = still active).

Empty list when no match. Each triple is a dict with keys:
``subject, relation_type, object, source, valid_from, valid_to,
confidence, context``. ``context`` is the SPOC anchor (see
``add_triple``); ``None`` for triples written before the slot was
introduced.

#### `add_entity`

```python
def add_entity(self, name: str, entity_type: str = 'unknown', properties: Optional[dict] = None) -> str
```

Add or update an entity node.

Mirrors ``KnowledgeGraph.add_entity`` in the SQLite backend. MERGE
creates the node if absent, and sets ``type``/``properties`` on
creation only — AGE doesn't support ``ON CREATE SET``, so the
property setting happens via ``MATCH ... SET`` in a follow-up
Cypher call to keep semantics close to the SQLite ``INSERT OR
REPLACE``.

Returns the entity id (``name.lower().replace(' ', '_')``) for
SQLite-callsite source compatibility.

#### `invalidate`

```python
def invalidate(self, subject: str, predicate: str, obj: str, ended: Optional[str] = None) -> int
```

Mark active triples matching (subject, predicate, object) as expired.

Sets ``valid_to`` to ``ended`` (or today if None) on every RELATION
whose ``valid_to`` is currently NULL. Mirrors SQLite KG's
``invalidate`` exactly.

Returns the number of triples affected.

Inverted-interval check: if the resulting ``valid_to`` would precede
an existing ``valid_from`` on any affected triple, raises ValueError
before any write happens.

#### `query_entity`

```python
def query_entity(self, name: str, as_of: Optional[str] = None, direction: str = 'both') -> list
```

Return all triples touching ``name`` (entity name, not id).

Mirrors ``KnowledgeGraph.query_entity``:

- ``direction``: "outgoing" (entity → ?), "incoming" (? → entity), "both"
- ``as_of``: only return facts whose interval covers this date

Each result dict has: ``direction``, ``subject``, ``predicate``,
``object``, ``valid_from``, ``valid_to``, ``confidence``,
``source_closet`` (None on AGE — not yet plumbed), ``current``.

#### `query_relationship`

```python
def query_relationship(self, predicate: str, as_of: Optional[str] = None) -> list
```

Return all triples with the given relation type.

Mirrors SQLite ``KnowledgeGraph.query_relationship``.

#### `timeline`

```python
def timeline(self, entity_name: Optional[str] = None, limit: int = 100, offset: int = 0, as_of: Optional[str] = None) -> list
```

Return triples in chronological order, optionally filtered by entity.

Mirrors SQLite ``KnowledgeGraph.timeline``, including ``limit``/``offset`` pagination (upstream added it to
SQLite in the 2026-10 sync). Limit defaults to 100 for parity; ``id(r)`` breaks ``valid_from`` ties so pages
are stable, like SQLite's ``t.id`` tie-break. AGE ``ORDER BY ... LIMIT`` works inside cypher() so no
workaround needed.

``as_of`` (P5 / #161) filters to triples whose temporal interval
contains the given date; NULL ends are treated as open intervals
(same semantics as ``query_triples``). Default (None) returns the
full timeline including expired facts.

#### `timeline_total`

```python
def timeline_total(self, entity_name: Optional[str] = None, as_of: Optional[str] = None) -> int
```

Total number of triples a :meth:`timeline` query matches (all pages), with the same entity/as_of filters.

#### `add_mention`

```python
def add_mention(self, drawer_id: str, entity_name: str, *, entity_type: str = 'unknown', count: int = 1, confidence: float = 0.5, commit: bool = True) -> None
```

Add a (Drawer)-[:MENTIONS]->(Entity) edge.

Connects the palace-structure layer (Drawer nodes from
``palace_graph_age``) to the entity layer (Entity nodes).
MERGE pattern on the nodes — re-running for the same
(drawer, entity) pair creates a *new parallel edge* rather
than incrementing count.

CREATE-ALWAYS edge semantics is intentional and matches the
SQLite KG's triples-table behavior (each ``add_triple`` inserts
a new row, no UPSERT). Callers that need idempotency should
track write state externally (e.g. ``backfill_age``'s
``mempalace_kg_backfill_state`` table) and skip the call if the
drawer was already processed.

AGE 1.6.0 Cypher dialect gaps respected:
  - No SET on edge properties inline (parser errors at '=').
  - No ON CREATE SET.
  - No coalesce() in SET.
Edge properties are set at CREATE time and never modified after.

#### `commit`

```python
def commit(self) -> None
```

Commit the pending KG-write transaction.

Used by bulk-write callers (``backfill_age``) that pass
``commit=False`` to ``add_mention``/``_run_cypher`` to batch many
statements into one transaction, then call ``kg.commit()`` once
per batch. The single-statement default still commits per call.

#### `delete_drawer`

```python
def delete_drawer(self, drawer_id: str, *, commit: bool = True) -> int
```

Remove a Drawer node and all its edges from the AGE graph.

Returns the number of Drawer nodes removed (0 if the id wasn't
present, ≥1 if dedup hasn't run and there are duplicates).
Idempotent — calling on an already-deleted id is a no-op.

Used by the delete-through hook so drawer deletes propagate to
the graph; without it, orphan Drawer nodes accumulate over time
(one per delete since 2026-05-22 AGE rollout).

#### `delete_drawers`

```python
def delete_drawers(self, drawer_ids: list, *, commit: bool = True) -> int
```

Batched delete via UNWIND. Returns total Drawer nodes removed.

Use when removing many drawers in one logical transaction — one
Cypher round-trip instead of N — mirroring the UNWIND batching
``backfill_age`` uses for adds (commit ``4fbd8c6``).

#### `seed_from_entity_facts`

```python
def seed_from_entity_facts(self, entity_facts: dict) -> int
```

Seed the graph from fact_checker.py ENTITY_FACTS dict.

Mirrors SQLite ``KnowledgeGraph.seed_from_entity_facts``. ENTITY_FACTS
is a dict of &#123;entity_name: &#123;fact_label: value, ...}} — each
non-empty value becomes a (entity_name, fact_label, value) triple
with no temporal bounds and confidence 1.0.

Returns the number of triples written.

#### `stats`

```python
def stats(self) -> dict
```

Return aggregate counts mirroring the SQLite KG's ``stats()`` shape.

Result keys match ``mempalace/knowledge_graph.py::KnowledgeGraph.stats``
so callers (``tool_kg_stats``, palace-daemon's ``/graph`` panel) get
the same envelope regardless of backend:

- ``entities`` — total Entity nodes
- ``triples`` — total RELATION edges (active + expired)
- ``current_facts`` — RELATIONs with ``valid_to IS NULL`` (still active)
- ``expired_facts`` — triples − current_facts
- ``relationship_types`` — sorted distinct ``relation_type`` values

Tries ``_stats_fast`` first (SQL against the AGE backing label
tables — sub-ms on ≥1M edges because Postgres serves plain
``count(*)`` straight off the visibility map without unwrapping
every value through agtype). Falls back to ``_stats_cypher`` when
the backing tables aren't available yet (fresh palace, label not
yet created, or non-standard AGE setup). The fallback runs three
full graph walks and is the slow path the issue measures at ~9s
on the production palace at ~1M entities / ~1.6M RELATION edges.

Implemented to close techempower-org/mempalace#96 (envelope
shape) and #266 (performance — replaces the ~9s slow path with
sub-100ms).
