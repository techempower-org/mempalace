# `mempalace.sensitive`

Source: [`mempalace/sensitive.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/sensitive.py)

Which conversation chunks are sensitive, and so go to the private collection.

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
without it. A marker split across a chunk boundary is still found.

Known limits. The boundaries are read from rendered text, so a private
file that itself holds a line shaped like a column-0 tool call ends its
block early. A file already mined and unchanged is not reclassified until it
changes or is re-mined.

## Classes

### `class SensitiveMarkersUnavailable(RuntimeError)`

The configured markers could not be read or compiled. Classifying without them could file
text they mark into the shared collection, so the mine stops instead.

## Functions

### `configured_markers`

```python
def configured_markers() -> Tuple[str, ...]
```

The palace config's ``sensitive_markers`` (a list of regular expressions). No config file
means none; a config file that cannot be read or parsed raises, failing closed.

### `is_marked`

```python
def is_marked(text: str, extra: Sequence[str] = ()) -> bool
```

### `sensitive_chunk_flags`

```python
def sensitive_chunk_flags(contents: Iterable[str], extra: Sequence[str] = ()) -> List[bool]
```

One flag per chunk, in order: True for a marked chunk and for the chunks that continue it.
