# `mempalace.dream.keys`

Source: [`mempalace/dream/keys.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/dream/keys.py)

Keys for a dream run, read quietly from env files instead of systemd ``EnvironmentFile=``.

systemd logs a line of an ``EnvironmentFile=`` it cannot parse, in full, so a secrets file must never be one
(2026-11-14). ``--key-file`` names such a file (0600, ``NAME=value`` lines); the run reads only the names it needs
from it, and only when the environment does not already set them.

## Functions

### `read_var`

```python
def read_var(path: Path, name: str) -> str
```

One ``NAME=value`` (or ``export NAME=value``) from an env file; "" when absent or unreadable.

### `load`

```python
def load(files, names = NAMES, environ = None) -> list
```

Set each of ``names`` the environment lacks from the first file that has it. Returns the names set (never
the values).
