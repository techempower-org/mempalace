# `mempalace.palace.backend`

Source: [`mempalace/palace/backend.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace/backend.py)

## Functions

### `resolve_backend_name`

```python
def resolve_backend_name(palace_path: str, explicit: Optional[str] = None) -> str
```

Resolve and validate the selected backend for ``palace_path``.

Public resolution order:

1. Explicit CLI/MCP flag or direct ``get_collection(..., backend=...)``.
2. ``backend`` in ``~/.mempalace/config.json``.
3. ``MEMPALACE_BACKEND``.
4. Detected existing palace artifacts.
5. ``chroma``.

If artifacts for a different backend are already present, raise
``BackendMismatchError`` so normal write paths cannot silently mix storage
formats in one palace directory.

### `backend_requires_single_writer`

```python
def backend_requires_single_writer(backend_name: str) -> bool
```

Return whether a backend needs one process-lifetime writer owner.

Local file-backed backends cannot safely coordinate independent long-lived
clients by serializing only individual calls: each process may retain
SQLite/WAL, FTS, or vector-index state across operations. Unknown and
plugin backends are treated conservatively. Only backends whose storage
service is explicitly responsible for cross-process concurrency opt out.

### `backend_stores_data_locally`

```python
def backend_stores_data_locally(backend_name: str) -> bool
```

Return whether this backend keeps the drawers in the palace directory.

Answered from the ``local_mode`` / ``server_mode`` capability tokens every
in-tree backend declares, so a plugin backend gets the right answer by
declaring the token rather than by being added to a list here.

Callers use this to decide whether a "does the palace directory hold a
database?" precheck means anything. On a service-backed palace it does
not: postgres, pgvector, qdrant and a remote Milvus keep the drawers in
the service, and the directory holds at most sidecars (``dsn.env``,
``hallways.json``, ``tunnels.json``, ``locks/``, ``wal/``).
``PostgresBackend.detect()`` returns ``False`` unconditionally for exactly
that reason. Running the precheck anyway is what made ``purge`` and
``sync`` refuse every Postgres palace with "No palace found at …", leaving
no working bulk delete on that deployment at all (#418).

An unregistered backend name answers ``True`` — the caller keeps its
directory guard rather than skipping a check it may still need.

### `get_backend_for_palace`

```python
def get_backend_for_palace(palace_path: str, explicit: Optional[str] = None)
```

Return the resolved backend instance for ``palace_path``.
