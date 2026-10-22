# `mempalace.layers`

Source: [`mempalace/layers.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/layers.py)

layers.py — 4-Layer Memory Stack for mempalace
===================================================

Load only what you need, when you need it.

    Layer 0: Identity       (~100 tokens)   — Always loaded. "Who am I?"
    Layer 1: Essential Story (~500-800)      — Always loaded. Top moments from the palace.
    Layer 2: On-Demand      (~200-500 each)  — Loaded when a topic/wing comes up.
    Layer 3: Deep Search    (unlimited)      — Full ChromaDB semantic search.

Wake-up cost: ~600-900 tokens (L0+L1). Leaves 95%+ of context free.

Reads directly from ChromaDB (mempalace_drawers)
and ~/.mempalace/identity.txt.

## Classes

### `class Layer0`

~100 tokens. Always loaded.
Reads from ~/.mempalace/identity.txt — a plain-text file the user writes.

Example identity.txt:
    I am Atlas, a personal AI assistant for Alice.
    Traits: warm, direct, remembers everything.
    People: Alice (creator), Bob (Alice's partner).
    Project: A journaling app that helps people process emotions.

#### `__init__`

```python
def __init__(self, identity_path: str = None)
```

#### `render`

```python
def render(self) -> str
```

Return the identity text, or a sensible default.

#### `token_estimate`

```python
def token_estimate(self) -> int
```

### `class Layer1`

~500-800 tokens. Always loaded.
Auto-generated from the highest-weight / most-recent drawers in the palace.
Groups by room, picks the top N moments, compresses to a compact summary.

#### `__init__`

```python
def __init__(self, palace_path: str = None, wing: str = None)
```

#### `generate`

```python
def generate(self) -> str
```

Pull top drawers from the palace and format as compact L1 text.

### `class Layer2`

~200-500 tokens per retrieval.
Loaded when a specific topic or wing comes up in conversation.
Queries ChromaDB with a wing/room filter.

#### `__init__`

```python
def __init__(self, palace_path: str = None)
```

#### `retrieve`

```python
def retrieve(self, wing: str = None, room: str = None, n_results: int = 10) -> str
```

Retrieve drawers filtered by wing and/or room.

### `class Layer3`

Unlimited depth. Semantic search against the full palace.
Reuses searcher.py logic against mempalace_drawers.

#### `__init__`

```python
def __init__(self, palace_path: str = None)
```

#### `search`

```python
def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str
```

Semantic search, returns compact result text.

#### `search_raw`

```python
def search_raw(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> list
```

Return raw dicts instead of formatted text.

### `class MemoryStack`

The full 4-layer stack. One class, one palace, everything works.

    stack = MemoryStack()
    print(stack.wake_up())                # L0 + L1 (~600-900 tokens)
    print(stack.recall(wing="my_app"))     # L2 on-demand
    print(stack.search("pricing change"))  # L3 deep search

#### `__init__`

```python
def __init__(self, palace_path: str = None, identity_path: str = None)
```

#### `wake_up`

```python
def wake_up(self, wing: str = None) -> str
```

Generate wake-up text: L0 (identity) + L1 (essential story).
Typically ~600-900 tokens. Inject into system prompt or first message.

Args:
    wing: Optional wing filter for L1 (project-specific wake-up).

#### `recall`

```python
def recall(self, wing: str = None, room: str = None, n_results: int = 10) -> str
```

On-demand L2 retrieval filtered by wing/room.

#### `search`

```python
def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str
```

Deep L3 semantic search.

#### `status`

```python
def status(self) -> dict
```

Status of all layers.
