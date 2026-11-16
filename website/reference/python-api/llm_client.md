# `mempalace.llm_client`

Source: [`mempalace/llm_client.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/llm_client.py)

llm_client.py — Minimal provider abstraction for LLM-assisted entity refinement.

Three providers cover the useful space:

- ``ollama`` (default): local models via http://localhost:11434. Works fully
  offline. Honors MemPalace's "zero-API required" principle.
- ``openai-compat``: any OpenAI-compatible ``/v1/chat/completions`` endpoint.
  Covers OpenRouter, LM Studio, llama.cpp server, vLLM, Groq, Fireworks,
  Together, and most self-hosted setups.
- ``anthropic``: the official Messages API. Opt-in for users who want Haiku
  quality without setting up a local model.

All providers expose the same ``classify(system, user, json_mode)`` method and
the same ``check_available()`` probe. No external SDK dependencies — stdlib
``urllib`` only.

JSON mode matters here: we always ask for structured output. Providers
differ on how to request it (Ollama: ``format: json``; OpenAI-compat:
``response_format``; Anthropic: prompt-level instruction) and this module
normalizes that away from the caller.

## Classes

### `class LLMError(RuntimeError)`

Raised for any provider failure — transport, parse, auth, missing model.

``status`` is the HTTP status when the endpoint answered with an error.
The message is masked: an endpoint URL or reply may carry a key.

#### `__init__`

```python
def __init__(self, message: str, status: Optional[int] = None)
```

### `class LLMResponse`

### `class LLMProvider`

#### `__init__`

```python
def __init__(self, model: str, endpoint: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 120, api_key_source: Optional[str] = None)
```

#### `classify`

```python
def classify(self, system: str, user: str, json_mode: bool = True, think: Optional[bool] = None) -> LLMResponse
```

Classify a (system, user) pair into a structured response.

``think`` controls reasoning emission for thinking-capable models
(currently honored by ``OllamaProvider`` for Qwen 3 / DeepSeek-R1
style toggles). Other providers ignore it. Pass ``False`` to
disable reasoning when the caller wants a fast classification
without ``&lt;think>`` overhead.

#### `check_available`

```python
def check_available(self) -> tuple[bool, str]
```

Return ``(ok, message)``. Fast probe that the provider is reachable.

#### `is_external_service`

```python
def is_external_service(self) -> bool
```

Return True if this provider's endpoint will send user content
off the local machine/network.

Used by ``mempalace init`` to decide whether to print a privacy
warning before first use (issue #24). URL-based heuristic only —
the endpoint determines, regardless of which provider class.
Subclasses that resolve their endpoint dynamically should override
if needed; the default works for the three in-tree providers
(Ollama / OpenAI-compat / Anthropic).

#### `sends_api_key`

```python
def sends_api_key(self) -> bool
```

Whether the stored key may be attached to a request.

An environment key is withheld from an external endpoint until the
caller has accepted that use. An explicit key is sent.

#### `block_silent_external_key`

```python
def block_silent_external_key(self) -> Optional[str]
```

Error when classify would send an env key off-machine, else None.

### `class OllamaProvider(LLMProvider)`

#### `__init__`

```python
def __init__(self, model: str, endpoint: Optional[str] = None, timeout: int = 180, num_ctx: Optional[int] = None, **_: object)
```

#### `check_available`

```python
def check_available(self) -> tuple[bool, str]
```

#### `classify`

```python
def classify(self, system: str, user: str, json_mode: bool = True, think: Optional[bool] = None) -> LLMResponse
```

### `class OpenAICompatProvider(LLMProvider)`

Any OpenAI-compatible ``/v1/chat/completions`` endpoint.

Supply ``--llm-endpoint http://host:port`` (with or without ``/v1``).
API key via ``--llm-api-key`` or the ``OPENAI_API_KEY`` env var.

#### `__init__`

```python
def __init__(self, model: str, endpoint: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 120, **_: object)
```

#### `served_models`

```python
def served_models(self) -> list[str]
```

Model ids the endpoint lists; ``[]`` when it cannot be read.

#### `check_available`

```python
def check_available(self) -> tuple[bool, str]
```

Reachability probe; ``model="auto"`` is resolved here to the served model.

A local server (vLLM, NInfer, LM Studio) serves one model at a time
and its id changes whenever the operator swaps it; ``auto`` follows
the swap instead of failing with ``model_not_found``.

#### `classify`

```python
def classify(self, system: str, user: str, json_mode: bool = True, think: Optional[bool] = None) -> LLMResponse
```

``think=False`` sends ``reasoning_effort: "none"``.

Reasoning models served through an OpenAI-compatible endpoint (Qwen 3.x
on vLLM, NInfer, llama.cpp) think before answering, and their default
effort can spend the whole completion budget on the reasoning channel
and return an empty ``content`` — a 60-excerpt room proposal did
exactly that at 8192 reasoning tokens. ``reasoning_effort`` is the
OpenAI field for this and the servers above honor it; a server that
rejects unknown fields with HTTP 400 gets one retry without it.

### `class AnthropicProvider(LLMProvider)`

#### `__init__`

```python
def __init__(self, model: str, api_key: Optional[str] = None, endpoint: Optional[str] = None, timeout: int = 120, **_: object)
```

#### `check_available`

```python
def check_available(self) -> tuple[bool, str]
```

#### `classify`

```python
def classify(self, system: str, user: str, json_mode: bool = True, think: Optional[bool] = None) -> LLMResponse
```

## Functions

### `endpoint_is_local`

```python
def endpoint_is_local(url: Optional[str]) -> bool
```

Public name for :func:`_endpoint_is_local`.

### `resolve_openai_url`

```python
def resolve_openai_url(endpoint: str, leaf: str) -> str
```

Join an OpenAI-compatible base URL with ``leaf`` (``chat/completions`` or ``embeddings``).

### `redact_secrets`

```python
def redact_secrets(text: str) -> str
```

Mask credentials a URL or one of our own messages may carry: a second layer, not a filter for untrusted
text (errors never repeat an endpoint's reply; see describe_failure). Patterns cannot find every secret: an
escaped quote or a multi-line YAML value defeats the assignment mask, which is why replies are not echoed.

### `require_external_consent`

```python
def require_external_consent(provider: LLMProvider, *, accept: bool = False, interactive: bool = False, prompt: Optional[str] = None) -> bool
```

Allow an external endpoint only with an explicit yes.

Local endpoints are allowed. ``accept`` records consent without a prompt.
``interactive`` asks on stdin. A yes sets ``external_use_accepted`` so an
environment key may be sent.

### `endpoint_label`

```python
def endpoint_label(url: str) -> str
```

The endpoint as an error may name it: scheme, host, port and a plain path. Never userinfo, a query or a
fragment, which can carry a key under any spelling (xreview 2026-11-15); a URL that does not parse cleanly
is "the configured endpoint".

### `describe_failure`

```python
def describe_failure(e: BaseException) -> str
```

What went wrong, in words that never come from the endpoint: the exception's type, and an OS error's own
description. Never the exception's text, a reply, or an HTTP reason phrase, any of which can carry a key
(xreview 2026-11-15: no pattern reliably finds an arbitrary credential in untrusted text).

### `get_provider`

```python
def get_provider(name: str, model: str, endpoint: Optional[str] = None, api_key: Optional[str] = None, timeout: int = 120, **provider_kwargs: object) -> LLMProvider
```

Build a provider by name. Raises LLMError on unknown provider.

Extra kwargs (e.g. num_ctx for Ollama) are forwarded to the provider's
constructor; providers that don't recognize them ignore via **_.
