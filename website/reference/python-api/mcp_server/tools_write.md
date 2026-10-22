# `mempalace.mcp_server.tools_write`

Source: [`mempalace/mcp_server/tools_write.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mcp_server/tools_write.py)

## Functions

### `tool_add_drawer`

```python
def tool_add_drawer(wing: str, room: str, content: str, source_file: str = None, added_by: str = 'mcp', tags: list = None, session_id: str = '')
```

File verbatim content into a wing/room. Checks for duplicates first.

Content above ``chunk_size`` is split into bounded per-chunk drawers
via a single batched upsert. Each chunk carries ``parent_drawer_id``
linkage and ``chunk_index`` metadata so search can rejoin them. The
returned ``drawer_id`` is the LOGICAL group handle on the chunked
path; physical drawer ids are in ``chunk_ids`` (#1539).
``tool_get_drawer(drawer_id)`` automatically hydrates and reassembles all
chunks, and ``tool_delete_drawer(drawer_id)`` removes both the logical group
and all constituent physical chunks.

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
