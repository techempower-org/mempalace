# `mempalace.kg_normalize`

Source: [`mempalace/kg_normalize.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/kg_normalize.py)

Normalize knowledge-graph predicates onto a closed vocabulary.

Agents filing facts one at a time invent a predicate per fact
(``brazil_presidential_mae_after_ranker_fix``), so ``mempalace_kg_query`` can
never find facts by relation: on the maintainers' palace 51 of 57 facts had a
predicate no other fact used. This module maps every open fact whose
predicate is outside the vocabulary onto one of the allowed predicates, with
the detail the old predicate carried folded into the object.

Two steps, like ``rooms``:

* **plan** — the LLM proposes, per off-vocabulary fact, the target predicate
  and the rewritten object (``(weatherstation, deployed_commit, fc81c6f6)`` →
  ``(weatherstation, status, "deployed commit fc81c6f6 on staging")``). The
  plan is written to ``&lt;palace>/kg/normalize.json`` for review; any row can
  be edited or dropped.
* **apply** — for each row, the old fact is closed and the new one opened at
  one shared instant (``invalidate`` + ``add_triple`` with the same
  timestamp), so history survives and an as-of query before the boundary
  still returns the original wording. Nothing is deleted.

## Functions

### `off_vocabulary_facts`

```python
def off_vocabulary_facts(kg, vocabulary: Iterable[str]) -> list[dict]
```

Open facts whose predicate is not in ``vocabulary``, oldest first.

### `plan_normalize`

```python
def plan_normalize(facts: list[dict], provider, vocabulary: Iterable[str] = DEFAULT_VOCABULARY) -> dict
```

Ask the LLM for a rewrite of every off-vocabulary fact.

Rows whose proposed predicate is not in the vocabulary are dropped from
the plan and counted in ``rejected``; the user can add them by hand.

### `normalize_plan_path`

```python
def normalize_plan_path(config: MempalaceConfig) -> str
```

### `save_normalize_plan`

```python
def save_normalize_plan(config: MempalaceConfig, plan: dict) -> str
```

### `load_normalize_plan`

```python
def load_normalize_plan(config: MempalaceConfig) -> dict
```

### `apply_normalize`

```python
def apply_normalize(kg, plan: dict, at: Optional[str] = None) -> dict
```

Close each old fact and open its rewrite at one shared instant.

Each row is one ``KnowledgeGraph.rewrite`` call, addressed by the
triple id the plan recorded: one transaction per fact, and a fact that
was closed or replaced while the plan sat under review is left alone
and counted in ``stale`` instead of being resurrected.
