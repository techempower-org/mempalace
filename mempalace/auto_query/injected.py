"""Per-session record of drawers auto-query has already injected.

Auto-query never injects the same drawer twice in one session (#429): the
ids it has shown live in ``~/.mempalace/auto_query/injected/<session>.json``
and are filtered out of later results.

That is right while the context holds. It is exactly wrong after a context
compaction, where the session id survives but the context does not — the file
then suppresses the drawers the agent just lost, and suppresses more of them
the longer the session has run (#449). Compaction therefore needs a way to
drop the record, which is why this bookkeeping lives in its own module
instead of inside the auto-query runner: the SessionStart path clears it
without importing the whole classifier.

Every function fails open. A hook must never break a session over a cache.
"""

import json
import os

__all__ = [
    "injected_dir",
    "injected_path",
    "load_injected",
    "remember_injected",
    "clear_injected",
]

# Keep the tail of the list only; a long session would otherwise grow the
# file without bound.
MAX_REMEMBERED = 500


def injected_dir():
    # type: () -> str
    """Directory holding one dedupe file per session."""
    return os.path.expanduser("~/.mempalace/auto_query/injected")


def injected_path(session_id):
    # type: (str) -> str
    """Path to one session's dedupe file (id sanitized to a safe basename)."""
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_")[:80] or "cli"
    return os.path.join(injected_dir(), safe + ".json")


def load_injected(session_id):
    # type: (str) -> set
    """Drawer ids already injected this session; empty set if unknown."""
    try:
        with open(injected_path(session_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(str(x) for x in data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def remember_injected(session_id, drawer_ids):
    # type: (str, Iterable) -> None
    """Add drawer ids to the session's record (fail-open on I/O)."""
    ids = load_injected(session_id)
    ids.update(str(d) for d in drawer_ids if d)
    path = injected_path(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(ids)[-MAX_REMEMBERED:], f)
    except OSError:
        pass


def clear_injected(session_id):
    # type: (str) -> int
    """Forget everything injected this session; return how many ids were dropped.

    Called on ``SessionStart(source="compact")``. The count is reported to the
    human, because "cleared 119 suppressed drawers" is the measurement that
    says how much recall the dedupe was costing (#449).
    """
    count = len(load_injected(session_id))
    try:
        os.remove(injected_path(session_id))
    except OSError:
        # Nothing was dropped — no file, or it could not be removed. Report
        # the effect, not the intent: a caller that prints "cleared 119" over
        # a file that is still there would be lying to the human.
        return 0
    return count
