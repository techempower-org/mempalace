# `mempalace.dream.contradictions`

Source: [`mempalace/dream/contradictions.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/dream/contradictions.py)

Dream day 2: feedback memories that contradict a CLAUDE.md rule, judged by a local model.

Every rule and every feedback memory is embedded once (palace-daemon ``/embed``). Each memory is
paired with its nearest rules above a similarity floor, the closest pairs first, up to a cap: a
contradiction is only worth a model call when both sides talk about the same thing. A local
OpenAI-compatible model (qwen36-coder on the B60 by default) reads each pair and answers
``contradict``, ``consistent`` or ``unrelated`` with a one-line why. Only ``contradict`` becomes a
proposal, labelled with the model as its authority: unreviewed.

A failed model call stops the pass where it is: what was judged is still reported, with the
failure, and nothing is retried (a busy shared model is not hammered).

## Classes

### `class Rule`

## Functions

### `applies`

```python
def applies(memory: Memory, rule: Rule, global_dirs: Sequence[Path] = ()) -> bool
```

Whether a rule governs a memory's project. A rule in a global CLAUDE.md (in one of
``global_dirs``, by default ``~/.claude``) governs every project; one in a project's CLAUDE.md
governs only the memories saved in that project (its memory folder's project key), so a
lesson saved in one repo is never weighed against another repo's rules.

### `read_rules`

```python
def read_rules(paths: Sequence[Path]) -> List[Rule]
```

### `candidate_pairs`

```python
def candidate_pairs(memories: Sequence[Memory], rules: Sequence[Rule], embed: Embedder, floor: float = PAIR_FLOOR, per_memory: int = PER_MEMORY, cap: int = MAX_PAIRS, global_dirs: Sequence[Path] = ())
```

(similarity, memory, rule) for each feedback memory's nearest rules that govern it (see
:func:`applies`) at or above ``floor``, most similar first, at most ``cap``.

### `parse_verdict`

```python
def parse_verdict(text: str) -> Dict[str, str]
```

### `local_judge`

```python
def local_judge(url: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None) -> Judge
```

An OpenAI-compatible chat endpoint; ``DREAM_LLM_URL``, ``DREAM_LLM_MODEL`` and
``DREAM_LLM_API_KEY`` from the environment by default (the key never in argv).

### `contradictions`

```python
def contradictions(memories: Sequence[Memory], rules: Sequence[Rule], embed: Embedder, judge: Judge, authority: str = 'qwen36-coder (local), unreviewed', **pair_args)
```

(proposals, stats). Stops at the first failed judgement and records it in ``stats``.
