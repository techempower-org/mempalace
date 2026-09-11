# `mempalace.miner`

Source: [`mempalace/miner.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/miner.py)

miner.py — Files everything into the palace.

Reads mempalace.yaml from the project directory to know the wing + rooms.
Routes each file to the right room based on content.
Stores verbatim chunks as drawers. No summaries. Ever.

## Classes

### `class GitignoreMatcher`

Lightweight matcher for one directory's .gitignore patterns.

#### `__init__`

```python
def __init__(self, base_dir: Path, rules: list)
```

#### `from_dir`

```python
def from_dir(cls, dir_path: Path)
```

#### `from_patterns`

```python
def from_patterns(cls, base_dir: Path, patterns)
```

Create a matcher from an explicit list of gitignore-inspired pattern strings.

Supports a gitignore-inspired subset of pattern syntax for
``exclude_patterns`` in ``mempalace.yaml``.  Patterns are parsed by
the same rule parser used for ``.gitignore`` files, so familiar
constructs (trailing ``/`` for dir-only, leading ``/`` for anchoring,
``!`` negation, ``**`` globs) work as expected.  Full gitignore
semantics are not guaranteed for every edge case.

``patterns`` must be a list of strings.  A single string is coerced
to a one-element list for convenience.  Non-string entries are
converted via ``str()``.

#### `matches`

```python
def matches(self, path: Path, is_dir: bool = None)
```

## Functions

### `load_gitignore_matcher`

```python
def load_gitignore_matcher(dir_path: Path, cache: dict)
```

Load and cache one directory's .gitignore matcher.

### `is_gitignored`

```python
def is_gitignored(path: Path, matchers: list, is_dir: bool = False) -> bool
```

Apply active .gitignore matchers in ancestor order; last match wins.

### `should_skip_dir`

```python
def should_skip_dir(dirname: str) -> bool
```

Skip known generated/cache directories before gitignore matching.

### `normalize_include_paths`

```python
def normalize_include_paths(include_ignored: list) -> set
```

Normalize comma-parsed include paths into project-relative POSIX strings.

### `is_exact_force_include`

```python
def is_exact_force_include(path: Path, project_path: Path, include_paths: set) -> bool
```

Return True when a path exactly matches an explicit include override.

### `is_force_included`

```python
def is_force_included(path: Path, project_path: Path, include_paths: set) -> bool
```

Return True when a path or one of its ancestors/descendants was explicitly included.

### `load_config`

```python
def load_config(project_dir: str) -> dict
```

Load mempalace.yaml from project directory (falls back to mempal.yaml).

### `detect_room`

```python
def detect_room(filepath: Path, content: str, rooms: list, project_path: Path) -> str
```

Route a file to the right room.
Priority:
1. Folder path exactly matches a room name or keyword
2. Filename exactly matches a room name or keyword
3. Content keyword scoring (word-boundary matching)
4. Fallback: "general"

Fork-ahead: stricter than upstream's substring-match. Fork tests in
test_miner.py guarantee that a folder named ``components`` does NOT
route to a room whose keyword is ``component`` (substring would match);
a folder named ``src`` does not match anything just because ``src`` is
a substring of other words; and content scoring uses word boundaries
so ``api`` in ``capital`` doesn't bump the backend score.

### `chunk_text`

```python
def chunk_text(content: str, source_file: str, chunk_size: int = None, chunk_overlap: int = None, min_chunk_size: int = None, *, symbol_header_prefix = None) -> list
```

Split content into drawer-sized chunks.
Tries to split on paragraph/line boundaries.
Returns list of &#123;"content": str, "chunk_index": int, "line_start": int, "line_end": int}

``line_start`` / ``line_end`` are 1-indexed line numbers in the stripped
source, giving an approximate locator for where the chunk came from.
Closet pointers (Tier 6a) use this to emit ``YYYY-MM-DD:L42-L78`` segments
so retrieval can jump straight to the right span without opening the
whole drawer.

Optional ``chunk_size`` / ``chunk_overlap`` / ``min_chunk_size`` params
override module-level defaults when provided (upstream #1024).

Args:
    content: text to chunk.
    source_file: file path used for room/topic inference and (when
        ``symbol_header_prefix`` is supplied) chunk enrichment.
    chunk_size: max chars per chunk; falls back to ``CHUNK_SIZE``.
    chunk_overlap: chars of overlap between adjacent chunks; falls back
        to ``CHUNK_OVERLAP``.
    min_chunk_size: minimum chunk size; drops trailing fragments shorter
        than this. Falls back to ``MIN_CHUNK_SIZE``.
    symbol_header_prefix: optional callable
        ``(chunk_text, source_file, chunk_index) -> str``. When
        supplied, the returned header is prepended to each chunk
        with a blank line separator before storage. Lets AST-lite
        symbol enrichment (function names, class paths, imports)
        and similar representation-axis experiments stack on this
        code path without forking it. Default ``None`` preserves
        original behavior exactly. Discussed in
        MemPalace/mempalace#1384.

Returns:
    list of ``&#123;"content": str, "chunk_index": int}``.

### `add_to_known_entities`

```python
def add_to_known_entities(entities_by_category: dict, wing: str = None) -> str
```

Union ``entities_by_category`` into ``~/.mempalace/known_entities.json``.

Accepts ``&#123;category: [names]}`` shape as produced by ``mempalace init``
and merges into the registry the miner reads at mine time. Existing
categories are preserved untouched unless also present in the input;
for categories present in both, entries are unioned case-insensitively
without changing the on-disk ordering of pre-existing names.

If a category is stored on-disk as ``&#123;name: code}`` (the alternate
miner-supported shape, used by dialect-style configs), new names are
added as keys with ``None`` values so existing code mappings aren't
overwritten. A later compress pass can assign codes.

When ``wing`` is provided AND ``entities_by_category`` contains a
``topics`` list, those topics are also recorded under
``topics_by_wing[wing]`` (case-insensitive dedup, preserving the
casing of the first observed name). This is the signal source for
``palace_graph.compute_topic_tunnels`` at mine time. Topics for a
wing are *replaced*, not unioned, so a re-run of ``init`` reflects
the user's latest confirmation rather than accumulating stale labels
indefinitely.

The in-process cache is invalidated on write so same-process callers
(notably ``cmd_init`` → ``cmd_mine`` in sequence) see the update
immediately instead of waiting for a mtime re-check.

Returns the registry path as a string for logging.

### `get_topics_by_wing`

```python
def get_topics_by_wing() -> dict
```

Return ``topics_by_wing`` from the global registry as a dict.

Returns ``&#123;}`` if the registry is missing, malformed, or has no
``topics_by_wing`` key. Casing is preserved from disk; callers that
need case-insensitive comparison should normalize themselves.

### `detect_hall`

```python
def detect_hall(content: str) -> str
```

Route content to a hall based on keyword scoring.

Halls connect rooms within a wing — they categorize the TYPE of content
(emotional, technical, family, etc.) while rooms categorize the TOPIC.

### `add_drawer`

```python
def add_drawer(collection, wing: str, room: str, content: str, source_file: str, chunk_index: int, agent: str)
```

Add one drawer to the palace.

Returns a dict ``&#123;"id": drawer_id, "warnings": [...]}``. ``warnings``
is a list of human-readable strings — empty when the room is one of
the canonical 7 (see ``mempalace.room_taxonomy``). Per #86 a non-
canonical room is accepted and surfaced via the warning instead of
rejected at the backend.

### `add_drawers`

```python
def add_drawers(collection, wing, room, chunks, source_file, agent)
```

Batch-insert multiple drawers in one ChromaDB call per sub-batch.

Collects all chunks into batch lists and upserts them in groups of
``DRAWER_UPSERT_BATCH_SIZE`` (alias of ``CHROMA_BATCH_LIMIT``, kept
so existing fork tests that ``monkeypatch.setattr(miner,
"DRAWER_UPSERT_BATCH_SIZE", N)`` still drive the sub-batch loop).
Returns ``(drawers_added, batch_ids, warnings)`` where ``warnings``
is a list of room-taxonomy warning strings (empty when ``room`` is
canonical; see ``mempalace.room_taxonomy``). The room is shared
across every chunk in a batch, so a single warning per call is
sufficient — no per-drawer fan-out.

### `process_file`

```python
def process_file(filepath: Path, project_path: Path, collection, wing: str, rooms: list, agent: str, dry_run: bool, closets_col = None, chunk_size: int = None, chunk_overlap: int = None, min_chunk_size: int = None, max_chunks_per_file: Optional[int] = None, room_resolver: Optional[callable] = None) -> tuple
```

Read, chunk, route, and file one file.

Returns ``(drawer_count, room_name, skip_reason)``. ``skip_reason`` is
``None`` on success and on every non-chunk-cap skip path: already
filed (pre- or post-lock re-check), unreadable (``OSError``), or
too-short content (below ``min_chunk_size``). It is ``"chunk_cap"``
when the per-file chunk cap aborted the file. Callers use the tag to
surface a separate counter in the mine summary (see #1455).

### `file_passes_scan_gates`

```python
def file_passes_scan_gates(filepath: Path, project_path: Path, *, matchers: list = None, include_paths: set = None, exclude_matcher = None, explain: bool = False) -> bool
```

Decide whether one candidate file may be mined.

Extracted verbatim from :func:`scan_project`'s walk so that a mine of a
single named file applies exactly the same gates as that file would meet
inside a directory walk — there is no second policy to drift out of sync.

``matchers`` is the ancestor-ordered list of active ``.gitignore``
matchers (empty disables the gitignore gate, which is what
``--no-gitignore`` does). ``explain`` makes the otherwise-silent
rejections print a ``SKIP:`` line.

### `scan_project`

```python
def scan_project(project_dir: str, respect_gitignore: bool = True, include_ignored: list = None, exclude_patterns: list = None) -> list
```

Return list of all readable file paths under ``project_dir``.

Skips symlinks and oversized files. Each skipped symlink is logged to
``sys.stderr`` with a ``  SKIP: &lt;relative-path> (symlink)`` line so the
caller can tell why a directory looks empty after walking.

### `resolve_project_root`

```python
def resolve_project_root(path) -> Path
```

Return the project directory a single mined file belongs to.

A directory mine gets its wing from the directory it was handed and its
rooms from paths relative to it. A single-file mine has no such argument,
and answering "which project is this file in?" with "the directory it
happens to sit in" would file ``&lt;repo>/docs/notes/CLAUDE.md`` under a wing
called ``notes``. So walk up to the nearest ancestor carrying a project
marker (``.git`` / ``mempalace.yaml`` / ``mempal.yaml``) — the same two
things ``load_config`` and the rest of the toolchain already treat as "a
project lives here" — and fall back to the file's own directory when the
file is loose on disk and belongs to no project at all.

Nearest marker wins, so a subproject with its own ``mempalace.yaml``
inside a larger git repo keeps its own wing.

### `scan_single_file`

```python
def scan_single_file(filepath, project_path, respect_gitignore: bool = True, include_ignored: list = None, exclude_patterns: list = None) -> list
```

Return ``[path]`` for one explicitly named file, or ``[]`` if a gate rejects it.

The single-file counterpart of :func:`scan_project`. It applies the very
same per-file gates — via the shared :func:`file_passes_scan_gates` — so a
file mined by name behaves exactly as it would have inside a directory
walk: same extension whitelist, same ``SKIP_FILENAMES``, same
``.gitignore`` and ``exclude_patterns`` handling, same symlink / regular
file / size checks, same ``--include-ignored`` override.

The one difference is voice. ``scan_project``'s rejections are silent
because a tree walk would otherwise drown the mine in them; here the
operator named this one file and got nothing back, so every rejection
explains itself on stderr.

``.gitignore`` matchers are collected from ``project_path`` down to the
file's own directory, in ancestor order, which is the state the walk would
have accumulated by the time it reached that directory.

### `mine`

```python
def mine(project_dir: str, palace_path: str, wing_override: str = None, agent: str = 'mempalace', limit: int = 0, dry_run: bool = False, respect_gitignore: bool = True, include_ignored: list = None, files: list = None, max_chunks_per_file: Optional[int] = None, workers: int = 1, compute_derived: bool = True, *, collection = None, closets_collection = None)
```

Mine a project directory — or one file inside a project — into the palace.

``project_dir`` normally names a directory and the whole tree is walked.
When it names a single regular file, only that file is mined (#451): the
project it belongs to is found by walking up to the nearest ``.git`` /
``mempalace.yaml`` (:func:`resolve_project_root`) and supplies the wing
and the relative path room detection keys off, so a file is filed exactly
where a directory mine would have filed it. The file's existing drawers
are replaced, not duplicated — ``process_file`` deletes by ``source_file``
before inserting — and an unchanged file is still skipped on its stored
mtime, so re-queuing one costs nothing. This is the targeted re-index
path: re-mining a 111 MB corpus to refresh one edited CLAUDE.md holds the
palace write lock for hours.

``workers`` controls parallelism of the read/chunk/route prep half.
The default ``1`` runs the unchanged sequential path (zero behaviour
change). When ``> 1``, files are prepared across a thread pool while
every backend write (embedding ``upsert`` + closet build) stays serial
on the main thread — see :func:`_prepare_file` / :func:`_write_prepared`
and issue #330 for why the encoder must stay single-threaded.

``files`` may optionally be a pre-scanned list of file paths from
:func:`scan_project`. When provided, the corpus walk is skipped — the
caller (e.g. ``init`` showing a file-count estimate before the mine
prompt) avoids walking the tree twice. When ``None`` (the default),
``mine`` walks the tree itself just like before.

The same block is ALSO skipped, with no flag, whenever the mine upserted
zero drawers. The count that matters is **drawers upserted, not files
processed**: since palace-daemon#262 most drain mines walk their files
and upsert nothing because the stored ``source_mtime`` still matches, so
a files-processed test would almost never fire. Measured on the palace
host, one such no-op mine still spent 14+ minutes at 3.5 GB here.

``compute_derived`` controls the post-mine derived-analytics block
(cross-wing topic tunnels, within-wing hallways, cross-wing entity
tunnels). It defaults to ``True`` -- existing callers see no change.
Pass ``False`` when the mine is small and targeted: those three steps
cost O(wing), not O(change), so a hook-driven sweep of a handful of
files otherwise pays for the whole wing (#474 measured 29+ min of CPU
and 1.6-4.1 GB of RSS for a 31-file memory sweep, holding the
exclusive mine lock throughout). The drawers written are identical
either way; only the derived graph is left un-refreshed.

``max_chunks_per_file`` overrides the per-file chunk cap (see
:func:`_resolve_max_chunks_per_file`). ``None`` defers to
``MEMPALACE_MAX_CHUNKS_PER_FILE`` or ``MAX_CHUNKS_PER_FILE``; ``0``
disables the cap entirely (#1455).

``collection`` / ``closets_collection`` let a single-client host
(e.g. palace-daemon) write through its own already-open backend handle
instead of constructing a second one. When ``collection`` is supplied:

  - the internal ``get_collection(palace_path)`` call is skipped;
  - ``mine_palace_lock(palace_path)`` is NOT acquired (the caller
    guarantees exclusivity around its own client);
  - the post-mine FTS5 ``_validate_palace_fts5_after_mine`` step is
    skipped because it would call ``_close_chroma_handles`` against
    the caller's still-open client and reopen sqlite3 read-only —
    the caller can run its own integrity check on its own schedule.

If ``collection`` is supplied but ``closets_collection`` is not, the
closet upserts use the same injected collection's backend the same
way the non-injected path would (via ``get_closets_collection`` on
the live palace path) — so callers that only have a drawers handle
are still served correctly.

Existing positional/keyword callers see no behaviour change: when
both kwargs are omitted, ``mine`` walks exactly the original code
path (construct client, acquire lock, validate at end).

### `recompute_derived_graph`

```python
def recompute_derived_graph(wing: str, *, collection = None, config = None) -> dict
```

Recompute one wing's derived graph. THE definition of those steps.

Three steps, in order, because they are a chain:

1. cross-wing **topic** tunnels — link this wing to any other sharing a
   confirmed TOPIC label;
2. within-wing **hallways** — link entities that co-occur in drawers
   across this wing's rooms;
3. cross-wing **entity** tunnels — derived from the hallway records
   step 2 just materialized.

Every step is independently fault-tolerant: a derived analytic must never
take down its caller. For the post-mine block that is because the drawer
write has already committed; for ``mempalace tunnels --rebuild`` it is
because a partial refresh beats none. Failures land in ``errors`` keyed by
step rather than raising, and the caller decides how loudly to say so.

Returns ``&#123;"topic_tunnels": int, "hallways": int, "entity_tunnels": int,
"errors": &#123;step: Exception}}``; a step that failed has a count of 0 and an
entry in ``errors``.

Both the post-mine block and the rebuild verb call this, so a fourth
derived step cannot land in one and be forgotten in the other.

### `status`

```python
def status(palace_path: str)
```

Show what's been filed in the palace.

Tallies drawers by wing/room directly from ``chroma.sqlite3`` so a routine
status check never cold-loads the HNSW vector index — a load that costs
tens of seconds of CPU per call on large palaces (#1681). Falls back to the
ChromaDB client path when the sqlite read is unavailable (missing DB,
un-bootstrapped collection, or an unexpected schema); the fallback also
emits the state-specific guidance for absent/empty palaces.
