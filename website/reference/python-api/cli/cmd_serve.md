# `mempalace.cli.cmd_serve`

Source: [`mempalace/cli/cmd_serve.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/cmd_serve.py)

## Functions

### `cmd_hook`

```python
def cmd_hook(args)
```

Run hook logic: reads JSON from stdin, outputs JSON to stdout.

### `cmd_instructions`

```python
def cmd_instructions(args)
```

Output skill instructions to stdout.

### `cmd_rules`

```python
def cmd_rules(args)
```

Output the shared-brain agent rules block for a given agent identity.

### `cmd_mcp`

```python
def cmd_mcp(args)
```

Show how to wire MemPalace into MCP-capable hosts.

### `cmd_serve`

```python
def cmd_serve(args)
```

Run a secure remote HTTP MCP server for a team to share one palace (#1877).

A turnkey wrapper over ``mempalace-mcp --transport http``: it resolves a
bearer token (auto-generating a strong one for non-loopback binds), prints a
ready-to-paste client config, then execs the real server in the foreground so
Docker/systemd own the process lifecycle. The token is passed via the
environment, never argv, so it can't leak through ``ps``.

### `cmd_compress`

```python
def cmd_compress(args)
```

Compress drawers in a wing using AAAK Dialect.
