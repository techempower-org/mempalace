# `mempalace.cli.cmd_mine`

Source: [`mempalace/cli/cmd_mine.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/cmd_mine.py)

## Classes

### `class UnknownSourceAdapterError(ValueError)`

Raised when an explicit ``--source`` name is absent from the registry.

### `class UnsupportedSourceAdapterProtocolError(ValueError)`

Raised when an adapter requires runner semantics not implemented yet.

### `class SensitiveKnowledgeGraphWrite(RuntimeError)`

Raised when a ``sensitive`` adapter calls a knowledge-graph mutation.

## Functions

### `cmd_mine`

```python
def cmd_mine(args)
```

### `mine_source_adapter`

```python
def mine_source_adapter(*, source_name: str, source_path: str, palace_path: str, dry_run: bool = False, project: Optional[str] = None, related_drawer_id: Optional[str] = None) -> int
```

Run an explicitly selected RFC 002 source adapter through ``PalaceContext``.

This deliberately sits alongside, rather than inside, the legacy mode
miners.  Until those miners are migrated to first-party adapters, no-flag
and ``--mode`` calls must retain their established dispatch paths.

### `cmd_sweep`

```python
def cmd_sweep(args)
```

Sweep a transcript file or directory.

The sweeper deduplicates against its own prior writes via
deterministic drawer IDs + a timestamp cursor. It does NOT currently
coordinate with the file-level miners (miner.py / convo_miner.py) —
those produce char-chunked drawers without compatible message
metadata, so running both miners may store overlapping content under
different IDs.
