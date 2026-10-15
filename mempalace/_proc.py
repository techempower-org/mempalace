"""Process liveness.

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
"""

from __future__ import annotations

import os

_WIN_SYNCHRONIZE = 0x00100000
_WIN_WAIT_TIMEOUT = 0x00000102
_WIN_ERROR_ACCESS_DENIED = 5


def _pid_alive_windows(pid: int) -> bool:
    """Win32 liveness probe with no console-control side effect."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(_WIN_SYNCHRONIZE, False, int(pid))
    if not handle:
        # Access denied: the process exists but isn't ours to open.
        # Any other error (invalid parameter / not found) means it's gone.
        return ctypes.get_last_error() == _WIN_ERROR_ACCESS_DENIED
    try:
        # A live process is not signalled, so a zero-timeout wait returns
        # WAIT_TIMEOUT. An exited process is signalled and returns WAIT_OBJECT_0.
        return kernel32.WaitForSingleObject(handle, 0) == _WIN_WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` is a running process. See the module docstring."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _pid_alive_windows(pid)
        except OSError:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
