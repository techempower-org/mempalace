# `mempalace.auto_query.injected`

Source: [`mempalace/auto_query/injected.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/auto_query/injected.py)

Per-session record of drawers auto-query has already injected.

Auto-query never injects the same drawer twice in one session (#429): the
ids it has shown live in ``~/.mempalace/auto_query/injected/&lt;session>.json``
and are filtered out of later results.

That is right while the context holds. It is exactly wrong after a context
compaction, where the session id survives but the context does not — the file
then suppresses the drawers the agent just lost, and suppresses more of them
the longer the session has run (#449). Compaction therefore needs a way to
drop the record, which is why this bookkeeping lives in its own module
instead of inside the auto-query runner: the SessionStart path clears it
without importing the whole classifier.

Every function fails open. A hook must never break a session over a cache.

## Functions

### `injected_dir`

```python
def injected_dir()
```

Directory holding one dedupe file per session.

### `injected_path`

```python
def injected_path(session_id)
```

Path to one session's dedupe file (id sanitized to a safe basename).

### `load_injected`

```python
def load_injected(session_id)
```

Drawer ids already injected this session; empty set if unknown.

### `remember_injected`

```python
def remember_injected(session_id, drawer_ids)
```

Add drawer ids to the session's record (fail-open on I/O).

### `clear_injected`

```python
def clear_injected(session_id)
```

Forget everything injected this session; return how many ids were dropped.

Called on ``SessionStart(source="compact")``. The count is reported to the
human, because "cleared 119 suppressed drawers" is the measurement that
says how much recall the dedupe was costing (#449).
