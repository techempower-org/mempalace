"""Source manifest (#575): a re-mine no longer re-stamps unchanged chunks.

On familiar (2026-10-21) every re-mine of a live transcript refreshed each unchanged chunk's
``source_mtime``/``chunk_total`` with a non-HOT UPDATE that wrote all eight drawer indexes,
HNSW included: 33k rows per pass of one transcript, most of the NVMe load. With a manifest,
completeness lives in one row per (source_file, extract_mode), written last in each pass.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from unittest.mock import patch

import pytest

import mempalace.convo_miner as convo_miner
from mempalace.convo_miner import _unchanged_to_refresh
from mempalace.palace import CONVO_CHUNKER_VERSION, NORMALIZE_VERSION
from mempalace.palace import (
    _manifest_mtime,
    _source_manifest,
    file_already_mined,
    prefetch_mined_set,
)


# --- unit: the helpers ----------------------------------------------------------------------


class _ManifestCol:
    supports_source_manifest = True

    def __init__(self, rows=None, fail=False):
        self.rows = rows or {}
        self.fail = fail
        self.calls = []

    def manifest_get(self, extract_mode, source_files=None):
        self.calls.append((extract_mode, source_files))
        if self.fail:
            raise RuntimeError("down")
        return {k: v for k, v in self.rows.items() if source_files is None or k in source_files}


def _item(i):
    return ({"content": f"c{i}", "chunk_index": i}, "room", f"id{i}")


def test_with_a_manifest_only_chunk_0_is_refreshed():
    use, items = _unchanged_to_refresh(
        _ManifestCol(), [_item(0), _item(1), _item(2)], "exchange", "h"
    )
    assert use is True and [i[2] for i in items] == ["id0"]


def test_with_a_manifest_and_no_content_hash_nothing_is_refreshed():
    assert _unchanged_to_refresh(_ManifestCol(), [_item(0), _item(1)], "exchange", None) == (
        True,
        [],
    )


def test_without_the_capability_every_unchanged_drawer_is_refreshed():
    from unittest.mock import MagicMock

    unchanged = [_item(0), _item(1)]
    assert _unchanged_to_refresh(MagicMock(), unchanged, "exchange", "h") == (False, unchanged)
    assert _unchanged_to_refresh(_ManifestCol(), unchanged, None, "h") == (False, unchanged)


def test_manifest_reads_fail_toward_the_drawer_groups():
    assert _source_manifest(_ManifestCol(fail=True), "exchange", ["a"]) == {}
    assert _source_manifest(_ManifestCol(rows={"a": {}}), None, ["a"]) == {}


def test_only_a_current_schema_row_with_an_mtime_counts():
    assert _manifest_mtime({"normalize_version": NORMALIZE_VERSION, "source_mtime": 5.0}) == 5.0
    assert (
        _manifest_mtime({"normalize_version": NORMALIZE_VERSION - 1, "source_mtime": 5.0}) is None
    )
    assert _manifest_mtime({"normalize_version": NORMALIZE_VERSION, "source_mtime": None}) is None
    assert _manifest_mtime(None) is None


def test_prefetch_reads_a_generator_of_source_files_twice():
    """The scoped scan and the manifest overlay both iterate source_files."""

    class Col(_ManifestCol):
        def get(self, **kwargs):
            return {"ids": [], "metadatas": []}

        def count(self):
            return 0

    col = Col(rows={"/t/a.jsonl": {"source_mtime": 7.0, "normalize_version": NORMALIZE_VERSION,
                                   "convo_chunker_version": CONVO_CHUNKER_VERSION}})
    mined = prefetch_mined_set(col, "exchange", source_files=(p for p in ["/t/a.jsonl"]))
    assert mined == {"/t/a.jsonl": 7.0}


@pytest.mark.parametrize("version, mode, current", [(None, "exchange", False), (CONVO_CHUNKER_VERSION - 1, "exchange", False),
                                                   (CONVO_CHUNKER_VERSION, "exchange", True), (None, "general", True)])
def test_a_manifest_row_from_an_older_chunker_is_stale_in_the_exchange_scope(version, mode, current):
    """Upstream #2591 re-mines conversation files whose drawers came from an older exchange chunker. The manifest
    (#576) must agree, or its row would keep saying "current" and block exactly that re-mine."""

    class Col(_ManifestCol):
        def get(self, **kwargs):
            return {"ids": [], "metadatas": []}

        def count(self):
            return 0

    row = {"source_mtime": 7.0, "normalize_version": NORMALIZE_VERSION}
    if version is not None:
        row["convo_chunker_version"] = version
    mined = prefetch_mined_set(Col(rows={"/t/a.jsonl": row}), mode, source_files=["/t/a.jsonl"])
    assert mined == ({"/t/a.jsonl": 7.0} if current else {})


# --- real postgres: the miner end to end ----------------------------------------------------

DSN = os.environ.get("TEST_POSTGRES_DSN")
pgmark = pytest.mark.skipif(DSN is None, reason="set TEST_POSTGRES_DSN to run against postgres")


@pytest.fixture()
def pg(tmp_path, monkeypatch):
    from mempalace.backends import PalaceRef, get_backend
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    name = f"manifest_{uuid.uuid4().hex[:8]}"
    inner = get_backend("postgres").get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name=name,
        create=True,
        options={"dsn": DSN},
    )
    monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
    monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")
    yield EmbeddingCollection(inner), inner, tmp_path
    import psycopg
    from psycopg import sql

    with psycopg.connect(DSN, autocommit=True) as conn:
        for t in (name, f"{name}_source_manifest"):
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(t)))


def _chunks(n):
    return [{"content": f"exchange {i}: " + "words " * 30, "chunk_index": i} for i in range(n)]


def _touch(path, t):
    os.utime(path, (t, t))


@pgmark
def test_a_re_mine_updates_only_chunk_0_and_moves_the_manifest(pg):
    col, inner, tmp = pg
    src = str(tmp / "session.jsonl")
    open(src, "w").write("x")
    _touch(src, time.time() - 100)

    added, _, skipped = convo_miner._file_chunks_locked(
        col, src, _chunks(5), "w", "r", "agent", "exchange", content_hash="h1"
    )
    assert (added, skipped) == (5, False)
    row = inner.manifest_get("exchange", [src])[src]
    assert row["chunk_total"] == 5 and row["normalize_version"] == NORMALIZE_VERSION
    assert abs(row["source_mtime"] - os.path.getmtime(src)) < 0.001
    assert file_already_mined(col, src, check_mtime=True, extract_mode="exchange")

    # The session appends: same first five chunks, one new chunk, a newer mtime.
    _touch(src, time.time())
    assert not file_already_mined(col, src, check_mtime=True, extract_mode="exchange")
    updated = []
    real_update = inner.update

    def spy(*, ids, **kw):
        updated.extend(ids)
        return real_update(ids=ids, **kw)

    with patch.object(inner, "update", side_effect=spy):
        added, _, skipped = convo_miner._file_chunks_locked(
            col, src, _chunks(6), "w", "r", "agent", "exchange", content_hash="h2"
        )
    assert (added, skipped) == (1, False)
    assert len(updated) == 1, updated  # only chunk 0 (content_hash), not 5 unchanged rows
    row = inner.manifest_get("exchange", [src])[src]
    assert row["chunk_total"] == 6
    assert abs(row["source_mtime"] - os.path.getmtime(src)) < 0.001
    assert file_already_mined(col, src, check_mtime=True, extract_mode="exchange")
    full = prefetch_mined_set(col, "exchange")
    scoped = prefetch_mined_set(col, "exchange", source_files=[src])
    assert abs(full[src] - os.path.getmtime(src)) < 0.001
    assert abs(scoped[src] - os.path.getmtime(src)) < 0.001


@pgmark
def test_a_pass_that_fails_mid_upsert_leaves_the_manifest_stale(pg):
    col, inner, tmp = pg
    src = str(tmp / "session.jsonl")
    open(src, "w").write("x")
    _touch(src, time.time() - 100)
    convo_miner._file_chunks_locked(col, src, _chunks(3), "w", "r", "agent", "exchange")
    before = inner.manifest_get("exchange", [src])[src]["source_mtime"]

    _touch(src, time.time())
    with patch.object(inner, "upsert", side_effect=RuntimeError("disk gone")):
        with pytest.raises(RuntimeError):
            convo_miner._file_chunks_locked(col, src, _chunks(4), "w", "r", "agent", "exchange")
    assert inner.manifest_get("exchange", [src])[src]["source_mtime"] == before
    assert not file_already_mined(col, src, check_mtime=True, extract_mode="exchange")
    assert abs(prefetch_mined_set(col, "exchange", source_files=[src])[src] - before) < 0.001


@pgmark
def test_without_the_capability_the_old_refresh_runs(pg, monkeypatch):
    col, inner, tmp = pg
    monkeypatch.setattr(type(inner), "supports_source_manifest", False)
    src = str(tmp / "session.jsonl")
    open(src, "w").write("x")
    _touch(src, time.time() - 100)
    convo_miner._file_chunks_locked(col, src, _chunks(4), "w", "r", "agent", "exchange")
    _touch(src, time.time())
    updated = []
    real_update = inner.update

    def spy(*, ids, **kw):
        updated.extend(ids)
        return real_update(ids=ids, **kw)

    with patch.object(inner, "update", side_effect=spy):
        convo_miner._file_chunks_locked(col, src, _chunks(5), "w", "r", "agent", "exchange")
    assert len(updated) == 4  # every unchanged drawer re-stamped, as before
    assert file_already_mined(col, src, check_mtime=True, extract_mode="exchange")
