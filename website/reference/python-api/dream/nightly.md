# `mempalace.dream.nightly`

Source: [`mempalace/dream/nightly.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/dream/nightly.py)

The nightly dream: inventory and contradictions in one run, published for review.

The run writes one JSON document (``schema: dreams/v1``) to a review folder, as
``YYYY-MM-DD.json`` and ``latest.json``, each written atomically. A review page reads it and
records each decision in ``decisions.json`` in the same folder (``&#123;id: &#123;"decision": "accept" |
"reject", ...}}``). Every proposal has a stable id, so the same finding keeps its id from night
to night even when the lines around it move. A decided id is never proposed again: a rejected
one is settled, and an accepted one is applied by the next run when that is mechanical (see
:func:`apply_decisions`), recorded in ``applied`` and the ``applied.json`` ledger; otherwise it is
listed under ``run.accepted_waiting`` with the reason, for a lane.

The inputs are private, and so is the document: point ``--publish-dir`` at a folder that is
never in a repository. The host's memory is checked before every model call; under the floor
the contradictions pass stops and says so.

## Classes

### `class DecisionsMissing(RuntimeError)`

The decisions store is gone after a first run: publishing would re-propose rejections.

### `class TooLarge(ValueError)`

The document is over the page's limit even with no proposals left.

## Functions

### `proposal_id`

```python
def proposal_id(p: inventory.Proposal) -> str
```

Stable across nights: kind, the source paths without line numbers, and the rule lead or
memory names that say what the finding is about.

### `load_decisions`

```python
def load_decisions(path: Path, first_run: bool) -> Dict[str, dict]
```

The review page's decisions. On a first run (no ledger yet) a missing store is created
empty, once, exclusively; after that a missing, unreadable or malformed one is an error, so
a deleted or half-written file can never bring a rejection back.

### `memory_guard`

```python
def memory_guard(min_available_mb: int = MIN_AVAILABLE_MB, meminfo: str = '/proc/meminfo')
```

A guard for the model pass: a reason to stop when MemAvailable is under the floor.

### `published_proposals`

```python
def published_proposals(publish_dir: Path) -> Dict[str, dict]
```

Every proposal the folder's published nights still show, by id (newest night wins), so
a decision on a proposal that no longer recurs can still be applied.

### `apply_decisions`

```python
def apply_decisions(decisions: Dict[str, dict], applied: Sequence[dict], known: Dict[str, dict], now: Optional[str] = None)
```

(the applied ledger with tonight's additions, the accepted ids still waiting). Only what
needs no judgement is applied here: a near-duplicate whose two sources are one file read
through a symlinked folder (nothing to merge; the inventory now reads each folder once).
Every other accepted id waits, with its reason, for a lane; a rejected id needs nothing.

### `run`

```python
def run(memory_root: Path, claude_md: Sequence[Path], embed: inventory.Embedder, judge: Optional[contradictions.Judge], decisions: Dict[str, dict], guard: Optional[Callable[[], Optional[str]]] = None, day: Optional[str] = None, seen: Optional[Dict[str, str]] = None, applied: Optional[List[dict]] = None, global_dirs: Sequence[Path] = (), known: Optional[Dict[str, dict]] = None)
```

(the night's document, the updated first-seen ledger). ``judge`` None skips the model
pass (recorded in ``run``). ``seen`` maps each id to the night it first appeared.

### `fit`

```python
def fit(proposal: dict) -> dict
```

One proposal inside the page's limits: summary cut to its length, at most ten sources,
a flat detail of at most twelve string, number or boolean values.

### `within_size`

```python
def within_size(doc: dict) -> dict
```

Drop the lowest-priority proposals (they are sorted) until the final document, its
``run.truncated`` count included, fits; refuse when the rest alone is over the limit.

### `single_writer`

```python
def single_writer(publish_dir: Path)
```

An exclusive, non-blocking lock on ``&lt;publish_dir>/.nightly.lock``, held while the
returned file object stays open (the process's life); None when another run holds it.

### `private_dir`

```python
def private_dir(path: Path) -> Path
```

``path`` as a 0700 folder (created if needed) before anything private goes into it.

### `check_day`

```python
def check_day(day: str) -> str
```

A canonical YYYY-MM-DD, so a day can only ever name a file directly in the folder.

### `read_json`

```python
def read_json(path: Path, default)
```

A ledger this writer or the lane keeps; ``default`` when it does not exist yet.

### `publish`

```python
def publish(doc: dict, publish_dir: Path, seen: Optional[Dict[str, str]] = None) -> Path
```

Write ``&lt;day>.json`` and ``latest.json`` (0640) atomically into ``publish_dir`` (0700),
save the first-seen ledger, and prune day files older than :data:`KEEP_DAYS`.

### `prune`

```python
def prune(publish_dir: Path, today: str, keep_days: int = KEEP_DAYS) -> List[Path]
```

Remove ``YYYY-MM-DD.json`` files more than ``keep_days`` before ``today``; nothing else.
