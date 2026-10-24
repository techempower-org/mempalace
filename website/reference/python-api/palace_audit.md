# `mempalace.palace_audit`

Source: [`mempalace/palace_audit.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace_audit.py)

Palace organization audit — how navigable is the palace, not how big.

``mempalace status`` answers "what is filed". This module answers "is it
filed somewhere an agent can find by walking the palace", scoring five
layers that each have a known failure mode:

* **rooms** — mined transcripts collapse into a handful of generic rooms
  (``technical``, ``architecture``, ``planning``, ``general``, ``problems``),
  so the room layer stops carrying information.
* **naming** — wings and rooms drift into near-duplicate spellings
  (``acme-app`` / ``acme_app``, ``release-3.6.0`` / ``release_3_6_0``).
* **tunnels** — auto-generated entity tunnels on generic tokens that nobody
  ever traverses.
* **hallways** — entity co-occurrence links between an entity and its own
  path or file spelling (``main.zig`` ↔ ``src/main.zig``).
* **knowledge graph** — one-off predicates that make the graph a log rather
  than something queryable by relation.

Every reader here is read-only and never opens the vector index: drawer
counts come from the backend's grouped sqlite query, tunnels and hallways
from their JSON sidecars, and the knowledge graph from a read-only sqlite
connection. The audit therefore runs while an MCP server holds the palace
lock, which is exactly when you want to look at a live palace.

## Functions

### `drift_key`

```python
def drift_key(name: str) -> str
```

Collapse a wing/room name to the key two drifted spellings share.

Lower-cases, drops every non-alphanumeric character, and strips a
trailing ``s`` so ``concierge-automation`` and ``concierge-automations``
meet. ``release-3.6.0`` and ``release_3_6_0`` both become ``release360``.

### `hallway_entity_key`

```python
def hallway_entity_key(entity: str) -> str
```

Kept as the audit's name for :func:`mempalace.hallways.entity_spelling_key`.

### `resolve_kg_path`

```python
def resolve_kg_path(palace_path: str, explicit: bool = False) -> str
```

The knowledge graph that belongs to ``palace_path``.

A palace-local ``knowledge_graph.sqlite3`` always wins. The legacy
``~/.mempalace/knowledge_graph.sqlite3`` belongs to the legacy default
palace (``~/.mempalace/palace``) alone, so it is used only for that
palace — whether it was reached by default, ``--palace``,
``MEMPALACE_PALACE_PATH`` or ``config.json``. Any other palace keeps its
graph inside itself, so ``audit`` cannot report and ``kg normalize
--yes`` cannot rewrite an unrelated graph. ``explicit`` (``--palace``
given) forces the palace-local path, as the MCP server does for its
flag. Neither file is created here.

### `audit_palace`

```python
def audit_palace(palace_path: Optional[str] = None, config: Optional[MempalaceConfig] = None, progress = None, explicit_palace: Optional[bool] = None) -> dict
```

Run every read-only check and return the report as a dict.

``explicit_palace`` says the caller passed ``--palace``; see
:func:`resolve_kg_path` for which knowledge graph belongs to a palace.

``progress(step, detail)`` is called before and after each reader — the
hallway sidecar alone can be tens of megabytes, and a caller on a terminal
wants to see that something is happening. ``detail`` is ``None`` at the
start of a step and a short result string at its end.

Raises ``FileNotFoundError`` when the palace directory does not exist and
``RuntimeError`` when drawer counts cannot be read from either the sqlite
fast path or the collection.

### `render_audit`

```python
def render_audit(report: dict, width: Optional[int] = None) -> str
```

Terminal rendering of :func:`audit_palace`'s dict.

Wraps to ``width`` (default: the terminal's, capped at 100 columns) with a
hanging indent, so a finding stays one visual unit on a narrow terminal.

### `audit_to_json`

```python
def audit_to_json(report: dict) -> str
```

### `terminal_progress`

```python
def terminal_progress(stream)
```

A ``progress`` callback that prints one timed line per reader.

Meant for stderr on an interactive terminal: it never lands in ``--json``
output or a redirected report.
