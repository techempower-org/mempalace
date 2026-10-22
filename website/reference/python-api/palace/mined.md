# `mempalace.palace.mined`

Source: [`mempalace/palace/mined.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace/mined.py)

## Functions

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
