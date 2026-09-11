# `mempalace.palace_graph`

Source: [`mempalace/palace_graph.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace_graph.py)

palace_graph.py — Graph traversal layer for MemPalace
======================================================

Builds a navigable graph from the palace structure:
  - Nodes = rooms (named ideas)
  - Edges = shared rooms across wings (tunnels)
  - Edge types = halls (the corridors)

Enables queries like:
  "Start at chromadb-setup in wing_code, walk to wing_myproject"
  "Find all rooms connected to riley-college-apps"
  "What topics bridge wing_hardware and wing_myproject?"

No external graph DB needed — built from ChromaDB metadata.

## Functions

### `sqlite_grouped_counts_reader`

```python
def sqlite_grouped_counts_reader(config = None)
```

Return the backend's grouped-counts function, or ``None``.

``None`` means the sqlite path cannot serve this palace and the caller
should use the collection — which is also how a missing or unreadable
palace keeps reporting a real diagnostic instead of an empty graph.

The backend is resolved from *configuration*, not by sniffing the palace
directory for db files: a directory holding artifacts for two backends is
a ``BackendMismatchError`` on every normal path, and sniffing would quietly
pick one instead of surfacing that.

### `invalidate_graph_cache`

```python
def invalidate_graph_cache()
```

Clear the graph cache. Called from mcp_server.py on writes.

### `build_graph`

```python
def build_graph(col = None, config = None)
```

Build the palace graph from ChromaDB metadata.

Returns cached result if fresh (within TTL). Cache is invalidated
on writes via invalidate_graph_cache(). Thread-safe via _graph_cache_lock.

Note: warm cache ignores ``col`` and ``config`` arguments — this is
intentional for the MCP server's single-palace use case. Callers
switching collections should call ``invalidate_graph_cache()`` first.

On postgres backends the implementation dispatches to
``_build_graph_postgres`` which does the grouping in one SQL
aggregate — avoids the O(N) Python-dict accumulation that
OOM-killed palace-daemon on the 271k-drawer palace
(techempower-org/mempalace#95). The chroma walk path stays for
chroma backends (and as a fallback if the postgres path errors).

Returns:
    nodes: dict of &#123;room: &#123;wings: list, halls: list, count: int, dates: list}}
    edges: list of &#123;room, wing_a, wing_b, hall, count} — one per tunnel crossing

### `traverse`

```python
def traverse(start_room: str, col = None, config = None, max_hops: int = 2)
```

Walk the graph from a starting room. Find connected rooms
through shared wings.

Returns list of paths: [&#123;room, wing, hall, hop_distance}]

### `find_tunnels`

```python
def find_tunnels(wing_a: str = None, wing_b: str = None, col = None, config = None)
```

Find rooms that connect two wings (or all tunnel rooms if no wings specified).
These are the "hallways" — same named idea appearing in multiple domains.

### `graph_stats`

```python
def graph_stats(col = None, config = None)
```

Summary statistics about the palace graph.

``total_rooms`` keeps its historical meaning: unique room-name nodes in
the passive graph. ``total_room_instances`` counts distinct (wing, room)
placements, which is the number users naturally compare with ``status``.
Explicit tunnel records are reported separately so the overview does not
silently omit agent-created graph connections.

### `create_tunnel`

```python
def create_tunnel(source_wing: str, source_room: str, target_wing: str, target_room: str, label: str = '', source_drawer_id: str = None, target_drawer_id: str = None, kind: str = 'explicit', config = None)
```

Create an explicit (symmetric) tunnel between two locations in the palace.

Tunnels are undirected: ``create_tunnel(A, B)`` and ``create_tunnel(B, A)``
resolve to the same canonical ID. A second call with the same endpoints
updates the stored label (and drawer IDs, if provided) rather than
creating a duplicate. Endpoints are compared **verbatim** — ``"my-wing"``
and ``"my_wing"`` are distinct (see Note below and #1504).

The ``source`` / ``target`` fields on the returned dict preserve the
argument order the caller used, so callers can display it directionally
if they like. The ID and dedup are symmetric.

Args:
    source_wing: Wing of the source (e.g., "project_api").
    source_room: Room in the source wing.
    target_wing: Wing of the target (e.g., "project_database").
    target_room: Room in the target wing.
    label: Description of the connection.
    source_drawer_id: Optional specific drawer ID.
    target_drawer_id: Optional specific drawer ID.
    kind: Tunnel category — ``"explicit"`` (default, user-created link
        between real rooms) or ``"topic"`` (auto-generated cross-wing
        topical link where rooms are synthetic ``topic:&lt;name>``
        identifiers). Preserved on the stored dict so readers can
        distinguish real-room traversals from topic connections.
    config: Optional ``MempalaceConfig`` selecting the palace and its
        tunnel sidecar. Explicit-path callers must pass the matching
        config instead of falling back to the ambient default palace.

Returns:
    The stored tunnel dict.

Raises:
    ValueError: if any wing or room is empty or non-string, or if an explicit
                tunnel points to a nonexistent room.

Note:
    Wing slugs are stored verbatim — passing ``"my-wing"`` and ``"my_wing"``
    produces two distinct tunnels (canonical IDs differ). Read-path helpers
    (``list_tunnels`` / ``follow_tunnels``) normalize both sides at compare
    time so legacy underscore data and explicit-flag hyphen data both
    match queries in either form. See #1504.

### `create_tunnels`

```python
def create_tunnels(specs: list, config = None) -> list
```

Create or refresh many tunnels with ONE load and ONE save.

``create_tunnel`` persists on every call — a full ``_load_tunnels`` and a
full atomic ``_save_tunnels`` each time — and it is called from inside the
per-entity loop of :func:`entity_tunnels_for_wing` and the per-wing loop
of :func:`compute_topic_tunnels`. N tunnels therefore cost N loads and N
rewrites, and the per-tunnel cost grows with the tunnels already on disk.
Measured on a throwaway palace: 100 tunnels 0.61 s, 1,000 tunnels 11.5 s,
2,000 tunnels 37.9 s — O(n²), extrapolating to ~16 min at 10K tunnels,
which is the 29-minute mine reported on #474. The file was 837 KB at
2,000 tunnels; this was never a big-file problem.

Batching makes it O(n). Each spec is a dict of the same arguments
``create_tunnel`` takes. Results come back in spec order; a spec whose
endpoints resolve to a tunnel already in the batch refreshes it rather
than duplicating it, exactly as two sequential calls would.

Room validation for ``kind="explicit"`` runs for the whole batch BEFORE
the lock is taken, so a rejected batch leaves the file untouched rather
than half-written.

### `list_tunnels`

```python
def list_tunnels(wing: str = None, include_passive: bool = False, col = None, config = None)
```

List cross-wing tunnels, optionally filtered by wing.

Two kinds of tunnels exist in mempalace:

- **Explicit tunnels** are agent-created cross-wing links persisted at
  ``~/.mempalace/tunnels.json``. Each is a directed pair of (wing, room)
  with optional drawer IDs, intentionally placed by something noticing
  "these two specific spots in different wings refer to the same thing".

- **Passive tunnels** are emergent structure: rooms that appear in two or
  more wings. ``graph_stats(col)`` discovers these by inspecting the
  palace itself; no JSON file involved.

Default behavior returns only explicit tunnels — the file-backed list.
Pass ``include_passive=True`` to also include passive tunnels computed
from ``graph_stats(col, config).top_tunnels``. Each result in the merged
list is tagged with a ``kind`` key (``"explicit"`` or ``"passive"``) so
consumers can render them differently.

Returns tunnels where ``wing`` appears as either source or target
(explicit tunnels are symmetric; passive tunnels are filtered by whether
``wing`` appears in their ``wings`` list). See techempower-org/mempalace#75
for why this asymmetry mattered to downstream consumers.

### `delete_tunnel`

```python
def delete_tunnel(tunnel_id: str)
```

Delete an explicit tunnel by ID. Returns ``&#123;"deleted": &lt;id>}``.

### `follow_tunnels`

```python
def follow_tunnels(wing: str, room: str, col = None, config = None)
```

Follow explicit tunnels from a room — returns connected drawers.

Given a location (wing/room), finds all tunnels leading from or to it,
and optionally fetches the connected drawer content.

### `topic_room`

```python
def topic_room(name: str) -> str
```

Return the synthetic room identifier for a topic tunnel.

Prefixing avoids collisions with literal folder-derived rooms of the
same name (e.g. a wing that has both an "Angular" folder room and an
"Angular" topic tunnel).

### `compute_topic_tunnels`

```python
def compute_topic_tunnels(topics_by_wing: dict, min_count: int = 1, label_prefix: str = 'shared topic', config = None) -> list[dict]
```

Create tunnels for every pair of wings that share >= ``min_count`` topics.

Args:
    topics_by_wing: ``&#123;wing_name: [topic_name, ...]}`` mapping. Topic
        names are compared case-insensitively; the first observed
        casing is used for the tunnel room name.
    min_count: minimum number of overlapping topics required to drop
        any tunnel between a wing pair. ``1`` means a single shared
        topic is enough; bumping to e.g. ``2`` requires multiple
        overlaps and filters out coincidental single-topic links.
    label_prefix: human-readable string prefixed to the tunnel label.

Returns:
    List of tunnel dicts as returned by ``create_tunnel`` — one per
    (wing_a, wing_b, topic) triple that crossed the threshold. A
    wing-pair below ``min_count`` produces no tunnels at all (not
    even for its single shared topic).

No-op semantics:
  - empty/None ``topics_by_wing`` returns ``[]``.
  - wings whose topic list is empty are skipped.
  - ``min_count <= 0`` is clamped to 1.

### `topic_tunnels_for_wing`

```python
def topic_tunnels_for_wing(wing: str, topics_by_wing: dict, min_count: int = 1, label_prefix: str = 'shared topic', config = None) -> list[dict]
```

Compute topic tunnels involving a single wing.

Used by the miner to incrementally update tunnels for the wing that
just finished mining without recomputing pairs that don't involve it.
Returns the list of tunnels created or refreshed.

### `entity_tunnels_for_wing`

```python
def entity_tunnels_for_wing(wing: str, hallways: list, label_prefix: str = 'shared entity', config = None) -> list
```

Compute entity tunnels involving a single wing.

An entity tunnel bridges two wings when the same entity (person,
project, concept, interest) appears in within-wing hallways of both.
This is the architectural counterpart to ``topic_tunnels_for_wing`` —
same storage path (``create_tunnel`` → ``~/.mempalace/tunnels.json``),
same dedup, same listing API — but the substrate is hallway records
rather than raw topic words. See v4 architecture doc, Wing →
Drawer-entities → Hallway → Tunnel.

Endpoints use the synthetic room id ``entity:&lt;name>`` (mirrors
``topic:&lt;slug>``) so they can't collide with literal folder-derived
rooms of the same name. Casing of the entity is preserved.

Topic tunnels are NOT replaced — both systems coexist for one release
cycle while entity tunnels prove out. Deprecation is a separate PR.
