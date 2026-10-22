# `mempalace.cli.cmd_sync`

Source: [`mempalace/cli/cmd_sync.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/cmd_sync.py)

## Functions

### `cmd_sync`

```python
def cmd_sync(args)
```

Prune drawers whose source files are gitignored, deleted, or moved (#1252).

Where it runs (#418): ``--daemon`` submits to the local job queue;
daemon-strict with no ``--palace`` routes to the palace daemon; otherwise
the backend is resolved *before* the palace-directory precheck, and that
precheck is skipped for a service-backed store, which has no local
database file by design.

### `cmd_daemon`

```python
def cmd_daemon(args)
```
