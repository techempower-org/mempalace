# `mempalace._proc`

Source: [`mempalace/_proc.py`](https://github.com/techempower-org/mempalace/blob/main/mempalace/_proc.py)

Process liveness.

One probe so the daemon, the hook mine slots, and the server registry
agree on whether a pid is still running.

Semantics:
- A value that is not a positive ``int`` is not alive. ``bool`` is rejected
  (it is a subclass of ``int`` but never a pid).
- POSIX: ``os.kill(pid, 0)``. ``ProcessLookupError`` and any other
  ``OSError`` mean the process is gone. ``PermissionError`` means the
  process exists and this user cannot signal it, so it is alive.
- Windows: ``WaitForSingleObject`` on a ``SYNCHRONIZE`` handle. Never
  ``os.kill`` — signal 0 is ``CTRL_C_EVENT`` and can interrupt this
  process. Access denied means the process exists. If the Win32 probe
  itself fails, assume alive rather than discard a live holder.

## Functions

### `pid_alive`

```python
def pid_alive(pid: int) -> bool
```

Return whether ``pid`` is a running process. See the module docstring.
