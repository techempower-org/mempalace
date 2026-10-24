# `mempalace.entity_registry`

Source: [`mempalace/entity_registry.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/entity_registry.py)

entity_registry.py — Persistent personal entity registry for MemPalace.

Knows the difference between Riley (a person) and ever (an adverb).
Built from three sources, in priority order:
  1. Onboarding — what the user explicitly told us
  2. Learned — what we inferred from session history with high confidence
  3. Researched — legacy ``wiki_cache`` entries from older versions, read only

Usage:
    from mempalace.entity_registry import EntityRegistry
    registry = EntityRegistry.load()
    result = registry.lookup("Riley", context="I went with Riley today")
    # → &#123;"type": "person", "confidence": 1.0, "source": "onboarding"}

## Classes

### `class EntityRegistry`

Persistent personal entity registry.

Stored at ~/.mempalace/entity_registry.json
Schema:
&#123;
  "mode": "personal",   # work | personal | combo
  "version": 1,
  "people": &#123;
    "Riley": &#123;
      "source": "onboarding",
      "contexts": ["personal"],
      "aliases": [],
      "relationship": "daughter",
      "confidence": 1.0
    }
  },
  "projects": ["MemPalace", "Acme"],
  "ambiguous_flags": ["riley", "max"],
  "wiki_cache": &#123;
    "Sam": &#123;"inferred_type": "person", "confidence": 0.9, "confirmed": true, ...}
  }
}

#### `__init__`

```python
def __init__(self, data: dict, path: Path)
```

#### `load`

```python
def load(cls, config_dir: Optional[Path] = None) -> 'EntityRegistry'
```

#### `save`

```python
def save(self)
```

#### `mode`

```python
def mode(self) -> str
```

#### `people`

```python
def people(self) -> dict
```

#### `projects`

```python
def projects(self) -> list
```

#### `ambiguous_flags`

```python
def ambiguous_flags(self) -> list
```

#### `seed`

```python
def seed(self, mode: str, people: list, projects: list, aliases: dict = None)
```

Seed the registry from onboarding data.

people: list of dicts &#123;"name": str, "relationship": str, "context": str}
projects: list of str
aliases: dict &#123;"Max": "Maxwell", ...}

#### `lookup`

```python
def lookup(self, word: str, context: str = '') -> dict
```

Look up a word. Returns entity classification.

context: surrounding sentence (used for disambiguation of ambiguous words)

Returns:
    &#123;"type": "person"|"project"|"concept"|"unknown",
     "confidence": float,
     "source": "onboarding"|"learned"|"wiki"|"inferred",
     "name": canonical name if found,
     "needs_disambiguation": bool}

#### `learn_from_text`

```python
def learn_from_text(self, text: str, min_confidence: float = 0.75, languages = ('en',)) -> list
```

Scan session text for new entity candidates.
Returns list of newly discovered candidates for review.

``languages`` is forwarded to entity detection — pass the user's
configured ``MempalaceConfig().entity_languages`` to match the
locales used at ``mempalace init`` time.

#### `extract_people_from_query`

```python
def extract_people_from_query(self, query: str) -> list
```

Extract known person names from a query string.
Returns list of canonical names found.

#### `extract_unknown_candidates`

```python
def extract_unknown_candidates(self, query: str) -> list
```

Find capitalized words in query that aren't in registry or common words.
These are candidates for the caller to resolve — the registry itself
never looks anything up off the machine.

#### `summary`

```python
def summary(self) -> str
```
