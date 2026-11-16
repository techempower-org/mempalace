# `mempalace.general_extractor`

Source: [`mempalace/general_extractor.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/general_extractor.py)

general_extractor.py — Extract 5 types of memories from text.

Types:
  1. DECISIONS    — "we went with X because Y", choices made
  2. PREFERENCES  — "always use X", "never do Y", "I prefer Z"
  3. MILESTONES   — breakthroughs, things that finally worked
  4. PROBLEMS     — what broke, what fixed it, root causes
  5. EMOTIONAL    — feelings, vulnerability, relationships

No LLM required. Pure keyword/pattern heuristics.
No external dependencies on palace.py, dialect.py, or layers.py.

Usage:
    from general_extractor import extract_memories

    chunks = extract_memories(text)
    # [&#123;"content": "...", "memory_type": "decision", "chunk_index": 0}, ...]

## Functions

### `extract_memories`

```python
def extract_memories(text: str, min_confidence: float = 0.3, chunk_size: int = DEFAULT_CHUNK_SIZE) -> List[Dict]
```

Extract memories from a text string.

Paragraphs that match no marker are stored as ``memory_type="general"``
with their text unchanged. ``min_confidence`` is accepted for callers
and does not discard text.

Args:
    text: The text to extract from (any format).
    min_confidence: Retained for callers. Not used to drop paragraphs.
    chunk_size: Per-memory character cap. Segments exceeding this
        size are sliced on whitespace into multiple memories that share
        the same ``memory_type``. Caller (typically ``mine_convos``)
        should pass ``MempalaceConfig.chunk_size`` so config-driven
        sizing reaches this path; the default matches the
        module-level CHUNK_SIZE in ``convo_miner.py``.

Returns:
    List of dicts: &#123;"content": str, "memory_type": str, "chunk_index": int}
