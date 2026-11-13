# `mempalace.kg_delete_queue`

Source: [`mempalace/kg_delete_queue.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/kg_delete_queue.py)

Deferred AGE delete-through (#558, #610).

Deleting a drawer must also remove its AGE ``Drawer`` vertex, or the graph fills
with orphans. Doing that inline is too slow on a large graph: AGE 1.6's
``DETACH DELETE`` paid ~16 s per statement plus ~1 s per vertex on the
production graph (2026-11-13: 17.0 s for one vertex, 21.1 s for four), and the
miners delete stale drawers while holding the palace mine lock.

So the delete hook only records the ids in a small table (one INSERT), and
:func:`sweep` removes the queued vertices later in batches: one ``DETACH
DELETE`` statement per batch, so the fixed cost is paid once per batch rather
than once per delete. The nightly dream run calls :func:`sweep_from_env`.

## Functions

### `make_queued_deletethrough`

```python
def make_queued_deletethrough(dsn: str, connect: Optional[Callable[[str], Any]] = None)
```

Delete hook that enqueues the deleted drawer ids. Never raises: the
relational delete has already committed, and a failed enqueue only leaves
an orphan vertex behind (logged), exactly what an absent hook would.

### `sweep`

```python
def sweep(dsn: str, kg: Any = None, *, batch: int = BATCH, max_batches: int = MAX_BATCHES, connect: Optional[Callable[[str], Any]] = None) -> dict
```

Remove queued Drawer vertices, ``batch`` ids per DETACH DELETE statement,
oldest first, at most ``max_batches`` statements. Each batch's wall time is
logged and returned. A batch's ids leave the queue only after its delete
returned; an error stops the sweep and keeps the rest queued.

### `sweep_from_env`

```python
def sweep_from_env(env: Optional[dict] = None, **kw) -> dict
```

The nightly step. Skips (never fails the night) when the switch is off,
there is no Postgres DSN, or the graph does not exist.
