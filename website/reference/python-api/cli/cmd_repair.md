# `mempalace.cli.cmd_repair`

Source: [`mempalace/cli/cmd_repair.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/cmd_repair.py)

## Functions

### `cmd_palace_set_embedder`

```python
def cmd_palace_set_embedder(args)
```

Record (or force-override) a palace's embedder identity (RFC 001).

Resolves the ``unknown`` state for a legacy palace, or records a specific
model with ``--model``. It records identity on the palace only; it does not
change the configured model — when the two differ it prints how to align
``MEMPALACE_EMBEDDING_MODEL``. ``--force`` overwrites an existing,
differently-named identity.

### `cmd_lookup_indexes`

```python
def cmd_lookup_indexes(args)
```

Postgres only: build the missing metadata lookup indexes online, ANALYZE, and check each plan uses its index.

Exit 0 when no build failed and no judged plan missed its index. A dry run never builds or judges plans, so it
exits 0 even with indexes missing (it lists them), and keys whose plans are not judged (source_file, a small
table) don't affect the exit. 1 when a build failed (the index is named) or a judged per-value or generic
(prepared) plan does not use its index; 2 for another backend; 3 when another lookup-indexes run holds the
table's lock.

### `cmd_repair_status`

```python
def cmd_repair_status(args)
```

Read-only HNSW capacity health check (#1222).

### `cmd_repair`

```python
def cmd_repair(args)
```

Repair palace state.

Default mode is full HNSW rebuild via extract + re-upsert
(``--mode rebuild`` / ``--mode legacy``, synonyms). Also handles
``--mode max-seq-id`` for un-poisoning ``max_seq_id`` rows
corrupted by the legacy 0.6.x → 1.5.x chromadb migration shim
(#1208 / #1288 family). The earlier ``reorganize`` mode was
retired alongside the recovery collection (PR #8 / row 32).

Closes Copilot finding on jphein/mempalace#8: docstring claimed
only "rebuild" while the function continued to dispatch
``max-seq-id`` based on ``args.mode``.

On a successful rebuild the palace SQLite file is VACUUMed and the
FTS5 index is rebuilt, so the next repair's integrity preflight reads
a consistent database (#1747).
