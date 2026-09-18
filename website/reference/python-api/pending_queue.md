# `mempalace.pending_queue`

Source: [`mempalace/pending_queue.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/pending_queue.py)

Append-only journal for mine requests that couldn't reach the daemon.

When ``PALACE_DAEMON_URL`` is set but the daemon (or its backend) is
unreachable, ``_post_daemon_mine`` would previously log the failure and
drop the request. With daemon-strict mode in effect, hooks don't fall
back to a local mine — so a stale daemon means silent write loss for
the duration of the outage.

This module captures the dropped requests as JSONL lines under
``~/.mempalace/pending/YYYY-MM-DD.jsonl`` and exposes a ``replay``
function that re-issues them when the daemon recovers. Lines that
succeed are removed from the file; lines that fail stay for the next
attempt. Atomic rewrite via ``tempfile + os.replace`` prevents partial
file corruption on crash mid-drain.

The request payload is tiny — ``&#123;dir, wing, mode, ts}`` — because the
transcript file the daemon actually mines lives on disk and survives
the outage. We don't archive conversation content here; the file on
disk is the durable source.

## Classes

### `class ReplayReport`

Outcome of a replay sweep.

#### `is_empty`

```python
def is_empty(self) -> bool
```

## Functions

### `enqueue`

```python
def enqueue(request: dict, *, now: datetime | None = None) -> Path
```

Append a pending mine request to today's queue file.

``request`` must contain at minimum ``dir``, ``wing``, ``mode``. A
UTC ``ts`` field is added automatically. Writes are flushed and
fsynced before return so a crash immediately after enqueue cannot
lose the line.

### `pending_count`

```python
def pending_count(directory: Path | None = None) -> int
```

Return the number of queued (not-yet-replayed) requests.

Useful for the session-start warning. Cheap: counts non-empty
lines without parsing JSON.

### `peek`

```python
def peek(directory: Optional[Path] = None) -> list
```

Every request a :func:`replay` would POST, without touching the queue.

Read-only on purpose and by construction: ``replay`` claims each file by
renaming it before it reads a line, so using it with a no-op ``post_fn``
to preview a drain would still mutate the queue — a dry run that is not
dry.

It reproduces replay's filter chain rather than counting lines, because
the two must agree. Replay de-duplicates per file, skips unparseable
lines, and DROPS legacy whole-directory requests without posting them
(the multi-hour lock-holder of #414/#426). A preview over raw lines would
promise twelve and post nine — the same "report disagrees with what
happened" defect ``pending drain`` was added to remove (#498).

Not deadline-aware: a plan describes the whole queue, while a real sweep
may stop early. That difference is one-sided and safe — the plan can
over-state what a time-boxed run achieves, never under-state what an
unbounded one will touch.

### `replay`

```python
def replay(post_fn: Callable[[dict], bool], *, directory: Optional[Path] = None, deadline: Optional[float] = None) -> ReplayReport
```

Drain the queue by re-issuing each request via ``post_fn``.

``post_fn(request) -> bool`` is the caller's hook to actually
transmit one request. ``True`` means the daemon accepted it and
the line should be consumed; ``False`` means keep it for next
time. Anything that raises is treated as ``False`` — replay is a
best-effort background sweep and must not abort partway just
because one request blew up.

**Concurrency model (Gemini PR #104 review):** atomic-rewrite via
``tempfile + os.replace`` is NOT safe against concurrent
``enqueue`` calls: any line appended while we hold the file in
memory would be silently lost when we replace. We instead
"claim" each file by renaming it to ``&lt;name>.replay-&lt;pid>`` before
reading. ``enqueue`` always writes to ``YYYY-MM-DD.jsonl``, so any
concurrent appends land in a fresh file rather than the one we're
draining. Failed lines are appended back to the live file (not
rewritten) so they merge with any newly-enqueued entries.

``deadline`` is a ``time.monotonic()`` timestamp at which the
sweep stops processing more lines and returns whatever has been
drained so far. Used by ``hook_session_start`` to cap total replay
cost on the hot path. ``None`` (default) means no time limit.
