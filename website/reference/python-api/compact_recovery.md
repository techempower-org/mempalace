# `mempalace.compact_recovery`

Source: [`mempalace/compact_recovery.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/compact_recovery.py)

Re-inject what a compaction took away (techempower-org/mempalace#449).

A context compaction keeps the session id and throws away the context. Two
things follow, and both were measured on the fleet:

1. **The auto-query dedupe outlives the context it was protecting.**
   ``~/.mempalace/auto_query/injected/&lt;session>.json`` lists the drawers
   already shown this session so they are not repeated (#429). After a
   compaction those drawers are gone from the agent and still marked shown —
   so auto-query suppresses precisely the material the agent just lost, and
   suppresses more of it the longer the session ran. One live session on
   katana had 119 ids in that file.

2. **The post-compaction hooks offered a pointer, not content.** They printed
   "your history is in the palace, run ``mempalace wake-up``". Fleet
   check-in 2026-09-03: agents do not act on invitations ("the reflex has
   never fired unprompted"). Hooks that inject without asking do.

So on ``SessionStart(source="compact")`` this module clears the dedupe file
and returns the wake-up story itself as ``additionalContext``.

The hook side is one call::

    printf '%s' "$INPUT" | PYTHONPATH=$MEMPALACE_DIR $PY -m mempalace.compact_recovery

It reads Claude Code's SessionStart JSON on stdin, prints the hook payload on
stdout, and always exits 0 — a recovery aid must never be able to fail a
session.

## Classes

### `class CompactRecovery(object)`

What a post-compaction recovery did, and what it wants injected.

#### `__init__`

```python
def __init__(self, cleared, context, system_message, receipt)
```

#### `hook_payload`

```python
def hook_payload(self)
```

The JSON Claude Code's SessionStart hook expects.

``systemMessage`` is the visible receipt — a palace query the human
can see, per the transparency rule that every query announces itself.
``additionalContext`` is omitted entirely when there is nothing to
inject, so a dead daemon costs the session nothing but one line.

## Functions

### `recover`

```python
def recover(session_id, wing, config = None)
```

Clear the session's dedupe and build the re-injection block.

Order matters: the dedupe is cleared first and unconditionally. It is the
half that does not depend on the network, and it is the half that was
actively suppressing recall.

### `wing_for_cwd`

```python
def wing_for_cwd(cwd, config)
```

Project directory -> canonical palace wing (same rule as the hooks).

### `main`

```python
def main(argv = None)
```
