"""Report which phase a running mine is in, for whoever spawned it (#548).

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
"""

from __future__ import annotations

import json
import os
import time

ENV_VAR = "MEMPALACE_MINE_PHASE_FILE"

# Within one phase, rewrite at most this often. A phase change always writes.
_MIN_INTERVAL_S = 2.0

_state = {"phase": None, "since": 0.0, "written": 0.0}


def report(phase: str, **counts) -> None:
    """Record that the mine is now in ``phase``, with optional counts."""
    path = os.environ.get(ENV_VAR, "").strip()
    if not path:
        return
    now = time.time()
    if phase != _state["phase"]:
        _state["phase"] = phase
        _state["since"] = now
    elif now - _state["written"] < _MIN_INTERVAL_S:
        return
    record = {
        "phase": phase,
        "since": round(_state["since"], 3),
        "updated": round(now, 3),
        "pid": os.getpid(),
    }
    record.update(counts)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)
        _state["written"] = now
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
