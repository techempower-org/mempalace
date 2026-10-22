# `mempalace.mcp_server.tools_coord`

Source: [`mempalace/mcp_server/tools_coord.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_server/tools_coord.py)

## Functions

### `tool_event_append`

```python
def tool_event_append(type: str, stream: str, room: str, from_agent: str, to_agent: str = None, correlation_id: str = None, branch: str = None, base_commit: str = None, status: str = None, body: str = '', metadata: dict = None, artifact_ids: list = None, topic: str = None)
```

Append one immutable coordination event.

### `tool_task_create`

```python
def tool_task_create(project: str, from_agent: str, to_agent: str, goal: str, branch: str, base_commit: str, done: str)
```

Create one canonical task request for local or remote MCP clients.

### `tool_event_list`

```python
def tool_event_list(stream: str = None, room: str = None, topic: str = None, type: str = None, to_agent: str = None, from_agent: str = None, correlation_id: str = None, status: str = None, since_event_id: str = None, before_event_id: str = None, since_created_at: str = None, limit: int = 50, order: str = 'asc', preview: bool = False)
```

List coordination events with structured filters.

``order='desc'`` returns newest events first (e.g. for sweeping recent inbox
or checking recent project history in a single call). Default is ``'asc'``.
``preview=True`` truncates each event's verbatim body to a short excerpt
(marking ``body_truncated`` + ``body_length``) so scanning many events
stays cheap.

### `tool_event_wait`

```python
def tool_event_wait(stream: str = None, room: str = None, topic: str = None, type: str = None, to_agent: str = None, from_agent: str = None, correlation_id: str = None, status: str = None, since_event_id: str = None, since_created_at: str = None, timeout_ms: int = 60000, limit: int = 50)
```

Block until a matching event exists or the timeout expires.

``limit`` mirrors ``event_list`` so the two tools accept the same
filter set — agents kept tripping over wait rejecting a parameter
that list accepts (reported by windows-codex during dogfood).

### `tool_event_ack`

```python
def tool_event_ack(event_id: str, from_agent: str, status: str = None, body: str = '', topic: str = None)
```

Append an event.ack referencing a prior event (never mutates it).

### `tool_artifact_put`

```python
def tool_artifact_put(kind: str, content: str, created_by: str, metadata: dict = None)
```

Store exact artifact content (patch, file, log, json, note).

### `tool_artifact_get`

```python
def tool_artifact_get(artifact_id: str)
```

Fetch an artifact by id — exact content and metadata.

### `tool_patch_submit`

```python
def tool_patch_submit(content: str, from_agent: str, stream: str, room: str = 'patches', to_agent: str = None, correlation_id: str = None, branch: str = None, base_commit: str = None, body: str = '', metadata: dict = None, topic: str = None)
```

Store a patch artifact and append its patch.ready event in one call.
