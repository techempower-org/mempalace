# `mempalace.cli.cmd_audit`

Source: [`mempalace/cli/cmd_audit.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/cmd_audit.py)

## Functions

### `cmd_audit`

```python
def cmd_audit(args)
```

Score how navigable the palace is and list what to fix.

Read-only and lock-free: safe to run while an MCP server or a mine holds
the palace. ``--json`` emits the full report for tracking over time.
