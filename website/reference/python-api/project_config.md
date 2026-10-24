# `mempalace.project_config`

Source: [`mempalace/project_config.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/project_config.py)

Project config file resolution, shared by the miner and ``mempalace init``.

Kept free of heavy imports: ``room_detector_local`` uses it at import time,
and the miner pulls in the storage backends.

## Functions

### `find_project_config`

```python
def find_project_config(project_dir) -> Optional[Path]
```

Return the project config file for ``project_dir``, or None.

Checks :data:`PROJECT_CONFIG_FILENAMES` in order. ``is_file()`` rather
than ``exists()``: the latter is true for a FIFO, and opening one would
block in the kernel until a writer appears, so a config that is not a
regular file is treated as absent. When more than one candidate exists,
the first one is used and the rest are named on stderr so the shadowed
file does not go silently unread.
