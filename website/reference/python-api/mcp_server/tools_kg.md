# `mempalace.mcp_server.tools_kg`

Source: [`mempalace/mcp_server/tools_kg.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_server/tools_kg.py)

## Functions

### `tool_kg_query`

```python
def tool_kg_query(entity: str, as_of: str = None, direction: str = 'both')
```

Query the knowledge graph for an entity's relationships.

### `tool_kg_add`

```python
def tool_kg_add(subject: str, predicate: str, object: str, valid_from: str = None, valid_to: str = None, source_closet: str = None, source_file: str = None, source_drawer_id: str = None, context: str = None)
```

Add a relationship to the knowledge graph.

All temporal and provenance fields are optional. ``valid_to`` lets callers
backfill historical facts with a known end date/time in a single call
instead of a separate ``kg_invalidate`` call.

Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
the form ``YYYY-MM-DDTHH:MM:SSZ``.

``context`` is the SPOC fourth-axis (techempower-org/mempalace#161):
a free-form anchor naming where the fact was witnessed (e.g.
``drawer:abc123``, ``conversation:2026-05-28``). The AGE backend
stores it on the RELATION edge and surfaces it through every read
path; the SQLite backend silently accepts and ignores it (storage
schema doesn't yet have a column for it) so callers don't need to
branch on backend.

### `tool_kg_invalidate`

```python
def tool_kg_invalidate(subject: str, predicate: str, object: str, ended: str = None)
```

Mark a fact as no longer true.

Returns the actual ``ended`` date/time that was stored. When the caller
omits ``ended``, the underlying graph stamps ``date.today()`` and the
response reflects that resolved value.

Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
the form ``YYYY-MM-DDTHH:MM:SSZ``.

### `tool_kg_supersede`

```python
def tool_kg_supersede(subject: str, predicate: str, old_object: str, new_object: str, at: str = None)
```

Atomically replace one fact with another at a single shared boundary.

Closes ``(subject, predicate, old_object)`` and opens
``(subject, predicate, new_object)`` at one shared instant, so a
point-in-time query at the boundary returns only the new value. Use this
instead of a separate ``kg_invalidate`` + ``kg_add`` when a single-valued
fact changes (e.g. a model, employer, or address changes).

``at`` accepts ``YYYY-MM-DD`` or a canonical UTC datetime
(``YYYY-MM-DDTHH:MM:SSZ``) and defaults to the current UTC instant.

### `tool_kg_timeline`

```python
def tool_kg_timeline(entity: str = None, as_of: str = None, limit: int = 100, offset: int = 0)
```

Get chronological timeline of facts, optionally for one entity.

``as_of`` (techempower-org/mempalace#161) filters to facts whose
temporal interval contains the given date/datetime; NULL ends are
treated as open intervals (same semantics as ``mempalace_kg_query``).
Omit ``as_of`` to see the full timeline including expired facts.

Paginated with ``limit``/``offset`` following the ``tool_list_drawers``
convention; defaults match the historical behavior (first 100 facts).

### `tool_kg_stats`

```python
def tool_kg_stats()
```

Knowledge graph overview: entities, triples, relationship types.

Returns a structured error envelope on transient postgres failures
(the connection dropped between `_call_kg` opening the handle and
`kg.stats()` finishing its query — typically caused by a postgres
OOM-kill or restart under load). The caller sees
``&#123;"error": "backend_unavailable", "retryable": True, ...}`` and can
surface "try again in a moment" instead of an opaque -32000
internal error. See techempower-org/mempalace#299.

Non-transient errors (cypher syntax, value-validation, schema
mismatch) still propagate — those need a real fix, not a retry.
