# `mempalace.palace.mine_lock`

Source: [`mempalace/palace/mine_lock.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace/mine_lock.py)

## Functions

### `mine_lock`

```python
def mine_lock(source_file: str)
```

Cross-platform file lock for mine operations.

Prevents multiple agents from mining the same file simultaneously,
which causes duplicate drawers when the delete+insert cycle interleaves.

### `reap_stale_mine_locks`

```python
def reap_stale_mine_locks(*, min_age_seconds: int = 3600) -> tuple[int, int]
```

Best-effort garbage collection for orphaned per-source-file mine locks.

``_cleanup_mine_lock_file`` reclaims a lock file correctly on the happy
path (see its docstring) — but only for the *specific* lock a
:func:`mine_lock` context manager just released. A process that dies
before reaching its own ``finally`` block (killed, crashed, force-quit,
host reboot) never runs that cleanup, and nothing else in this codebase
later revisits that lock file. Locks in ``~/.mempalace/locks/`` can
accumulate unboundedly over time as a result — one long-lived
installation was found with 5,636 stale entries, the oldest several
months old, none held by any live process (confirmed via ``lsof``).

This reuses :func:`_cleanup_mine_lock_file` itself for the actual
removal — same nonblocking-flock-reacquire safety mechanism, same
Windows/POSIX handling, no duplicated locking logic. A lock is only
ever removed after *this* process re-acquires it, so anything
genuinely held by a live process is left untouched regardless of
``min_age_seconds``. ``min_age_seconds`` is a courtesy throttle only —
it avoids racing a lock that was *just* released and may still be
mid-rendezvous with a waiter on the same pathname; it is not a
substitute for the flock check, which is what actually makes removal
safe.

Skips ``mine_palace_*.lock`` files — those belong to the newer
palace-level :func:`mine_palace_lock` and have their own
lifecycle/holder tracking; this targets only the per-source-file locks
:func:`mine_lock` creates via :func:`_mine_lock_path`.

Returns ``(reaped, skipped)`` counts, for logging/testing — callers
don't need to act on them.

### `mine_yield_hook`

```python
def mine_yield_hook(fn)
```

Call ``fn`` at every :func:`mine_yield_point` reached in this thread.

### `mine_yield_point`

```python
def mine_yield_point() -> bool
```

Mark a file boundary in a mine.

No file is half-written and no per-file lock is held here, so a host that
serializes the mine may briefly hand its lock to waiting requests. A no-op
unless :func:`mine_yield_hook` is active in this thread.

Returns True when the hook let other requests run. A request in that
window may have closed the palace backend (the hub's search retry after a
transient index error resets the Chroma caches), so the miner must reopen
the collections it holds before writing again.
