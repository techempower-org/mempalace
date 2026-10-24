# `mempalace.logstream`

Source: [`mempalace/logstream.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/logstream.py)

logstream.py — Agent coordination event log for MemPalace (RFC 003)
===================================================================

A small append-only event layer stored in the active palace directory as
``logstream.sqlite3``. Agents use it to delegate work, wait for replies,
and exchange exact patch/file artifacts through the shared MemPalace hub
without a human relaying messages between machines.

Design constraints (RFC 003):

- No Chroma dependency, no vector index open — plain SQLite only.
- Append-only: events are immutable; corrections are new events that
  reference prior events (see :meth:`Logstream.ack_event`).
- Exact payloads: event bodies and artifact content are stored verbatim.
- Safe under concurrent HTTP requests (WAL + per-instance lock, same
  pattern as ``knowledge_graph.py``).
- Explicit size limits with clear errors, never silent truncation.

Usage:
    from mempalace.logstream import Logstream

    ls = Logstream(db_path="/path/to/palace/logstream.sqlite3")
    evt = ls.append_event(
        type="task.request", stream="project/mempalace", room="delegation",
        from_agent="mac-codex", to_agent="windows-codex",
        correlation_id="task_123", body="Please fix search echo ranking.",
    )
    reply = ls.wait_events(correlation_id="task_123", type="patch.ready",
                           timeout_ms=300_000)

## Classes

### `class Logstream`

Durable append-only coordination log (events + artifacts).

Storage and threading mirror ``KnowledgeGraph``: one SQLite file in
WAL mode, a per-instance lock around writes, ``check_same_thread=False``
so the MCP HTTP server can call from worker threads.

#### `__init__`

```python
def __init__(self, db_path: str, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES, max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES, replica_id: str = None)
```

#### `close`

```python
def close(self)
```

#### `append_event`

```python
def append_event(self, type: str, stream: str, room: str, from_agent: str, to_agent: str = None, correlation_id: str = None, branch: str = None, base_commit: str = None, status: str = None, body: str = '', metadata: dict = None, artifact_ids: list = None, topic: str = None) -> dict
```

Append one immutable event. Returns the stored event dict.

``artifact_ids`` must reference already-stored artifacts; unknown
ids are rejected so readers never see a dangling reference.

#### `put_artifact`

```python
def put_artifact(self, kind: str, content: str, created_by: str, metadata: dict = None) -> dict
```

Store exact artifact content (v1: UTF-8 text only).

Returns the artifact record without echoing ``content`` back —
callers already hold the content; readers use :meth:`get_artifact`.
For ``kind=patch``, a ``warnings`` list is included when the diff
looks unappliable (missing trailing newline, CRLF endings); the
content itself is still stored verbatim.

#### `ack_event`

```python
def ack_event(self, event_id: str, from_agent: str, status: str = None, body: str = '', topic: str = None) -> dict
```

Append an ``event.ack`` referencing a prior event.

The target event is never mutated. The ack copies the target's
stream/room/topic, copies its ``correlation_id`` (falling back to the
target's id so request/ack stay tied together), and routes back
to the target's ``from_agent``.

#### `submit_patch`

```python
def submit_patch(self, content: str, from_agent: str, stream: str, room: str = 'patches', to_agent: str = None, correlation_id: str = None, branch: str = None, base_commit: str = None, body: str = '', metadata: dict = None, topic: str = None) -> dict
```

Convenience wrapper: store a patch artifact + ``patch.ready`` event.

Both writes go through the validated single-writer paths; the event
insert re-checks the artifact exists, so readers never observe a
``patch.ready`` event with a dangling artifact id.

#### `get_artifact`

```python
def get_artifact(self, artifact_id: str) -> Optional[dict]
```

Fetch one artifact by id, exact content included. None if missing.

#### `list_events`

```python
def list_events(self, stream: str = None, room: str = None, topic: str = None, type: str = None, to_agent: str = None, from_agent: str = None, correlation_id: str = None, status: str = None, since_event_id: str = None, before_event_id: str = None, since_created_at: str = None, limit: int = DEFAULT_LIST_LIMIT, order: str = 'asc') -> list[dict]
```

List events matching structured filters.

Cursor and ordering semantics:

- ``since_event_id`` is the precise cursor: strictly AFTER that
  event in append order (``rowid > anchor``), regardless of timestamp ties
  or output ordering.
- ``before_event_id`` filters strictly BEFORE that event in append
  order (``rowid < anchor``) for reverse / historical paging.
- ``order`` is ``'asc'`` (oldest first, default) or ``'desc'`` (newest first).
- ``since_created_at`` is inclusive (``>=``) so second-granularity
  timestamps never skip events; callers dedup by ``id``.
- ``to_agent`` also matches broadcast events (``to_agent='*'``).

#### `latest_event_id`

```python
def latest_event_id(self) -> Optional[str]
```

Id of the newest event, or None on an empty log.

Live-tail consumers (the SSE stream) capture this at connect time
as their starting cursor so they receive only post-connect events.

#### `wait_events`

```python
def wait_events(self, timeout_ms: int = 60000, poll_interval_s: float = None, **filters) -> dict
```

Block until at least one matching event exists or timeout expires.

v1 implementation per RFC 003: a polling loop inside the request,
sleeping 250-1000 ms with jitter. Timeouts are clamped to
``MAX_WAIT_TIMEOUT_MS`` and return ``&#123;"timed_out": True,
"events": []}`` rather than raising.

``filters`` accepts the same keyword filters as :meth:`list_events`.
``poll_interval_s`` pins the sleep (tests); default is jittered.

#### `watch_events`

```python
def watch_events(self, *, cursor: str = None, poll_timeout_ms: int = MAX_WAIT_TIMEOUT_MS, limit: int = DEFAULT_LIST_LIMIT, poll_interval_s: float = None, **spec)
```

Yield ``(matched_events, cursor)`` forever as new events arrive.

The long-poll primitive :meth:`wait_events` caps at
``MAX_WAIT_TIMEOUT_MS`` and reports a timeout, which makes it a
building block rather than a watcher: every caller ends up writing
the same re-arm loop, and each one has to remember to carry the
cursor forward. This owns both.

An idle poll yields ``([], cursor)`` rather than blocking silently,
so a caller can implement an idle timeout, emit a heartbeat, or
checkpoint its cursor without running a second clock.

``poll_timeout_ms`` may be a callable returning the timeout for
this iteration so a caller can cap it to a remaining idle
deadline (an idle of 400ms must not wait out a 300s long-poll).

The cursor advances past every event *examined*, not merely those
that matched, so a watcher that restarts never rescans what it has
already judged. ``spec`` takes the set-valued filters described by
:func:`event_matches_watch`.

#### `version_vector`

```python
def version_vector(self) -> dict
```

&#123;origin_replica: highest origin_seq applied locally}.

The complete description of this replica's knowledge — peers diff
their vectors to compute exactly which op ranges are missing.

#### `list_ops`

```python
def list_ops(self, origin: str, after_seq: int = 0, limit: int = 500) -> list[dict]
```

Events authored by ``origin`` with origin_seq > after_seq, in
author order. The anti-entropy pull unit.

#### `apply_remote_event`

```python
def apply_remote_event(self, event: dict) -> bool
```

Fold one remote op into the local log, idempotently.

Verbatim rule: the event is stored exactly as authored (id,
created_at, hlc, origin stamps untouched); only the local rowid —
the arrival cursor — is ours. Referenced artifacts must already be
applied (sync pulls artifacts first) so readers never see a dangling
id, same invariant as append_event. Returns True if inserted, False
if we already had it. Raises ValueError on malformed input or a
missing artifact.

#### `has_artifact`

```python
def has_artifact(self, artifact_id: str) -> bool
```

Cheap existence probe (no content transfer) for the sync engine.

#### `apply_remote_artifact`

```python
def apply_remote_artifact(self, artifact: dict) -> bool
```

Fold one remote artifact in, idempotently, verifying its hash.

Content is verbatim; the sha256 must match or the artifact is
rejected — a corrupt transfer must never enter the store.

## Functions

### `normalize_watch_values`

```python
def normalize_watch_values(value) -> Optional[set]
```

Coerce a watch filter argument to a set of strings, or ``None``.

``None`` means "any" — an absent filter, not an empty one. An argument
holding only blanks collapses to ``None`` for the same reason, so a
stray ``--type ""`` cannot silently match nothing forever.

### `event_matches_watch`

```python
def event_matches_watch(event: dict, *, streams = None, rooms = None, topics = None, types = None, statuses = None, to_agents = None, from_agents = None, exclude_from_agents = None, correlation_ids = None) -> bool
```

Client-side half of a watch filter. Every argument is a set or ``None``.

Exclusion beats every positive match: an event from an excluded writer is
rejected even when it satisfies the rest of the filter.

### `sanitize_watch_spec`

```python
def sanitize_watch_spec(spec: dict) -> dict
```

Validate and normalize every value in a watch spec.

``list_events`` sanitizes the filters it is given, so a *single-valued*
watch filter is checked for free by being pushed down. Multi-valued ones
are not pushed down and were therefore compared raw, which made
validation depend on how many values you happened to pass:
``--type Task.Request`` alone was rejected, while
``--type Task.Request --type patch.ready`` was silently accepted and then
matched nothing, leaving the watcher waiting forever for an event type
that cannot exist.

Sanitizing here — with the same functions ``list_events`` uses — makes
the two paths agree, and normalizes values (stripping whitespace) so a
padded routing value still matches. Raises ``ValueError`` naming the
offending value.

### `pushdown_watch_filters`

```python
def pushdown_watch_filters(spec: dict) -> dict
```

The subset of a watch spec that ``list_events`` can evaluate in SQL.

Only single-valued fields push down. Narrowing the query is an
optimization, never a correctness dependency — whatever stays behind is
re-checked by :func:`event_matches_watch` on every candidate row.

### `sanitize_watch_state_basename`

```python
def sanitize_watch_state_basename(agent: str) -> str
```

Turn an agent identity into a state-file basename (no directories).

RFC 005 identities use colons (``windows:grok:mempalace``), which are
illegal in Windows filenames. Slashes would escape ``~/.mempalace/watch``.

### `default_watch_state_file`

```python
def default_watch_state_file(agent: str, *, home: Optional[str] = None) -> str
```

``~/.mempalace/watch/&lt;sanitized-agent>.json`` for ``logstream watch``.

### `resolve_watch_state_file`

```python
def resolve_watch_state_file(state_file: Optional[str], agent: Optional[str]) -> Optional[str]
```

Choose a watch state-file path.

``None`` means "apply the default from ``--agent``". An empty string is
an explicit disable (tests, or a caller that does not want a cursor).
A non-empty string is used as-is.

### `read_watch_state`

```python
def read_watch_state(path: str) -> tuple
```

Return ``(cursor, condition)`` for a watch state file.

A cursor of ``None`` is ambiguous and the ambiguity is dangerous, so the
condition disambiguates it:

``absent``
    No state file. A genuine first run — the caller may start at the tip.
``ok``
    A usable cursor; resume strictly after it.
``empty``
    The file exists and records ``cursor: null`` — the watcher started
    against an empty log and has not seen an event yet. **Not** a first
    run: treating it as one and jumping to the tip would skip whatever
    arrived while the watcher was stopped, and the next checkpoint would
    make that permanent.
``corrupt``
    Unreadable, truncated, or valid JSON that is not an object. Costs a
    replay, never a skip — refusing to start would cost every event
    after it, and skipping would lose them silently.

### `read_watch_cursor`

```python
def read_watch_cursor(path: str) -> Optional[str]
```

The cursor from a watch state file, or ``None``.

Convenience wrapper over :func:`read_watch_state` for callers that do not
need to tell a first run from a corrupt file. Watchers should prefer
``read_watch_state``, because those two cases must not behave alike.

### `write_watch_cursor`

```python
def write_watch_cursor(path: str, cursor: str, agent: str = None, required: bool = False) -> None
```

Persist a watch cursor atomically.

Written to a temp file and renamed so a crash mid-write cannot leave a
half-file that reads back as a *different, earlier* cursor — that would
silently replay events the watcher had already handled.

Ordinary checkpoints are best effort, because losing one costs a replay
while crashing the watcher costs every event after it. That trade-off
inverts for the *first* checkpoint of a fresh watch: if it never lands,
the next launch sees no state file, calls itself a first run, and starts
at the tip — skipping everything that arrived in between, permanently.
Pass ``required=True`` there so the failure is raised rather than
swallowed.
