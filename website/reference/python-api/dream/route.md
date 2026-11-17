# `mempalace.dream.route`

Source: [`mempalace/dream/route.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/dream/route.py)

Route each dream proposal that names a project to that project's inbox, one line each.

A proposal names a project through its sources: a memory folder under the memory root whose
name is a project folder's path with every character but letters and digits turned into ``-``
(how the harness names them), or a path inside a project folder under a projects root. A
project folder that is a symlink counts as its real folder, and only a folder owned by this
user is ever a destination. A memory-folder name two different folders encode to is ambiguous
and names neither. A proposal whose sources name no project (the global CLAUDE.md) is left on
the review page.

Each project gets ``scratch/dream-inbox.md``, appended to, one line per proposal::

    - [ ] &lt;lane> · &lt;proposal id> · &lt;one-sentence ask> (dream &lt;day>, &lt;kind>)

``lane`` is the project's lane from an optional map, else the project folder's name. A folder
is written only when it is outside every git work tree or git ignores the inbox path. The
write goes through descriptors opened without following links (the project, ``scratch/``,
the inbox), and the inbox must then be a regular file with one link, owned by this user; it
is kept at mode 0600. A proposal id already in the inbox is never appended again, so a run
that stops between the append and the ledger save repeats nothing.

The routing is recorded in the verdict ledger (``judged.json``) under :data:`KEY`: for each
routed id, its kind, where it went and whether it has been acted on, which it has once it is
decided on the review page, or once a night whose producer for its kind ran complete no
longer proposes it (a proposal that comes back is open again).

## Classes

### `class Unsafe(Exception)`

The inbox's target is not one this writer will write through.

## Functions

### `project_of`

```python
def project_of(source: str, roots: Sequence[Path], _idx: Optional[Dict] = None) -> Optional[Path]
```

The real project folder a source names, or None.

### `ask`

```python
def ask(proposal: Mapping) -> str
```

The proposal's first sentence, on one line, at most :data:`MAX_ASK` characters.

### `route`

```python
def route(proposals: Sequence[Mapping], roots: Sequence[Path], ledger: Dict[str, dict], day: str, lanes: Optional[Mapping[str, str]] = None, decisions: Optional[Mapping[str, Mapping]] = None, complete_kinds: Optional[Collection[str]] = None) -> dict
```

Route tonight's proposals and update the record in ``ledger`` (in place). Only kinds in
``complete_kinds`` (default: every kind seen tonight) can be marked "no longer proposed".
Returns the figures: routed and acted (all nights), new (tonight), open, unrouted, skipped.
Never raises for one project: its reason goes to ``skipped`` and the rest go on.

### `summary`

```python
def summary(ledger: Mapping) -> str
```

The lead's one line: ``dreams: N proposals routed, M acted``.
