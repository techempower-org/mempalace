# `mempalace.palace.closets`

Source: [`mempalace/palace/closets.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/palace/closets.py)

## Functions

### `build_closet_lines`

```python
def build_closet_lines(source_file, drawer_ids, content, wing, room, drawer_metas = None)
```

Build compact closet pointer lines from drawer content.

Returns a LIST of lines (not joined). Each line is one complete topic
pointer — never split across closets.

Legacy format (3 segments): ``topic|entities|→drawer_ids``
Tier 6a format (4 segments): ``topic|entities|YYYY-MM-DD:Lstart-Lend|→drawer_ids``

When ``drawer_metas`` is provided and the first meta carries both
``line_start``/``line_end`` plus a parseable ``filed_at``, the 4-segment
form is emitted so retrieval can jump to the right span. Otherwise the
legacy 3-segment form is used — backward compat for drawers filed before
Tier 6a and for direct callers that don't have metadata handy.

### `purge_file_closets`

```python
def purge_file_closets(closets_col, source_file: str) -> None
```

Delete every closet associated with ``source_file``.

Call this before ``upsert_closet_lines`` on a re-mine so stale topics
from a prior schema/version don't survive in the closet collection.
Mirrors the drawer-purge step in process_file().

### `upsert_closet_lines`

```python
def upsert_closet_lines(closets_col, closet_id_base, lines, metadata)
```

Write topic lines to closets, packed greedily without splitting a line.

Closets are deterministically numbered (``..._01``, ``..._02``, …) and
each ``upsert`` fully overwrites the prior content at that ID. Callers
are expected to ``purge_file_closets`` first when re-mining a source
file so stale-numbered closets from larger prior runs don't leak.

Returns the number of closets written.
