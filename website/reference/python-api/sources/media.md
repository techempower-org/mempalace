# `mempalace.sources.media`

Source: [`mempalace/sources/media.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/sources/media.py)

Local image, audio, and video reference adapter.

## Classes

### `class MediaAdapter(BaseSourceAdapter)`

Enumerate local media references without decoding their contents.

#### `describe_schema`

```python
def describe_schema(self) -> AdapterSchema
```

#### `source_summary`

```python
def source_summary(self, *, source: SourceRef) -> SourceSummary
```

#### `ingest`

```python
def ingest(self, *, source: SourceRef, palace) -> Iterator[object]
```
