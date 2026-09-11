# `mempalace.provenance`

Source: [`mempalace/provenance.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/provenance.py)

Source provenance and staleness for search hits.

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

## Functions

### `source_kind`

```python
def source_kind(hit) -> str
```

Classify where a hit's content came from.

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

### `source_stale`

```python
def source_stale(hit, now: Optional[datetime] = None) -> Optional[bool]
```

Has the hit's source file been modified since the palace indexed it?

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

### `provenance_note`

```python
def provenance_note(hit) -> Optional[str]
```

The human-readable caveat for one hit, or ``None`` when there is none.

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

### `annotate`

```python
def annotate(results)
```

Stamp ``source_kind`` (and staleness when decidable) onto each hit.

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

### `all_transcript`

```python
def all_transcript(results) -> bool
```

True when there are hits and every one of them is a transcript copy.

That is the shape the fleet keeps getting burned by: no curated document
matched at all, so nothing in the result set can carry a later correction.

### `no_curated_source`

```python
def no_curated_source(results) -> bool
```

True when there are hits and not one of them came from a curated document.

The weaker, more common sibling of :func:`all_transcript`: a palace diary
drawer is not a transcript, but nobody maintains it either, so a result
set of transcripts and diaries still has nowhere a later correction could
have landed. An unclassifiable hit makes this ``False`` — "nothing curated
matched" is a claim, and a hit of unknown shape is not evidence for it.
