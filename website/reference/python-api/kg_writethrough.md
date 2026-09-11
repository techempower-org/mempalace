# `mempalace.kg_writethrough`

Source: [`mempalace/kg_writethrough.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/kg_writethrough.py)

KG write-through hooks for PostgresCollection drawer writes.

Inline KG enrichment: every drawer write extracts entities and adds
them to the AGE knowledge graph so retrieval can fuse vector + graph
signals without an offline backfill pass.

Hook contract (matches PostgresCollection.set_kg_writethrough):

    hook(drawer_id: str, document: str, metadata: dict) -> None

Hooks are called from inside ``_insert_rows`` *after* the drawer row
commits. They run synchronously on the writer's connection thread —
keep them fast or they slow down the write path. Exceptions are caught
upstream so a misbehaving extractor can't break ingest.

This module ships two hook factories:

- ``make_age_writethrough(kg, extractor)`` — extracts entities from each
  drawer and adds (drawer_filename → MENTIONS → entity_name) triples to
  the AGE KG via ``KnowledgeGraphAGE.add_triple``.
- ``make_null_writethrough()`` — no-op for tests / disabling.

The extractor is pluggable: pass any callable matching
``(text: str) -> list[Entity]`` where Entity has at least a ``.name``
attribute. The default is a regex-based extractor importable from the
SME repo (see sme/extractors/regex.py); production deployments can swap
in spaCy or LLM-backed extractors without touching the write-through
plumbing.

## Functions

### `make_age_writethrough`

```python
def make_age_writethrough(kg: Any, extractor: Extractor, *, relation_type: str = 'mentions', confidence: float = 0.5, max_entities_per_drawer: int = 100)
```

Build a write-through hook that populates AGE from drawer writes.

For each drawer, the hook:

1. Runs ``extractor(document)`` to get a list of entities.
2. For each entity (capped at ``max_entities_per_drawer``), calls
   ``kg.add_triple(subject=drawer_id, relation_type=relation_type,
   object_=entity.name, confidence=confidence)``.

The triples land as ``(drawer_id) -[mentions]-> (entity_name)`` in
AGE. ``drawer_id`` is typically the source filename (matches the
``expected_sources`` shape used in retrieval benchmarks), making the
graph queryable as "which drawers mention X" via:

    MATCH (d:Entity)-[r:RELATION]->(e:Entity)
    WHERE e.name = $entity AND r.relation_type = 'mentions'
    RETURN d.name

Capping at ``max_entities_per_drawer`` bounds the per-write cost;
each add_triple is ~2-5ms on AGE (MERGE + CREATE round-trip), so a
drawer with 100 entities adds ~250-500ms to its write. Tunable based
on extractor verbosity vs latency budget.

Args:
    kg: A ``KnowledgeGraphAGE`` (or any compatible KG with the same
        ``add_triple`` signature).
    extractor: Callable returning entities from text.
    relation_type: The Cypher edge label (default "mentions" matches
        the read-side fusion convention).
    confidence: Per-extraction confidence — 0.5 default reflects
        that regex extraction is high-recall but lower precision
        than e.g. LLM extraction.
    max_entities_per_drawer: Cap on entities per drawer write.

Returns:
    A hook callable suitable for ``PostgresCollection.set_kg_writethrough``.

### `make_age_batch_writethrough`

```python
def make_age_batch_writethrough(kg: Any, extractor: Extractor, *, relation_type: str = 'mentions', confidence: float = 0.5, max_entities_per_drawer: int = 100)
```

Like :func:`make_age_writethrough`, but one commit for the whole batch.

Hook signature: ``hook(drawers: list[dict])`` where each dict carries
``drawer_id`` / ``document`` / ``metadata`` — the same three values the
per-drawer hook takes, handed over together so the commit can be
amortized.

Why this exists (palace-daemon#265, design on #251). The MERGEs were
never the dominant cost; the *commits* were. ``add_mention`` and
``_run_cypher`` default to ``commit=True``, so a 1000-drawer batch with
up to 100 entities each issued up to 100,000 individual transaction
commits — each an fsync round-trip, on the connection holding the palace
write lock. Measured on the palace host: 4,832 drawers in 69 minutes
(~1.2 drawers/s), with ``pg_stat_activity`` showing one
``MERGE (d:Drawer …`` per drawer; and after the tunnel recompute was
removed (#264) a single changed file still spent 4+ minutes here after
its 690 drawers were already filed.

The machinery was already there and unused: ``kg.commit()`` exists
precisely for bulk callers, and ``backfill_age`` has used
``commit=False`` + one commit per batch since it shipped. The blocker
was the per-drawer hook contract, not the KG layer.

Failure handling matches the per-drawer hook's posture, because
enrichment is opportunistic and the drawer rows have already committed
by the time any of this runs:

- a failing extractor skips its drawer, not the batch;
- a failing ``commit()`` is logged, never raised;
- a failing ``add_mention`` does NOT cost only itself. Be precise here,
  because an earlier version of this docstring claimed it did:
  ``_run_cypher`` routes through ``_with_conn_retry``, which calls
  ``_rollback_quietly()`` on a statement-level DB error. Under
  ``commit=False`` that ``conn.rollback()`` **discards every mention
  pending in this batch**. The loop continues, so mentions extracted
  after the failure still land at the final commit, but the ones before
  it are gone.

That is a real narrowing versus the per-drawer hook, where a failure
cost exactly one mention. It is accepted for now because the *drawers*
are untouched — they committed before this hook ran — and the lost
edges are recoverable with ``backfill_age``, which exists for precisely
this state. Restoring per-mention isolation needs a SAVEPOINT around
each mention, which was measured on a scratch AGE palace at **3.0x**
the cost of the bare statements (1000 MERGEs: 0.50 s plain, 1.51 s
with SAVEPOINT + RELEASE each) -- enough to cut this change's 4.2x win
down to roughly 1.4x. Not worth it to buy back a rare, non-fatal,
backfill-recoverable loss, so it is filed as stage A2 on
techempower-org/palace-daemon#265 rather than built here. A savepoint
per *drawer* instead of per mention is the more promising point on
that curve: ~10x fewer savepoints at this corpus's entity density, and
it bounds a loss to one drawer's edges rather than the batch's.

Note the edges stay ``CREATE``-always (``add_mention`` does not upsert),
so a partially-applied batch is a state this system already tolerates.
That is also why nothing here retries: a retry after a lost commit would
duplicate the edges it did write.

### `make_null_writethrough`

```python
def make_null_writethrough()
```

A no-op hook. Useful for disabling KG writes in tests or rollouts
without removing the ``set_kg_writethrough`` call from the writer
setup path.

### `make_age_deletethrough`

```python
def make_age_deletethrough(kg: Any)
```

Build a delete-through hook that removes Drawer nodes from AGE.

Symmetric to ``make_age_writethrough`` for the delete path: when a
drawer row is removed from ``mempalace_drawers``, this hook removes
the matching ``(:Drawer &#123;id: ...})`` node and its incident edges
from the AGE graph. Without it, deleted drawers leave orphan Drawer
nodes that drift the graph out of sync with the relational table.

Hook signature: ``hook(drawer_ids: list[str]) -> None``. Called once
per ``PostgresCollection.delete`` invocation with the resolved id
list. Exceptions are caught upstream — KG sync is opportunistic,
not mandatory.

### `make_null_deletethrough`

```python
def make_null_deletethrough()
```

No-op delete hook. Mirror of ``make_null_writethrough`` for the
delete path; lets test setups disable AGE sync without removing the
``set_kg_deletethrough`` call.

### `make_extraction_enqueue_writethrough`

```python
def make_extraction_enqueue_writethrough(dsn: str)
```

Returns a writethrough callable that enqueues drawers for LLM triple extraction.

Idempotent: re-mines of the same drawer reset the queue row so it
gets re-processed (drawer content may have changed; the old triples
were extracted from the prior text). ON CONFLICT (drawer_id) DO
UPDATE clears started_at / completed_at / error / worker_id and
bumps queued_at to NOW().

Connection strategy mirrors the rest of the codebase — uses
``_load_psycopg2`` (psycopg3 driver under that legacy name) and opens a fresh connection per
drawer write. The drawer write path is already inside a transaction
on its own connection, so doing the enqueue on a separate connection
keeps the schemas independent and avoids dirty-read coupling.

Args:
    dsn: Postgres DSN — must point at the same database as the
        drawer collection (queue table lives in the public schema
        alongside ``mempalace_drawers``).

Returns:
    A hook callable matching the ``PostgresCollection.set_kg_writethrough``
    contract.

### `make_writethrough_from_env`

```python
def make_writethrough_from_env(kg: Optional[Any] = None, dsn: Optional[str] = None)
```

Build a hook based on environment variables.

Env vars:
  MEMPALACE_KG_WRITETHROUGH=0|1         — master switch for MENTIONS (default off)
  MEMPALACE_KG_EXTRACTOR=regex|spacy|llm|null  — choose extractor (default regex)
  MEMPALACE_KG_EXTRACTION_QUEUE=0|1     — also enqueue drawers for async
                                          LLM triple extraction (default off,
                                          composes with MENTIONS, never replaces)

Returns ``None`` if no writethrough stage is enabled. Returns a
single hook otherwise — if both MENTIONS and queue are enabled, the
hook calls them in order (MENTIONS first since it's the fast path).

``kg`` is required when the master switch is on. ``dsn`` is required
when ``MEMPALACE_KG_EXTRACTION_QUEUE`` is on (queue lives in
postgres, separate from AGE).

Regex extractor needs an SME-repo import — kept optional so the
mempalace package doesn't hard-require SME. If unavailable, falls
back to a built-in tiny regex extractor (lower recall than the SME
one but no cross-package dependency).

### `make_batch_writethrough_from_env`

```python
def make_batch_writethrough_from_env(kg: Optional[Any] = None, dsn: Optional[str] = None)
```

Batch-contract twin of :func:`make_writethrough_from_env`.

Same environment variables, same stages, same order — the only
difference is that the MENTIONS stage is built with
:func:`make_age_batch_writethrough`, so its MERGEs run with
``commit=False`` and the whole batch commits once
(palace-daemon#265). Stages with no batched form are wrapped by
:func:`_batchify` rather than dropped.

Returns ``None`` when no stage is enabled, exactly as the per-drawer
builder does, so the caller's "is write-through on?" check is
unchanged.

The env parsing deliberately lives in one place: this delegates to the
per-drawer builder for stage *selection* and swaps the MENTIONS
implementation, so the two cannot drift on which switches mean what.

### `make_deletethrough_from_env`

```python
def make_deletethrough_from_env(kg: Optional[Any] = None)
```

Build a delete hook gated on the same env switch as writethrough.

``MEMPALACE_KG_WRITETHROUGH=1`` enables both write and delete hooks
— they're a matched pair; running one without the other leaves the
graph drifting out of sync with the relational table. Returns
``None`` when the master switch is off.
