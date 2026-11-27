"""The suite's per-test chromadb reset releases raw clients' file descriptors without waiting for gc.

A clear of chromadb's System cache alone leaves every never-closed
``PersistentClient``'s handles open until a full gc pass (see
``tests/_chroma_reset.py``); the suite then ran past a 1024 soft fd limit.
"""

from __future__ import annotations

import gc
import os

import pytest

from _chroma_reset import stop_and_clear_chroma_systems

pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc/self/fd"), reason="counts fds through /proc"
)


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_the_reset_releases_raw_clients_without_a_gc_pass(tmp_path):
    import chromadb

    stop_and_clear_chroma_systems()
    gc.collect()
    gc.disable()  # as between the rare full passes of a long run
    try:
        start = _fds()
        for i in range(5):
            client = chromadb.PersistentClient(path=str(tmp_path / f"p{i}"))
            col = client.get_or_create_collection("fd_probe")
            col.add(ids=["a"], documents=["hello"], embeddings=[[0.1, 0.2, 0.3]])
            del col, client
        opened = _fds()
        stop_and_clear_chroma_systems()
        after = _fds()
    finally:
        gc.enable()
    # the instrument sees the clients' handles (measured: ~10 per client)
    assert opened - start >= 20, (start, opened)
    # and the reset gives at least three quarters of them back with no gc pass. Measured: ten clients alone
    # +100 -> +2; five inside the full suite +50 -> +6 (other tests' state); a clear alone gives back none.
    assert after - start <= (opened - start) // 4, (start, opened, after)
