"""Source provenance and staleness for search hits.

A palace search returns the *indexed copy* of whatever was mined. Transcripts
are mined continuously (the Stop / PreCompact hooks) while curated project
documents are mined only when someone runs ``mempalace mine`` — so the copy
that comes back for a project fact is usually a session transcript quoting a
claim, not the document that later corrected it. A refuted claim then reads as
authoritative, because nothing on the hit says which kind of source it came
from (techempower-org/mempalace#451: a claim ``2g/CLAUDE.md`` had carried a
REFUTED banner for two days was still being returned, unmarked, from the
transcript that first stated it).

This module is the shared, side-effect-free predicate set for that question:

* :func:`source_kind`  — transcript / memory / diary / file / unknown
* :func:`source_stale` — has the file on disk moved on since it was indexed?
* :func:`annotate`     — stamp both onto a result list, in place
* :func:`provenance_note` — the human-readable caveat for one hit
* :func:`all_transcript`  — "nothing curated matched" for the header line

Only :func:`source_stale` touches the filesystem (one ``os.stat``); everything
else is pure. Nothing here raises: a hit of an unexpected shape degrades to
``"unknown"`` / ``None`` rather than breaking a search the caller already paid
for.
"""

import os
from datetime import datetime
from typing import Optional

from .date_window import parse_date_bound

__all__ = [
    "all_transcript",
    "annotate",
    "no_curated_source",
    "provenance_note",
    "source_kind",
    "source_stale",
]

# Keys the search arms use for a source path, in preference order. ``searcher``
# writes the basename to ``source_file`` for display and keeps the full path in
# ``source_path`` (only ``_source_file_full`` / ``_chunk_index`` get stripped
# before a public return), so most hits carry BOTH — the basename first and an
# absolute path right behind it. ``_bm25_only_via_postgres`` is the exception:
# it sets no ``source_path``, so its hits really are basename-only.
# Any of these keys answers "what kind of source is this?"; only an absolute
# one can answer "is it stale?", which is why staleness resolves for most hits
# and not for that one branch.
_PATH_KEYS = ("source_file", "source_path", "_source_file_full", "source")

# Keys carrying the time the drawer was filed into the palace.
_INDEXED_AT_KEYS = ("created_at", "indexed_at", "filed_at")

# ``searcher`` writes "?" when a drawer has no source_file at all.
_EMPTY_PATHS = ("", "?", "none", "null", "unknown")

# A file touched within a minute of its own mine is the mine, not an edit.
_STALE_GRACE_SECONDS = 60

_TRANSCRIPT_NOTE = "quoted copy from a session transcript — verify at the curated source"


def _paths(hit) -> list:
    """Every non-empty source path on ``hit``, in key-preference order."""
    if not isinstance(hit, dict):
        return []
    out = []
    for key in _PATH_KEYS:
        value = hit.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or value.lower() in _EMPTY_PATHS:
            continue
        out.append(value)
    return out


def _drawer_id(hit) -> str:
    if not isinstance(hit, dict):
        return ""
    return str(hit.get("drawer_id") or hit.get("id") or "")


def source_kind(hit) -> str:
    """Classify where a hit's content came from.

    Returns one of:

    ``"transcript"``
        A session transcript (``*.jsonl``). The words are a *quoted copy* —
        true of the moment it was said, not necessarily true now.
    ``"memory"``
        A curated auto-memory file (``.../memory/*.md``): one fact per file,
        maintained by hand. The highest-trust shape in the palace.
    ``"diary"``
        A palace-written diary drawer (no source file; ``diary_``-prefixed id).
    ``"file"``
        Any other mined file — a project ``CLAUDE.md``, a findings doc, source.
    ``"unknown"``
        Nothing on the hit says.

    Basename-only paths are fine (the MCP path strips directories), and the
    extension match is case-insensitive.
    """
    paths = _paths(hit)
    for path in paths:
        lowered = path.lower()
        if lowered.endswith(".jsonl"):
            return "transcript"
        if lowered.endswith(".md") and "/memory/" in lowered:
            return "memory"
    if paths:
        return "file"
    if _drawer_id(hit).startswith("diary_"):
        return "diary"
    return "unknown"


def _indexed_at_raw(hit) -> Optional[str]:
    if not isinstance(hit, dict):
        return None
    for key in _INDEXED_AT_KEYS:
        value = hit.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _indexed_at(hit) -> Optional[datetime]:
    """Parsed index timestamp, or ``None`` when absent/unparseable.

    Wall-clock naive, matching ``filed_at`` storage and the shared
    ``date_window`` comparison convention.

    ``parse_date_bound`` dropping any ``tzinfo`` is LOAD-BEARING here, not
    incidental tidiness. Production ``filed_at`` values are naive local ISO
    strings, but ``diary_ingest`` writes an aware (UTC) one, and both
    comparisons downstream are against naive datetimes —
    ``datetime.fromtimestamp(mtime)`` and ``datetime.now()``. An aware value
    reaching either one raises ``TypeError: can't compare offset-naive and
    offset-aware datetimes`` from inside a search the caller has already paid
    for. Normalising on the way in is what lets :func:`source_stale` promise
    it never raises; keep any replacement parser naive-returning.
    """
    raw = _indexed_at_raw(hit)
    if raw is None:
        return None
    try:
        return parse_date_bound(raw, field_name="created_at")
    except (ValueError, TypeError):
        return None


def source_stale(hit, now: Optional[datetime] = None) -> Optional[bool]:
    """Has the hit's source file been modified since the palace indexed it?

    ``True``  — the file exists locally and its mtime is more than
    :data:`_STALE_GRACE_SECONDS` past the drawer's index timestamp, so the
    palace is serving an older copy than the file on disk.

    ``False`` — the file exists and has not moved on.

    ``None``  — undecidable, and *not* a denial of staleness. It covers a hit
    whose only path is a bare basename (``_bm25_only_via_postgres`` is the one
    arm that returns those), a missing or unparseable index timestamp, a file
    that is not on this machine, and an index timestamp in the future (a clock
    problem, not a staleness answer).

    WHOSE filesystem answers is deliberate: whichever host runs this. Under
    MCP ``mempalace_search`` that is the palace host, and its copy is the
    right one to compare — it is the copy the miner actually read, so its
    mtime is what "has the source moved on since indexing?" means. The CLI
    then re-annotates the same hits locally, and because :func:`annotate`
    only writes ``source_stale`` when it can decide, the answer you get is
    the last host that *could* decide (the reader's, when the file is present
    on both; the palace host's, when it is present only there). Where the
    file is Syncthing-replicated the two agree, because mtime is preserved.
    A path missing on both yields ``None``.

    ``now`` is injectable for tests and bounds the future-timestamp check; it
    must be naive (see :func:`_indexed_at`). Never raises.
    """
    indexed = _indexed_at(hit)
    if indexed is None:
        return None
    now = now or datetime.now()
    if indexed > now:
        # Bad clock or a mis-parse — nothing honest to say about staleness.
        return None
    for path in _paths(hit):
        if not os.path.isabs(path):
            continue
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        modified = datetime.fromtimestamp(mtime)
        return (modified - indexed).total_seconds() > _STALE_GRACE_SECONDS
    return None


def provenance_note(hit) -> Optional[str]:
    """The human-readable caveat for one hit, or ``None`` when there is none.

    Reuses ``source_kind`` / ``source_stale`` already stamped by
    :func:`annotate` when they are present, so a renderer handed an annotated
    hit never re-stats the disk.

    The staleness caveat is deliberately NOT rendered for transcripts: a live
    session's transcript is appended to continuously, so it is always
    "modified after indexing" and the note would fire on every open session
    while telling the reader nothing the transcript caveat does not already
    say. Staleness is the interesting signal for curated documents, which is
    the case #451 was filed about. The ``source_stale`` FIELD is still stamped
    on every kind by :func:`annotate` — this is a rendering rule, not a
    measurement one.
    """
    if not isinstance(hit, dict):
        return None
    kind = hit.get("source_kind") or source_kind(hit)
    stale = hit.get("source_stale")
    if stale is None:
        stale = source_stale(hit)

    notes = []
    if kind == "transcript":
        notes.append(_TRANSCRIPT_NOTE)
    if stale is True and kind != "transcript":
        indexed = _indexed_at(hit)
        when = indexed.strftime("%Y-%m-%d") if indexed else "unknown date"
        notes.append(
            f"source file modified after indexing (indexed {when}) — re-mine or read the file"
        )
    return "; ".join(notes) if notes else None


def annotate(results):
    """Stamp ``source_kind`` (and staleness when decidable) onto each hit.

    Mutates dicts in ``results`` in place and returns ``results`` itself, so
    callers can wrap an existing expression. Idempotent — re-annotating an
    already-annotated list produces the same fields. Non-dict items and a
    non-list ``results`` pass through untouched, because this runs on the
    return path of a search the caller has already paid for and must never
    turn a usable result into an exception.

    ``source_stale`` and ``source_indexed_at`` are omitted when there is
    nothing to base them on: ``source_stale`` needs an absolute path that
    exists on the host running this call, plus a parseable index timestamp.
    Most hits carry an absolute ``source_path`` alongside the display
    basename, so staleness usually DOES resolve — including inside the
    palace daemon, against the palace host's filesystem (see
    :func:`source_stale` for why that is the right copy). Because the field
    is written only when decidable, re-annotating on a second host refines
    the answer rather than clobbering it with ``None``.
    ``source_indexed_at`` is the parseable index timestamp echoed back.
    """
    if not isinstance(results, list):
        return results
    for hit in results:
        if not isinstance(hit, dict):
            continue
        hit["source_kind"] = source_kind(hit)
        stale = source_stale(hit)
        if stale is not None:
            hit["source_stale"] = stale
        indexed_raw = _indexed_at_raw(hit)
        if indexed_raw is not None and _indexed_at(hit) is not None:
            hit["source_indexed_at"] = indexed_raw
    return results


def all_transcript(results) -> bool:
    """True when there are hits and every one of them is a transcript copy.

    That is the shape the fleet keeps getting burned by: no curated document
    matched at all, so nothing in the result set can carry a later correction.
    """
    if not isinstance(results, list) or not results:
        return False
    return all(source_kind(hit) == "transcript" for hit in results)


def no_curated_source(results) -> bool:
    """True when there are hits and not one of them came from a curated document.

    The weaker, more common sibling of :func:`all_transcript`: a palace diary
    drawer is not a transcript, but nobody maintains it either, so a result
    set of transcripts and diaries still has nowhere a later correction could
    have landed. An unclassifiable hit makes this ``False`` — "nothing curated
    matched" is a claim, and a hit of unknown shape is not evidence for it.
    """
    if not isinstance(results, list) or not results:
        return False
    return all(source_kind(hit) in ("transcript", "diary") for hit in results)
