# `mempalace.knowledge_graph`

Source: [`mempalace/knowledge_graph.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/knowledge_graph.py)

knowledge_graph.py — Temporal Entity-Relationship Graph for MemPalace
=====================================================================

Real knowledge graph with:
  - Entity nodes (people, projects, tools, concepts)
  - Typed relationship edges (daughter_of, does, loves, works_on, etc.)
  - Temporal validity (valid_from → valid_to — knows WHEN facts are true)
  - Closet references (links back to the verbatim memory)

Storage: SQLite (local, no dependencies, no subscriptions)
Query: entity-first traversal with time filtering

This is what competes with Zep's temporal knowledge graph.
Zep uses Neo4j in the cloud ($25/mo+). We use SQLite locally (free).

Usage:
    from mempalace.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph()
    kg.add_triple("Max", "child_of", "Alice", valid_from="2015-04-01")
    kg.add_triple("Max", "does", "swimming", valid_from="2025-01-01")
    kg.add_triple("Max", "loves", "chess", valid_from="2025-10-01")

    # Query: everything about Max
    kg.query_entity("Max")

    # Query: what was true about Max in January 2026?
    kg.query_entity("Max", as_of="2026-01-15")

    # Query: who is connected to Alice?
    kg.query_entity("Alice", direction="both")

    # Invalidate: Max's sports injury resolved
    kg.invalidate("Max", "has_issue", "sports_injury", ended="2026-02-15")

## Classes

### `class KnowledgeGraph`

#### `__init__`

```python
def __init__(self, db_path: str = None)
```

#### `close`

```python
def close(self)
```

Close the database connection.

#### `add_entity`

```python
def add_entity(self, name: str, entity_type: str = 'unknown', properties: dict = None)
```

Add or update an entity node.

#### `add_triple`

```python
def add_triple(self, subject: str, predicate: str, obj: str, valid_from: str = None, valid_to: str = None, confidence: float = 1.0, source_closet: str = None, source_file: str = None, source_drawer_id: str = None, adapter_name: str = None)
```

Add a relationship triple: subject → predicate → object.

``source_drawer_id`` and ``adapter_name`` are RFC 002 §5.5 provenance
fields populated by adapters that advertise ``supports_kg_triples``;
they default to ``None`` so every existing caller stays
source-compatible.

Examples:
    add_triple("Max", "child_of", "Alice", valid_from="2015-04-01")
    add_triple("Max", "does", "swimming", valid_from="2025-01-01")
    add_triple("Alice", "worried_about", "Max injury", valid_from="2026-01-01")

#### `invalidate`

```python
def invalidate(self, subject: str, predicate: str, obj: str, ended: str = None)
```

Mark a relationship as no longer valid (set valid_to date/time).

#### `supersede`

```python
def supersede(self, subject: str, predicate: str, old_obj: str, new_obj: str, at: str = None, confidence: float = 1.0, source_closet: str = None, source_file: str = None, source_drawer_id: str = None, adapter_name: str = None)
```

Atomically replace one fact with another at a single shared boundary.

Closes the currently-open ``(subject, predicate, old_obj)`` triple with
``valid_to = at`` and opens ``(subject, predicate, new_obj)`` with
``valid_from = at`` in one transaction, at a single shared instant.
Paired with the half-open upper bound in ``_temporal_filter_sql``, an
as-of query at that instant returns only the successor.

This is the primitive for a value change. Hand-rolling a handover as
``invalidate(ended=D)`` + ``add_triple(valid_from=D)`` with date-only
``D`` leaves two facts sharing the whole day ``D`` (``valid_to`` expands
to ``T23:59:59Z`` while ``valid_from`` expands to ``T00:00:00Z``), so an
as-of query on ``D`` returns both. ``supersede`` avoids this by writing
one identical precise instant to both sides.

``at`` defaults to the current UTC instant. A date-only ``at`` is
normalized to ``&lt;date>T00:00:00Z`` so both sides carry the same precise
value rather than the asymmetric whole-day expansion.

Returns the new triple's id. If no open ``old_obj`` triple exists the
successor is still opened, so ``supersede`` degrades to ``add_triple``.

#### `query_entity`

```python
def query_entity(self, name: str, as_of: str = None, direction: str = 'both')
```

Get all relationships for an entity.

direction: "outgoing" (entity → ?), "incoming" (? → entity), "both"
as_of: ISO date or canonical UTC datetime — only return facts valid then

#### `find_entity_candidates`

```python
def find_entity_candidates(self, name: str) -> list
```

Return token/prefix entity matches for disambiguation (never substring).

#### `query_relationship`

```python
def query_relationship(self, predicate: str, as_of: str = None)
```

Get all triples with a given relationship type.

#### `timeline`

```python
def timeline(self, entity_name: str = None)
```

Get all facts in chronological order, optionally filtered by entity.

#### `dump_rows`

```python
def dump_rows(self, table: str, after_rowid: int = 0, limit: int = 500) -> list
```

Page KG rows in rowid order for snapshot replication.

Rows are returned verbatim with a ``_rowid`` pagination cursor.
rowid order is deterministic, so pages never skip under concurrent
appends (updates in earlier pages are caught by the next full pass).

#### `apply_row`

```python
def apply_row(self, table: str, row: dict) -> None
```

Fold one replicated KG row in, keyed by id (INSERT OR REPLACE).

REPLACE makes invalidations (valid_to updates) and entity edits
converge on re-pull; rows are never deleted by replication.

#### `stats`

```python
def stats(self)
```

#### `seed_from_entity_facts`

```python
def seed_from_entity_facts(self, entity_facts: dict)
```

Seed the knowledge graph from fact_checker.py ENTITY_FACTS.
This bootstraps the graph with known ground truth.
