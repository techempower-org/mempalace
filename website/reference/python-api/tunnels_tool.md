# `mempalace.tunnels_tool`

Source: [`mempalace/tunnels_tool.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/tunnels_tool.py)

Propose and prune cross-wing tunnels from the hallway graph.

The miner drops an entity tunnel for every entity that has hallways in two
wings. Before the audit repair session that meant tunnels on ``content`` and
``thinking`` and four copies of one link under different spellings, and
nothing to link wings that share no symbol. This module gives the user a
reviewable list instead:

* **propose** — rank shared entities by the weaker side of the link (the
  same rule the miner now uses), drop generic tokens, weak links and links
  that already exist, give every wing without a tunnel its strongest link
  before filling the rest by strength, and write
  ``&lt;palace>/tunnels/proposal.json``; ``apply`` creates the approved rows
  through ``create_tunnel`` so they dedupe with everything else.
* **prune** — remove tunnels the audit counts as artifacts: generic tokens,
  endpoints whose wing no longer exists, and duplicate spellings of one
  link between the same two wings (the strongest survives).

## Classes

### `class LinkIndex`

The links seen so far, matched the way a reader would.

Two tunnels are one link when they join the same two wings through the
same endpoints, whichever direction they were written and whichever
spelling they use (``entity:main.zig`` / ``entity:src/main.zig``). Two
files that only share a basename (``src/models/user.py`` and
``tests/fixtures/user.py``) are two links: deduping them as one would
delete a real connection.

``ambiguous`` lists entity spellings that could name more than one file
in the full endpoint set. Those only match an identical spelling, never
a qualified path, so an ambiguous bare alias cannot erase both distinct
links.

#### `__init__`

```python
def __init__(self, ambiguous: Optional[Iterable[str]] = None) -> None
```

#### `contains`

```python
def contains(self, ends: tuple) -> bool
```

#### `add`

```python
def add(self, ends: tuple) -> None
```

## Functions

### `room_spelling_key`

```python
def room_spelling_key(room: str) -> str
```

Bucket key for a tunnel endpoint room: ``entity:src/main.zig`` and
``entity:main.zig`` share one.

``entity_spelling_key`` takes a basename, so the ``entity:`` prefix must
come off first or a path spelling (``entity:src/main.zig`` → ``main``)
and a bare one (``entity:main.zig`` → ``entity:main``) never match. The
key is deliberately lossy — ``src/models/user.py`` and
``tests/fixtures/user.py`` share it too — so it only narrows the search;
:class:`LinkIndex` decides with :func:`same_file_spelling`.

### `tunnel_endpoints`

```python
def tunnel_endpoints(tunnel: dict) -> Optional[tuple]
```

``((wing, room), (wing, room))`` with wings normalized, or ``None``.

### `ambiguous_entity_spellings`

```python
def ambiguous_entity_spellings(spellings: Iterable[str]) -> set[str]
```

Spellings that could name two or more distinct files in ``spellings``.

Pairwise suffix matching is not transitive: bare ``CodeRouter.py`` matches
both ``src/models/CodeRouter.py`` and ``tests/fixtures/CodeRouter.py``,
while those two do not match each other. Treating the bare name as a
duplicate of either would delete a real connection. A spelling is
ambiguous when it ``same_file_spelling``-matches two hosts that are not
the same file as each other.

### `connection_spellings`

```python
def connection_spellings(tunnels: list, extra_entities: Iterable[str] = ()) -> list[str]
```

Spellings from complete endpoints, plus the full proposal candidate set.

### `connection_index`

```python
def connection_index(tunnels: list, extra_entities: Iterable[str] = (), *, seed: bool = True) -> LinkIndex
```

Build spelling context before deciding whether any endpoint is an alias.

### `propose_tunnels`

```python
def propose_tunnels(hallways: list, existing_wings: Iterable[str], max_tunnels: int = DEFAULT_MAX_TUNNELS, min_count: Optional[int] = None, existing_tunnels: Optional[list] = None) -> dict
```

Rank candidate cross-wing links, coverage first, then strength, capped.

Links that already exist as tunnels are left out, so re-running after
``--yes`` proposes only what is still missing. Every wing that has no
proposed link yet gets its strongest candidate before the remaining
slots go to the strongest links overall; the audit's coverage measure
is the share of linkable wings a tunnel reaches, and this ordering
raises it fastest.

### `proposal_path`

```python
def proposal_path(config: MempalaceConfig) -> str
```

### `save_proposal`

```python
def save_proposal(config: MempalaceConfig, plan: dict) -> str
```

### `load_proposal`

```python
def load_proposal(config: MempalaceConfig) -> dict
```

### `apply_proposal`

```python
def apply_proposal(plan: dict, config: Optional[MempalaceConfig] = None) -> int
```

Create the plan's tunnels; returns how many were created.

The plan sat under review, so the tunnel file is read again here: a row
whose link now exists under another spelling (``entity:main.py`` added
while the plan says ``entity:src/main.py``) is skipped, as is a row that
repeats an earlier row. ``create_tunnel`` dedupes only identical endpoint
ids and would write both spellings.

### `prune_tunnels`

```python
def prune_tunnels(tunnels: list, existing_wings: Iterable[str]) -> tuple[list, dict]
```

``(kept, report)`` — drop generic, dangling and duplicate-spelling tunnels.

Wings compare through ``normalize_wing_name`` on both sides, the same
way ``follow_tunnels`` resolves them, so a tunnel that still resolves
is never counted as dangling.
