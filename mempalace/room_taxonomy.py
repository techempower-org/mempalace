"""Canonical room taxonomy — soft-warn validation.

Palace storage organizes drawers under ``wing`` (project) + ``room``
(topic). The 7 canonical rooms are the recommended taxonomy per
``docs/superpowers/specs/2026-05-13-palace-room-taxonomy.md``:

    architecture, decisions, problems, planning,
    sessions, references, discoveries

Historically, the postgres backend enforced this list via a foreign-key
constraint on ``mempalace_drawers.room`` referencing
``mempalace_canonical_rooms``. Per techempower-org/mempalace#86 that FK
has been relaxed: non-canonical room names are now ACCEPTED and a
warning is emitted in the write-path response so the caller (and
ultimately the user) is informed instead of silently failing.

This module provides:

- ``CANONICAL_ROOMS`` — the canonical 7-tuple as a Python constant.
- ``validate_room(room)`` — return a list of warning strings for the
  given room name. Empty list when the room is canonical.

The check is intentionally case-sensitive: the canonical names are
lowercase, and ``sanitize_name`` already lowercases room values before
storage.
"""

from __future__ import annotations

import difflib
from typing import Iterable, List, Optional

# ── Canonical taxonomy ────────────────────────────────────────────────

#: The 7 canonical room names. Treat as immutable.
CANONICAL_ROOMS: tuple[str, ...] = (
    "architecture",
    "decisions",
    "problems",
    "planning",
    "sessions",
    "references",
    "discoveries",
)


def is_canonical_room(room: str) -> bool:
    """Return True iff ``room`` is one of the canonical 7."""
    return room in CANONICAL_ROOMS


def suggest_canonical(
    room: str, *, choices: Optional[Iterable[str]] = None, cutoff: float = 0.6
) -> Optional[str]:
    """Return the closest canonical match for ``room`` or ``None``.

    Thin wrapper around ``difflib.get_close_matches`` so callers can
    surface a "did you mean X?" hint when a non-canonical name lands.

    ``choices`` defaults to ``CANONICAL_ROOMS``; pass a different
    iterable to widen the lookup (e.g. against a runtime list that
    includes installation-specific custom rooms).
    """
    if not room:
        return None
    pool = list(choices) if choices is not None else list(CANONICAL_ROOMS)
    matches = difflib.get_close_matches(room, pool, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def validate_room(room: str) -> List[str]:
    """Return warning strings for ``room``; empty list when canonical.

    Per #86 — non-canonical rooms are accepted, not rejected. The
    warning shape is stable and machine-parseable: the canonical list
    is rendered inline so the caller does not have to import this
    module to render a useful message.
    """
    if not room:
        # Empty room string is its own pathology — callers should
        # validate non-empty before calling us, but be defensive.
        return [
            "room is empty; canonical rooms are " f"[{', '.join(CANONICAL_ROOMS)}]. Accepted as-is."
        ]
    if is_canonical_room(room):
        return []

    canonical_list = ", ".join(CANONICAL_ROOMS)
    suggestion = suggest_canonical(room)
    if suggestion:
        return [
            f"room {room!r} is not in the canonical taxonomy "
            f"[{canonical_list}]. Accepted as-is; "
            f"consider {suggestion!r} as the closest canonical match."
        ]
    return [
        f"room {room!r} is not in the canonical taxonomy " f"[{canonical_list}]. Accepted as-is."
    ]
