# `mempalace.migrate_hallways`

Source: [`mempalace/migrate_hallways.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/migrate_hallways.py)

Move ``hallways.json`` into the postgres hallway table (#442).

Idempotent, resumable, streaming, with a dry run. Every one of those is
forced by the size of the thing being moved: the production file is
**1,041,537,215 bytes / ~797K records** (palace host, 2026-09-10).

Streaming is the load-bearing one. ``json.load`` on that file materializes
several GB of Python dicts — the exact cost this migration exists to
eliminate — so the reader here yields one record at a time out of a small
buffer, using ``json.JSONDecoder.raw_decode`` rather than a third-party
streaming parser. No new dependency, which keeps the local-first install
unchanged.

Run it with ``python -m mempalace.migrate_hallways --help``. The cutover
itself — flipping ``hallway_backend`` to ``postgres`` — is deliberately NOT
part of this script: import first, verify against real data, flip second.

## Classes

### `class MigrationStateMismatch(RuntimeError)`

Resume state does not describe the file we were handed.

## Functions

### `iter_hallway_records`

```python
def iter_hallway_records(path: str, chunk_size: int = _DEFAULT_CHUNK) -> Iterator[dict]
```

Yield hallway records one at a time without loading the file.

Text mode is deliberate: Python's text IO will not split a multi-byte
character across two ``read()`` calls, so entity names in any script
survive chunking. ``raw_decode`` returning a ``ValueError`` is ambiguous
between "buffer ends mid-record" and "the file is malformed", so it is
only treated as malformed once the file is exhausted.

### `classify_entity`

```python
def classify_entity(entity) -> str
```

Bucket one entity name. ``other`` is a catch-all, not a junk verdict.

Deliberately ordered most-specific first: a template that contains a
slash is a template, not a path.

### `migrate`

```python
def migrate(source_path: str, *, store = None, config = None, dry_run: bool = False, restart: bool = False, state_path: Optional[str] = None, batch_size: int = DEFAULT_BATCH_SIZE, conn = None, progress_every: int = 50000) -> dict
```

Import ``source_path`` into the hallway table. Returns a summary dict.

### `build_parser`

```python
def build_parser() -> argparse.ArgumentParser
```

### `main`

```python
def main(argv = None) -> int
```
