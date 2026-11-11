# `mempalace.dream.inventory`

Source: [`mempalace/dream/inventory.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/dream/inventory.py)

Dream day 1: an inventory that needs no model.

Three checks, each read-only, each emitting proposals for a reviewer:

* ``oversized-index``: a memory folder's ``MEMORY.md`` over its line or byte cap, which the
  harness truncates when it loads the index (200 lines / 25 KB by default).
* ``no-mechanism``: a standing rule in a ``CLAUDE.md`` that names no mechanism (a hook, a gate,
  a test, a timer, a generated page) and carries no dated "none yet" line. A rule that does
  nothing is prose, and prose is what lets a rule die in a transcript.
* ``near-duplicate``: two feedback memories whose embeddings are this close, by the palace's own
  embedder (the daemon's ``/embed``), so a reviewer can merge them.

Nothing here writes to an input. The report goes to ``--out`` as ``YYYY-MM-DD.json`` and
``YYYY-MM-DD.md``. Inputs may be private (memory folders, CLAUDE.md files): keep ``--out``
somewhere private too.

    python -m mempalace.dream inventory --memory-root ~/.claude/projects \
        --claude-md ~/.claude/CLAUDE.md --out &lt;private dir>

## Classes

### `class Memory`

### `class Proposal`

#### `as_dict`

```python
def as_dict(self) -> Dict[str, object]
```

## Functions

### `parse_memory`

```python
def parse_memory(path: Path) -> Optional[Memory]
```

A memory file's frontmatter and body; None when it has no frontmatter. ``type`` is read at
the top level or under ``metadata:``, the two shapes the harness has written.

### `memory_folders`

```python
def memory_folders(root: Path) -> List[Path]
```

Every ``&lt;project>/memory`` folder one level under ``root`` (no deeper walk).

### `read_memories`

```python
def read_memories(root: Path) -> List[Memory]
```

### `oversized_indexes`

```python
def oversized_indexes(root: Path, max_lines: int = INDEX_MAX_LINES, max_bytes: int = INDEX_MAX_BYTES) -> List[Proposal]
```

### `is_rule`

```python
def is_rule(block: str) -> bool
```

### `discover_claude_md`

```python
def discover_claude_md(projects_root: Path) -> List[Path]
```

``&lt;projects_root>/*/CLAUDE.md``, one level down, skipping git worktrees (their ``.git`` is
a file): a worktree's CLAUDE.md is a copy of its repo's, and would repeat every proposal.

### `rule_blocks`

```python
def rule_blocks(text: str) -> Iterable[tuple]
```

(line number, lead, block text) for each top-level bold bullet: the bullet and its
continuation lines, up to the next top-level bullet, heading or blank-line-then-text.

### `unmechanised_rules`

```python
def unmechanised_rules(paths: Sequence[Path]) -> List[Proposal]
```

### `near_duplicates`

```python
def near_duplicates(memories: Sequence[Memory], embed: Embedder, threshold: float = NEAR_DUPLICATE) -> List[Proposal]
```

Pairs of feedback memories whose cosine similarity is at least ``threshold``.

### `daemon_embedder`

```python
def daemon_embedder(url: Optional[str] = None, api_key: Optional[str] = None) -> Embedder
```

The palace's own embedder through palace-daemon ``POST /embed`` (no model loaded here).
``PALACE_DAEMON_URL`` and ``PALACE_API_KEY`` from the environment by default.

### `run`

```python
def run(memory_root: Path, claude_md: Sequence[Path], embed: Optional[Embedder] = None, threshold: float = NEAR_DUPLICATE) -> List[Proposal]
```

### `render_markdown`

```python
def render_markdown(proposals: Sequence[Proposal], day: str) -> str
```

### `write_report`

```python
def write_report(proposals: Sequence[Proposal], out_dir: Path, day: Optional[str] = None)
```
