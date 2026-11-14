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

A verdict is kept in a ledger keyed by the exact rule and memory text the judge read
(:func:`pair_key`), so a pair judged once is not judged again, and a night with the judge off
still reports what an earlier judge said (2026-11-14: a one-off cloud run judged the 60 pairs
the local model had left waiting). Either text changing makes a new key, and so a new judgement.

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
``DREAM_LLM_API_KEY`` from the environment by default (the key never in argv). The judge's
``authority`` names the model and where it ran: ``DREAM_LLM_WHERE`` when set (a tunnelled
remote looks local by its URL), else "local" for a loopback URL, else the URL's host.

### `pair_key`

```python
def pair_key(rule_text: str, memory_text: str) -> str
```

The ledger key of one judgement: the exact texts the judge reads, nothing else.

### `judge_refused`

```python
def judge_refused(exc: BaseException) -> bool
```

The judge's port refused the connection: the model is stopped, which is a state (JP
stops :8091/:8092 when he needs the GPU), not a failure of the pass.

### `contradictions`

```python
def contradictions(memories: Sequence[Memory], rules: Sequence[Rule], embed: Embedder, judge: Judge, authority: Optional[str] = None, guard: Optional[Callable[[], Optional[str]]] = None, ledger: Optional[Dict[str, dict]] = None, used: Optional[Set[str]] = None, on_verdict: Optional[Callable[[], None]] = None, **pair_args)
```

(proposals, stats). Stops calling the judge at the first failed judgement and records it
in ``stats``. ``guard``, called before each model call, stops it the same way when it returns
a reason (a host under memory pressure, say).

``ledger`` (updated in place) holds earlier verdicts by :func:`pair_key`: a pair found there
is not sent to the judge, and its verdict carries the authority that gave it. With a ledger,
``stats["cached"]`` counts those, and pairs past a failure or an off judge still take their
ledger verdicts. ``used`` collects every key this pass met, so the caller can drop the rest.
An entry that is not a dict with a known verdict and a ``by`` is a miss: it is judged again,
never credited to the current judge. ``on_verdict`` runs after each new verdict enters the
ledger, so the caller can save it then and a run killed part way keeps what it judged.
