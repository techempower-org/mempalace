# `mempalace.backends._inproc_sqlite`

Source: [`mempalace/backends/_inproc_sqlite.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/_inproc_sqlite.py)

Python ``sqlite3`` access to a database another SQLite library in this process holds.

ChromaDB 1.5+ opens ``chroma.sqlite3`` through the SQLite statically linked
into ``chromadb_rust_bindings``. mempalace's fast paths (status, taxonomy,
``list_drawers``, BM25, the integrity gate, repair) open the same file through
Python's ``sqlite3``: a second, independent SQLite library in the same
process. Two consequences, both measured on Linux:

1. POSIX fcntl locks belong to the (process, inode) pair. When a Python
   connection closes its descriptor, the kernel drops every lock the process
   holds on that file, Chroma's included: the SHARED lock a WAL connection
   keeps for its whole life and the DMS lock on ``-shm``. SQLite's unix VFS
   parks a descriptor instead of closing it only while a connection of *its
   own* library holds locks on the inode. After a single fast-path read Chroma
   never takes those locks back, so the palace stays lock-naked to every
   other process: a newcomer takes the "first opener" path and truncates
   ``-shm``, or a clean close checkpoints and unlinks ``-wal``/``-shm`` under
   the live writer.
2. fcntl locks never conflict within one process, so neither library sees the
   other's. A ``quick_check`` running while Chroma commits reads a torn page
   set (``database disk image is malformed``), and a close in that window
   also releases the write transaction's locks.

This module is the one door for that access.

* :func:`palace_db_lock` is a process-local re-entrant lock per database file.
  Every connection opened here holds it from open to close, and
  ``ChromaBackend`` holds it around its writes and client opens, so the two
  libraries never overlap inside the process. That fixes (2) everywhere.
* On Linux, before a reader opens, an idle *anchor* connection is kept on the
  database inode for the life of the process. It reads nothing but the schema
  cookie, which in WAL mode leaves it holding SHARED and the wal-index mapping,
  so Python's SQLite parks the descriptor of every per-call connection that
  closes afterwards instead of closing it, and Chroma keeps its locks (1).
  The anchor also holds open-file-description (OFD) read locks on SQLite's
  SHARED range and on the ``-shm`` DMS byte. OFD locks belong to a descriptor
  rather than to the process: no other close releases them, and they conflict
  with the process-associated locks of this very process. Without them a
  long-lived Python connection is worse than none. Chroma's last close takes
  EXCLUSIVE straight past the anchor's in-process locks, the next open
  recreates ``-wal``/``-shm``, and every Python connection stays attached to
  the stale wal-index, blind to later commits (measured: a fresh connection saw
  1 of 3 committed rows). With the guards held, nobody can take EXCLUSIVE or
  the DMS write lock while the anchor lives, exactly as if another process held
  a WAL reader. In rollback-journal mode the anchor holds nothing between reads
  and Chroma holds nothing between transactions, so the lock covers the close.
* The reads themselves always use a fresh connection. A long-lived one is not a
  substitute: after Chroma's commits, Python's SQLite (3.45.1 measured) kept
  reporting ``malformed inverted index for FTS5 table`` from it, in either
  journal mode and after Chroma had closed, while fresh connections and a later
  ``integrity_check`` said ``ok``.
* Everywhere else connections open and close per call, still under the lock.
  Windows locks belong to a handle, so a close never drops another handle's
  locks. POSIX platforms without OFD locks (macOS) keep the same promise with
  a helper process: its idle WAL connection retains another process's SHARED
  and DMS locks, so they conflict with Chroma where in-process fcntl locks
  would not, and closing a connection in this process cannot drop them. Its
  read transaction ends after priming: retaining a snapshot would prevent
  checkpoints from progressing beyond it for the life of the parent. The
  in-process anchor is installed only after that helper is holding, so
  Chroma cannot recreate ``-wal``/``-shm`` under a parked wal-index.

## Classes

### `class PalaceSqliteConnection`

A ``sqlite3.Connection`` stand-in that holds :func:`palace_db_lock`.

Everything but ``close`` and the context-manager protocol is forwarded to
the real connection. ``close()`` closes the connection and releases the
lock; calling it again does nothing. Leaving a ``with`` block commits or
rolls back as ``sqlite3`` does and then closes.

#### `__init__`

```python
def __init__(self, conn: sqlite3.Connection, release: Callable[[], None])
```

#### `close`

```python
def close(self) -> None
```

## Functions

### `palace_db_lock`

```python
def palace_db_lock(db_path) -> threading.RLock
```

Return the process-local lock that serializes all access to ``db_path``.

Re-entrant, so a thread that already holds it (a Chroma client open that
runs a migration helper, say) can take it again.

### `open_reader`

```python
def open_reader(db_path, *, timeout: Optional[float] = None) -> PalaceSqliteConnection
```

Open ``db_path`` for reading under :func:`palace_db_lock`.

Same failure contract as :func:`mempalace.config.connect_sqlite_read`
(``sqlite3.Error`` for a database SQLite cannot open, ``ValueError`` for a
path the URI cannot carry). The caller must ``close()`` the result, which
releases the lock.

### `open_writer`

```python
def open_writer(db_path, **connect_kwargs) -> PalaceSqliteConnection
```

``sqlite3.connect(db_path, **connect_kwargs)`` under :func:`palace_db_lock`.

For maintenance writes (migrations, FTS rebuilds, repair) while Chroma's
handles are closed or not yet open; the lock keeps this process's own Chroma
writes out while the connection is open. The caller must close it.

### `release`

```python
def release(db_path) -> None
```

Close the anchor on ``db_path`` and drop its guards.

For maintenance that needs the file to itself (VACUUM) once Chroma's handles
are closed: closing drops this process's POSIX locks on the file.

### `release_all`

```python
def release_all() -> None
```

Close every anchor and lock holder. Tests and shutdown only (see :func:`release`).
