# `mempalace.auto_query.runner`

Source: [`mempalace/auto_query/runner.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/auto_query/runner.py)

Auto-query runner — chains signal extraction, routing, and formatting.

This module is the single entry point for the auto-query pipeline.
It is called from the UserPromptSubmit hook (via ``__main__.py``) or
directly from tests.

Pipeline::

    load config → check enabled/mode → extract signals → pick tool
    → execute MCP call → format injection → log decision

The MCP call is made via ``_call_mcp()``, which delegates to the
palace-daemon HTTP proxy (``/mcp`` endpoint).  When the daemon is not
reachable, the call is skipped and a dry-run decision is logged.

## Classes

### `class AutoQueryResult`

Result of a single auto-query pipeline run.

#### `__init__`

```python
def __init__(self, injection = None, decision = None, tool_call = None, mcp_result = None, receipt = None)
```

## Functions

### `deadline_timeout`

```python
def deadline_timeout(default)
```

``default`` capped to the time left before the hook deadline.

Returns None when there is not enough time left to make the call at all,
so the caller can skip it and degrade. With no deadline set, returns
``default`` unchanged.

### `run_auto_query`

```python
def run_auto_query(prompt, session_id, turn, project_wing = '', known_wings = None, known_entities = None, has_recent_drawers = False, config = None, queried_entities = None, log_dir = None, wing_counts = None)
```

Run the auto-query pipeline for a single user turn.

Returns an ``AutoQueryResult`` with the injection text (or None),
the decision record, and the raw MCP result.
