# `mempalace.auto_query.router`

Source: [`mempalace/auto_query/router.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/auto_query/router.py)

Tool router for auto-query integration.

Pure function: maps a SignalSet to an MCPCall (or None).  No I/O, no MCP
calls, no side effects.  The router is stateless; rate limiting and
deduplication are the caller's responsibility.

Priority order (from spec section 2):
  0.5 Resumption ask  -> mempalace_search      (the user asked to resume)
  1. Task resumption  -> mempalace_diary_read  (highest precision)
  2. Explicit hint     -> mempalace_search      (user is asking directly)
  3. Entity + temporal -> mempalace_kg_query    (entity-scoped history)
  4. Entity only       -> mempalace_search      (entity-scoped)
  5. Temporal only     -> mempalace_search      (project-scoped recent)

## Functions

### `pick_tool`

```python
def pick_tool(signals: SignalSet, mode: str, session_state: SessionState) -> Optional[MCPCall]
```

Select the MCP tool to invoke based on extracted signals.

Returns None if the score is below the threshold for the given mode,
or if no meaningful signal pattern is detected.

The router does NOT call ``query_sanitizer.sanitize_query()`` itself --
it performs lightweight truncation only.  The runner (harness shim)
is responsible for full sanitization before calling the router.
