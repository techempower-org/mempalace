# `mempalace.wing_split`

Source: [`mempalace/wing_split.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/wing_split.py)

Split a machine-level transcript wing into one wing per source project.

``mempalace mine &lt;convo-dir> --mode convos`` without ``--wing`` files every
session under one wing named after the export (``claude_conversations``,
``codex_conversations_windows``). On a real palace that wing is hundreds of
projects: the maintainers' own held 846 Claude Code project directories in
one wing, so no room set could mean anything and a wing-scoped search
returned everything. This module re-keys those drawers by the project the
transcript came from.

Two steps, like ``rooms``:

* **plan** — group the wing's drawers by *project key* (the encoded project
  directory in a Claude Code transcript path, or the ``cwd`` recorded in a
  Codex rollout when the file is readable), resolve each key to a target
  wing (an existing wing whose name the key ends with, else a name derived
  from the key) and write ``&lt;palace>/wings/split-&lt;wing>.json`` for review.
* **apply** — rewrite ``wing`` metadata for every drawer in the plan, in
  batches, under the palace lock. Content and room never change. Hallway
  records of the split wing are dropped because they are keyed by wing and
  the next mine rebuilds them per target wing.

## Functions

### `project_key`

```python
def project_key(source_file: Optional[str]) -> Optional[str]
```

The project identifier a transcript path carries, or ``None``.

Claude Code encodes the working directory in the path
(``.claude/projects/-Users-me-dev-mempalace/&lt;session>.jsonl``,
subagent transcripts nest below it); the encoded segment is the key.
Codex rollouts carry no project in the path; the key is the basename of
the ``cwd`` in the file when the file is on this machine.

### `resolve_target`

```python
def resolve_target(key: str, existing_wings: Iterable[str]) -> tuple[str, str]
```

``(target_wing, how)`` for a project key.

``how`` is ``"existing"`` when the key ends with an existing wing's name
at a segment boundary (longest match wins: ``p--acme-portal`` →
``portal``, ``P--org-invoices`` → ``invoices``), otherwise ``"derived"``
with a name taken from the key after the last root marker
(``-Users-me-dev-acme-app`` → ``acme_app``) or, without one,
after the leading drive and user segments.

### `plan_split`

```python
def plan_split(col, wing: str, existing_wings: Iterable[str]) -> dict
```

Group the wing's drawers by project key and resolve targets.

Returns ``&#123;"wing", "planned_at", "projects": &#123;key: &#123;"target", "how",
"drawers"}}, "unresolved": n}``; drawers whose path carries no project
stay where they are and are only counted.

### `split_plan_path`

```python
def split_plan_path(config: MempalaceConfig, wing: str) -> str
```

### `save_split_plan`

```python
def save_split_plan(config: MempalaceConfig, plan: dict) -> str
```

### `load_split_plan`

```python
def load_split_plan(config: MempalaceConfig, wing: str) -> dict
```

### `plan_targets`

```python
def plan_targets(plan: dict) -> dict[str, int]
```

``&#123;target_wing: drawers}`` the plan would produce.

### `split_pending_path`

```python
def split_pending_path(config: MempalaceConfig, wing: str) -> str
```

Marker that a split of ``wing`` started and has not finished every phase.

### `split_plan_fingerprint`

```python
def split_plan_fingerprint(plan: dict) -> str
```

Digest of the wing and each project's target — the decisions apply_split follows.

### `write_split_pending`

```python
def write_split_pending(config: MempalaceConfig, wing: str, plan: dict) -> str
```

Record that a split started, pinning the plan targets being applied.

### `assert_split_pending_matches_plan`

```python
def assert_split_pending_matches_plan(config: MempalaceConfig, wing: str, plan: dict) -> None
```

Refuse a retry whose saved plan targets no longer match the pinned split.

### `apply_split`

```python
def apply_split(col, plan: dict, config: Optional[MempalaceConfig] = None, progress = None, closets_col = None, resuming: bool = False) -> dict
```

Rewrite ``wing`` for every drawer whose project key is in the plan.

Returns ``&#123;"moved", "per_target", "skipped", "closets_moved",
"hallways_dropped"}``. Drawers whose key is missing from the plan (or
resolves back to the source wing) are skipped. ``closets_col`` — the AAAK
index layer, keyed by the same ``source_file`` and filtered by the same
``wing`` at search time — is re-keyed by the same rule, so a wing-scoped
search keeps its index boosts after the split. Hallway records for the
source wing are dropped since they are keyed by wing; rebuild them with
``mempalace hallways --rebuild`` or the next mine.

Interruption: rows move in batches, each batch one backend write, and a
row is only ever wholly in its old wing or wholly in its new one. The
loop reads the rows *currently* in the source wing, so running the same
plan again after a crash or Ctrl-C moves exactly the remainder (drawers,
then closets, then the hallway drop). ``resuming`` says a previous run
started and did not finish: the hallway drop then runs even if no drawer
is left to move, because the crash may have come after the drawer phase.
A completed split re-run without ``resuming`` changes nothing. Nothing
is deleted and no content is rewritten at any point.
