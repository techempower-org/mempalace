# `mempalace.service`

Source: [`mempalace/service.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/service.py)

Shared service operations used by daemon-backed entry points.

The MCP server remains the owner of MCP transport details. This module owns the
small, transport-neutral execution surface the daemon needs: classify known
tools and execute durable background jobs without printing directly to the
caller's terminal.

## Functions

### `classify_tool`

```python
def classify_tool(name: str) -> str
```

Return ``read``, ``write``, ``maintenance``, or ``unknown`` for an MCP tool.

### `execute_job`

```python
def execute_job(kind: str, payload: dict[str, Any]) -> dict[str, Any]
```

Execute one daemon job and return a JSON-serializable result.

### `run_mine`

```python
def run_mine(payload: dict[str, Any]) -> dict[str, Any]
```

Run the same mine operation as the CLI, without daemon transport concerns.

### `run_sweep`

```python
def run_sweep(payload: dict[str, Any]) -> dict[str, Any]
```

Run transcript sweep through the daemon worker.

### `run_sync`

```python
def run_sync(payload: dict[str, Any]) -> dict[str, Any]
```

Run sync and render the same operator-facing summary shape as the CLI.

### `run_diary_write`

```python
def run_diary_write(payload: dict[str, Any]) -> dict[str, Any]
```

### `run_mcp_tool`

```python
def run_mcp_tool(payload: dict[str, Any]) -> dict[str, Any]
```

Execute an MCP tool by name over the daemon queue.

The daemon is a durable, retried write surface — not a general MCP transport.
Restrict ``mcp_tool`` to write-classified tools only: read tools would
exfiltrate verbatim palace content into the queue DB and the job result
(stored world-readable-by-default without the perms fix, and returned over
/jobs), and maintenance tools already have their own dedicated kinds
(mine/sync). No internal caller currently uses ``mcp_tool``; this allowlist
bounds the blast radius of the generic escape hatch.

### `print_job_result`

```python
def print_job_result(result: dict[str, Any]) -> int
```

Replay captured daemon job output and return the intended process exit code.
