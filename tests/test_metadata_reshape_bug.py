"""Reproduction tests for the metadata reshape bug (#32).

These tests demonstrate exactly when chromadb's ``validate_metadata``
fires the ``Expected metadata to be a non-empty dict, got 0 metadata
attributes`` ValueError, and verify that the chokepoint sanitizer in
``ChromaCollection.add/upsert`` (commit f499814) prevents it across the
relevant input shapes.

Findings recorded in
``docs/investigations/metadata-reshape-root-cause.md``.
"""

from __future__ import annotations

import numpy as np
import pytest

from mempalace.backends.chroma import ChromaBackend, ChromaCollection

DIM = 8


def _emb(seed: int = 0) -> list[float]:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(DIM, dtype=np.float32).tolist()


@pytest.fixture
def backend(palace_path):
    yield ChromaBackend()


@pytest.fixture
def fresh_collection(backend, palace_path):
    """A freshly created collection on disk, without an embedding function.

    Skipping the embedding function lets the test control vectors
    directly and isolates the metadata path from re-embedding side
    effects.
    """
    client = backend._client(palace_path)
    name = "repro_drawers"
    try:
        client.delete_collection(name)
    except Exception:  # noqa: BLE001
        pass
    raw = client.create_collection(name, metadata={"hnsw:space": "cosine"})
    yield ChromaCollection(raw, palace_path=palace_path)
    try:
        client.delete_collection(name)
    except Exception:  # noqa: BLE001
        pass


# ────────────────────────────────────────────────────────────────────
# 1. Confirm the upstream behaviour: chromadb's validate_metadata
# rejects ``{}`` but accepts ``None`` entries in the list.
# ────────────────────────────────────────────────────────────────────


def test_chromadb_validate_metadata_rejects_empty_dict():
    from chromadb.api.types import validate_metadatas

    with pytest.raises(ValueError, match="non-empty dict"):
        validate_metadatas([{"wing": "x"}, {}])


def test_chromadb_validate_metadata_accepts_none_entries():
    from chromadb.api.types import validate_metadatas

    # ``None`` entries pass validate_metadata: see chromadb 1.5.x
    # types.py:1068. Only an ALL-None ``metadatas=None`` list parameter
    # is short-circuited at the call site (CollectionCommon.py:228);
    # a None *inside* a list is fine.
    out = validate_metadatas([{"wing": "x"}, None])
    assert out == [{"wing": "x"}, None]


# ────────────────────────────────────────────────────────────────────
# 2. ChromaCollection.add: empty-dict inputs must NOT crash.
# This is the chokepoint sanitizer behaviour (commit f499814).
# ────────────────────────────────────────────────────────────────────


def test_chromacollection_add_coerces_empty_dict(fresh_collection):
    fresh_collection.add(
        ids=["a", "b"],
        documents=["doc-a", "doc-b"],
        metadatas=[{"wing": "x", "room": "y"}, {}],
        embeddings=[_emb(1), _emb(2)],
    )
    # The empty-dict entry must have been coerced to the sentinel and
    # be discoverable.
    res = fresh_collection._collection.get(ids=["b"], include=["metadatas"])
    assert res["metadatas"][0] == {"_repaired_empty_meta": True}


def test_chromacollection_add_coerces_none_entry(fresh_collection):
    fresh_collection.add(
        ids=["a", "b"],
        documents=["doc-a", "doc-b"],
        metadatas=[{"wing": "x", "room": "y"}, None],
        embeddings=[_emb(1), _emb(2)],
    )
    res = fresh_collection._collection.get(ids=["b"], include=["metadatas"])
    assert res["metadatas"][0] == {"_repaired_empty_meta": True}


def test_chromacollection_upsert_coerces_empty_dict(fresh_collection):
    fresh_collection.upsert(
        ids=["a", "b"],
        documents=["doc-a", "doc-b"],
        metadatas=[{"wing": "x", "room": "y"}, {}],
        embeddings=[_emb(1), _emb(2)],
    )
    res = fresh_collection._collection.get(ids=["b"], include=["metadatas"])
    assert res["metadatas"][0] == {"_repaired_empty_meta": True}


# ────────────────────────────────────────────────────────────────────
# 3. Mutation-after-sanitize hypothesis (H3 in issue #32):
#    Our sanitizer hands chromadb a fresh list; verify chromadb does
#    not iterate the *original* caller list (which a bug could mutate
#    to introduce empty dicts after sanitize ran).
# ────────────────────────────────────────────────────────────────────


def test_sanitizer_inner_dicts_do_not_alias_caller_dicts():
    """#32 root cause, now FIXED: the sanitizer used to return the caller's own
    inner dicts (``[m if (...) else sentinel for m in metadatas]``), so a
    mutation between sanitize and chromadb's ``validate_metadata`` (a recycling
    producer, a thread holding a reference) reached chromadb as an empty dict
    and raised ``ValueError: Expected metadata to be a non-empty dict``.

    The sanitizer now copies each entry (arrived with the 2026-10-22 upstream sync).
    This pins the fix: equal content, distinct objects, and a later caller-side
    mutation is not observed.
    """
    metas = [{"wing": "x", "room": "y"}, {}]
    sanitized = ChromaCollection._sanitize_metadatas_for_chromadb(metas)

    assert sanitized is not metas
    assert sanitized[0] == metas[0]
    assert sanitized[0] is not metas[0]

    metas[0].clear()
    assert sanitized[0] == {"wing": "x", "room": "y"}


def test_caller_clear_after_sanitize_no_longer_reaches_chromadb(fresh_collection):
    """End-to-end form of the fix: clearing the caller's dict after sanitize no
    longer hands chromadb an empty metadata dict, so the add succeeds."""
    metas = [{"wing": "x", "room": "y"}]
    sanitized = ChromaCollection._sanitize_metadatas_for_chromadb(metas)
    metas[0].clear()
    raw = fresh_collection._collection
    raw.add(
        ids=["x"],
        documents=["d"],
        embeddings=[_emb(1)],
        metadatas=sanitized,
    )
    assert raw.get(ids=["x"], include=["metadatas"])["metadatas"] == [{"wing": "x", "room": "y"}]


# ────────────────────────────────────────────────────────────────────
# 4. The full repair.py pipeline: corrupted metadata extracted from
# the source collection should survive rebuild.
# ────────────────────────────────────────────────────────────────────


def test_extract_drawers_sanitizes_none_and_empty(fresh_collection):
    """``_extract_drawers`` itself sanitizes before returning, so the
    rebuild loop never sees a None/{}-entry — the chokepoint
    sanitizer in ChromaCollection is the second line of defence."""
    from mempalace import repair

    # Insert one drawer using the raw underlying chromadb collection
    # (bypassing ChromaCollection.add) so we can deliberately store
    # ``None`` as the metadata for one row — simulating a historical
    # write before validate_metadata became strict.
    raw = fresh_collection._collection
    raw.upsert(
        ids=["good", "empty"],
        documents=["a", "b"],
        embeddings=[_emb(1), _emb(2)],
        metadatas=[{"wing": "x", "room": "y"}, None],
    )

    ids, docs, metas, embs = repair._extract_drawers(raw, total=2, batch_size=10)
    assert len(ids) == 2
    assert all(isinstance(m, dict) and len(m) > 0 for m in metas), metas

    # The None-metadata row must have been coerced to the sentinel.
    by_id = dict(zip(ids, metas))
    assert by_id["empty"] == {"_repaired_empty_meta": True}
    assert by_id["good"]["wing"] == "x"


# ────────────────────────────────────────────────────────────────────
# 5. Direct hypothesis test: can we trigger the original ValueError
# by bypassing our sanitizer entirely? This documents the upstream
# bug surface (what the chokepoint sanitizer protects against).
# ────────────────────────────────────────────────────────────────────


def test_raw_chromadb_add_with_empty_dict_raises(fresh_collection):
    """Sanity check: without our sanitizer, an empty-dict entry
    reaches chromadb's validator and raises ``ValueError``.

    This is the exact ValueError chain reported in #32:
        Collection.add → validate_insert_record_set
        → validate_metadatas → validate_metadata
    """
    raw = fresh_collection._collection
    with pytest.raises(ValueError, match="non-empty dict"):
        raw.add(
            ids=["x"],
            documents=["d"],
            embeddings=[_emb(1)],
            metadatas=[{}],
        )
