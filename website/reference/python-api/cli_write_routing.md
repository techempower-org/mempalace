# `mempalace.cli_write_routing`

Source: [`mempalace/cli_write_routing.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli_write_routing.py)

CLI adapter for the shared daemon write-routing policy.

This module keeps parser flags, configuration precedence, and route selection
out of the large CLI module. It applies only to routine CLI writes.

Maintenance operations such as repair, migration, index rebuild, closet
compression, and embedder-identity changes deliberately remain outside this
policy until an exclusive-maintenance protocol exists.

## Classes

### `class CliWriteRouting`

Resolved route for one routine CLI write.

#### `use_daemon`

```python
def use_daemon(self) -> bool
```

#### `use_direct`

```python
def use_direct(self) -> bool
```

## Functions

### `add_cli_write_routing_flags`

```python
def add_cli_write_routing_flags(parser: argparse.ArgumentParser, *, allow_background: bool = True) -> None
```

Add consistent routing flags to a routine write command.

### `resolve_cli_write_routing`

```python
def resolve_cli_write_routing(args, *, operation: str) -> CliWriteRouting
```

Resolve an explicit flag or the configured CLI policy.

Interactive CLI commands are allowed to start the daemon. Therefore both
``prefer`` and ``require`` select the daemon when no explicit ``--direct``
override is present.

The daemon submission function is responsible for starting or reusing the
daemon atomically. We intentionally do not perform a separate health probe,
which would introduce a check-then-submit race.
