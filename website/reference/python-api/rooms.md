# `mempalace.rooms`

Source: [`mempalace/rooms.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/rooms.py)

Closed room sets for a wing: propose with an LLM, assign with a fast decider.

The transcript miner files nearly everything into five keyword rooms
(``technical``, ``architecture``, ``planning``, ``problems``, ``general``), so
on a real palace the room layer carries no information (``mempalace audit``
scores it near zero). This module fixes that in two decoupled steps:

1. **propose** — sample drawers from a wing, hand the sample to the
   configured LLM (local by default: Ollama, vLLM, NInfer, any
   OpenAI-compatible endpoint; an external provider only when the user
   configured one explicitly) and get back a *closed* room set: a slug, a
   one-line description and a few keywords per room. The user edits and
   approves the file before anything moves.

2. **apply** — assign every drawer in the wing to one room of that set with a
   :class:`RoomDecider`. The default decider embeds each room's description
   with the palace embedder and picks the nearest room by cosine against the
   drawer's *stored* embedding, so no drawer is re-embedded and no LLM call
   is made per drawer. A drawer whose best similarity is below the threshold
   keeps its current room. Only ``room`` metadata changes; content never.

``RoomDecider`` is deliberately tiny — ``decide(rows) -> [(room, confidence)]``
— so a calibrated structured-decision model (a System One / Jev-style
endpoint served locally) can replace the embedding decider without touching
the pipeline: it returns the same ``(room, probability)`` pairs.

## Classes

### `class RoomSpec`

#### `prototype_text`

```python
def prototype_text(self) -> str
```

The text that is embedded to stand for this room.

### `class RoomSet`

#### `names`

```python
def names(self) -> list[str]
```

#### `to_dict`

```python
def to_dict(self) -> dict
```

#### `from_dict`

```python
def from_dict(cls, data: dict, *, normalize_names: bool = True) -> 'RoomSet'
```

### `class RoomDecider(Protocol)`

Assign a room to each row; ``confidence`` is in ``[0, 1]``.

``rows`` carry ``id``, ``metadata`` and, when the store has them,
``embedding`` and ``document``. The embedding decider uses the vector; a
structured-decision model would use the document. Either returns one
``(room_name, confidence)`` per row, in order, with ``room_name`` drawn
from the room set it was built with.

#### `decide`

```python
def decide(self, rows: list[dict]) -> list[tuple[str, float]]
```

### `class EmbeddingRoomDecider`

Nearest room prototype by cosine against the drawer's stored embedding.

A room's prototype is the centroid of its exemplar drawers' stored
embeddings when ``col`` is given and the room has exemplars (the drawers
the LLM assigned to it at propose time), so the decision happens in the
palace's own vector space. Rooms without exemplars fall back to the
embedding of their description, via ``embed`` (ChromaDB EF protocol,
``embed_query`` preferred because a description is a query).
Confidence is the best cosine similarity: the same scale search ranks on,
not a calibrated probability.

#### `__init__`

```python
def __init__(self, room_set: RoomSet, embed, col = None)
```

#### `decide`

```python
def decide(self, rows: list[dict]) -> list[tuple[str, float]]
```

### `class RoomPlan`

#### `example_excerpts`

```python
def example_excerpts(self, col, per_room: int = 3, chars: int = 160) -> dict[str, list[str]]
```

Fetch short excerpts for :attr:`examples` so a dry run can be judged.

#### `summary`

```python
def summary(self) -> dict
```

## Functions

### `slugify_room`

```python
def slugify_room(name: str) -> str
```

``Release Process`` → ``release-process``; the LLM does not always obey kebab-case.

Dots inside a name survive (``release-3.6.0``): ``sanitize_name`` allows
them, existing palaces use them for versions, and stripping them here
would undo :func:`snap_to_existing` on the next load and recreate the
very spelling drift it exists to prevent.

### `room_set_path`

```python
def room_set_path(config: MempalaceConfig, wing: str) -> str
```

``&lt;palace>/rooms/&lt;wing>.json`` — one approved room set per wing.

### `save_room_set`

```python
def save_room_set(config: MempalaceConfig, room_set: RoomSet) -> str
```

### `load_room_set`

```python
def load_room_set(config: MempalaceConfig, wing: str) -> RoomSet
```

### `sample_drawers`

```python
def sample_drawers(col, wing: str, n: int = DEFAULT_SAMPLE_SIZE, seed: int = 0) -> list[dict]
```

A reproducible random sample of ``n`` drawers, each with an excerpt.

Two passes: ids and rooms first (cheap), then documents only for the
sampled ids, so a 124k-drawer wing never loads its text to pick 60.

### `existing_rooms`

```python
def existing_rooms(col, wing: str) -> dict[str, int]
```

``&#123;room: drawers}`` for the wing, so proposals can reuse spellings.

### `snap_to_existing`

```python
def snap_to_existing(room_set: RoomSet, existing: Iterable[str]) -> list[tuple[str, str]]
```

Rename proposed rooms that collide with an existing room's spelling.

``release-3-6-0`` next to an existing ``release-3.6.0`` would recreate the
naming drift ``mempalace audit`` flags, so the existing spelling wins.
Returns the ``(proposed, existing)`` renames applied.

### `propose_rooms`

```python
def propose_rooms(wing: str, samples: list[dict], provider, max_rooms: int = DEFAULT_MAX_ROOMS, existing: Optional[Iterable[str]] = None) -> RoomSet
```

Ask the LLM for a closed room set over ``samples``, then for exemplars.

Two calls: the room set first, then one narrower call that labels each
excerpt with a room from that set (small local models drop the
assignments when both are asked for at once; a model that returns them
inline skips the second call). Raises ``ValueError`` on an unparsable or
empty answer and lets the provider's ``LLMError`` propagate; never falls
back to a generic set, because a generic set is the problem being fixed.

### `plan_rooms`

```python
def plan_rooms(col, wing: str, decider: RoomDecider, threshold: float = DEFAULT_THRESHOLD, progress = None, from_rooms: Optional[frozenset] = GENERIC_ROOMS) -> RoomPlan
```

Decide a room for every drawer in ``wing``; change nothing.

Only drawers whose current room is in ``from_rooms`` are candidates to
move; the rest are counted under their current room as ``kept``. Pass
``from_rooms=None`` to reclassify every drawer in the wing.

### `closet_targets`

```python
def closet_targets(plan: RoomPlan) -> tuple[dict, int]
```

``(&#123;(source_file, old_room): new_room}, ambiguous_drawers)`` for ``plan``.

A closet is one record per ``(wing, room, source_file)`` and search
passes the *same* wing/room filter to the closet collection, so after a
reclassification a closet left on the old room stops boosting the
drawers it indexes.

A closet follows its source only when *every* drawer of that source and
room moved, and to one room: it indexes the whole source, so moving it
while some of those drawers stayed put would take the boost away from
the ones that stayed. Sources that split, or only partly moved, are
counted in ``ambiguous`` and left alone; re-mining such a source
rebuilds its closets exactly.

### `plan_closet_moves`

```python
def plan_closet_moves(closets_col, wing: str, targets: dict) -> dict[str, list[str]]
```

Snapshot ``&#123;closet_id: [source_file, old_room, destination]}`` for one wing.

Resolved once against each closet's room at plan time. Chained plans
(``general→technical`` and ``technical→releases`` for one source) assign
each closet a single destination; a retry must not reinterpret a closet
already moved into an intermediate room as input to the next mapping.
Source and original room are retained so replay can refuse a closet that
changed ownership or left this wing between attempts.

### `resolve_closet_id_targets`

```python
def resolve_closet_id_targets(closets_col, wing: str, targets: dict) -> dict[str, str]
```

``&#123;closet_id: destination}`` view of :func:`plan_closet_moves`.

### `rekey_closets_by_ids`

```python
def rekey_closets_by_ids(closets_col, wing: str, moves: dict) -> int
```

Move closets by stable id within ``wing``; returns moved.

Idempotent: a closet already in its destination is skipped. Each move
must still belong to ``wing`` with the planned source file, and its room
must be the planned original or destination. Identities come from
:func:`plan_closet_moves` (or a pending apply marker), never from
reinterpreted current ``room`` metadata alone.

### `rekey_closets_to`

```python
def rekey_closets_to(closets_col, wing: str, targets: dict, *, moves: Optional[dict] = None) -> int
```

Move each matching closet to its target room; returns closets moved.

Resolves closet identities once, then moves by id so a retry cannot
treat an intermediate room as a fresh source mapping. Only ``room``
metadata is rewritten, never the record id.

### `rekey_closets`

```python
def rekey_closets(closets_col, plan: RoomPlan) -> dict
```

Follow the drawers with the AAAK index layer; ``&#123;"moved", "ambiguous"}``.

### `pending_apply_path`

```python
def pending_apply_path(config: MempalaceConfig, wing: str) -> str
```

### `apply_inputs`

```python
def apply_inputs(config: MempalaceConfig, wing: str, threshold: float, from_rooms: Optional[Iterable[str]]) -> dict
```

Everything besides the palace that decides a room apply's plan.

Recorded with the pending apply, so a retry that would plan differently
(another threshold, other source rooms, an edited room set) is refused
instead of finishing the first run's closet phase against a different
set of drawer moves.

### `save_pending_apply`

```python
def save_pending_apply(config: MempalaceConfig, wing: str, targets: dict, ambiguous: int, inputs: Optional[dict] = None, moves: Optional[dict] = None) -> str
```

### `load_pending_apply`

```python
def load_pending_apply(config: MempalaceConfig, wing: str) -> Optional[tuple[dict, int, Optional[dict], Optional[dict]]]
```

``(targets, ambiguous, inputs, moves)`` from an interrupted apply, or ``None``.

``inputs`` is ``None`` for a marker written before inputs were recorded.
``moves`` is ``None`` only when ``closet_moves`` was omitted (legacy marker);
an explicitly empty ``&#123;}`` means this operation owns no closet moves and must
not be recomputed. Malformed or inconsistent ``closet_moves`` raise before
any caller can write.

### `clear_pending_apply`

```python
def clear_pending_apply(config: MempalaceConfig, wing: str) -> None
```

### `apply_plan`

```python
def apply_plan(col, plan: RoomPlan, progress = None) -> int
```

Write the planned ``room`` changes in batches; returns rows updated.

Each batch is one backend write and a drawer is only ever wholly in its
old room or wholly in its new one. An interrupted apply leaves the
already-moved drawers classified and the rest where they were; running
``rooms apply`` again plans only over drawers still in ``from_rooms``,
so it finishes the remainder and a completed apply is a no-op. Only
``room`` metadata changes; content is never rewritten.
