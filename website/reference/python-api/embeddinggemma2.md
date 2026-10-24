# `mempalace.embeddinggemma2`

Source: [`mempalace/embeddinggemma2.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/embeddinggemma2.py)

Local Sentence Transformers provider for Google's EmbeddingGemma 2.

The model is loaded only on first use. Documents and search queries use the
model's distinct task prompts, and document metadata can supply a title.

## Classes

### `class EmbeddingOutputError(RuntimeError)`

Raised when the model returns vectors that cannot safely be stored.

### `class EmbeddingGemma2OutOfMemoryError(RuntimeError)`

CUDA ran out of memory even at batch size 1.

### `class EmbeddingGemma2EmbeddingFunction`

Chroma-compatible local EmbeddingGemma 2 function.

``__call__`` and :meth:`embed_documents` produce corpus vectors. Use
:meth:`embed_query` for natural-language retrieval and
:meth:`embed_code_query` for code retrieval. Media helpers accept local
image, audio, and video inputs when the configured modality includes the
corresponding encoder.

#### `__init__`

```python
def __init__(self, dimension: int = 768, modalities: str = 'text', device: str = 'auto', revision: Optional[str] = None, batch_size: Optional[int] = None)
```

#### `name`

```python
def name() -> str
```

Stable Chroma embedding-function class name.

#### `default_space`

```python
def default_space() -> str
```

#### `supported_spaces`

```python
def supported_spaces() -> list[str]
```

#### `validate_config`

```python
def validate_config(config: dict) -> None
```

#### `build_from_config`

```python
def build_from_config(config: dict) -> 'EmbeddingGemma2EmbeddingFunction'
```

#### `get_config`

```python
def get_config(self) -> dict
```

#### `effective_device`

```python
def effective_device(self) -> Optional[str]
```

Resolved runtime device after the lazy model load, if loaded.

#### `planned_device`

```python
def planned_device(self) -> str
```

The device the model runs on, or will run on once loaded.

Before the lazy load this previews ``auto`` (and an unavailable
explicit device) without loading weights or warning, so the mine
header can name it. Without torch it returns the configured value.

#### `unavailable_device`

```python
def unavailable_device(self) -> Optional[str]
```

The explicit device (``cuda``/``mps``) torch cannot use, if any.

``None`` when the configured device is usable, or is ``auto``/``cpu``.
Probed without loading the model.

#### `device_label`

```python
def device_label(self) -> str
```

The device for headers and status: ``cuda``, or ``cpu; cuda requested but unavailable``.

#### `warn_if_device_unavailable`

```python
def warn_if_device_unavailable(self) -> None
```

Warn once (stderr, via the logger) that the explicit device falls back to CPU.

Called before the mine header is printed, so the warning comes first,
and again at model load; only the first call warns.

#### `batch_size_for`

```python
def batch_size_for(self, device: Optional[str]) -> int
```

Documents per encode() call on ``device``; a configured value wins.

#### `is_legacy`

```python
def is_legacy(self) -> bool
```

#### `identity`

```python
def identity(self) -> str
```

#### `embed_documents`

```python
def embed_documents(self, input: str | list[str], metadatas: Optional[list[dict]] = None)
```

#### `embed_query`

```python
def embed_query(self, input)
```

#### `embed_code_query`

```python
def embed_code_query(self, input)
```

#### `embed_media`

```python
def embed_media(self, input, media_type: str) -> list[float]
```

Embed one local image, audio, or video file without a task prefix.

#### `embed_image`

```python
def embed_image(self, input) -> list[float]
```

#### `embed_audio`

```python
def embed_audio(self, input) -> list[float]
```

#### `embed_video`

```python
def embed_video(self, input) -> list[float]
```
