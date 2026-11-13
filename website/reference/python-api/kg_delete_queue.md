# `mempalace.kg_delete_queue`

Source: [`mempalace/kg_delete_queue.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/kg_delete_queue.py)

Deferred AGE delete-through (#558, #610).

Deleting a drawer must also remove its AGE ``Drawer`` vertex, or the graph fills
with orphans. Doing that inline is too slow on a large graph: AGE 1.6's
``DETACH DELETE`` paid ~16 s per statement plus ~1 s per vertex on the
production graph (2026-11-13: 17.0 s for one vertex, 21.1 s for four), and the
miners delete stale drawers while holding the palace mine lock.

So the delete hook only records the ids in a small table (one upsert), and
:func:`sweep` removes the queued vertices later in batches: one ``DETACH
DELETE`` statement per batch, so the fixed cost is paid once per batch rather
than once per delete. The nightly dream run calls :func:`sweep_from_env`.

Correctness rules the sweep keeps:

* The hook runs before the deleting transaction commits, so a row the sweep
  still sees may be a delete in flight. A visible row is therefore left queued
  until its entry is older than ``SETTLE_HOURS``; only then is it taken as live
  (re-mined under the same deterministic id, or a delete that rolled back) and
  acknowledged WITHOUT touching its vertex.
* Every enqueue takes a fresh ``version`` from a sequence (never repeats, even
  after the row was dequeued and re-created); the sweep acknowledges only the
  (id, version) pairs it selected, so a delete of the same id that lands while
  a batch is in flight stays queued.
* One sweeper at a time (a session advisory lock); a second one skips.
* A batch leaves the queue only after its graph delete returned. Residual: a
  drawer re-added between the liveness check and the DETACH DELETE loses its
  vertex; with write-through off a re-added drawer has none anyway.

## Functions

### `make_queued_deletethrough`

```python
def make_queued_deletethrough(dsn: str, table: Optional[str] = None, connect: Optional[Callable[[str], Any]] = None)
```

Delete hook that enqueues the deleted drawer ids. Never raises: the
caller's relational delete stands either way, and a failed enqueue only
leaves an orphan vertex behind (logged), exactly what an absent hook would.

### `sweep`

```python
def sweep(dsn: str, kg: Any = None, *, batch: int = BATCH, max_batches: int = MAX_BATCHES, connect: Optional[Callable[[str], Any]] = None, settle_hours: int = SETTLE_HOURS) -> dict
```

Remove queued Drawer vertices, ``batch`` ids per DETACH DELETE statement,
oldest first, at most ``max_batches`` statements. Each batch's wall time is
logged and returned. An error stops the sweep; the failing batch and
everything after it stay queued.

### `sweep_from_env`

```python
def sweep_from_env(env: Optional[dict] = None, **kw) -> dict
```

The nightly step. Skips (never fails the night) when the switch is off,
there is no Postgres DSN, or the graph does not exist.
