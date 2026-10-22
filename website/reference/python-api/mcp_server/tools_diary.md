# `mempalace.mcp_server.tools_diary`

Source: [`mempalace/mcp_server/tools_diary.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_server/tools_diary.py)

## Functions

### `tool_diary_write`

```python
def tool_diary_write(agent_name: str, entry: str, topic: str = 'general', wing: str = '', session_id: str = '')
```

Write a diary entry for this agent. Entries are timestamped and
accumulate over time in a diary room.

This is the agent's personal journal — observations, thoughts,
what it worked on, what it noticed, what it thinks matters.

All entries land in the main ``mempalace_drawers`` collection — the
earlier dedicated checkpoint collection has been retired (verbatim
transcripts already cover the recovery use case).

Note: ``agent_name`` is normalized to lowercase before storage so
that diary reads are case-insensitive (see #1243). "Claude",
"claude", and "CLAUDE" all resolve to the same agent.

### `tool_diary_read`

```python
def tool_diary_read(agent_name: str = '', last_n: int = 10, wing: str = '')
```

Read an agent's recent diary entries. Returns the last N entries
in chronological order — the agent's personal journal.

When ``wing`` is provided, reads only from that wing. When ``wing``
is empty or omitted, returns entries from every wing this agent has
written to. Diary writes from hooks land in project-derived wings
(``wing_&lt;project>``), so requiring a specific wing on read would
silo those entries from agent-initiated reads.

``agent_name`` is OPTIONAL (#501). Omit it (or pass a blank string) to
read the diary room itself rather than one agent's slice — every
agent's entries, newest first, each row tagged with its own ``agent``.
This exists because the reader frequently cannot know the name to ask
for: the Stop/PreCompact hook writes ``agent_name=&lt;harness>`` (hook.py's
``--harness`` flag — ``claude-code`` / ``codex`` / ``gemini-cli``) and
never consults ``identity.txt`` or ``MEMPALACE_AGENT_NAME``, so entries
plainly visible in ``list --wing W`` answered "No diary entries yet."
to every name a human would guess. Use ``tool_diary_agents`` to
enumerate the names actually present.

Note: ``agent_name`` is normalized to lowercase before filtering so
that reads are case-insensitive (see #1243). Entries written under
pre-fix mixed-case agent names will not match the lowercase filter;
use ``mempalace repair`` to migrate legacy data if needed.

### `tool_diary_agents`

```python
def tool_diary_agents(wing: str = '')
```

List the agent names that have diary entries, with a count each.

The companion to an identity-less ``tool_diary_read`` (#501): the read
path keys on drawers, but choosing an ``agent_name`` still requires
knowing which ones exist. Hook-written entries are keyed on the
*harness* name rather than any configured identity, so the only
reliable way to learn the name is to ask what is present.

Scoped to one wing when ``wing`` is given, otherwise palace-wide.
Rows are sorted by entry count, descending, so the busiest writer is
first. Each row carries ``latest`` (the newest ``filed_at`` seen for
that agent) so a reader can tell an active writer from a dormant one.

Counts are a LOWER BOUND when ``truncated`` is true — the scan is
capped at ``_DIARY_SCAN_LIMIT`` because the daemon runs near a 2 GB
cgroup ceiling (palace-daemon#256) and an unbounded metadata sweep is
a memory hazard. Reporting a capped number as a total would be a
report that disagrees with reality, so the flag is part of the
contract rather than a detail.

### `tool_hook_settings`

```python
def tool_hook_settings(silent_save: bool = None, desktop_toast: bool = None)
```

Get or set hook behavior settings.

- silent_save: True = stop hook saves directly (no MCP clutter),
  False = legacy blocking MCP calls. Default: True.
- desktop_toast: True = show notify-send desktop toast on save,
  False = terminal-only notification. Default: False.

Call with no arguments to see current settings.

### `tool_memories_filed_away`

```python
def tool_memories_filed_away()
```

Acknowledge the latest silent checkpoint. Returns a short summary.

### `tool_reconnect`

```python
def tool_reconnect()
```

Force the MCP server to drop cached ChromaDB + KnowledgeGraph state.

Use after external scripts or CLI commands modify the palace database
or replace ``knowledge_graph.sqlite3`` directly, which can leave the
in-memory HNSW index stale or pin a closed-on-disk SQLite connection.

### `tool_checkpoint`

```python
def tool_checkpoint(items, diary = None, dedup_threshold = 0.9, added_by = None)
```

Batch session save in a single call.

Semantic-dedups each item, files the non-duplicates as drawers, then
writes one diary entry. Collapses the per-item ``check_duplicate`` /
``add_drawer`` / ``diary_write`` sequence into one MCP request so the
host UI renders a single tool-call card (and keeps its spinner up for
the whole save) instead of one card per underlying call.

``items`` is a list of ``&#123;"wing", "room", "content"}`` dicts. ``diary``
is an optional ``&#123;"agent_name", "entry", "topic"?, "wing"?}`` dict.
``added_by`` attributes the filed drawers; when omitted it falls back to
the diary's ``agent_name`` (and then to ``"checkpoint"``), so the agent
that filed the session is recorded instead of a generic label.
Reuses the existing single-item handlers so dedup/idempotency/WAL
behaviour is identical to calling them directly.
