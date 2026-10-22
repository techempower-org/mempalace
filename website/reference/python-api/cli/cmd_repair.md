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
