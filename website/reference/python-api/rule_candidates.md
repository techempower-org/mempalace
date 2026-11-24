# `mempalace.rule_candidates`

Source: [`mempalace/rule_candidates.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/rule_candidates.py)

Rule candidates: user lines that read as standing rules, listed for review (#584).

A rule said once in a session ("we should use the good dragon voices") is in
the palace verbatim, but nobody searches for it until it has already been
broken. At mining time the convo miner hands every newly filed chunk, and the
user's messages as :func:`mempalace.normalize.collect_user_messages` gathered
them, to :func:`flag_chunks`; each line of a user message that reads like a
standing rule is appended, verbatim and with the id of the drawer holding it,
to a per-day review list ``rule-candidates-YYYY-MM-DD.md``. A reviewer turns
each one into a memory plus a mechanism, or ticks it off. ``mempalace rules
--since YYYY-MM-DD`` prints the lists.

The check runs on the messages, not on the rendered chunk text, because the
transcript marks only the first line of a user message with ``>``: a rule on
its third line sits next to the assistant's reply with nothing to tell them
apart, and an assistant blockquote starts with ``>`` without being the
user's. A format with no role record (a plain ``>``-quoted text file) falls
back to the ``>`` lines of the chunks.

Nothing here touches the palace: the list is a local markdown file, and any
failure is logged and swallowed so it can never fail a mine.

Environment:

* ``MEMPALACE_RULE_CANDIDATES=0`` turns flagging off.
* ``MEMPALACE_RULE_CANDIDATES_DIR`` is where the lists go
  (default ``~/.mempalace/rule_candidates``).
* ``MEMPALACE_RULE_MEMORY_GLOB`` is where existing memories live
  (default ``~/.claude/projects/*/memory/*.md``); a candidate whose words
  cover an existing memory's ``name:`` slug is dropped as already a rule.

## Functions

### `enabled`

```python
def enabled() -> bool
```

### `candidates_dir`

```python
def candidates_dir() -> Path
```

### `candidate_lines`

```python
def candidate_lines(content: str) -> list
```

Return the ``>`` lines of ``content`` that read as a standing rule, verbatim.

The fallback for a transcript with no role record. Only the first line
of a user turn carries the ``>`` marker, so a rule on a later line of a
multi-line message is missed here, and an assistant blockquote is taken
for the user's. When the user's messages are known, :func:`rule_lines`
checks every line of them instead.

### `rule_lines`

```python
def rule_lines(user_messages: Iterable[str]) -> list
```

Return every line of the user's messages that reads as a standing rule.

Each line is returned as it stands in the transcript: the first line of a
message carries the ``> `` marker, the later lines are bare.

### `memory_slugs`

```python
def memory_slugs(pattern: Optional[str] = None) -> list
```

The ``name:`` slug of every memory file matching ``pattern``.

### `matching_slug`

```python
def matching_slug(line: str, slugs: Iterable[str]) -> Optional[str]
```

The first slug whose words (two or more) all appear, fuzzily, in ``line``.

### `flag_chunks`

```python
def flag_chunks(items, wing: str, source_file: str, today: Optional[date] = None, user_messages: Optional[Iterable[str]] = None) -> int
```

Append rule candidates from ``items`` (``(chunk, room, drawer_id)``) to today's list.

``user_messages`` is what :func:`mempalace.normalize.collect_user_messages`
gathered while the file was normalized. With it, every line of every user
message is checked and an assistant blockquote is never taken for the
user's; a candidate is listed only when one of ``items`` holds it, so an
unchanged drawer, or a sensitive chunk planned out of the pass, is never
listed. Empty or ``None`` (a format with no role record) falls back to
the ``>`` lines of the chunks.

Returns the number of entries written. Never raises.

### `list_files`

```python
def list_files(since: Optional[date] = None) -> list
```

Per-day list files dated on or after ``since``, oldest first.

### `run_list`

```python
def run_list(since: Optional[str]) -> int
```

CLI: print every rule-candidate list dated on or after ``since`` (YYYY-MM-DD).
