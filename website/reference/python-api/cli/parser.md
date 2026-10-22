# `mempalace.cli.parser`

Source: [`mempalace/cli/parser.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/cli/parser.py)

## Functions

### `cmd_wings`

```python
def cmd_wings(args)
```

List every wing with its drawer count (slice of #191, issue #356).

Wraps ``mempalace_list_wings``. On the daemon path the counts come
from ``GET /status/fast`` first — that endpoint already aggregates
wing counts from the metadata index and answers in well under a
second, whereas ``mempalace_list_wings`` over ``/mcp`` walks the
facet path and does not return in any usable time on a palace of a
few hundred thousand drawers (measured against the production
daemon, 2026-08-20). ``mempalace_list_wings`` stays as the fallback
for daemons that predate ``/status/fast``. Both shapes are reduced
to the tool's ``&#123;"wings": &#123;...}}`` envelope so ``--json`` consumers
see one contract regardless of which path served the request.

The issue also asked for a "last updated" column; ``tool_list_wings``
returns counts only and no timestamp is available anywhere on the
read path, so the table carries a share-of-palace percentage instead.

### `cmd_taxonomy`

```python
def cmd_taxonomy(args)
```

Print the wing → room → drawer-count tree (slice of #191, issue #362).

Wraps ``mempalace_get_taxonomy``. ``--wing`` narrows the output to
one wing; the underlying tool takes no arguments, so the filter is
applied client-side after the tree comes back (documented here
because it does not reduce the work the daemon does).

### `cmd_aaak`

```python
def cmd_aaak(args)
```

Print the AAAK dialect specification (slice of #191, issue #362).

Wraps ``mempalace_get_aaak_spec``. Routed rather than read straight
out of the local package on purpose: when a daemon is configured,
the spec that matters is the one the daemon is actually filing
against, which can differ from the locally installed version.

### `cmd_hallway_list`

```python
def cmd_hallway_list(args, _deprecated_alias: bool = False)
```

List within-wing entity hallways (slice of #191, issue #358).

Wraps ``mempalace_list_hallways``. The pre-existing ``mempalace
hallways`` verb is a deprecated alias that delegates here (#407), so
both spellings share one fetch path, one sort and one limit rule;
``_deprecated_alias`` only adds the ``deprecated`` marker to the
JSON payload when the caller arrived by the old name.

### `cmd_hallway_delete`

```python
def cmd_hallway_delete(args)
```

Delete one hallway record by id (slice of #191, issue #358).

Destructive, so it refuses to act without ``--confirm``. On an
interactive terminal the flag can be supplied by answering the
prompt; with no TTY there is nobody to ask, so the command exits 2
rather than deleting on an implied yes.

### `cmd_hallway`

```python
def cmd_hallway(args)
```

Dispatch the two-level ``hallway`` verb.

### `cmd_checkpoint`

```python
def cmd_checkpoint(args)
```

Batch-file a session in one call (slice of #191, issue #360).

Wraps ``mempalace_checkpoint``: semantic-dedups each item, files the
non-duplicates as drawers, then writes one optional diary entry.

This writes to the palace, so ``--dry-run`` prints the exact payload
that would be sent and exits without calling the tool — the cheap
way to check a generated ``--items-file`` before it lands.

### `main`

```python
def main()
```

CLI entry point for the ``mempalace`` console script.

Side effect: pops ``PYTHONPATH`` from ``os.environ`` (see #1423) so
any subprocess this CLI spawns inherits a clean env. Host applications
that call ``main()`` programmatically should be aware that the parent
process loses ``PYTHONPATH`` as well. Library imports
(``import mempalace.searcher`` from a host app) do NOT trigger this
side effect; only the CLI/MCP entry points pop the env var.
