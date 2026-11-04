# `mempalace.convo_miner`

Source: [`mempalace/convo_miner.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/convo_miner.py)

convo_miner.py — Mine conversations into the palace.

Ingests chat exports (Claude Code, ChatGPT, Slack, plain text transcripts).
Normalizes format, chunks by exchange pair (Q+A = one unit), files to palace.

Same palace as project mining. Different ingest strategy.

## Functions

### `file_conversation_exchange`

```python
def file_conversation_exchange(collection, *, wing: str, room: str, text: str, source_file: str, agent: str, authored_at: Optional[str] = None, extra_metadata: Optional[dict] = None) -> Optional[str]
```

File one verbatim conversation exchange as a single drawer.

Canonical write path for live agent integrations (e.g. Hermes) and
their backfills — both must route here so routing, normalization,
and metadata conventions stay identical between live and historical
ingest. Builds the same metadata the convo miner writes so hallway
traversal, entity search, and since/before date filters see
integration drawers exactly like mined ones.

``wing`` and ``room`` are validated with the same ``sanitize_name``
rules the MCP write tools apply, but a failed name falls back
(``wing_general`` / ``conversations``) instead of erroring: this
path files *live* turns, and dropping a turn over a config typo
would break the verbatim / 100%-recall promise. The fallback is
logged at warning level so the misconfiguration is visible.

``extra_metadata`` lets callers append integration-specific fields
(e.g. ``source`` / ``session_id``); keys that collide with the
canonical fields are ignored, so it cannot be used to overwrite or
drop them. Returns the drawer id, or None when ``text`` is empty
after stripping.

### `size_cap_for`

```python
def size_cap_for(path) -> int
```

Largest file, in bytes, the convo miner will read from ``path``.

A ``.jsonl`` is streamed by ``normalize_conversations`` (#412), so it gets
the streaming bound instead of the whole-file cap; everything else keeps
MAX_FILE_SIZE. palace-daemon reads this to log, rather than silently
"mine", a transcript the miner would skip.

### `chunk_exchanges`

```python
def chunk_exchanges(content: str, chunk_size: int = None, min_chunk_size: int = None) -> list
```

Chunk by exchange pair: one > turn + AI response = one unit.
Falls back to paragraph chunking if no > markers.

Optional params override module-level defaults when provided.

Raises ``ValueError`` if ``chunk_size`` is not a positive integer or
``min_chunk_size`` is negative. A non-positive ``chunk_size`` would
cause ``_chunk_by_exchange`` below to loop forever — ``content[:0]``
is empty, ``content[0:]`` is the whole string, and the remainder
never shrinks.

### `detect_convo_room`

```python
def detect_convo_room(content: str) -> str
```

Score conversation content against the canonical room keyword rules.

Returns one of the canonical 7 rooms (or whatever the per-installation
config.yaml has registered). FK-safe: never returns a non-canonical
room provided the config and DB lookup are in sync — which they are
by default since both ship with the same seed set.

### `scan_convos`

```python
def scan_convos(convo_dir: str, include_subagents: bool = False) -> list
```

Find all potential conversation files.

Skips symlinks and oversized files. Each skipped symlink is logged to
``sys.stderr`` with a ``  SKIP: &lt;relative-path> (symlink)`` line so the
caller can tell why an apparent conversation directory yielded no files.

By default, directories named ``subagents`` are skipped: Claude Code
records Explore/Plan/Grep subagent transcripts there, and on typical
workspaces they outnumber main session files by one to two orders of
magnitude. Pass ``include_subagents=True`` to mine them anyway.

The match is case-insensitive on the directory name only (``subagents``
or ``Subagents``), so directories like ``mysubagents`` or
``subagentsbackup`` are not affected.

### `mine_sessions`

```python
def mine_sessions(convo_dir: str, palace_path: str, wing: str = None, agent: str = 'mempalace', limit: int = 0, dry_run: bool = False)
```

Mine a directory of session/transcript files into per-session
manifest drawers — ONE drawer per session, addressable by session_id.

Restores the addressable-session-anchor semantic that the hook's
legacy ``mempalace_diary_write`` call used to provide. The
unified-write-path refactor moved hook checkpointing to ``mine_convos``
which produces
N chunked drawers; this complements it with a single manifest
drawer per session that callers can grab as a navigation anchor
("did session X exist? show me a summary").

Content of each manifest drawer (no LLM call required — pure
structural extraction):

  Session manifest
  ─────────────────
  session_id:        &lt;UUID from filename stem>
  started_at:        &lt;first message timestamp>
  ended_at:          &lt;last message timestamp>
  exchanges:         &lt;count of user messages>
  first_user_msg:    &lt;first 400 chars of first user message>
  last_user_msg:     &lt;first 400 chars of last user message>
  cwd:               &lt;cwd field if present>

Drawer ID is stable: ``drawer_&lt;wing>_sessions_&lt;sha256(session_id)>``
so re-running on the same session overwrites the existing manifest
rather than duplicating.

Wing defaults to the convo_dir basename per the spec's normalization
rules. Room is hard-coded ``sessions`` (canonical).

### `mine_convos`

```python
def mine_convos(convo_dir: str, palace_path: str, wing: str = None, agent: str = 'mempalace', limit: int = 0, dry_run: bool = False, extract_mode: str = 'exchange', include_subagents: bool = False)
```

Mine a directory of conversation files into the palace.

extract_mode:
    "exchange" — default exchange-pair chunking (Q+A = one unit)
    "general"  — general extractor: decisions, preferences, milestones, problems, emotions
include_subagents:
    False (default) — skip Claude Code ``subagents/`` directories
    True            — also mine subagent transcripts

The real work is in :func:`_mine_convos_impl`; this wrapper holds the
per-palace flock around it so two concurrent ``mempalace mine --mode
convos`` invocations against the same palace can't pile up. This
mirrors the pattern in :func:`mempalace.miner.mine`. The lock is
non-blocking: ``MineAlreadyRunning`` propagates to the CLI (which
renders a holder-aware message and exits non-zero) or to in-process
callers that expect to coexist with another writer.

Dry-run skips the lock — it never writes to the palace and so cannot
corrupt anything, and skipping the lock lets dry-run probes coexist
with a live mine.

Chunking parameters (chunk_size, min_chunk_size) are read from
MempalaceConfig inside :func:`_mine_convos_impl` so `config.json`
governs both this path and the project-file miner in `miner.py`.
