# `mempalace.repair`

Source: [`mempalace/repair.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/repair.py)

repair.py — Scan, prune corrupt entries, and rebuild HNSW index
================================================================

When ChromaDB's HNSW index accumulates duplicate entries (from repeated
add() calls with the same ID), link_lists.bin can grow unbounded —
terabytes on large palaces — eventually causing segfaults.

This module provides four operations:

  status  — compare sqlite vs HNSW element counts (read-only health check)
  scan    — find every corrupt/unfetchable ID in the palace
  prune   — delete only the corrupt IDs (surgical)
  rebuild — extract all drawers, delete the collection, recreate with
            correct HNSW settings, and upsert everything back

The rebuild backs up ONLY chroma.sqlite3 (the source of truth), not the
full palace directory — so it works even when link_lists.bin is bloated.

Usage (standalone):
    python -m mempalace.repair status
    python -m mempalace.repair scan [--wing X]
    python -m mempalace.repair prune --confirm
    python -m mempalace.repair rebuild

Usage (from CLI):
    mempalace repair
    mempalace repair-scan [--wing X]
    mempalace repair-prune --confirm

## Classes

### `class RebuildCollectionError(RuntimeError)`

Raised when temp rebuild fails, carrying whether the live swap happened.

#### `__init__`

```python
def __init__(self, message: str, *, live_replaced: bool)
```

### `class TruncationDetected(Exception)`

Raised by :func:`check_extraction_safety` when extraction looks short.

Carries the human-readable abort message so callers (CLI ``cmd_repair``,
``rebuild_index``) can print and exit consistently without re-deriving
the wording.

#### `__init__`

```python
def __init__(self, message: str, sqlite_count: 'int | None', extracted: int)
```

### `class SqliteIntegrityStatus`

Whether a quick_check verdict exists for a palace, and what it says.

``checked`` False means no probe ran, so an empty ``errors`` says nothing
about the database. That is the distinction :func:`sqlite_integrity_errors`
cannot express: it answers ``[]`` both for a database quick_check found
intact and for a palace that has none to open.

``errors`` is a tuple rather than a list so ``frozen=True`` means what it
says: a list field would leave the generated ``__hash__`` raising
``TypeError`` on every call, and would let a caller append to a verdict it
was handed.

### `class RebuildPartialError(Exception)`

Raised when ``rebuild_from_sqlite`` fails partway through upserts.

Carries enough state for the user (or CLI) to recover: the
per-collection counts that succeeded, the collection that failed,
the dest path holding the partial palace, and the archive path
(when an in-place rebuild had moved the original aside). Re-raises
the underlying chromadb error as ``__cause__``.

#### `__init__`

```python
def __init__(self, message: str, *, partial_counts: dict[str, int], failed_collection: str, dest_palace: str, archive_path: Optional[str])
```

### `class RebuildCleanupError(Exception)`

Raised when all recoverable rows landed but final cleanup failed.

The destination is intentionally retained for inspection, and an in-place
rebuild's original archive remains untouched. Callers must not treat this
as success because the derived FTS5 index has not been verified clean.

#### `__init__`

```python
def __init__(self, message: str, *, counts: dict[str, int], dest_palace: str, archive_path: Optional[str])
```

### `class MaxSeqIdVerificationError(RuntimeError)`

Raised when post-repair detection still sees poisoned rows.

## Functions

### `scan_palace`

```python
def scan_palace(palace_path = None, only_wing = None, collection_name: Optional[str] = None)
```

Scan the palace for corrupt/unfetchable IDs.

Probes in batches of 100, falls back to per-ID on failure.
Writes corrupt_ids.txt to the palace directory for the prune step.

Returns (good_set, bad_set).

### `prune_corrupt`

```python
def prune_corrupt(palace_path = None, confirm = False, collection_name: Optional[str] = None)
```

Delete corrupt IDs listed in corrupt_ids.txt.

### `check_extraction_safety`

```python
def check_extraction_safety(palace_path: str, extracted: int, confirm_truncation_ok: bool = False, collection_name: Optional[str] = None) -> None
```

Cross-check that ``extracted`` matches the SQLite ground truth.

Two signals trip the guard:

1. **Strong** — ``chroma.sqlite3`` reports more drawers than were
   extracted. This is the user-reported #1208 case: 67 580 on disk,
   10 000 came back through the chromadb collection layer, repair
   would have destroyed the difference.
2. **Weak** — extracted count equals exactly ``CHROMADB_DEFAULT_GET_LIMIT``
   AND the SQLite check couldn't run (schema drift, locked file).
   Hitting the chromadb default ``get()`` cap exactly is suspicious
   enough to refuse without explicit acknowledgement.

Raises :class:`TruncationDetected` with a printable message when the
guard fires. Does nothing on safe extractions or when
``confirm_truncation_ok`` is set.

### `sqlite_drawer_count`

```python
def sqlite_drawer_count(palace_path: str, collection_name: Optional[str] = None) -> 'int | None'
```

Count rows in ``chroma.sqlite3.embeddings`` for the drawers collection.

Used as an independent ground-truth check against the chromadb
collection-layer ``count()`` / ``get()``: when the on-disk SQLite
row count exceeds the extraction count, the segment metadata is
stale and repair would destroy the difference.

Returns ``None`` when the schema isn't readable (chromadb version
drift, missing tables, locked file), and without opening anything when
nothing resolves under the path or when the path names a FIFO. Callers
treat ``None`` as "unknown" and fall back to the cap-detection check.

### `sqlite_integrity_status`

```python
def sqlite_integrity_status(palace_path: str) -> SqliteIntegrityStatus
```

Run the quick_check probe and report whether it produced a verdict.

Callers that state an integrity result to an operator want this rather
than :func:`sqlite_integrity_errors`, whose empty list cannot separate a
clean database from one that was never opened.

### `sqlite_integrity_errors`

```python
def sqlite_integrity_errors(palace_path: str) -> list[str]
```

Return SQLite quick_check errors for chroma.sqlite3.

The repair rebuild path eventually calls Chroma's delete_collection().
If the SQLite layer has corrupt secondary indexes or FTS5 shadow pages,
Chroma can raise an opaque SQLITE_CORRUPT_INDEX / code 779 error before
repair reaches the HNSW rebuild.

Run a direct SQLite quick_check first so repair can fail with a clear,
actionable message before invoking Chroma's destructive collection-delete
path.

An empty list means one of two things and cannot tell them apart: the
check ran and found nothing, or the palace provably has no database to
check. Callers stating a result to an operator want
:func:`sqlite_integrity_status` instead. A path that resolves to a
directory entry but cannot be opened — a dangling symlink, a database
under a directory this process may not enter — is reported here as an
error, since the probe did fail.

### `print_sqlite_integrity_abort`

```python
def print_sqlite_integrity_abort(palace_path: str, errors: list[str]) -> None
```

Print a clear repair abort message for SQLite-layer corruption.

### `maybe_autoheal_fts5_index`

```python
def maybe_autoheal_fts5_index(palace_path: str, errors: list[str], *, progress = print) -> list[str]
```

Rebuild a malformed FTS5 inverted index in place; return remaining errors.

The repair preflight aborts when ``PRAGMA quick_check`` reports SQLite-layer
corruption. After concurrent killed-mid-write mines (#1596) the common
failure is an isolated ``malformed inverted index for FTS5 table``, and
``rebuild`` recovers it by regenerating the index from
``embedding_fulltext_search_content``.

That error says the index and the content table disagree; it does not say
which of them is wrong. So the content table is checked against
``embedding_metadata`` first, and any row that disagrees is restored from it
before the rebuild reads it — otherwise a rebuild over a damaged content
table would overwrite an index that still held the drawer's own words and
leave quick_check clean, reporting success for a palace that lost full-text
reach. Both writes are derived from ``embedding_metadata``; rows that
table cannot speak for are left untouched.

Everything happens under the palace write lock (so a live mine cannot race
it) and in one transaction, which is why a restored row cannot outlive a
rebuild that then fails. Returns the remaining quick_check errors — empty
when the heal succeeded. Broader corruption, a lock held by another writer,
a content table that cannot be checked or cannot be brought into agreement,
a rebuild failure, or a quick_check still dirty afterwards leaves ``errors``
unchanged so the caller still aborts with the banner.

### `index_read_recovery_guidance`

```python
def index_read_recovery_guidance() -> str
```

Recovery guidance for a failed drawer-index read in the legacy paths.

Both ``cmd_repair`` (cli.py) and :func:`rebuild_index` read the drawers
collection via ``Collection.count()`` as their first step. The common
reason that read raises is the chromadb compactor failing to apply the
WAL into the HNSW segment (``InternalError: Failed to apply logs to the
hnsw segment writer``, issues #1308 / #1843): the on-disk HNSW index is
corrupt while the rows stay intact in ``chroma.sqlite3``, so
:func:`rebuild_from_sqlite` (``repair --mode from-sqlite``) recovers them
and re-mining would needlessly drop drawers added through the MCP server
and diary entries that have no source file.

The other thing that strands this read is a live MemPalace server or
mine still holding the palace open, so the guidance says to stop it and
retry before assuming corruption. Worded conditionally because the bare
``except Exception`` cannot prove which case it caught. Returned as a
pre-indented block so the ``print``-based CLI path and the
``progress``-callable rebuild path emit it unchanged.

### `maybe_repair_poisoned_max_seq_id_before_rebuild`

```python
def maybe_repair_poisoned_max_seq_id_before_rebuild(palace_path: str, *, backup: bool = True, dry_run: bool = False, assume_yes: bool = False) -> 'dict | None'
```

Run non-destructive max_seq_id repair before a rebuild if needed.

A poisoned ``max_seq_id`` row can make Chroma believe it has already
consumed every row in ``embeddings_queue``. Writes then report success
because they land in the queue, but they never become visible in
``embeddings``.

If this precise corruption is present, do the narrow bookmark repair and
stop instead of continuing into the legacy rebuild path. The rebuild path
extracts only already-visible embeddings and can discard queued writes.

### `rebuild_index`

```python
def rebuild_index(palace_path = None, confirm_truncation_ok: bool = False, collection_name: Optional[str] = None, progress: Optional[Callable[[str], None]] = None)
```

Rebuild the HNSW index from scratch.

1. Extract all drawers via ChromaDB get()
2. Cross-check against the SQLite ground truth (#1208 guard)
3. Back up ONLY chroma.sqlite3 (not the bloated HNSW files)
4. Delete and recreate the collection with hnsw:space=cosine
5. Upsert all drawers back

``confirm_truncation_ok`` overrides the safety guard from step 2.
Set to ``True`` only when you have independently verified that the
palace genuinely contains exactly the extracted number of drawers
(typically only a concern for palaces sized at exactly 10 000 rows).

``progress`` is the callable used for status output. Defaults to
:class:`_DefaultProgress` which prints with elapsed/rate/ETA
annotations on ``Staged N/M`` and ``Re-filed N/M`` lines. Pass a
custom callable (e.g. a daemon-side capture for HTTP status, or a
silent ``lambda *_: None`` for tests) to override.

### `extract_via_sqlite`

```python
def extract_via_sqlite(palace_path: str, collection_name: str) -> Iterator[tuple[str, str, dict]]
```

Yield ``(embedding_id, document, metadata)`` for every row in
``collection_name``'s metadata segment by reading ``chroma.sqlite3``
directly.

Bypasses the chromadb client entirely — never opens a
``PersistentClient``, never imports hnswlib, never invokes the
HNSW segment writer. This is the recovery path for palaces where
``Collection.count()`` / ``Collection.get()`` raise ``InternalError``
because the compactor cannot apply WAL logs to the HNSW segment
(#1308). The drawer rows are still on disk in
``embeddings`` + ``embedding_metadata``; the corruption lives in the
on-disk index files, not the SQLite tables.

Resolution rule for chromadb's typed metadata columns: each
``embedding_metadata`` row stores its value in exactly one of
``string_value`` / ``int_value`` / ``float_value`` / ``bool_value``;
we pick the first non-NULL column in that order. Rows where every
typed column is NULL are dropped (chromadb never writes that shape).
The ``chroma:document`` key is removed from the metadata dict and
returned as the document; this matches how chromadb itself stores
``add(documents=...)``.

Driven from ``embeddings`` (LEFT JOIN ``embedding_metadata``), not
the other way around: an embedding with zero ``embedding_metadata``
rows — a sparse historical write with no ``chroma:document`` and no
other key, the same condition ``_extract_drawers`` already sanitizes
for the collection-layer path, see #1458 — must still be yielded
with an empty metadata dict, not silently excluded by the join.

Silent on missing palace, missing ``chroma.sqlite3``, or unknown
collection name — yields nothing. Callers that need to distinguish
"empty collection" from "collection not present" should query
:func:`sqlite_drawer_count` first.

### `rebuild_from_sqlite`

```python
def rebuild_from_sqlite(source_palace: str, dest_palace: str, *, archive_existing_dest: bool = False, batch_size: int = 1000, dry_run: bool = False) -> dict[str, int]
```

Rebuild a palace by reading drawers from ``source_palace``'s
``chroma.sqlite3`` and upserting them into a fresh palace at
``dest_palace``.

Recovery path for the #1308 failure mode: the chromadb client raises
``InternalError: Failed to apply logs to the hnsw segment writer``
on every operation that touches the index (``count``, ``get``,
``query``), but the underlying SQLite tables are intact. Both the
legacy ``rebuild_index`` and the inline ``cli.cmd_repair`` path call
``Collection.count()`` as their first read — exactly the call that
fails — so neither can recover this class of corruption. This
function bypasses the chromadb read path entirely via
:func:`extract_via_sqlite`.

Re-embeds documents at upsert time using the configured embedding
function; the original HNSW vectors are not preserved (they live in
the corrupt ``data_level0.bin`` / ``link_lists.bin``, not in
SQLite). Acceptable for a corruption-recovery flow because the
embedding model is deterministic — same model + same document text
yields semantically equivalent search results.

``archive_existing_dest`` controls behavior when ``dest_palace``
already exists:

* ``False`` (default) — refuse with a clear message. Callers must
  manually move the existing palace aside first.
* ``True`` — rename ``dest_palace`` to
  ``&lt;dest_palace>.pre-rebuild-&lt;timestamp>`` and read from there
  instead. Used by the in-place CLI flow where ``--source`` defaults
  to the same path as ``--palace``.

``dry_run`` (CLI: ``--dry-run``) previews the rebuild without making any
change: source validation runs as normal, then per-collection row counts
are read from the source SQLite and printed, and the function returns
those would-be counts *without* archiving the existing palace, taking the
mine-lock, creating collections, or re-embedding (#2095, #2133). Useful
before a multi-hour rebuild on a large palace. A dry run returns a
populated dict (one key per recoverable collection) so CLI callers treat
it as success; a validation refusal still returns ``&#123;}`` exactly as a real
run would. If SQLite row counts cannot be read, the preview fails closed
with ``&#123;}`` rather than inventing zeros.

Returns a ``&#123;collection_name: row_count}`` dict so callers (CLI,
tests) can verify the per-collection rebuild count without parsing
stdout. A successful rebuild always returns a dict with one key per
recoverable collection (values may be ``0`` when a collection is
legitimately empty in the source). The empty dict ``&#123;}`` is reserved
for validation refusals (missing source DB, refusing to overwrite an
existing dest, in-place mode without ``archive_existing_dest``); CLI
callers should treat ``&#123;}`` as an error and exit non-zero so CI and
scripts can distinguish "invalid inputs" from "successful recovery
that found zero rows." Raises :class:`RebuildPartialError` if a
chromadb upsert fails partway through; the dest palace is left in
place so the user can inspect what landed, and the in-place archive
(when applicable) is reported in the error so the user can re-run
against it. Raises :class:`RebuildCleanupError` if all rows land but the
required FTS5 rebuild, VACUUM, or final quick_check fails; this prevents a
structurally unverified recovery from being reported as complete.

.. warning::

   In-place mode (``source_palace == dest_palace`` with
   ``archive_existing_dest=True``) calls
   ``chromadb.api.client.SharedSystemClient.clear_system_cache()`` to
   drop chromadb's process-wide System registry — required because
   an existing cached System built against the original palace will
   refuse ``create_collection`` after the dir is renamed (chromadb
   still thinks the collections exist). This invalidates any
   PersistentClient instances held elsewhere in the same process for
   *any* palace, not just this one. Do not call this function from
   inside a long-running mempalace process (MCP server, daemon)
   while other callers hold live ``PersistentClient`` references —
   use the CLI in a separate process instead. Cross-palace use
   (``source != dest``) does not touch the cache.

Note on metadata fidelity: the resolution rule
(``string_value`` → ``int_value`` → ``float_value`` → ``bool_value``)
matches the precedent in :mod:`mempalace.migrate`. ChromaDB 0.4.x
occasionally wrote booleans as ``int_value=0/1``; those will
round-trip as ``int`` rather than ``bool`` after this rebuild. This
is a known divergence and matches the existing migrate-path
behavior.

### `resolve_repair_preflight_errors`

```python
def resolve_repair_preflight_errors(palace_path: str, errors: list[str], *, dry_run: bool, progress = print) -> list[str]
```

Return the quick_check errors that still block a repair.

A real run heals an isolated malformed FTS5 inverted index in place and
carries on (#1596). ``--dry-run`` must not perform that write, so it
classifies the errors with the same :func:`_errors_are_isolated_fts5`
predicate the real path gates on: an isolated FTS5 error is reported and
cleared, anything broader still aborts. Without this a preview would print
the ABORT banner — offline ``sqlite3 .recover``, recreate the FTS5 table —
for a palace the tool repairs by itself.

The prediction is deliberately the optimistic branch, and it is stated as
an attempt rather than a promise: the real heal still returns the errors
unchanged when another process holds the mine lock, when the content table
cannot be checked against ``embedding_metadata`` or cannot be brought into
agreement with it, when the rebuild raises, or when ``quick_check`` is still
dirty afterwards. A dry run cannot tell those apart without taking the lock
and writing, which is exactly what it must not do, so the wording names them
instead.

### `status`

```python
def status(palace_path = None, collection_name: Optional[str] = None) -> dict
```

Read-only health check: compare sqlite vs HNSW element counts.

Catches the #1222 failure mode where chromadb's HNSW segment freezes
at a stale ``max_elements`` while sqlite keeps accumulating rows.
Once the divergence is large enough, every tool call segfaults when
chromadb tries to load the undersized HNSW. Running ``mempalace
repair-status`` *before* opening the segment lets the operator
discover the problem without crashing the MCP server.

The check itself never opens a chromadb client and never imports
hnswlib — it reads ``chroma.sqlite3`` and ``index_metadata.pickle``
directly via :func:`mempalace.backends.chroma.hnsw_capacity_status`.

Returns the capacity-status dict (also printed), or a one-key dict in its
place: ``status="unknown"`` when no palace directory is reachable at the
given path or when ``chroma.sqlite3`` resolves to a named pipe,
``status="uninitialized"`` when the palace directory is there but nothing
resolves under ``chroma.sqlite3``, and ``status="empty"`` when the
database is readable and holds no drawers.

A state this gate cannot settle takes neither early return: the capacity
report below answers for it instead, returning the ``&#123;"drawers": …,
"closets": …}`` shape a healthy palace returns with both counts printed as
unreadable. That report reaches its verdict through its own probes, not by
opening the file: with ``sqlite3.connect`` instrumented, an unreadable
directory, a broken link and a symlink loop each produce zero opens of
``chroma.sqlite3``, while the same instrumentation counts opens on a
healthy palace.

### `repair_max_seq_id`

```python
def repair_max_seq_id(palace_path: str, *, segment: Optional[str] = None, from_sidecar: Optional[str] = None, threshold: int = MAX_SEQ_ID_SANITY_THRESHOLD, backup: bool = True, dry_run: bool = False, assume_yes: bool = False) -> dict
```

Un-poison ``max_seq_id`` rows corrupted by ``_fix_blob_seq_ids`` misfire.

The old shim ran ``int.from_bytes(blob, 'big')`` across every BLOB
``max_seq_id.seq_id`` row, including chromadb 1.5.x's native
``b'\x11\x11' + ASCII digits`` format. That conversion yields a
~1.23e18 integer that silently suppresses every subsequent
``embeddings_queue`` write for the affected segment. This command
restores clean values either from a pre-corruption sidecar DB
(exact) or heuristically (``MAX(embeddings.seq_id)`` over the owning
collection).
