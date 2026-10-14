# `mempalace.mine_phase`

Source: [`mempalace/mine_phase.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/mine_phase.py)

Report which phase a running mine is in, for whoever spawned it (#548).

A drainer-spawned ``mempalace mine`` once waited 11 h 26 m at ~0 % CPU and
left no line in any log: its stdout and stderr are piped and only read when
it exits, so anything it printed was invisible while it mattered. The
spawner's own heartbeat (palace-daemon #304) can say *that* a mine is idle,
not *where*.

This module closes that gap with a file, not a stream. When
``MEMPALACE_MINE_PHASE_FILE`` names a path, :func:`report` rewrites it with a
small JSON object -- the phase, when that phase began, when the file was last
written, the pid, and any counts the caller passes. The spawner reads it on
its own schedule. A stuck mine shows up as one ``phase`` whose ``updated``
stops advancing; ``since`` says how long it has been in that phase.

Unset, every call is a no-op. Reporting must never break a mine, so every
failure here is swallowed.

## Functions

### `report`

```python
def report(phase: str, **counts) -> None
```

Record that the mine is now in ``phase``, with optional counts.
