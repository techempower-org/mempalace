# `mempalace.sources.outline`

Source: [`mempalace/sources/outline.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/sources/outline.py)

Outline source adapter: every published document of the configured collections.

Reads an Outline wiki through its API::

    "outline_url": "https://outline.example",
    "outline_sources": [&#123;"collection": "Projects", "wing": "wiki"}]

``outline_sources`` is optional: without it every collection is read into wing
``outline_wiki``. The API token comes from ``MEMPALACE_OUTLINE_TOKEN`` in the
environment, never from the config, and only read methods are called.

Each document is one source, ``outline://&lt;team_id>/&lt;document_id>``: a short
header (collection, title, last update, URL) and the document's markdown,
verbatim, chunked like any text. A document that matches a sensitive marker
(``mempalace.sensitive``) is labelled ``sensitive`` as a whole, and core files it
into the private collection. Each configured collection read in full is a
snapshot scope (the whole workspace when every collection is read), so a document
deleted, archived or moved out of it, or a deleted collection, leaves nothing behind.

## Classes

### `class OutlineSourceAdapter(BaseSourceAdapter)`

Ingest an Outline wiki's published documents.

#### `__init__`

```python
def __init__(self, call: Optional[OutlineCall] = None, sources: Optional[List[dict]] = None, base_url: Optional[str] = None)
```

#### `snapshot_scopes`

```python
def snapshot_scopes(self) -> List[dict]
```

#### `ingest`

```python
def ingest(self, *, source: SourceRef, palace: PalaceContext) -> Iterator[DrawerRecord]
```

#### `describe_schema`

```python
def describe_schema(self) -> AdapterSchema
```

#### `source_summary`

```python
def source_summary(self, *, source: SourceRef) -> SourceSummary
```
