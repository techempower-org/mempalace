# `mempalace.auto_query.signals`

Source: [`mempalace/auto_query/signals.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/auto_query/signals.py)

Signal extraction for auto-query context classifier.

Pure functions — no I/O, no MCP calls. All external state
(wings, entities, drawer existence) is passed in by the caller.

## Functions

### `strip_foreign_blocks`

```python
def strip_foreign_blocks(text)
```

Remove peer-message / harness blocks so only the user's words remain.

### `extract_signals`

```python
def extract_signals(text, session_state, project_wing, known_wings, known_entities = None, has_recent_drawers = False)
```

Extract auto-query signals from a user message.

Pure function — no I/O, no MCP calls. All external state
(wings, entities, drawer existence) is passed in by the caller.

### `resumption_phrase`

```python
def resumption_phrase(text)
```

The session-resumption phrase in ``text``, or "" if there is none.

Public because the router needs to subtract the phrase from the search
query: "where were we on the pgvector cutover" should search the cutover,
not the question.
