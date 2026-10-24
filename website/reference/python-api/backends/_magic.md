# `mempalace.backends._magic`

Source: [`mempalace/backends/_magic.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/_magic.py)

Lock-safe SQLite header probes for the file-backed backends.

``detect()`` on the file-backed backends verifies the SQLite header before
claiming a palace directory (#1893). It runs on essentially every MCP tool
call (``resolve_backend_name`` -> ``detect_backends_for_path``) and used to do
a plain ``open()`` on the very database file the backend already holds a
long-lived WAL connection to. ``SQLiteExactBackend._database_signature``
did the same on the read-only query path.

POSIX fcntl advisory locks belong to the ``(process, inode)`` pair, not to a
descriptor: closing ANY descriptor on that inode drops EVERY lock the process
holds on it. SQLite's unix VFS carries the ``unixInodeInfo`` / ``pUnused``
machinery precisely so that closing one of *its own* connections cannot do
this to a sibling connection, but a descriptor opened outside SQLite is not
covered. A WAL-mode connection keeps a SHARED lock on the main database for
its whole life, and that lock is the only thing stopping another process's
clean ``close()`` from checkpointing and unlinking the ``-wal`` / ``-shm``
sidecars underneath it. After one plain open+close the server was
lock-naked: any external ``sqlite3.connect()`` + ``close()`` orphaned the
sidecars while the server kept writing (successfully, as far as it could
tell) into the unlinked inodes. See #2302 for the same kernel mechanism.

The fix is the one SQLite documents: every read of the database file goes
through SQLite itself. A throwaway ``mode=ro&immutable=1`` connection takes
no locks and creates no sidecars. For ``SQLiteExactBackend``, which shares
Python's ``libsqlite3`` instance, when the throwaway connection closes the unix
VFS parks its descriptor in ``pUnused`` instead of closing it while the writer
connection holds locks on the inode.

However, ChromaDB (1.5+) embeds its own statically linked SQLite inside native
Rust bindings (``chromadb_rust_bindings``), with an entirely separate VFS state
and inode table from Python's ``sqlite3`` module. Closing a throwaway connection
from Python on a live Chroma database issues a libc ``close()``, causing the
Linux kernel to drop Chroma's writer lock. To prevent this on POSIX,
``has_sqlite_magic()`` retains the read-only probe connection for the lifetime
of the process so its descriptor is never closed while the process is alive.
On Windows, where file locking is handle-based and closing handles never drops
sibling locks, the probe connection is closed normally to avoid holding open
file handles that prevent directory deletion. ``os.stat`` opens no descriptor,
so the cheap checks around the probe are lock-safe everywhere.

``has_sqlite_magic`` caches POSITIVE results by ``(st_dev, st_ino)``: once a
file is known to be a SQLite database it stays one until it is replaced
(new inode). Negative results are never cached, because a 0-byte file left
behind by a bare ``sqlite3.connect()`` becomes a real database as soon as the
header is written (#1893).

## Functions

### `read_header_fields`

```python
def read_header_fields(db_path: str) -> Optional[tuple[int, ...]]
```

Return SQLite header fields of ``db_path`` without a plain descriptor.

Reads page 1 through a throwaway ``immutable=1`` connection, which is the
only lock-safe way to look at a database file another connection in this
process may hold POSIX locks on. Returns ``None`` when the file is empty
or is not a SQLite database. Raises ``OSError`` when the file cannot be
stat'ed, matching the old ``open()``-based readers.

### `has_sqlite_magic`

```python
def has_sqlite_magic(db_path: str) -> bool
```

Return True when ``db_path`` is a regular file starting with the SQLite magic.

Opens a plain descriptor only on files SQLite itself refuses to open (which
no connection in this process can be holding), and opens nothing at all on
a path already confirmed in this process (keyed by inode).

### `forget`

```python
def forget(db_path: Optional[str] = None) -> None
```

Drop cached detections (all of them when ``db_path`` is None).

Test/maintenance hook; the inode key makes it unnecessary in normal use.

### `close_retained`

```python
def close_retained() -> None
```

Close all retained probe connections. Test/maintenance hook only.
