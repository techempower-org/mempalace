# `mempalace.embedding`

Source: [`mempalace/embedding.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/embedding.py)

Embedding function factory with hardware acceleration.

Returns a ChromaDB-compatible embedding function — either a local ONNX model
bound to a user-selected ONNX Runtime execution provider, or an
OpenAI-compatible HTTP ``/v1/embeddings`` endpoint.

Four embedding-model options are available, selected via
``MEMPALACE_EMBEDDING_MODEL`` or ``embedding_model`` in
``~/.mempalace/config.json``:

* ``minilm`` (default) — ``all-MiniLM-L6-v2``, 384-dim, English-only training.
  ChromaDB's default; what every existing palace was built with.
* ``embeddinggemma`` — ``onnx-community/embeddinggemma-300m-ONNX`` (q8), 384-dim
  via Matryoshka truncation, multilingual (100+ languages). Cross-lingual cos
  ~0.88 on parallel translations vs MiniLM's ~0.35. Recommended for any
  non-English use; onboarding offers it as the default. The ~300 MB ONNX
  model is lazy-downloaded from HuggingFace on first use. Switching models
  on an existing palace requires ``mempalace repair rebuild-index``
  (different vector space). Its ``session.run()`` sub-batch size (32 docs by
  default, #1770) is overridable via ``MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE``
  or ``embeddinggemma_batch_size`` in ``config.json`` for palaces whose
  drawers are long enough that the default sub-batch exceeds available
  memory (#2330).
* ``adaptmem_ft`` — a SentenceTransformer-shaped fine-tuned checkpoint from
  techempower-org/adaptmem, loaded from a local path (``MEMPALACE_ADAPTMEM_PATH``
  or ``adaptmem_path`` in config). Nothing is downloaded. Same 384-dim shape as
  MiniLM, so it drops into existing collections, but it is a different vector
  space — switching requires ``mempalace repair rebuild-index``. Requires the
  ``sentence-transformers`` package.
* ``openai-compat`` — embeddings served by any OpenAI-compatible
  ``/v1/embeddings`` endpoint (LM Studio, llama.cpp, vLLM, Ollama's OpenAI
  shim, or a self-hosted server) instead of a local ONNX model. Useful for
  larger / multilingual embedders (e.g. Qwen3-Embedding) or GPU offload.
  Endpoint settings are read from ``config.json`` as ``embedding_api_url`` /
  ``embedding_api_model`` / ``embedding_api_key`` (each overridable via the
  matching ``MEMPALACE_EMBEDDING_API_*`` env var). Vectors are L2-normalized
  for the cosine collection; the dimension is whatever the server returns, so
  switching to/from this backend also requires ``mempalace repair
  rebuild-index``. Stays local when the endpoint is on your machine/LAN.

Supported devices (env ``MEMPALACE_EMBEDDING_DEVICE`` or ``embedding_device``
in ``~/.mempalace/config.json``):

* ``auto`` — prefer CUDA ▸ CoreML ▸ DirectML, fall back to CPU
* ``cpu`` — force CPU (the historical default)
* ``cuda`` — NVIDIA GPU via ``onnxruntime-gpu`` (``pip install mempalace[gpu]``)
* ``coreml`` — Apple Neural Engine (macOS)
* ``dml`` — DirectML (Windows / AMD / Intel GPUs)

``embeddinggemma2`` runs on PyTorch rather than ONNX Runtime and reads the same
setting: ``auto`` (CUDA ▸ MPS ▸ CPU), ``cuda``, ``mps`` or ``cpu``. The
ONNX-only ``coreml`` and ``dml`` read as ``auto`` there, and ``mps`` reads as
``auto`` for the ONNX models, each with a one-time warning.

Requesting an unavailable accelerator emits a warning and falls back to CPU
rather than hard-failing — mining must still work on a laptop without CUDA.
The same applies to an accelerator that runs but computes the model wrongly:
``embeddinggemma`` on CoreML returns NaN or all-zero vectors without raising,
so ``auto`` never selects CoreML for it and an explicitly requested one is
rejected by a witness embedding at load time.

## Classes

### `class EmbeddinggemmaONNX`

ChromaDB-compatible EF using embeddinggemma-300m ONNX (q8, MRL→384d).

Cross-lingual cosine similarity on parallel-translated text averages 0.88
across DE/FR/HI/IT/KO/RU vs 0.35 for ``all-MiniLM-L6-v2``. Output dim is
truncated to 384 via Matryoshka Representation Learning so the model is a
drop-in replacement for the MiniLM-shaped 384-dim collections ChromaDB
creates by default — same vector width, no schema change.

Switching an existing palace from minilm → embeddinggemma still requires
re-embedding (different vector space) — collections persist the EF name
and ChromaDB rejects mismatched reads. Run ``mempalace repair rebuild-index``.

#### `name`

```python
def name() -> str
```

#### `__init__`

```python
def __init__(self, preferred_providers = None, batch_size: int = _EMBEDDINGGEMMA_BATCH_SIZE, intra_op_num_threads: int = 0)
```

#### `embed_query`

```python
def embed_query(self, input: list[str]) -> list[list[float]]
```

Embed query documents (ChromaDB EF protocol).

#### `embed_documents`

```python
def embed_documents(self, input: list[str]) -> list[list[float]]
```

Embed a batch of documents (ChromaDB EF protocol).

### `class EmbeddingAPIError(RuntimeError)`

Raised when the embedding API is unreachable or returns an invalid body.

Module-specific subclass mirroring ``llm_client.LLMError`` so callers can
distinguish embedding-endpoint failures; subclasses ``RuntimeError`` so
existing ``except RuntimeError`` paths still catch it.

### `class OpenAICompatEmbeddingFunction`

ChromaDB-compatible EF backed by an OpenAI-compatible ``/v1/embeddings``
endpoint (LM Studio, llama.cpp, vLLM, Ollama's OpenAI shim, etc.).

Selected via ``embedding_model == "openai-compat"``. Vectors are produced
server-side and fetched over HTTP, which changes the vector space.
``name()`` encodes the model id, but this does **not** guard a model or
endpoint change: chromadb 1.5.x persists this class as a legacy EF
(``&#123;"type": "legacy"}``, no name), so it never compares names on open,
and the recorded palace identity is the bare ``openai-compat`` for every
endpoint model. Switching ``embedding_api_model`` to another model with
the same dimension is therefore accepted silently; run ``mempalace
repair rebuild-index`` yourself after changing the model or endpoint.
(Recording the endpoint model in the identity is follow-up item D.)
stdlib ``urllib`` only, no new dependency.

#### `__init__`

```python
def __init__(self, base_url: str, model: str, api_key: Optional[str] = None)
```

#### `name`

```python
def name(self) -> str
```

#### `embed_query`

```python
def embed_query(self, input)
```

### `class UnknownEmbeddingModelError(ValueError)`

``embedding_model`` looks like a misspelled supported model name.

Raised instead of the MiniLM fallback for near misses of EmbeddingGemma
or ``openai-compat``: filing MiniLM vectors under a palace the user meant
to embed with another model can only be undone by re-embedding everything
(``mempalace repair rebuild-index``). The Chroma backend re-raises it
rather than opening with chromadb's default.

### `class EmbeddingFunctionUnavailableError(RuntimeError)`

The configured embedding function could not be built.

MemPalace refuses the call instead of letting chromadb embed with its
own default function: that writes vectors from a model the palace's
recorded identity does not name, and nothing downstream notices (the
stored and configured names still agree). Raised for writes and
searches; reads that never embed keep working (see
:class:`UnavailableEmbeddingFunction`).

#### `__init__`

```python
def __init__(self, model: str, cause: BaseException)
```

### `class UnavailableEmbeddingFunction`

Stand-in Chroma EF for a read-only open when the configured one failed to build.

Opening a collection to count, list or fetch rows never embeds, so those
reads keep working; any embed (a query, or a write that passes text)
raises the :class:`EmbeddingFunctionUnavailableError` instead of
reaching chromadb's default function. ``name()`` is ``"default"`` so
chromadb's EF-name check on open never trips on it; it is never passed
to a create, so chromadb never persists it.

#### `__init__`

```python
def __init__(self, error: EmbeddingFunctionUnavailableError)
```

#### `name`

```python
def name() -> str
```

#### `is_legacy`

```python
def is_legacy(self) -> bool
```

#### `get_config`

```python
def get_config(self) -> dict
```

#### `default_space`

```python
def default_space(self) -> str
```

#### `supported_spaces`

```python
def supported_spaces(self) -> list
```

#### `embed_query`

```python
def embed_query(self, input)
```

#### `embed_documents`

```python
def embed_documents(self, input)
```

## Functions

### `set_embedding_section_hook`

```python
def set_embedding_section_hook(hook) -> None
```

Install or clear this thread's embedding-section context manager factory.

``hook`` is ``None`` or a zero-arg callable that returns a context manager.

### `embedding_section`

```python
def embedding_section()
```

Run the body under this thread's embedding-section hook, if any.

### `embedding_function_unavailable`

```python
def embedding_function_unavailable(cause: BaseException) -> EmbeddingFunctionUnavailableError
```

Wrap a failure to build the configured embedding function.

### `configured_embedding_function`

```python
def configured_embedding_function()
```

:func:`get_embedding_function` for the configured model, refusing on a build failure.

A misspelled model keeps raising :class:`UnknownEmbeddingModelError`, and
an EmbeddingGemma 2 failure keeps its own error (it already names the
missing extra or device); any other failure to build raises
:class:`EmbeddingFunctionUnavailableError`. Nothing calls chromadb's
default function instead.

### `log_model_refusal`

```python
def log_model_refusal(log, kind: str, exc: BaseException, palace_path = None) -> None
```

Log a refused call: the full message once per palace and message, then one short line.

A long-lived MCP server refuses every call until the config or the palace
is fixed; repeating the multi-line explanation on each call buries
everything else on stderr.

### `model_error_result`

```python
def model_error_result(exc: BaseException, *, palace_path = None, log = None) -> Optional[dict]
```

The tool/search result for a model error, or None for any other exception.

Matched by exception class. ``error`` is the human-readable kind,
``error_class`` the model-error class name (``MODEL_ERROR_CLASS_NAMES``)
that MCP checks to set ``isError``, ``details`` the full message with the
fix. ``log``, when given, gets one rate-limited line (no traceback).

### `get_embedding_function`

```python
def get_embedding_function(device: Optional[str] = None, model: Optional[str] = None)
```

Return a cached embedding function for the requested device + model.

``device=None`` reads :attr:`MempalaceConfig.embedding_device`;
``model=None`` reads :attr:`MempalaceConfig.embedding_model`.
The returned function is shared across calls with the same resolved
provider list + model so we only pay model-load cost once per process.

### `describe_device`

```python
def describe_device(device: Optional[str] = None, model: Optional[str] = None) -> str
```

Return a short human-readable label for the resolved embedding backend.

Used by the miner CLI header / MCP status so users can see at a glance
whether GPU acceleration engaged — or, for the ``openai-compat`` backend,
that embeddings are served by a remote endpoint rather than local hardware
(in which case the ``embedding_device`` accelerator label is irrelevant).

### `current_model_name`

```python
def current_model_name(model: Optional[str] = None) -> str
```

Resolve the canonical embedder model name (cheap, no model load).

This is the configured ``embedding_model`` (``"minilm"`` /
``"embeddinggemma"`` / ...), not the embedding function's internal
``name()`` (which is spoofed to ``"default"`` for ChromaDB compatibility).
Unrecognized names resolve to ``"minilm"``, the model that embeds them,
and near misses raise :class:`UnknownEmbeddingModelError`.

### `probe_dimension`

```python
def probe_dimension(device: Optional[str] = None, model: Optional[str] = None) -> int
```

Return the embedder's output dimension by embedding a short probe.

Model-agnostic — works for any model without a hardcoded table — and
cached per resolved model name so the probe is paid at most once per
process. Returns ``0`` if the probe fails (treated as "dimension unknown"
by the identity check, so a probe failure never blocks normal operation).

### `get_embedder_identity`

```python
def get_embedder_identity(device: Optional[str] = None, model: Optional[str] = None)
```

Resolve the current embedder identity (RFC 001).

``model_name`` from config (cheap); ``dimension`` from a cached one-time
probe. Returns an :class:`~mempalace.backends.base.EmbedderIdentity`.
