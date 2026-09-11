# `mempalace.hallway_store`

Source: [`mempalace/hallway_store.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/hallway_store.py)

Hallway persistence — JSON file (default) or a postgres table (opt-in).

Why this module exists, measured on the production palace host 2026-09-10:

``hallways.json`` is growing fast enough that any single figure is stale on
arrival: **479 MB** (early evening) → **1,041,537,215 B / ~797K records /
21 wings** (20:03) → **1,142,562,893 B / 1,903,306 records / 50 wings**
(measured read-only at 23:0x the same night). No duplication — record count,
distinct ids and distinct (wing, sorted-pair) tuples all agree exactly, so
the growth is new wings plus the N(N-1)/2 pair combinatorics, not a writer
bug. Every hallway operation loads and JSON-parses the whole thing:

- ``list_hallways(wing=...)`` filters in Python *after* the full load, so the
  ``wing`` argument does not reduce the work at all. The daemon cannot
  fast-intercept it (palace-daemon#255): it runs under a 2 GB cgroup cap
  already sitting at 98.4%, and parsing a 1 GB JSON array materializes
  several GB of dicts.
- ``compute_hallways_for_wing`` loads it too, just to preserve four dynamics
  fields, and ``_compute_entity_tunnels_for_wing`` loads it a second time via
  ``list_hallways`` — so two parsed copies of the corpus are live at once,
  which is where the 1.6-4.1 GB RSS on a mine subprocess comes from.
- ``_save_hallways`` rewrites all 1.04 GB per mine that produces a hallway —
  O(total) per write, when the change is a handful of rows.

Sizing this honestly, because the issue's own numbers invite an overstatement:
measured at production scale (900K records / 396 MB synthetic) json.load is
3.1 s and json.dump 8.4 s, so the two-loads-plus-one-save per mine is
**~37 s of JSON I/O**, not the 29 minutes #442's comment reports for the
post-mine block. That 29 minutes is ``create_tunnel`` doing a full
``_load_tunnels`` + atomic ``_save_tunnels`` per call from inside a loop
(``palace_graph.py`` :840/:861, called at :1145 and :1278) — O(n^2) in the
tunnel count, against a *tunnels.json* that is only 837 KB at 2000 tunnels.
The sweep's open file was ``tunnels.json.tmp``, which says so directly.
Measured by the #474 lane, mechanism confirmed here. Moving hallways to
postgres removes the ~37 s and the RSS and unblocks the daemon's
fast-intercept; it does **not** fix the 29-minute mine.

The postgres store makes each of those proportional to the *wing* rather
than to the whole palace: an indexed ``WHERE wing = %s``, a wing-scoped
dynamics read, and a delete+insert of one wing.

**JSON remains the default.** The flag is ``hallway_backend`` (config.json)
or ``MEMPALACE_HALLWAY_BACKEND`` (env), and the production cutover is a
separate, deliberate step run after the migration is verified against real
data. Nothing here changes behaviour until someone opts in. That also keeps
the local-first promise intact: a chroma/sqlite install never needs postgres.

Connections are opened per operation rather than cached. Hallway reads and
writes are infrequent (a list call, one replace per wing per mine), so the
per-call connect is cheap — and it makes this store immune by construction
to the stale-cached-connection failure that #385 had to add a retry seam
for. The one high-volume caller, the migration script, passes its own
connection in and holds it open.

## Classes

### `class JsonHallwayStore`

The historical store: one JSON file, loaded and rewritten whole.

Kept as the default and as the fallback for non-postgres installs. Calls
back into ``hallways`` at call time rather than importing its helpers at
module scope, so the existing tests that monkeypatch
``hallways._get_hallway_file`` keep working unchanged.

#### `__init__`

```python
def __init__(self, config = None)
```

#### `ensure_schema`

```python
def ensure_schema(self) -> None
```

#### `list`

```python
def list(self, wing: Optional[str] = None, limit = None, offset = None) -> list[dict]
```

#### `count`

```python
def count(self, wing: Optional[str] = None) -> int
```

#### `dynamics_for_wing`

```python
def dynamics_for_wing(self, wing: str) -> dict
```

#### `replace_wing`

```python
def replace_wing(self, wing: str, records: list[dict]) -> None
```

#### `delete`

```python
def delete(self, hallway_id: str) -> bool
```

### `class PostgresHallwayStore`

Hallways in a postgres table: wing-scoped reads, wing-scoped writes.

#### `__init__`

```python
def __init__(self, dsn: str)
```

#### `ensure_schema`

```python
def ensure_schema(self, conn = None) -> None
```

Create the table and indexes if absent. Never drops, never rewrites.

#### `list`

```python
def list(self, wing: Optional[str] = None, limit = None, offset = None, conn = None) -> list[dict]
```

#### `count`

```python
def count(self, wing: Optional[str] = None, conn = None) -> int
```

#### `dynamics_for_wing`

```python
def dynamics_for_wing(self, wing: str, conn = None) -> dict
```

The four accumulated fields for one wing, keyed by sorted entity pair.

Keyed the same way ``_hallway_id`` hashes — sorted — so a record
stored with the pair in the other order still matches and keeps its
accumulated weights instead of silently resetting them on recompute.

#### `upsert_many`

```python
def upsert_many(self, records, conn = None) -> int
```

#### `replace_wing`

```python
def replace_wing(self, wing: str, records: list[dict], conn = None) -> None
```

Swap one wing's rows in ONE transaction. Other wings are untouched.

Both halves of that matter, and the JSON path had both: it preserved
other wings (scope) *and* wrote a temp file then ``os.replace``d it
(atomicity), so a wing could never be observed half-written. A DELETE
followed by N autocommit INSERTs keeps the scope and quietly drops the
atomicity -- a crash after the DELETE leaves the wing EMPTY, a crash
midway leaves it partial, and the recompute that would repair it only
runs on the next mine of that wing. So the whole swap commits or rolls
back together.

When the caller passes ``conn`` the caller's transaction governs.

The delete runs even when ``records`` is empty: a wing whose pairs all
fell below ``min_count`` must end up with no rows, not with its
previous snapshot left behind.

#### `delete`

```python
def delete(self, hallway_id: str, conn = None) -> bool
```

## Functions

### `get_hallway_store`

```python
def get_hallway_store(config = None)
```

Return the configured hallway store. JSON unless postgres is opted into.

A postgres selection with no DSN falls back to JSON with a warning rather
than raising: a misconfigured flag must not take down a mine, and silently
returning an empty store would look like "this wing has no hallways",
which is worse than slow.
