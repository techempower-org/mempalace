# `mempalace.mcp_server`

Source: [`mempalace/mcp_server.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_server.py)

MemPalace MCP Server — read/write palace access for Claude Code
================================================================
Install: claude mcp add mempalace -- mempalace-mcp [--palace /path/to/palace]

Tools (read):
  mempalace_status          — total drawers, wing/room breakdown
  mempalace_list_wings      — all wings with drawer counts
  mempalace_list_rooms      — rooms within a wing
  mempalace_get_taxonomy    — full wing → room → count tree
  mempalace_search          — semantic search, optional wing/room/source_file filter
  mempalace_check_duplicate — check if content already exists before filing

Tools (write):
  mempalace_add_drawer      — file verbatim content into a wing/room
  mempalace_delete_drawer   — remove a drawer by ID
  mempalace_delete_by_source — bulk-remove all drawers mined from one source_file

Tools (maintenance):
  mempalace_reconnect       — force cache invalidation and reconnect after external writes

## Functions

### `tool_status`

```python
def tool_status()
```

### `tool_list_wings`

```python
def tool_list_wings()
```

### `tool_list_rooms`

```python
def tool_list_rooms(wing: str = None)
```

### `tool_get_taxonomy`

```python
def tool_get_taxonomy()
```

### `tool_search`

```python
def tool_search(query: str, limit: int = 15, wing: str = None, room: str = None, tags: list = None, source_file: str = None, since: str = None, before: str = None, max_distance: float = None, min_similarity: float = None, context: str = None, candidate_strategy: str = None, fusion_mode: str = 'convex', include_trace: bool = False, cli_compatible: bool = False)
```

### `tool_check_duplicate`

```python
def tool_check_duplicate(content: str, threshold: float = 0.9)
```

### `tool_get_aaak_spec`

```python
def tool_get_aaak_spec()
```

Return the AAAK dialect specification.

### `tool_traverse_graph`

```python
def tool_traverse_graph(start_room: str, max_hops: int = 2)
```

Walk the palace graph from a room. Find connected ideas across wings.

### `tool_walk_palace`

```python
def tool_walk_palace(start_wing: str = None, start_room: str = None, start_entity: str = None, depth: int = 2, limit: int = 50)
```

Agent-facing palace walk via AGE Cypher traversal.

Phase 6 of the AGE-integration goal. Exposes the "agent walks into the
palace" metaphor as a single tool: pass a starting node (wing OR room
OR entity) and a depth, get back the navigable subgraph it touches.

Three traversal modes by starting node:

- **start_wing="memorypalace"**: enumerate rooms in this wing
  (depth=1), plus drawers in those rooms (depth=2), plus mentioned
  entities (depth=3). The "walking into a wing" pattern.
- **start_room="problems"**: enumerate drawers in this room across
  all wings (depth=1), plus their mentioned entities (depth=2). The
  "walking into a specific room" pattern.
- **start_entity="pgvector"**: enumerate drawers mentioning this
  entity (depth=1), plus the rooms+wings containing them (depth=2).
  The "find where in the palace X is discussed" pattern (inverse
  walk — entity → drawer → room → wing).

Exactly one of (start_wing, start_room, start_entity) must be given.

Returns a structured walk result with:
  - ``start``: the input anchor
  - ``walk``: list of &#123;wing, room, drawer, entity} rows, one per
    leaf reached
  - ``stats``: &#123;wings_touched, rooms_touched, drawers_touched,
    entities_touched}

Requires MEMPALACE_BACKEND=postgres and the AGE graph populated via
kg_writethrough or backfill_age.

### `tool_find_tunnels`

```python
def tool_find_tunnels(wing_a: str = None, wing_b: str = None)
```

Find rooms that bridge two wings — the hallways connecting domains.

### `tool_graph_stats`

```python
def tool_graph_stats()
```

Palace graph overview: nodes, tunnels, edges, connectivity.

### `tool_mesh_peers`

```python
def tool_mesh_peers()
```

Mesh estate snapshot — the committed compat surface for PalaceMind's
mesh view: exactly the GET /sync/peers payload, produced by the same
function so the tool and the endpoint can never drift. Read-only;
peers.json tokens are never included.

### `tool_create_tunnel`

```python
def tool_create_tunnel(source_wing: str, source_room: str, target_wing: str, target_room: str, label: str = '', source_drawer_id: str = None, target_drawer_id: str = None)
```

Create an explicit cross-wing tunnel between two palace locations.

Use when you notice content in one project relates to another project.
Example: an API design discussion in project_api connects to the
database schema in project_database.

### `tool_list_tunnels`

```python
def tool_list_tunnels(wing: str = None, include_passive: bool = False)
```

List cross-wing tunnels, optionally filtered by wing.

Default returns only explicit (agent-created) tunnels stored at
``~/.mempalace/tunnels.json``. Pass ``include_passive=True`` to also
include passive tunnels (rooms appearing in 2+ wings, computed from
``graph_stats``). Each result is tagged with ``kind: 'explicit'|'passive'``.
See techempower-org/mempalace#75 for the asymmetry that motivated the
merged-result form.

### `tool_delete_tunnel`

```python
def tool_delete_tunnel(tunnel_id: str)
```

Delete an explicit tunnel by its ID.

### `tool_list_hallways`

```python
def tool_list_hallways(wing: str = None)
```

List within-wing hallway records, optionally filtered by wing.

### `tool_delete_hallway`

```python
def tool_delete_hallway(hallway_id: str)
```

Delete a hallway record by its ID.

### `tool_follow_tunnels`

```python
def tool_follow_tunnels(wing: str, room: str)
```

Follow explicit tunnels from a room to see connected drawers in other wings.

### `tool_add_drawer`

```python
def tool_add_drawer(wing: str, room: str, content: str, source_file: str = None, added_by: str = 'mcp', tags: list = None, session_id: str = '')
```

File verbatim content into a wing/room. Checks for duplicates first.

Content above ``chunk_size`` is split into bounded per-chunk drawers
via a single batched upsert. Each chunk carries ``parent_drawer_id``
linkage and ``chunk_index`` metadata so search can rejoin them. The
returned ``drawer_id`` is the LOGICAL group handle on the chunked
path; physical drawer ids are in ``chunk_ids`` (#1539). To delete
or fetch the underlying drawers, iterate ``chunk_ids`` or query by
``parent_drawer_id`` — ``tool_get_drawer(drawer_id)`` and
``tool_delete_drawer(drawer_id)`` report "not found" on the chunked
path because no row is stored under the logical group id.

``tags`` is an optional list of cross-cutting labels (multi-label
additive layer over the strict wing/room hierarchy). See
``mempalace.tags`` for normalisation rules.

### `tool_delete_drawer`

```python
def tool_delete_drawer(drawer_id: str)
```

Delete a single logical drawer by ID.

### `tool_mine`

```python
def tool_mine(source: str, mode: str = 'projects', wing: str = None, agent: str = 'mempalace', limit: int = 0, dry_run: bool = False, extract: str = 'exchange')
```

Mine a directory into the palace — the MCP equivalent of ``mempalace mine``.

Lets MCP clients that cannot shell out (Claude Desktop, LM Studio, Aionui,
Desktop Commander) trigger indexing in-conversation (#1662). Wraps the same
in-process miners the CLI's ``cmd_mine`` calls; it adds no new ingestion
logic of its own.

mode:
    ``"projects"`` (default) — code/docs via ``miner.mine``.
    ``"convos"``             — chat transcripts via ``convo_miner.mine_convos``.
    ``"extract"``            — office documents (PDF/DOCX/RTF/…) via
                               ``format_miner.mine_formats``; requires the
                               optional ``mempalace[extract]`` dependency.
wing:    target wing (default: derived from the source directory name).
agent:   recorded on every drawer (default ``"mempalace"``).
limit:   max files to process (0 = all).
dry_run: walk + chunk and report, but file nothing.
extract: convos extraction strategy — ``"exchange"`` (default) or
         ``"general"``; ignored by the other modes.

Runs synchronously and mirrors the :func:`tool_sync` contract: success
returns ``&#123;success: True, mode, dry_run, output[, output_truncated]}`` where ``output`` is
the miner's human-readable summary (captured so it cannot corrupt the
JSON-RPC stream); failure returns ``&#123;success: False, error[, error_class]}``.
The palace write lock is held by the miners themselves, so a concurrent mine
surfaces as a structured already-running error. Orphan cleanup is not part of
mining — use ``mempalace_sync`` for that.

### `tool_delete_by_source`

```python
def tool_delete_by_source(source_file: str, dry_run: bool = True)
```

Delete every drawer whose ``source_file`` metadata matches exactly.

Bulk cleanup for the contamination case in #1722, where benchmark/eval
files (ShareGPT dumps, ``results_mempal_*.jsonl``, language config JSON)
get mined into the same wing as real user data and drown out semantic
search. Previously the only recourse was hand-rolled SQLite ``DELETE``
against ``chroma.sqlite3``.

Matching is exact on the stored ``source_file`` value and pushed down to
the backend via ``delete(where=...)`` — the same idiom used by the miner
and diary ingest paths — so there is no client-side id list and the
SQLite "too many variables" limit cannot be hit, regardless of how many
drawers share the source (the reporter had 55k).

Also purges the matching closets (the AAAK index layer) so deleting the
drawers doesn't strand stale index pointers at the dead source (#1722).

Defaults to a dry run: it reports the drawer match count, the closet match
count, and a small sample so the caller can confirm the blast radius before
anything is removed. Pass ``dry_run=False`` to commit the deletion
(irreversible).

### `tool_sync`

```python
def tool_sync(project_dir: str = None, wing: str = None, apply: bool = False)
```

Prune drawers whose source files are gitignored, missing, or moved (#1252).

### `tool_get_drawer`

```python
def tool_get_drawer(drawer_id: str)
```

Fetch a single logical drawer by ID. Returns full content and metadata.

### `tool_list_drawers`

```python
def tool_list_drawers(wing: str = None, room: str = None, since: str = None, before: str = None, tags: list = None, limit: int = 20, offset: int = 0)
```

List logical drawers with pagination. Optional wing/room/tag filter.

Optional ``since`` / ``before`` filter by drawer ``filed_at`` (ISO date or
timestamp): ``since`` is inclusive, ``before`` is exclusive (#1128). A
drawer whose ``filed_at`` is missing or unparseable is excluded while a
date bound is active. The filter is applied in Python after the rows are
fetched — ChromaDB rejects string operands for ``$gte``/``$lt`` (1.5.7),
and ``filed_at`` is stored as an ISO string, so a server-side ``where``
comparison is not available.

### `tool_update_drawer`

```python
def tool_update_drawer(drawer_id: str, content: str = None, wing: str = None, room: str = None, tags: list = None)
```

Update an existing logical drawer's content and/or metadata.

``tags`` semantics:
    * ``None`` — leave the existing tag list untouched.
    * ``[]``   — clear all tags.
    * non-empty list — replace the existing tag list with the
      normalised input.

### `tool_rate_memory`

```python
def tool_rate_memory(drawer_id: str, useful: bool)
```

Record feedback on whether a search result was helpful (#159).

Stores the rating as drawer *metadata* — the verbatim content is never
touched. Each call increments one of two counters (``rating_useful`` /
``rating_not_useful``); the net of the two becomes a bounded, capped
ranking signal in ``search_memories`` that can reorder neighbours but
never excludes a drawer (recall is preserved).

### `tool_rename_wing`

```python
def tool_rename_wing(from_wing: str, to_wing: str, batch_size: int = 500)
```

Rename all drawers in one wing to another, server-side.

Iterates through the source wing in batches and updates each
drawer's metadata. Much faster than individual update_drawer calls
over HTTP since it operates directly on the collection.

### `tool_list_tags`

```python
def tool_list_tags(wing: str = None, room: str = None, min_count: int = 1)
```

Return every unique tag in the palace with the number of drawers carrying it.

Results are sorted by count (descending). ``wing`` and ``room`` scope
the count to a subset of the palace. ``min_count`` drops tags below
the threshold from the result; default 1 keeps any tag with at least
one drawer.

### `tool_kg_query`

```python
def tool_kg_query(entity: str, as_of: str = None, direction: str = 'both')
```

Query the knowledge graph for an entity's relationships.

### `tool_kg_add`

```python
def tool_kg_add(subject: str, predicate: str, object: str, valid_from: str = None, valid_to: str = None, source_closet: str = None, source_file: str = None, source_drawer_id: str = None, context: str = None)
```

Add a relationship to the knowledge graph.

All temporal and provenance fields are optional. ``valid_to`` lets callers
backfill historical facts with a known end date/time in a single call
instead of a separate ``kg_invalidate`` call.

Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
the form ``YYYY-MM-DDTHH:MM:SSZ``.

``context`` is the SPOC fourth-axis (techempower-org/mempalace#161):
a free-form anchor naming where the fact was witnessed (e.g.
``drawer:abc123``, ``conversation:2026-05-28``). The AGE backend
stores it on the RELATION edge and surfaces it through every read
path; the SQLite backend silently accepts and ignores it (storage
schema doesn't yet have a column for it) so callers don't need to
branch on backend.

### `tool_kg_invalidate`

```python
def tool_kg_invalidate(subject: str, predicate: str, object: str, ended: str = None)
```

Mark a fact as no longer true.

Returns the actual ``ended`` date/time that was stored. When the caller
omits ``ended``, the underlying graph stamps ``date.today()`` and the
response reflects that resolved value.

Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
the form ``YYYY-MM-DDTHH:MM:SSZ``.

### `tool_kg_supersede`

```python
def tool_kg_supersede(subject: str, predicate: str, old_object: str, new_object: str, at: str = None)
```

Atomically replace one fact with another at a single shared boundary.

Closes ``(subject, predicate, old_object)`` and opens
``(subject, predicate, new_object)`` at one shared instant, so a
point-in-time query at the boundary returns only the new value. Use this
instead of a separate ``kg_invalidate`` + ``kg_add`` when a single-valued
fact changes (e.g. a model, employer, or address changes).

``at`` accepts ``YYYY-MM-DD`` or a canonical UTC datetime
(``YYYY-MM-DDTHH:MM:SSZ``) and defaults to the current UTC instant.

### `tool_kg_timeline`

```python
def tool_kg_timeline(entity: str = None, as_of: str = None)
```

Get chronological timeline of facts, optionally for one entity.

``as_of`` (techempower-org/mempalace#161) filters to facts whose
temporal interval contains the given date/datetime; NULL ends are
treated as open intervals (same semantics as ``mempalace_kg_query``).
Omit ``as_of`` to see the full timeline including expired facts.

### `tool_kg_stats`

```python
def tool_kg_stats()
```

Knowledge graph overview: entities, triples, relationship types.

Returns a structured error envelope on transient postgres failures
(the connection dropped between `_call_kg` opening the handle and
`kg.stats()` finishing its query — typically caused by a postgres
OOM-kill or restart under load). The caller sees
``&#123;"error": "backend_unavailable", "retryable": True, ...}`` and can
surface "try again in a moment" instead of an opaque -32000
internal error. See techempower-org/mempalace#299.

Non-transient errors (cypher syntax, value-validation, schema
mismatch) still propagate — those need a real fix, not a retry.

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

### `handle_request`

```python
def handle_request(request)
```

### `main`

```python
def main()
```

MCP server entry point for the ``mempalace-mcp`` console script.

Side effect: pops ``PYTHONPATH`` from ``os.environ`` (see #1423) so any
subprocess this server spawns inherits a clean env. Host applications that
call ``main()`` programmatically should be aware that the parent process
loses ``PYTHONPATH`` as well. Library imports do NOT trigger this side
effect; only the CLI/MCP entry point does.

Transports:
- ``stdio`` remains the default for existing Claude/MCP deployments.
- ``http`` is opt-in and serves JSON-RPC POSTs at ``/mcp`` in the same
  process, avoiding the long-lived stdio framing failure surface from
  #1801.
