# `mempalace.sync`

Source: [`mempalace/sync.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/sync.py)

sync.py — Gitignore-aware drawer prune (#1252).

Removes drawers whose source files are now gitignored, deleted, or moved
out of the project. Reuses the same GitignoreMatcher infrastructure that
the miner uses on the way in, so the same rules that block ingest also
drive the corresponding cleanup.

Usage:
    from mempalace.sync import sync_palace
    report = sync_palace(palace_path, project_dirs=["/repo"], dry_run=True)

## Classes

### `class SyncReport(TypedDict)`

## Functions

### `validate_apply_scope`

```python
def validate_apply_scope(wing, project_dirs) -> None
```

Raise ``ValueError`` unless a destructive sync is explicitly scoped.

On apply, at least one of ``wing`` or ``project_dirs`` must be set so a
caller cannot accidentally prune every wing in a multi-project palace via
auto-detected roots.

Split out of :func:`sync_palace` so a caller that previews with
``dry_run=True`` before applying can run the *apply* rule first. The CLI
does exactly that for its confirmation prompt, and without this the
preview passed the guard (it is a dry run) and an unscoped ``--apply``
reached the prompt instead of exiting 2.

### `sync_palace`

```python
def sync_palace(palace_path: str, project_dirs: Optional[list] = None, wing: Optional[str] = None, dry_run: bool = True, batch_size: int = _BATCH, wal_log: Optional[Callable] = None) -> SyncReport
```

Prune drawers whose source files are gitignored, missing, or moved.

Returns a SyncReport with bucket counts. Dry-run by default; pass
dry_run=False to actually delete drawers and matching closets.

Only ``gitignored`` and ``missing`` are removed. A source file this
could not establish as deleted, or as ignored by a rule of the directory
it was mined from, lands in ``unresolved`` and is counted, printed and
kept: an unmounted volume must not be read as a deletion, and a volume's
rule must not be read as the project's.

A file that is not at its path reaches ``missing`` only when the palace
can still see a source file of its own in that same directory. A
deletion leaves the file's neighbours where they were; a volume that is
not mounted takes every one of them away at once, and there is no call
that tells those two apart from the file alone. Both halves of that are
read again when the verdict is formed rather than trusted from earlier
in the pass, since a volume can leave inside one pass and can come back
inside one.

A witness proves the directory is reachable, not that it is the
directory the missing file was mined from, so the corroboration is read
together with the inode ``source_identity`` recorded at mine time. A
mount point whose lower layer holds a mined file of its own, a volume
mounted over a directory the palace already knows a file in, and a bind
mount of another directory over it all answer with an inode other than
the drawer's, and the drawer is kept. A drawer filed before that existed,
or from a directory that could not be stat'ed, carries no identity and is
decided by corroboration alone.

Three limits of that reading are worth stating. A directory the palace
knows no *surviving* file in cannot corroborate anything, so a file
deleted on its own from a one-file directory is kept and reported, and
so is a whole directory's worth of files deleted together. A volume
that leaves and returns between two adjacent ``stat`` calls is not
covered, because nothing spans two syscalls. And a directory that is
deleted and recreated may answer with a different inode than the one its
drawers carry, which reads exactly like a volume swapped in: the drawers
of files that really went are then kept and reported as ``unresolved``
for good, since the files they name are gone and cannot be mined again.
``mempalace_delete_by_source`` removes them once the report names them,
and ``mempalace_delete_drawer`` takes one at a time; there is no bulk way
out of that state on purpose, since a stranded drawer and a drawer a
volume is holding are the same reading, and nothing here can prune one
kind without pruning the other.

A ``gitignored`` verdict is read the same way: the path answers and a
rule found above it ignores it, both taken from whatever answers there,
so a drawer carrying an identity is removed only when the directory
answered with that inode before and after the rule was read. Otherwise
it is kept as ``unresolved``: the rule then cannot be placed in the
directory the file was mined from, which is what a volume mounted over
that directory looks like when it carries a file of the same name and a
rule that names it. A drawer carrying no identity is removed by the rule
alone, as before. The last two limits above hold here too, with the
window being the whole reading between the two identity readings rather
One more is the symlink: the identity compared is that of the directory
``source_file`` names, as the miner recorded it, while the rule is read
along the resolved path, so for a source that is a symlink into another
directory the rule is read in a directory that is not above the one
compared, and a volume over it is not seen.

``wing`` scopes the corroboration as well as the scan, since only that
wing's drawers are read. A wing-scoped run therefore keeps what a run
over the whole palace would prune, and every candidate the wider run
has is a candidate it has too.

Holds ``mine_palace_lock`` for the whole call so the classify pass and
the apply branch see the same drawer snapshot. Raises
``MineAlreadyRunning`` if another mine is in progress on this palace.

On apply (``dry_run=False``), at least one of ``wing`` or
``project_dirs`` must be set so a caller cannot accidentally prune
every wing in a multi-project palace via auto-detected roots.
