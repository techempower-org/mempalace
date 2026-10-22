# `mempalace.backends.rust_exact`

Source: [`mempalace/backends/rust_exact.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/backends/rust_exact.py)

Rust exact-vector backend for MemPalace.

High-performance native backend powered by crates/mempalace-core and PyO3.
Stores data in the same sqlite_exact.sqlite3 database as SQLiteExactBackend,
but uses a native contiguous vector index in Rust.
Memory consumption is 526 MB for 334k documents (vs 2,430 MB in pure Python)
and multi-core parallel queries execute in 7–11 ms.

## Classes

### `class RustExactCollection(SQLiteExactCollection)`

Collection wrapper that delegates vector scanning to mempalace_core_rs.

### `class RustExactBackend(SQLiteExactBackend)`

Backend factory for rust_exact.

#### `get_collection`

```python
def get_collection(self, *args, **kwargs) -> RustExactCollection
```

#### `detect`

```python
def detect(cls, path: str) -> bool
```

Native acceleration is selected explicitly, not a separate disk format.
