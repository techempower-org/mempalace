# `mempalace.palace`

Source: [`mempalace/palace.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace.py)

palace.py — Shared palace operations.

Consolidates collection access patterns used by both miners and the MCP server.

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

### `clear_validated_embedder_identity`

```python
def clear_validated_embedder_identity(palace_path: Optional[str] = None) -> None
```

Drop cached embedder-identity verdicts so the next open re-checks.

Read-only opens of an empty collection can mark a key as validated without
recording identity on disk (``create=False``). When MCP later promotes that
reader to a writable owner, the writable open must re-run enforcement so
the first drawers still get labelled with the active model.

### `get_collection`

```python
def get_collection(palace_path: str, collection_name: Optional[str] = None, create: bool = True, backend: Optional[str] = None, read_only: bool = False, _skip_identity_check: bool = False)
```

Get the palace collection through the backend layer.

``read_only=True`` asks local backends to open storage without schema
initialization, migrations, or metadata writes. Backends that support a
genuine read-only mode receive it through the backend ``options`` mapping.

`collection_name=None` (the fork-side convention) and
`collection_name=DEFAULT_COLLECTION_NAME` (#665's convention) both mean
"use the configured default collection." Either resolves through
`MempalaceConfig().collection_name` (env `MEMPALACE_COLLECTION_NAME`
overrides the config file). Fork-side callers in `searcher.py`,
`convo_miner.py`, `sweeper.py`, etc. pass None explicitly; #665 was
written assuming all callers omitted the keyword and got the default,
which broke the None path. Accepting both forms is the minimal shim
until fork-side callers migrate to the new convention.

``backend`` explicitly selects a backend (CLI ``--backend`` / RFC 001);
when omitted, resolution follows ``resolve_backend_name`` (config, env,
detected artifacts, chroma default) with mismatch protection.

``_skip_identity_check`` bypasses the embedder-identity enforcement so the
``set-embedder`` override path can open a palace whose recorded model
differs from the current one (the very state it exists to repair).

### `set_palace_embedder_identity`

```python
def set_palace_embedder_identity(palace_path: str, model: Optional[str] = None, *, force: bool = False, backend: Optional[str] = None, collection_name: Optional[str] = None)
```

Record (or force-override) a palace collection's embedder identity (RFC 001).

Backs ``mempalace palace set-embedder``. Returns ``(old, new)`` identities.
Without ``force``, refuses to overwrite an existing identity that names a
different model (the user must confirm they know the vectors are
compatible). Opens with the identity check skipped so a mismatched palace —
the exact state being repaired — can be opened at all.

### `get_closets_collection`

```python
def get_closets_collection(palace_path: str, create: bool = True, backend: Optional[str] = None)
```

Get the closets collection — the searchable index layer.

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

### `build_closet_lines`

```python
def build_closet_lines(source_file, drawer_ids, content, wing, room, drawer_metas = None)
```

Build compact closet pointer lines from drawer content.

Returns a LIST of lines (not joined). Each line is one complete topic
pointer — never split across closets.

Legacy format (3 segments): ``topic|entities|→drawer_ids``
Tier 6a format (4 segments): ``topic|entities|YYYY-MM-DD:Lstart-Lend|→drawer_ids``

When ``drawer_metas`` is provided and the first meta carries both
``line_start``/``line_end`` plus a parseable ``filed_at``, the 4-segment
form is emitted so retrieval can jump to the right span. Otherwise the
legacy 3-segment form is used — backward compat for drawers filed before
Tier 6a and for direct callers that don't have metadata handy.

### `purge_file_closets`

```python
def purge_file_closets(closets_col, source_file: str) -> None
```

Delete every closet associated with ``source_file``.

Call this before ``upsert_closet_lines`` on a re-mine so stale topics
from a prior schema/version don't survive in the closet collection.
Mirrors the drawer-purge step in process_file().

### `upsert_closet_lines`

```python
def upsert_closet_lines(closets_col, closet_id_base, lines, metadata)
```

Write topic lines to closets, packed greedily without splitting a line.

Closets are deterministically numbered (``..._01``, ``..._02``, …) and
each ``upsert`` fully overwrites the prior content at that ID. Callers
are expected to ``purge_file_closets`` first when re-mining a source
file so stale-numbered closets from larger prior runs don't leak.

Returns the number of closets written.

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

### `file_already_mined`

```python
def file_already_mined(collection, source_file: str, check_mtime: bool = False, extract_mode: Optional[str] = None) -> bool
```

Check if a file has already been filed in the palace.

Returns False (so the file gets re-mined) when:
  - no drawers exist for this source_file
  - the stored `normalize_version` is missing or older than the current
    schema (triggers silent rebuild after a normalization upgrade)
  - `check_mtime=True` and the file's mtime differs from the stored one

When check_mtime=True (used by the project miner, and by the convo
miner's in-lock recheck), also re-mines on content change. Conversation
transcripts are NOT assumed immutable: a Claude Code session keeps
appending to its own file while active, and /compact or /clear can
rewrite one in place. The convo miner's bulk skip-check uses
prefetch_mined_set()'s stored mtimes instead of calling this function
per file (same mtime-aware decision, without the O(n) per-file query
cost); this function's check_mtime=True path remains its per-file,
lock-held race-condition recheck.

When extract_mode is set (used by convo miner), idempotency is scoped to
that extraction mode so exchange-mode and general-mode drawers can coexist
for the same source transcript. Legacy drawers without extract_mode are
treated as exchange-mode drawers.

A drawer whose metadata carries ``chunk_total`` (see #21) is only
counted toward a match once its stored_mtime group has accumulated at
least that many drawers -- guarding against a mid-file crash between
upsert batches, where the surviving drawers share the current mtime
(the file itself was never touched) but are short of the full set. A
drawer with no ``chunk_total`` (legacy rows, or a single-shot
``add_drawer()`` call with no partial-batch risk) is trusted on its own,
exactly as before.

### `prefetch_mined_set`

```python
def prefetch_mined_set(collection, extract_mode: Optional[str] = None) -> dict[str, Optional[float]]
```

Pre-fetch source_file -> stored source_mtime for files already mined
at the current NORMALIZE_VERSION, in one bulk pass instead of one
ChromaDB query per file.

Return type is a dict rather than a bare set so callers get mtime
awareness "for free": conversation transcripts are not immutable once
mined (a Claude Code session keeps appending to the same file while
active, and /compact or /clear can rewrite one in place), so "we've
seen this source_file before" is not suffient to skip it -- the caller
must also confirm its current on-disk mtime still matches what was
stored. `if src in mined_set` still means the same thing as the old
set-based return (dict `in` checks keys); a caller that wants staleness
detection reads `mined_set[src]` and compares against
os.path.getmtime(src) itself. `None` means either no mtime was ever
stored (drawers written before this field existed) or getmtime failed
when the drawer was written -- both should be treated as stale.

When extract_mode is set, mirrors file_already_mined(..., extract_mode=...)
so conversation mines skip per extraction mode rather than per source file.

Completeness mirrors :func:`file_already_mined`'s ``chunk_total`` rule
(#2183): a source that only has a mid-file partial (surviving drawers
share the current mtime but are short of ``chunk_total``) is **omitted**
from the result so the bulk skip path re-mines instead of permanently
stranding the missing exchanges. Drawers with no ``chunk_total``
(legacy rows, registry sentinels) are trusted on their own, as before.

The convo miner walks thousands of transcript files; per-file
`collection.get(where={"source_file": X})` costs ~2s on a 150k-drawer
palace, making a 2000-file sweep take >1h of pure skip-checking. This
helper drops that to a single paginated scan plus O(1) lookups.

### `prefetch_content_hashes`

```python
def prefetch_content_hashes(collection, extract_mode: Optional[str] = None) -> dict[tuple[str, str], str]
```

Pre-fetch (wing, content_hash) -> source_file for drawers already
filed at the current NORMALIZE_VERSION, in one bulk pass.

Repeated exports from Claude/ChatGPT land under a new filename each run
(timestamped bundle, regenerated slug, etc.) even when the conversation
itself hasn't changed. `prefetch_mined_set` only recognizes a file as
already-mined by its exact path, so the same conversation re-exported
under a new path always looked "new" and got re-mined as a duplicate
drawer. This does the same bulk scan but keyed on the SHA-256 of the
normalized transcript text, so the convo miner can recognize "this exact
conversation is already filed under a different path" and skip it.

Keyed by (wing, content_hash) rather than content_hash alone — mining
the same transcript into a second wing is a deliberate re-file, not a
duplicate, and should produce real drawers in that wing rather than
just the registry sentinel.

A drawer's ``content_hash`` metadata may hold several comma-joined
SHA-256 hashes: a privacy-export bundle normalizes to one conversation
per drawer set, but the hash is computed per conversation so that a
re-export with one new conversation added doesn't change the hash of
the ones that didn't. Only the first source_file seen for a given
(wing, hash) pair is kept — good enough to detect and skip a repeat,
the point is not to track every alias.
