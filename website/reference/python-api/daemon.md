# `mempalace.daemon`

Source: [`mempalace/daemon.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/daemon.py)

Long-lived local daemon for queued MemPalace writes.

Daemon mode is strictly opt-in. The default CLI, hooks, and MCP paths still use
their direct execution behavior unless callers explicitly request daemon-backed
execution.

## Classes

### `class DaemonError(RuntimeError)`

Raised when daemon client operations fail.

### `class Job`

### `class QueueStore`

#### `__init__`

```python
def __init__(self, path: Path)
```

#### `prune_terminal`

```python
def prune_terminal(self, older_than_days: int = JOB_RETENTION_DAYS) -> int
```

Delete terminal (succeeded/failed/cancelled) jobs older than the
retention window.

Bounded growth for the queue DB, which holds verbatim payloads. Only
terminal jobs are eligible — queued/running jobs are never touched, so
a crash mid-prune cannot drop in-flight work (incremental-only). The
cutoff uses ``finished_at``; a terminal job is never re-examined by
recover_running, so deleting it is safe.

#### `recover_running`

```python
def recover_running(self) -> int
```

Re-queue jobs left ``running`` by a crashed/killed daemon.

Jobs that have already exhausted ``MAX_ATTEMPTS`` claims are dead-lettered
to ``failed`` instead of being retried — non-idempotent kinds (diary_write
derives its entry_id from wall-clock time) would otherwise duplicate
verbatim palace content on every restart, violating the incremental-only
principle. The last error_json is preserved for diagnostics.

#### `enqueue`

```python
def enqueue(self, kind: str, payload: dict[str, Any], *, dedupe_key: str | None = None, priority: int = 0) -> Job
```

#### `claim_next`

```python
def claim_next(self, *, exclude: set[str] | None = None) -> Job | None
```

#### `finish`

```python
def finish(self, job_id: str, *, state: str, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None, only_if_running: bool = False) -> Job
```

#### `defer`

```python
def defer(self, job_id: str, *, claimed_started_at: str | None = None, error: dict[str, Any] | None = None) -> Job
```

Return a job refused the palace write lock to ``queued``, unspent.

``mine_palace_lock`` wraps the palace write itself, so a refusal means
no drawer was filed and re-running cannot duplicate palace content.
That is what separates it from the failure ``MAX_ATTEMPTS`` guards -- a
daemon that died mid-execution, whose outcome is unknown and whose blind
retry would re-file verbatim content. Undoing ``claim_next``'s increment
keeps a palace that stays locked from spending the retry budget of work
that never landed.

``error`` records why the job went back to the queue, so a job parked
behind a lock is distinguishable from one merely awaiting its turn.
``claim_next`` clears it on the next claim, so the reason never outlives
the claim it describes.

The ``state = 'running'`` guard mirrors ``finish(only_if_running=True)``:
if shutdown already cancelled this job, deferring must not resurrect it.
``claimed_started_at`` narrows the update further, to the claim that was
actually refused: if the row was re-queued and re-claimed in the window
(recover_running on another daemon start), a late defer must not throw
away that newer claim by re-queuing -- and refunding -- work it does not
own. ``None`` skips the check.

#### `get`

```python
def get(self, job_id: str) -> Job
```

#### `list`

```python
def list(self, limit: int = 20) -> list[Job]
```

#### `counts`

```python
def counts(self) -> dict[str, int]
```

### `class DaemonRuntime`

#### `__init__`

```python
def __init__(self, palace_path: str, backend: str | None = None)
```

#### `start_worker`

```python
def start_worker(self) -> threading.Thread
```

#### `worker_alive`

```python
def worker_alive(self) -> bool
```

### `class DaemonClient`

#### `__init__`

```python
def __init__(self, palace_path: str)
```

#### `base_url`

```python
def base_url(self) -> str
```

#### `request`

```python
def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 5.0) -> dict[str, Any]
```

#### `health`

```python
def health(self, *, timeout: float = 5.0) -> dict[str, Any]
```

#### `submit`

```python
def submit(self, kind: str, payload: dict[str, Any], *, dedupe_key: str | None = None, priority: int = 0) -> dict[str, Any]
```

#### `get_job`

```python
def get_job(self, job_id: str) -> dict[str, Any]
```

#### `list_jobs`

```python
def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]
```

#### `wait`

```python
def wait(self, job_id: str, *, timeout: float = DEFAULT_WAIT_TIMEOUT, stop_on_lock_deferral: bool = False) -> dict[str, Any]
```

#### `shutdown`

```python
def shutdown(self) -> dict[str, Any]
```

## Functions

### `canonical_palace_path`

```python
def canonical_palace_path(path: str | None = None) -> str
```

### `palace_key`

```python
def palace_key(palace_path: str) -> str
```

### `state_root`

```python
def state_root() -> Path
```

### `state_dir`

```python
def state_dir(palace_path: str) -> Path
```

### `ensure_token`

```python
def ensure_token(palace_path: str) -> str
```

### `read_token`

```python
def read_token(palace_path: str) -> str
```

### `endpoint_path`

```python
def endpoint_path(palace_path: str) -> Path
```

### `pid_path`

```python
def pid_path(palace_path: str) -> Path
```

### `queue_path`

```python
def queue_path(palace_path: str) -> Path
```

### `job_deferred_by_lock`

```python
def job_deferred_by_lock(job: dict[str, Any]) -> bool
```

True when a job's last claim was refused the palace lock.

Such a job is deferred, not failed (#2014): it went back to the queue and
runs once the lock frees. That makes it non-terminal, so a caller blocking
until ``TERMINAL_STATES`` would wait out the holder -- which can be a
long-lived session. Interactive callers use this to report the parked job
instead of stranding the terminal.

Keyed on the reason ``defer`` records, which ``claim_next`` clears on the
next claim: the answer is about the claim that just ended, not a live probe
of the lock (the holder may already have exited during the backoff).

### `job_to_dict`

```python
def job_to_dict(job: Job, *, include_payload: bool = True) -> dict[str, Any]
```

### `run_server`

```python
def run_server(palace_path: str, *, backend: str | None = None, port: int = 0) -> None
```

### `get_client_if_running`

```python
def get_client_if_running(palace_path: str, *, health_timeout: float = 5.0) -> DaemonClient | None
```

### `start_daemon`

```python
def start_daemon(palace_path: str, *, backend: str | None = None, foreground: bool = False, timeout: float = 15.0) -> DaemonClient
```

### `ensure_client`

```python
def ensure_client(palace_path: str, *, backend: str | None = None, auto_start: bool = True) -> DaemonClient
```

### `submit_job`

```python
def submit_job(kind: str, payload: dict[str, Any], *, palace_path: str | None = None, backend: str | None = None, dedupe_key: str | None = None, priority: int = 0, wait: bool = True, auto_start: bool = False, timeout: float = DEFAULT_WAIT_TIMEOUT, stop_on_lock_deferral: bool = False) -> dict[str, Any]
```

### `stop_daemon`

```python
def stop_daemon(palace_path: str) -> bool
```

Ask the registered daemon to stop. True when a running daemon was asked.

When the health probe fails, the registration decides what happens: a dead
pid, or a pid reused by an unrelated process, is a stale registration and
is removed so the next ``daemon start`` works. A live daemon that does not
answer cannot be asked to stop over HTTP, so its registration is left in
place (#2442) and the error names the process to stop instead.

### `main`

```python
def main(argv: list[str] | None = None) -> None
```
