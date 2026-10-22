# `mempalace.cli.cmd_coord`

Source: [`mempalace/cli/cmd_coord.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/cmd_coord.py)

## Functions

### `cmd_window`

```python
def cmd_window(args) -> None
```

Drawers in filed order between two bounds — unranked, paged.

#500: ``search --since`` filters a *ranked* search, so inside a window
you get whatever scores highest rather than the sequence, and ``list``
has no time filter at all (against a 201K-drawer wing a date is ~200
pages away). This is the chronological walk.

Semantics are the palace's existing ones, not new: ``--from`` inclusive,
``--to`` exclusive, wall-clock comparison, and a drawer with no
``filed_at`` excluded while a bound is active — the same contract
``list --since`` and ``search --since`` use. What changed is that the
daemon evaluates it in SQL instead of filtering in Python after
fetching every row.

### `cmd_source`

```python
def cmd_source(args) -> None
```

Every drawer from one source file, in chunk order (#502).

Search hits carry ``source_file``; the natural next question is "give
me the drawers from THAT file, in order", which had no command. Order
is chunk order within the file — ``chunk_index`` numerically, so chunk
10 follows chunk 2 — then filed time, then id.

### `cmd_diary`

```python
def cmd_diary(args)
```

``mempalace diary write|read`` — the agent diary at the CLI (#354).

``write`` wraps ``mempalace_diary_write``; ``read`` wraps
``mempalace_diary_read``; ``agents`` wraps ``mempalace_diary_agents``.
``read``'s ``--topic`` / ``--since`` filters are applied client-side —
the tool has no such parameters.

Only ``write`` needs an agent name (#501). A read does not: the hook
writes ``agent_name=&lt;harness>`` (``claude-code`` / ``codex`` /
``gemini-cli``) and never consults ``identity.txt`` or
``MEMPALACE_AGENT_NAME``, so requiring one on read meant entries
plainly visible in ``list --wing W`` answered "No diary entries" to
every name a reader could guess. ``agents`` exists to discover the
names that do work.

### `cmd_kg`

```python
def cmd_kg(args)
```

``mempalace kg add|invalidate|timeline`` — KG writes + temporal read (#357).

Deviations from issue #357's sketch, driven by the tool schemas:
``kg invalidate`` addresses a fact by ``--subject/--predicate/--object``
(``mempalace_kg_invalidate`` has no triple ids and no reason field), and
``kg timeline``'s ``--limit`` truncates client-side because
``mempalace_kg_timeline`` takes only ``(entity, as_of)``.

A ``--limit`` above ``_KG_TIMELINE_TOOL_CAP`` cannot be honoured: the
graph backend's own ``timeline()`` stops at that many rows and the tool
doesn't expose the parameter, so the CLI reports how many it actually
received rather than implying the requested window was searched. Seen
live: ``kg timeline JP --limit 200`` returns ``count: 100``.

### `cmd_walk`

```python
def cmd_walk(args)
```

``mempalace walk`` — traverse the palace graph (#359).

Two tools behind one verb: ``--follow palace`` (default) calls
``mempalace_walk_palace`` from a ``--wing`` / ``--room`` / ``--entity``
anchor, and ``--follow tunnels`` calls ``mempalace_traverse`` from a
``--room`` anchor with ``--depth`` as its hop budget. Issue #359's
``--from &lt;drawer_id>`` is not offered: neither tool accepts a drawer
anchor (``mempalace why &lt;drawer_id>`` is the per-drawer view).

### `cmd_rate`

```python
def cmd_rate(args)
```

``mempalace rate &lt;drawer_id> --useful|--not-useful`` (#361).

``mempalace_rate_memory`` records a boolean, not a 1–5 score, and has
no field for a free-text reason — so issue #361's ``--score N
--reason "..."`` sketch is expressed as the two boolean flags. The
rating lands in drawer metadata and becomes a bounded ranking signal;
it never touches verbatim content.

### `cmd_logstream`

```python
def cmd_logstream(args)
```

### `cmd_task`

```python
def cmd_task(args)
```

Create and run complete logstream tasks through a small public interface.

### `cmd_artifact`

```python
def cmd_artifact(args)
```
