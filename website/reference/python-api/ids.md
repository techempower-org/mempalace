# `mempalace.ids`

Source: [`mempalace/ids.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/ids.py)

Centralized drawer/triple ID construction with collision-safe delimiter.

Drawer IDs and content-addressed identifiers built by concatenating strings
without a delimiter before hashing form a defect class that allows
``hash(s1 + str(i1)) == hash(s2 + str(i2))`` whenever
``s1 + str(i1) == s2 + str(i2)``. Under ChromaDB's primary-key constraint
the second upsert silently overwrites the first, losing content with no
error raised. The styleguide's partial-scope-key-migration rule names this
shape — every concat-into-hash site is a candidate that must be triaged.

This module is the single source of truth for ID construction in mempalace.
All call sites use the named helpers below; no module should inline
``hashlib.sha256(a + b)`` patterns.

## Functions

### `make_drawer_id_from_chunk`

```python
def make_drawer_id_from_chunk(wing: str, room: str, source_file: str, chunk_index: int) -> str
```

Drawer ID for the project / format miner paths.

Hash input is ``f"&#123;source_file}|&#123;chunk_index}"`` — the '|' separator
prevents the classic ``"/a1" + "23" == "/a" + "123"`` collision.

Returns ``drawer_&#123;wing}_&#123;room}_&#123;hash24}`` where hash24 is the first
24 hex chars of SHA-256 over the delimited input.

### `make_drawer_id_from_content`

```python
def make_drawer_id_from_content(wing: str, room: str, content: str) -> str
```

Drawer ID for the MCP ``add_drawer`` tool path.

Hash input is ``f"&#123;wing}|&#123;room}|&#123;content}"`` — the delimiters prevent
``wing="foo" + room="bar"`` colliding with ``wing="fooba" + room="r"``
(architecturally identical defect class to the chunk-index sites,
even though astronomically rare in practice since content is large
freeform text).

### `make_convo_drawer_id`

```python
def make_convo_drawer_id(wing: str, room: str, source_file: str, extract_mode: str, chunk_index: int) -> str
```

Drawer ID for the conversation miner path.

Pre-v2 the convo miner used ':' as delimiter; this helper migrates
to '|' for codebase-wide consistency and to remove the Windows-path
/ URL-source edge case that ':' carried.

Hash input is ``f"&#123;source_file}|&#123;extract_mode}|&#123;chunk_index}"``.

### `make_format_sentinel_id`

```python
def make_format_sentinel_id(wing: str, source_file: str) -> str
```

Sentinel id for a format-miner file that produced no drawers.

Hash input is ``wing``, ``source_file`` and ``format``, so two wings
filing the same path do not overwrite each other's marker.

### `make_convo_sentinel_id`

```python
def make_convo_sentinel_id(source_file: str, extract_mode: str) -> str
```

Sentinel registry ID for the conversation miner zero-chunk-file path.

Pre-v2 the sentinel used ':' as delimiter; this helper migrates to
'|' for the same reasons as ``make_convo_drawer_id``.

Hash input is ``f"&#123;source_file}|&#123;extract_mode}"``.

### `make_exchange_drawer_id`

```python
def make_exchange_drawer_id(wing: str, room: str, source_file: str, filed_at: str, content: str) -> str
```

Drawer ID for a single verbatim conversation exchange.

Used by live agent integrations (e.g. Hermes) and their backfills via
``convo_miner.file_conversation_exchange``. Hashes the FULL content,
not a prefix — prefix hashing collided on common openings ("User: hi
can you help me with…") and ChromaDB's upsert silently overwrote the
earlier drawer. ``filed_at`` is included so genuinely repeated
exchanges stay distinct drawers (verbatim always — repetition is
signal, not noise).

Hash input is ``f"&#123;source_file}|&#123;filed_at}|&#123;content}"``.

### `make_triple_id`

```python
def make_triple_id(sub_id: str, predicate: str, obj_id: str, valid_from: str, recorded_at: str) -> str
```

Triple ID for knowledge-graph insertion.

Pre-v2 the recorded_at hash input was
``f"&#123;valid_from}&#123;datetime.now().isoformat()}"`` with no delimiter —
two ISO datetimes concatenated could collide in principle (e.g.
``valid_from="2026-01-01" + isoformat "T12:..."`` vs
``valid_from="2026-01-01T12" + isoformat ":..."``).

Returns ``t_&#123;sub_id}_&#123;predicate}_&#123;obj_id}_&#123;hash12}`` where hash12 is
the first 12 hex chars of SHA-256 over ``f"&#123;valid_from}|&#123;recorded_at}"``.
