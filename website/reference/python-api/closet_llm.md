# `mempalace.closet_llm`

Source: [`mempalace/closet_llm.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/closet_llm.py)

closet_llm.py — Generate closets via a user-configured LLM for richer indexing.

The regex-based closet extraction catches action verbs, headers, and proper
nouns — but misses implicit topics, foreign-language content, and contextual
references. An LLM reads everything and produces better closets.

This module is **OPTIONAL and opt-in**. Regex closets are always created by
the miner; this path regenerates them afterward using whatever LLM the user
chooses. Core memory operations remain API-free by design (see CLAUDE.md,
"Local-first, zero API").

## Bring-your-own-LLM configuration

The endpoint is any OpenAI-compatible Chat Completions URL:

    MEMPALACE_LLM_ENDPOINT=http://localhost:11434/v1   # Ollama
    MEMPALACE_LLM_ENDPOINT=http://localhost:8000/v1    # vLLM, llama.cpp
    MEMPALACE_LLM_ENDPOINT=https://api.openai.com/v1

Set:
    MEMPALACE_LLM_ENDPOINT — base URL (required)
    MEMPALACE_LLM_KEY      — bearer token (optional; local inference usually doesn't need it)
    MEMPALACE_LLM_MODEL    — model name (required), e.g. "llama3:8b", "qwen2.5:7b"

A non-local endpoint also needs ``--accept-external-llm`` or
``MEMPALACE_ACCEPT_EXTERNAL_LLM=1``. Generic ``LLM_ENDPOINT`` / ``LLM_KEY`` /
``LLM_MODEL`` variables are ignored.

Or pass flags on the CLI (flags win over env):

    python -m mempalace.closet_llm \
        --palace ~/.mempalace/palace \
        --endpoint http://localhost:11434/v1 \
        --model llama3:8b

No vendor lock-in. No hidden dependency on any specific provider. Zero deps
added to pyproject — uses stdlib urllib.

## Classes

### `class LLMConfig`

Resolved LLM connection config. CLI flags > env vars.

#### `__init__`

```python
def __init__(self, endpoint: Optional[str] = None, key: Optional[str] = None, model: Optional[str] = None, accept_external: Optional[bool] = None)
```

#### `missing`

```python
def missing(self) -> list
```

#### `egress_error`

```python
def egress_error(self) -> Optional[str]
```

Why this config must not receive drawer content, or None when it may.

## Functions

### `regenerate_closets`

```python
def regenerate_closets(palace_path, wing = None, sample = 0, dry_run = False, cfg: Optional[LLMConfig] = None)
```

Regenerate closets using a configured LLM for richer topic extraction.

Reads existing drawers, sends content to the configured endpoint,
replaces regex closets with LLM-generated ones. Regex closets remain
as the fallback whenever the call fails.
