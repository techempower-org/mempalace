# `mempalace.palace.mined`

Source: [`mempalace/palace/mined.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace/mined.py)

## Classes

### `class MinedSetUnavailable(RuntimeError)`

The palace could not be read to learn which sources are already filed.

Raised instead of returning a partial registry: an incomplete one reads as
"not mined yet" and "no duplicate", which cannot be told apart from a
complete answer, so a mine would re-file sources or write duplicates blind.

## Functions

### `file_already_mined`

```python
def file_already_mined(collection, source_file: str, check_mtime: bool = False, extract_mode: Optional[str] = None) -> bool
```

Check if a file has already been filed in the palace.

Returns False (so the file gets re-mined) when:
  - no drawers exist for this source_file
  - the stored `normalize_version` is missing or older than the current
    schema (triggers silent rebuild after a normalization upgrade), or,
    for extract_mode="exchange", the stored `convo_chunker_version` is
    missing or older than CONVO_CHUNKER_VERSION
  - `check_mtime=True` and the file's mtime differs from the stored one

When check_mtime=True (used by the project miner, and by the convo
miner's in-lock recheck), also re-mines on content change. Conversation
transcripts are NOT assumed immutable: a Claude Code session keeps
appending to its own file while active, and /compact or /clear can
rewrite one in place. The convo miner's bulk skip-check uses
prefetch_mined_set()'s stored mtimes instead of calling this function
per file (same mtime-aware decision, without the O(n) per-file query
cost); this function's check_mtime=True path remains its per-file,
lock-held race-condition recheck. The project miner skips a file
without calling this only when prefetch_complete_mtimes() proves this
would return True.

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
def prefetch_mined_set(collection, extract_mode: Optional[str] = None, source_files: Optional[Iterable[str]] = None) -> dict[str, Optional[float]]
```

Pre-fetch source_file -> stored source_mtime for files already mined
at the current NORMALIZE_VERSION, in one bulk pass instead of one
ChromaDB query per file.

Return type is a dict rather than a bare set so callers get mtime
awareness "for free": conversation transcripts are not immutable once
mined (a Claude Code session keeps appending to the same file while
active, and /compact or /clear can rewrite one in place), so "we've
seen this source_file before" is not sufficient to skip it -- the caller
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

``source_files`` scopes the answer to those paths: one ``source_file``
lookup each instead of the whole-palace scan. The scan
pays for every drawer in every wing whatever the target, so a one-file
drain mine spent 5-10 minutes in it (#548). The
result is exactly the full scan's, restricted to ``source_files``.

When `source_files` is given and holds at most `_PREFETCH_SCOPE_THRESHOLD`
paths, the caller already knows the only source_file values that could
possibly match, so a paged `where={"source_file": {"$in": ...}}` get()
replaces the full-collection scan. On a one-file mine of a 660k-drawer
palace that mtime scan was about 42% of the mine; the content-hash scan
was the rest, and it stays unscoped because a stored hash can be several
comma-joined values. Above the threshold, or when source_files is
omitted, the behaviour is unchanged: a bulk sweep still benefits more
from one full scan than from many filtered queries.

### `prefetch_complete_mtimes`

```python
def prefetch_complete_mtimes(collection, source_files) -> Optional[dict[str, tuple]]
```

``source_file -> stored mtimes of its complete drawer groups``, in one
sqlite pass, for the project miner's skip check (#2684).

``file_already_mined(collection, src, check_mtime=True)`` costs one
``get(where=&#123;"source_file": src})`` per file, ~0.1 s on a 1M-drawer
palace, which made a no-op mine of a 1.4k-file project take minutes.
A source is listed here only when its stored drawers prove that call
would return True for a matching on-disk mtime: some ``source_mtime``
group at the current ``normalize_version`` either has a drawer with no
``chunk_total`` or holds at least the largest ``chunk_total`` any of
its drawers names. A source with a value that check could not compare
(a non-numeric ``normalize_version``, ``source_mtime`` or
``chunk_total``) is left out, since there it returns False. A source
missing from the result is not "not mined": the caller asks
``file_already_mined()`` as before.

Only ``source_files`` are tracked. Returns ``None`` on backends without
Chroma's sqlite stream, or when the stream fails, so the caller keeps
the per-file check for every file.

### `prefetch_content_hashes`

```python
def prefetch_content_hashes(collection, extract_mode: Optional[str] = None, wing: Optional[str] = None) -> dict[tuple[str, str], str]
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

``wing`` scopes the answer to that wing: one paged fetch of the wing
instead of the whole-palace scan. Every key is ``(wing, hash)``,
so a miner filing into one wing can never match another wing's drawers.
The result is exactly the full scan's keys for that wing.

Deliberately NOT scoped by candidate source_files the way
prefetch_mined_set is: a stored `content_hash` can hold several
comma-joined hashes (one privacy-export bundle drawer covers several
conversations), so a `where`-filtered query can only match a document
whose entire stored value equals one candidate hash and would silently
miss any bundle row. An incomplete result here is worse than none,
because a caller reading the returned dict has no way to tell "no
duplicate" from "the query couldn't see this row". Narrowing this scan
correctly needs either a schema change (one hash per metadata field
instead of a joined list) or a second index; out of scope for the fix
that scoped prefetch_mined_set. See test_mine_convos_skips_same_content_
under_new_filename and test_mine_convos_skips_same_conversation_within_
re_exported_bundle, which a source_files-scoped or skipped version of
this function broke.
