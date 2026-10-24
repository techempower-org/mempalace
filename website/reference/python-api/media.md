# `mempalace.media`

Source: [`mempalace/media.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/media.py)

First-party local media asset references and vector storage.

## Classes

### `class MediaAssetError(RuntimeError)`

Raised when a media reference cannot safely be indexed.

### `class MediaAssetSetupError(MediaAssetError)`

A global provider or package problem that prevents all media indexing.

### `class MediaAsset`

A local media file reference. The media bytes stay at ``source_file``.

#### `descriptor`

```python
def descriptor(self) -> str
```

Return searchable descriptor text without captioning or copying bytes.

#### `metadata`

```python
def metadata(self) -> dict[str, str | int | float | bool]
```

Return flat scalar metadata accepted by every supported backend.

## Functions

### `media_asset_exists`

```python
def media_asset_exists(asset: MediaAsset | dict[str, Any]) -> bool
```

Check whether a stored reference still resolves to a local file.

### `stable_asset_id`

```python
def stable_asset_id(source_file: str | os.PathLike[str]) -> str
```

Build a repeatable ID from a resolved local path.

### `store_media_asset`

```python
def store_media_asset(asset: MediaAsset, palace_path: str, *, drawer_collection = None) -> int
```

Embed and idempotently upsert one media reference into the fixed assets collection.

### `asset_from_path`

```python
def asset_from_path(path: str | os.PathLike[str], *, source_root: str | os.PathLike[str], media_type: str, project: str, related_drawer_id: Optional[str] = None) -> MediaAsset
```

Construct flat asset metadata from a path already checked by the adapter.
