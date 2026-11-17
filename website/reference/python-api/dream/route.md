# `mempalace.dream.route`

Source: [`mempalace/dream/route.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/dream/route.py)

Route each dream proposal that names a project to that project's inbox, one line each.

A proposal names a project through its sources: a memory folder under the memory root whose
name is a project folder's path with every character but letters and digits turned into ``-``
(how the harness names them), or a path inside a project folder under a projects root. A
proposal whose sources name no project (the global CLAUDE.md) is left on the review page.

Each project gets ``scratch/dream-inbox.md``, appended to, one line per proposal::

    - [ ] &lt;lane> · &lt;proposal id> · &lt;one-sentence ask> (dream &lt;day>, &lt;kind>)

``lane`` is the project's lane from an optional map, else the project folder's name. A folder
is written only when it is not a git repository or its ``scratch/`` is git-ignored, and the
inbox must be a regular file (a symlink is never followed). The routing is recorded in the
verdict ledger (``judged.json``) under :data:`KEY`: for each routed id, where it went and
whether it has been acted on, which it has once it is decided on the review page or is no
longer proposed.

## Functions

### `project_of`

```python
def project_of(source: str, roots: Sequence[Path], _index: Optional[Dict[str, Path]] = None)
```

The project folder a source names, or None.

### `ask`

```python
def ask(proposal: Mapping) -> str
```

The proposal's first sentence, on one line, at most :data:`MAX_ASK` characters.

### `route`

```python
def route(proposals: Sequence[Mapping], roots: Sequence[Path], ledger: Dict[str, dict], day: str, lanes: Optional[Mapping[str, str]] = None, decisions: Optional[Mapping[str, Mapping]] = None) -> dict
```

Route tonight's proposals and update the record in ``ledger`` (in place). Returns the
night's figures: routed and acted (all nights), new (tonight), open, unrouted, skipped.

### `summary`

```python
def summary(ledger: Mapping) -> str
```

The lead's one line: ``dreams: N proposals routed, M acted``.
