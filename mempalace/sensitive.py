"""Which conversation chunks are sensitive, and so go to the private collection.

A session transcript can carry text that must not be searchable by every
agent: a phone call pasted into a tool result, a live transcription read
line by line, a file read from a private directory. The convo miner asks
:func:`sensitive_chunk_flags` which of a transcript's chunks are such, files
those verbatim into ``PRIVATE_COLLECTION_NAME`` and leaves them out of the
drawers collection (#588). The answer depends only on the chunks, so a
re-mine of a growing session classifies every chunk the same way again.

A chunk is sensitive when it matches a marker, or when it continues one that
did: a tool output longer than a chunk spills into the next ones, and only
the first names the file it came from. A continuation ends at the next user
turn (``>``) or the next tool call (``[Tool] …``).

Markers are regular expressions: :data:`DEFAULT_MARKERS`, plus any listed
under ``sensitive_markers`` in the palace config. Site-specific names belong
in the config, not here.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Iterable, List, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MARKERS: Tuple[str, ...] = (
    # Anything read from a private directory.
    r"(?:~|/home/[^/\s]+)/private/",
    # livekit-voice call records, by path, and a record's raw turn shape.
    r"livekit-voice/calls/",
    r'"t":\s*-?[0-9.]+,\s*"text":',
    # The live-transcription script's segment markers.
    r"=== listening (?:started|stopped)",
)

# A line that opens a new block: a user turn, or a tool call as normalize renders it.
# normalize renders a tool call as "[Bash] cmd", "[Read /path]", "[mcp__x__y] …" or "[tool] …".
_BLOCK_START = re.compile(r"^\s*(?:>|\[(?:[A-Z][A-Za-z_]*|mcp__[\w-]+|tool)(?:\]|\s))")


@lru_cache(maxsize=8)
def _compiled(extra: Tuple[str, ...]) -> Tuple[re.Pattern, ...]:
    out = []
    for pattern in DEFAULT_MARKERS + extra:
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            logger.warning("sensitive_markers: skipping invalid pattern %r", pattern)
    return tuple(out)


def configured_markers() -> Tuple[str, ...]:
    """The palace config's ``sensitive_markers`` (a list of regular expressions), or none."""
    try:
        from .config import MempalaceConfig

        extra = MempalaceConfig().sensitive_markers
    except Exception:
        logger.debug("sensitive_markers: config unreadable; defaults only", exc_info=True)
        return ()
    return tuple(p for p in extra if isinstance(p, str) and p)


def is_marked(text: str, extra: Sequence[str] = ()) -> bool:
    return any(p.search(text) for p in _compiled(tuple(extra)))


def sensitive_chunk_flags(contents: Iterable[str], extra: Sequence[str] = ()) -> List[bool]:
    """One flag per chunk, in order: True for a marked chunk and for the chunks that continue it."""
    flags: List[bool] = []
    carrying = False
    for text in contents:
        if is_marked(text, extra):
            flags.append(True)
            # The marked block runs on past this chunk unless a later line here opens a new one.
            carrying = not any(_BLOCK_START.match(line) for line in _lines_after_match(text, extra))
            continue
        lines = text.splitlines()
        if carrying and lines and not _BLOCK_START.match(lines[0]):
            flags.append(True)
            carrying = not any(_BLOCK_START.match(line) for line in lines[1:])
            continue
        flags.append(False)
        carrying = False
    return flags


def _lines_after_match(text: str, extra: Sequence[str]) -> List[str]:
    lines = text.splitlines()
    last = 0
    for i, line in enumerate(lines):
        if is_marked(line, extra):
            last = i
    return lines[last + 1 :]
