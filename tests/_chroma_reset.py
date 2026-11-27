"""Test-suite helper: stop every cached chromadb System, then drop the cache.

``SharedSystemClient.clear_system_cache()`` replaces chromadb's process-global
System and refcount maps without calling ``System.stop()`` (see
``mempalace.backends.chroma._clear_chroma_system_cache``). A System that a test
reached through a raw ``chromadb.PersistentClient`` and never closed keeps its
rust-side handles (sqlite files, HNSW segments, an epoll/eventfd/socket set per
runtime) until a full garbage-collection pass happens to reach it. Measured with
chromadb 1.5.9: ten such clients held +102 fds after the clear and +2 after
stop-then-clear. Across the suite they piled up past a 1024 soft fd limit
(tests/test_sync.py alone left 1,221 open) and the run ended in an
"Errno 24: Too many open files" cascade.
"""

from __future__ import annotations


def stop_and_clear_chroma_systems() -> None:
    """Stop every System in chromadb's shared cache, then clear the cache.

    Call it only after the clients this process still means to use (the
    backend layer's) have been closed: a stopped System cannot serve again.
    No-op if chromadb or its internals are absent.
    """
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
    except ImportError:
        return
    systems = getattr(SharedSystemClient, "_identifier_to_system", None) or {}
    for system in list(systems.values()):
        try:
            system.stop()
        except Exception:  # noqa: BLE001 -- a test-teardown helper: one bad System must not stop the rest
            pass
    clear = getattr(SharedSystemClient, "clear_system_cache", None)
    if callable(clear):
        clear()
