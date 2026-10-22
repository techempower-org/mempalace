# `mempalace.palace.palace_lock`

Source: [`mempalace/palace/palace_lock.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace/palace_lock.py)

## Classes

### `class MineAlreadyRunning(RuntimeError)`

Raised when another `mempalace mine` already holds the per-palace lock.

### `class MineValidationError(RuntimeError)`

Raised at end of mine when PRAGMA quick_check on the palace reports errors.

#### `__init__`

```python
def __init__(self, palace_path: str, errors: list[str]) -> None
```

## Functions

### `mine_palace_lock`

```python
def mine_palace_lock(palace_path: str)
```

Per-palace non-blocking lock around the full `mine` pipeline.

The per-file `mine_lock` only protects delete+insert interleave for a
single source; it does not prevent N copies of `mempalace mine <dir>`
from being spawned concurrently by hooks. When that happens, each copy
drives ChromaDB HNSW inserts in parallel against the same palace,
which (combined with chromadb's multi-threaded ParallelFor) can
corrupt the HNSW graph and produce sparse link_lists.bin blowups.

The lock file is keyed by sha256(palace_path) so mines against
*different* palaces can still run in parallel — we only serialize
writes into the same palace, which is the correctness boundary.

The key is derived from a fully normalized form of the path:
`realpath` resolves symlinks and `..` segments, and `normcase` folds
case on Windows (which has a case-insensitive filesystem). Without
normcase, `C:\Palace` and `c:\palace` would hash to different keys
on Windows and let two concurrent mines touch the same on-disk palace.

Non-blocking: if another `mine` is already writing to this palace,
raise MineAlreadyRunning so the caller can exit cleanly instead of
piling up as a waiting worker.

Re-entrant: if the current process already holds the lock for the same
palace, the context manager passes through without re-acquiring. This
lets ChromaCollection write methods (which acquire the lock themselves
to protect MCP/direct callers) compose with miner.mine() (which holds
the outer lock for the entire mine pipeline) without self-deadlock, and
lets the threaded MCP HTTP transport write from a worker thread while the
long-lived writer-lease is held on another thread of the same process.
