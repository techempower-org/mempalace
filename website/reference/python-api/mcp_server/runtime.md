# `mempalace.mcp_server.runtime`

Source: [`mempalace/mcp_server/runtime.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_server/runtime.py)

## Functions

### `main`

```python
def main()
```

MCP server entry point for the ``mempalace-mcp`` console script.

Parses ``sys.argv`` and applies ``--palace`` / ``--backend`` / ``--read-only``
to the process, including ``MEMPALACE_PALACE_PATH`` and ``MEMPALACE_BACKEND``
in ``os.environ``; importing the package parses no argv (#2528).

Side effect: pops ``PYTHONPATH`` from ``os.environ`` (see #1423) so any
subprocess this server spawns inherits a clean env. Host applications that
call ``main()`` programmatically should be aware that the parent process
loses ``PYTHONPATH`` as well. Library imports do NOT trigger this side
effect; only the CLI/MCP entry point does.

Transports:
- ``stdio`` remains the default for existing Claude/MCP deployments.
- ``http`` is opt-in and serves JSON-RPC POSTs at ``/mcp`` in the same
  process, avoiding the long-lived stdio framing failure surface from
  #1801.
