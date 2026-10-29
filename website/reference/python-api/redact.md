# `mempalace.redact`

Source: [`mempalace/redact.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/redact.py)

Secrets never reach a drawer: every write replaces them with a placeholder first.

A session transcript can print a credential: a shell expansion, an ``env``
dump, a config file read for debugging. Mined as is, the value lands in a
drawer that any agent can search, and in the private collection it would
still be a stored copy (#596). So every backend collection's ``add``,
``upsert`` and ``update`` pass their documents and the string values of their
metadata through :func:`redact` before the backend sees them; see
``BaseCollection.__init_subclass__``. A secret becomes
``[redacted &lt;kind> sha256:&lt;first 8 hex>]``. The short hash lets someone who
already holds a candidate check it against the drawer (a vault item, a leak
report); for a provider token it recovers nothing. Existing placeholders are
never matched again, so a write is redacted the same way however many layers
pass it through.

Patterns are regular expressions: :data:`DEFAULT_PATTERNS`, keyed by kind,
plus any listed under ``secret_patterns`` in the palace config (kind
``custom``). They match a credential's shape, never a value, so the config
never holds one. Every write checks the patterns, even one with nothing to
scan: a config that cannot be read, or a pattern that does not compile,
raises :class:`SecretPatternsUnavailable` and the write fails rather than
store text unscanned.

Known limits. Shapes catch tokens with a recognisable prefix; a bare
password or a provider without one passes through. Embeddings passed in by
the caller were computed from the text before redaction. Configured patterns
are trusted like the rest of the config: one with nested quantifiers can
backtrack badly on long text, so keep them to a prefix and a character class.
Metadata keys are not scanned, only values. A configured pattern is scanned
inside a named group, so it must not use numbered group references, must scope
any inline flag (``(?i:…)``, not ``(?i)``), and must not name a group
starting ``_mpr``; one that breaks these fails closed.

## Classes

### `class SecretPatternsUnavailable(RuntimeError)`

The configured secret patterns could not be read or compiled. Writing without them could
store a credential they would have caught, so the write stops instead.

## Functions

### `patterns`

```python
def patterns() -> List[Pass]
```

The compiled patterns, re-read only when the config file changes.

### `redact`

```python
def redact(text: str) -> str
```

``text`` with every secret-shaped run replaced by its placeholder.

### `redact_documents`

```python
def redact_documents(documents)
```

Documents as a write passes them (a list, a single string, or None), redacted.

### `redact_metadatas`

```python
def redact_metadatas(metadatas)
```

Metadata as a write passes it (a list of dicts, one dict, or None), its string values
redacted. The caller's dicts are never changed in place.
