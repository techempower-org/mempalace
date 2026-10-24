# `mempalace.mcp_light_server`

Source: [`mempalace/mcp_light_server.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_light_server.py)

MemPalace Lightweight MCP Server
================================
Consolidates 52 MemPalace tools into a high-performance 3-tool interface
(`palace_query`, `palace_exec`, `palace_coordinate`) powered by Palace Query
Language (PQL) and Command DSL, while retaining 100% of underlying features,
guarantees, and safety checks.

## Functions

### `tool_palace_query`

```python
def tool_palace_query(arguments: Dict[str, Any] | str) -> Dict[str, Any]
```

Unified read tool for semantic search, taxonomy, KG, graph, diary, and status.

### `tool_palace_exec`

```python
def tool_palace_exec(arguments: Dict[str, Any] | str) -> Dict[str, Any]
```

Unified write tool for drawers, KG facts, tunnels, diaries, and maintenance.

### `tool_palace_coordinate`

```python
def tool_palace_coordinate(arguments: Dict[str, Any] | str) -> Dict[str, Any]
```

Unified multi-agent coordination tool (RFC 003 logstream, tasks, artifacts, mesh).

### `handle_light_request`

```python
def handle_light_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]
```

Process a single JSON-RPC request for the lightweight MCP server.

### `dispatch_light_stdio_request`

```python
def dispatch_light_stdio_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]
```

Hub-first stdio dispatch: translate palace_* calls, keep the 3-tool handshake local.

### `main`

```python
def main()
```
