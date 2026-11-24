# `mempalace.backfill_age`

Source: [`mempalace/backfill_age.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backfill_age.py)

Backfill the AGE graph from an existing drawer table.

Phase 4 of the AGE-integration goal. Builds the full palace-graph + KG
state from drawer rows that were written BEFORE the write-through
middleware was registered. Companion to ``migrate_to_postgres``: that
script copies chroma → postgres, this one copies postgres-drawers →
postgres-AGE.

Design goals:

1. **Restartable.** A checkpoint table (`mempalace_kg_backfill_state`)
   tracks last completed (wing, room) pair. Re-running from scratch is
   safe but skips already-completed (wing, room) groups.
2. **Idempotent.** All inserts use MERGE, so the same drawer being
   processed twice has no duplicating effect.
3. **Bounded memory.** Streams drawer rows via server-side cursor
   (psycopg2's named cursor), processes one at a time, never holds
   the full result set.
4. **Configurable scope.** Can target one wing at a time, or all wings,
   or only do the high-level palace map (skip per-drawer entity
   extraction) for a fast first pass.

Three layers populated:

- Palace structure (Wing → Room → Drawer): from
  ``palace_graph_age.populate_from_postgres``. Idempotent re-MERGE.
- Entity extraction + MENTIONS edges: per-drawer regex extractor by
  default (configurable via env var).
- Optional: KG triples seeded from ENTITY_FACTS if present.

CLI entry point (registered as ``mempalace-backfill-age``):

    mempalace-backfill-age \
        [--dsn &lt;dsn>] \
        --table mempalace_drawers \
        [--wing &lt;name>]  \
        [--skip-palace] \
        [--skip-entities] \
        [--restart]

``--dsn`` is optional: without it the DSN comes from
``MEMPALACE_POSTGRES_DSN`` (or ``MEMPALACE_PG_DSN``), so a password in
the DSN never shows up in the process list (#583). A DSN with no
password also works with libpq's own ``PGPASSWORD`` / ``~/.pgpass``.

## Functions

### `backfill`

```python
def backfill(*, dsn: str, table_name: str = 'mempalace_drawers', wing_filter: Optional[str] = None, skip_palace: bool = False, skip_entities: bool = False, extractor_name: str = 'regex', max_entities_per_drawer: int = 50, relation_type: str = 'mentions', confidence: float = 0.5, restart: bool = False, log_every: int = 500, commit_every: int = 100) -> dict
```

Backfill AGE graph from an existing drawer table.

Args:
    dsn: Postgres DSN — must point at a database where AGE is loaded.
    table_name: Source drawer table.
    wing_filter: If set, only process drawers in this wing.
    skip_palace: Skip Wing/Room/Drawer/SHARED_VIA population. Useful
        if you've already run it once and just want a fresh entity
        pass.
    skip_entities: Skip MENTIONS extraction. Useful for first-pass
        "just give me the palace map" on huge palaces.
    extractor_name: regex (default), spacy, llm — only regex
        implemented today.
    max_entities_per_drawer: Cap on entities per drawer write; same
        knob as ``kg_writethrough.make_age_writethrough``.
    relation_type: Edge label for drawer → entity mentions.
    confidence: Default confidence for extracted mentions.
    restart: Clear the checkpoint table before starting (forces a
        full re-backfill).
    log_every: How often to emit progress logs.
    commit_every: Flush pending KG writes + checkpoint marks every
        N drawers. Bigger batches amortize commit overhead but lose
        more progress on crash. Default 100 (~4× faster than per-
        drawer commit on the production palace per `techempower-org/mempalace#101`
        review). Set to 1 to restore the old per-drawer semantics.

Returns counters dict tracking what was processed.

### `main`

```python
def main(argv: Optional[list[str]] = None) -> int
```
