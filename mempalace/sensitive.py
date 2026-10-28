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
the first names the file it came from. A continuation ends only at the next
tool call rendered at column 0 (``[Tool] …``); see ``_BLOCK_START``.

Markers are regular expressions: :data:`DEFAULT_MARKERS`, plus any listed
under ``sensitive_markers`` in the palace config. Site-specific names belong
in the config, not here. A config that cannot be read, or a pattern that does
not compile, raises :class:`SensitiveMarkersUnavailable` rather than classify
without it. A marker split across a chunk boundary is still found, and marks
both chunks, as long as it starts within the last 512 characters of the
earlier one: markers are short tokens, not spans of text.

Known limits. The boundaries are read from rendered text, so a private
file that itself holds a line shaped like a column-0 tool call ends its
block early. A file already mined and unchanged is not reclassified until it
changes or is re-mined.
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

# The only line that ends a sensitive block: a tool call exactly as normalize renders it, at
# column 0 ("[Bash] cmd", "[Read /path]", "[mcp__x__y] …", "[tool] …"). A ">" line does not end
# one, because tool output can hold quoted lines; a real user turn after a sensitive output is
# then held privately until the next tool call. Failing that way keeps the text private.
_BLOCK_START = re.compile(r"^\[(?:[A-Z][A-Za-z_]*|mcp__[\w-]+|tool)(?:\]|\s)")


# How much of the previous chunk is joined to the next when looking for a marker, so one split
# across a chunk boundary ("cat ~/pri" | "vate/x") is still found.
_BOUNDARY_OVERLAP = 512


class SensitiveMarkersUnavailable(RuntimeError):
    """The configured markers could not be read or compiled. Classifying without them could file
    text they mark into the shared collection, so the mine stops instead."""


@lru_cache(maxsize=8)
def _compiled(extra: Tuple[str, ...]) -> Tuple[re.Pattern, ...]:
    out = []
    for pattern in DEFAULT_MARKERS + extra:
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            raise SensitiveMarkersUnavailable(
                f"invalid sensitive marker {pattern!r}: {exc}"
            ) from exc
    return tuple(out)


def configured_markers() -> Tuple[str, ...]:
    """The palace config's ``sensitive_markers`` (a list of regular expressions). No config file
    means none; a config file that cannot be read or parsed raises, failing closed."""
    from .config import MempalaceConfig

    config = MempalaceConfig()
    if getattr(config, "_file_config_state", "read") in ("unread", "unparsed"):
        raise SensitiveMarkersUnavailable("palace config unreadable; sensitive_markers unknown")
    raw = config._file_config.get("sensitive_markers", [])
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        raise SensitiveMarkersUnavailable("sensitive_markers must be a list of non-empty strings")
    extra = tuple(raw)
    _compiled(extra)  # raise now on an invalid pattern
    return extra


def _matches(text: str, extra: Sequence[str]) -> List[Tuple[int, int]]:
    return [m.span() for p in _compiled(tuple(extra)) for m in p.finditer(text)]


def is_marked(text: str, extra: Sequence[str] = ()) -> bool:
    return bool(_matches(text, extra))


def sensitive_chunk_flags(contents: Iterable[str], extra: Sequence[str] = ()) -> List[bool]:
    """One flag per chunk, in order: True for a marked chunk and for the chunks that continue it."""
    flags: List[bool] = []
    carrying = False
    previous = ""
    for text in contents:
        tail = previous[-_BOUNDARY_OVERLAP:]
        joined = tail + text
        # A match ending inside the previous chunk was counted there; one ending in this chunk
        # marks it, and one begun in the previous chunk marks that one too.
        spans = [(start, end) for start, end in _matches(joined, extra) if end > len(tail)]
        if spans:
            if flags and min(start for start, _end in spans) < len(tail):
                flags[-1] = True
            end = max(end for _start, end in spans)
            flags.append(True)
            # The marked block runs on past this chunk unless a tool call opens a new one after it.
            after = joined[end:].split("\n")[1:]
            carrying = not any(_BLOCK_START.match(line) for line in after)
        else:
            lines = text.split("\n")
            if carrying and not _BLOCK_START.match(lines[0]):
                flags.append(True)
                carrying = not any(_BLOCK_START.match(line) for line in lines[1:])
            else:
                flags.append(False)
                carrying = False
        previous = text
    return flags
