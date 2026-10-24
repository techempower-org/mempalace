# `mempalace.mcp_server.tools_read`

Source: [`mempalace/mcp_server/tools_read.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_server/tools_read.py)

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
def tool_search(query: str, limit: int = 15, wing: str = None, room: str = None, tags: list = None, source_file: str = None, since: str = None, before: str = None, max_distance: float = None, min_similarity: float = None, context: str = None, candidate_strategy: str = None, fusion_mode: str = 'convex', include_trace: bool = False, cli_compatible: bool = False, include_media: bool = False, query_task: str = 'search')
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
def tool_list_hallways(wing: str = None, limit: int = 100, offset: int = 0)
```

List within-wing hallway records, strongest first, one page at a time.

A palace of any size holds hundreds of thousands of hallways; returning
them all in one response closed the MCP connection. The page is sorted by
co-occurrence count so the first page is the one worth reading.

### `tool_delete_hallway`

```python
def tool_delete_hallway(hallway_id: str)
```

Delete a hallway record by its ID.

### `tool_follow_tunnels`

```python
def tool_follow_tunnels(wing: str, room: str, record: bool = True)
```

Follow explicit tunnels from a room to see connected drawers in other wings.

An agent calling this tool is crossing the tunnel, so the traversal is
recorded (``access_count`` / ``strength``). Internal callers that only
decorate another result, such as the light server's search enrichment,
pass ``record=False``: a search hit is not a crossing. A read-only
server or a peer without the writer lock never writes the tunnel file.
