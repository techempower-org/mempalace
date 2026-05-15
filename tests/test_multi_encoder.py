"""Tests for mempalace.multi_encoder — env parsing + fused_query glue.

These tests stub the encoder + collection so they run without
SentenceTransformer or chromadb. The end-to-end probe-set
evaluation is in ``scripts/eval_multi_encoder_rrf.py``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable

import pytest

from mempalace import multi_encoder as mc


@contextmanager
def _env(**overrides: str):
    """Set env vars for the duration of a block. None deletes."""
    saved: dict[str, str | None] = {}
    try:
        for key, value in overrides.items():
            saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


def test_is_enabled_default_off():
    with _env(PALACE_USE_MULTI_ENCODER_RRF=None):
        assert mc.is_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_is_enabled_truthy_strings(val: str):
    with _env(PALACE_USE_MULTI_ENCODER_RRF=val):
        assert mc.is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_is_enabled_falsy_strings(val: str):
    with _env(PALACE_USE_MULTI_ENCODER_RRF=val):
        assert mc.is_enabled() is False


def test_load_roster_default_when_unset():
    with _env(PALACE_RRF_ENCODERS=None, PALACE_RRF_ENCODER_PATHS=None, PALACE_RRF_PALACES=None):
        roster = mc.load_roster()
    assert len(roster) == 1
    assert roster[0].name == "default"
    assert roster[0].model_path is None
    assert roster[0].palace_path is None


def test_load_roster_parses_names_paths_palaces():
    with _env(
        PALACE_RRF_ENCODERS="default,ft-code-1000",
        PALACE_RRF_ENCODER_PATHS="ft-code-1000=/models/ft1000",
        PALACE_RRF_PALACES="default=/palaces/default,ft-code-1000=/palaces/ft1000",
    ):
        roster = mc.load_roster()
    assert [s.name for s in roster] == ["default", "ft-code-1000"]
    assert roster[0].palace_path == "/palaces/default"
    assert roster[1].model_path == "/models/ft1000"
    assert roster[1].palace_path == "/palaces/ft1000"


def test_load_roster_drops_non_default_without_path():
    """A non-default encoder needs a model path; missing entry → skipped."""
    with _env(
        PALACE_RRF_ENCODERS="default,ft-code-1000",
        PALACE_RRF_ENCODER_PATHS="",
        PALACE_RRF_PALACES=None,
    ):
        roster = mc.load_roster()
    assert [s.name for s in roster] == ["default"]


def test_parse_kv_list_handles_whitespace_and_empty():
    parsed = mc._parse_kv_list("  a = 1 ,, b=two , c=  ")
    assert parsed == {"a": "1", "b": "two", "c": ""}


def test_parse_kv_list_keeps_value_with_equals_signs():
    parsed = mc._parse_kv_list("path=C:=weird, other=fine")
    assert parsed["path"] == "C:=weird"
    assert parsed["other"] == "fine"


def test_parse_kv_list_drops_malformed_no_equals(caplog):
    parsed = mc._parse_kv_list("good=ok,broken-no-equals,also=ok")
    assert parsed == {"good": "ok", "also": "ok"}


# ── fused_query — stubbed encoder + collection ─────────────────────────


class _FakeCollection:
    """Mimics chromadb's collection.query return shape."""

    def __init__(self, hits_by_query_vec: dict[tuple, list[dict]]):
        # hits_by_query_vec keys are tuple(query_vec); value is the
        # ranked list of hit dicts {id, document, metadata, distance}.
        self._hits_by_query_vec = hits_by_query_vec
        self.calls: list[dict] = []

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        include: list[str],
        where: dict | None = None,
    ) -> dict:
        self.calls.append(
            {"query_embeddings": query_embeddings, "n_results": n_results, "where": where}
        )
        vec = tuple(query_embeddings[0])
        ranked = self._hits_by_query_vec.get(vec, [])[:n_results]
        return {
            "ids": [[h["id"] for h in ranked]],
            "documents": [[h["document"] for h in ranked]],
            "metadatas": [[h["metadata"] for h in ranked]],
            "distances": [[h["distance"] for h in ranked]],
        }


def _hit(id_: str, source: str, chunk_index: int, distance: float, doc: str = "") -> dict:
    return {
        "id": id_,
        "document": doc or f"doc-{id_}",
        "metadata": {"source_file": source, "chunk_index": chunk_index},
        "distance": distance,
    }


def _stub_encoder(name: str, query_vec: list[float]) -> Callable[[str], list[float]]:
    """Build a one-shot encoder callable that ignores text and returns query_vec."""

    def encode(_text: str) -> list[float]:
        return list(query_vec)

    encode.encoder_name = name  # type: ignore[attr-defined]
    return encode


def test_fused_query_default_only_passthrough(monkeypatch):
    """With only the default encoder, fused_query returns its single ranked list."""
    mc.reset_encoder_cache()

    fake = _FakeCollection(
        {
            (0.1, 0.2): [
                _hit("D1", "fileA.py", 0, 0.10),
                _hit("D2", "fileB.py", 0, 0.20),
                _hit("D3", "fileC.py", 0, 0.30),
            ]
        }
    )

    def fake_getter(_palace: str) -> Any:
        return fake

    monkeypatch.setattr(mc, "get_encoder", lambda spec: _stub_encoder(spec.name, [0.1, 0.2]))

    with _env(PALACE_RRF_ENCODERS=None):
        result = mc.fused_query(
            query="how do we wire JWT auth",
            palace_path="/dev/null",
            n_results=3,
            collection_getter=fake_getter,
        )

    docs = result["documents"][0]
    assert docs == ["doc-D1", "doc-D2", "doc-D3"]
    assert result["ids"][0] == ["D1", "D2", "D3"]
    assert len(fake.calls) == 1


def test_fused_query_two_encoders_rrf_promotes_consensus(monkeypatch):
    """When two encoders both rank the same doc highly, it wins under RRF."""
    mc.reset_encoder_cache()

    # Encoder A produces query_vec=[1.0], its palace returns D2 first then D1.
    palace_a = _FakeCollection(
        {
            (1.0,): [
                _hit("D2", "fileB.py", 0, 0.10),
                _hit("D1", "fileA.py", 0, 0.20),
                _hit("D3", "fileC.py", 0, 0.30),
            ]
        }
    )
    # Encoder B produces query_vec=[2.0], its palace returns D1 first then D2.
    palace_b = _FakeCollection(
        {
            (2.0,): [
                _hit("D1", "fileA.py", 0, 0.10),
                _hit("D2", "fileB.py", 0, 0.20),
                _hit("D4", "fileD.py", 0, 0.30),
            ]
        }
    )
    palaces = {"/p/a": palace_a, "/p/b": palace_b}

    def fake_getter(palace_path: str) -> Any:
        return palaces[palace_path]

    def fake_get_encoder(spec):
        return _stub_encoder(spec.name, [1.0] if spec.name == "default" else [2.0])

    monkeypatch.setattr(mc, "get_encoder", fake_get_encoder)

    with _env(
        PALACE_RRF_ENCODERS="default,ft-code-1000",
        PALACE_RRF_ENCODER_PATHS="ft-code-1000=/dummy/model",
        PALACE_RRF_PALACES="default=/p/a,ft-code-1000=/p/b",
    ):
        result = mc.fused_query(
            query="…",
            palace_path="/p/fallback-unused",
            n_results=3,
            collection_getter=fake_getter,
        )

    # D1 and D2 each rank 1 in one list and 2 in the other → equal RRF score.
    # Stable sort: first-seen wins. Encoder A is processed first; D2 lands first
    # in its list, so D2 is seen before D1 → D2 should rank first or tie.
    docs = result["documents"][0]
    assert set(docs[:2]) == {"doc-D1", "doc-D2"}
    # D3 and D4 each only appear in one list at rank 3 → tied at the bottom.
    assert "doc-D3" in docs or "doc-D4" in docs


def test_fused_query_collapses_duplicates_by_source_and_chunk(monkeypatch):
    """The same (source_file, chunk_index) in two palaces becomes one fused hit."""
    mc.reset_encoder_cache()

    palace_a = _FakeCollection(
        {(1.0,): [_hit("D1a", "shared.py", 0, 0.05), _hit("D2", "other.py", 0, 0.10)]}
    )
    palace_b = _FakeCollection(
        {(2.0,): [_hit("D1b", "shared.py", 0, 0.07), _hit("D3", "third.py", 0, 0.12)]}
    )
    palaces = {"/p/a": palace_a, "/p/b": palace_b}

    monkeypatch.setattr(
        mc,
        "get_encoder",
        lambda spec: _stub_encoder(spec.name, [1.0] if spec.name == "default" else [2.0]),
    )

    with _env(
        PALACE_RRF_ENCODERS="default,ft-code-1000",
        PALACE_RRF_ENCODER_PATHS="ft-code-1000=/dummy",
        PALACE_RRF_PALACES="default=/p/a,ft-code-1000=/p/b",
    ):
        result = mc.fused_query(
            query="…",
            palace_path="/p/fallback",
            n_results=5,
            collection_getter=lambda p: palaces[p],
        )

    docs = result["documents"][0]
    # shared.py rank 1 in both lists → top.
    assert docs[0] == "doc-D1a"
    # Only 3 distinct logical hits (shared.py collapsed; other.py; third.py).
    assert len(docs) == 3


def test_fused_query_skips_encoder_when_collection_unavailable(monkeypatch, caplog):
    """One bad encoder palace doesn't sink the whole query."""
    mc.reset_encoder_cache()

    good = _FakeCollection({(1.0,): [_hit("D1", "f.py", 0, 0.1)]})

    def getter(palace_path: str) -> Any:
        if palace_path == "/p/bad":
            raise RuntimeError("collection vanished")
        return good

    monkeypatch.setattr(
        mc,
        "get_encoder",
        lambda spec: _stub_encoder(spec.name, [1.0] if spec.name == "default" else [2.0]),
    )

    with _env(
        PALACE_RRF_ENCODERS="default,ft-code-1000",
        PALACE_RRF_ENCODER_PATHS="ft-code-1000=/dummy",
        PALACE_RRF_PALACES="default=/p/good,ft-code-1000=/p/bad",
    ):
        result = mc.fused_query(
            query="…",
            palace_path="/p/fallback",
            n_results=3,
            collection_getter=getter,
        )

    # Default encoder result still surfaces.
    assert result["documents"][0] == ["doc-D1"]


def test_fused_query_all_encoders_fail_returns_empty(monkeypatch):
    mc.reset_encoder_cache()

    def getter(_palace: str) -> Any:
        raise RuntimeError("nope")

    monkeypatch.setattr(mc, "get_encoder", lambda spec: _stub_encoder(spec.name, [1.0]))

    with _env(PALACE_RRF_ENCODERS=None):
        result = mc.fused_query(
            query="…",
            palace_path="/p/fallback",
            n_results=3,
            collection_getter=getter,
        )

    assert result == {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }


def test_overfetch_respected(monkeypatch):
    """Per-encoder pull size is n_results * overfetch."""
    mc.reset_encoder_cache()

    palace_a = _FakeCollection({(1.0,): [_hit(f"D{i}", "f.py", i, 0.1) for i in range(50)]})

    monkeypatch.setattr(mc, "get_encoder", lambda spec: _stub_encoder(spec.name, [1.0]))

    with _env(PALACE_RRF_ENCODERS=None, PALACE_RRF_OVERFETCH="5"):
        mc.fused_query(
            query="…",
            palace_path="/p/a",
            n_results=2,
            collection_getter=lambda _p: palace_a,
        )
    # n_results=2, overfetch=5 → per-encoder n_results=10.
    assert palace_a.calls[0]["n_results"] == 10


def test_invalid_env_overfetch_falls_back(monkeypatch):
    mc.reset_encoder_cache()
    palace_a = _FakeCollection({(1.0,): [_hit("D1", "f.py", 0, 0.1)]})
    monkeypatch.setattr(mc, "get_encoder", lambda spec: _stub_encoder(spec.name, [1.0]))

    with _env(PALACE_RRF_OVERFETCH="garbage"):
        mc.fused_query(
            query="…",
            palace_path="/p/a",
            n_results=2,
            collection_getter=lambda _p: palace_a,
        )
    # Default overfetch is 3 → per-encoder pull = 6.
    assert palace_a.calls[0]["n_results"] == 6


# ── End-to-end via search_memories ────────────────────────────────────


def test_search_memories_invokes_fused_query_when_env_set(
    monkeypatch, palace_path, seeded_collection
):
    """When PALACE_USE_MULTI_ENCODER_RRF=1, search_memories routes through fused_query."""
    from mempalace import multi_encoder as _mc

    _mc.reset_encoder_cache()

    captured: dict = {}
    real_fused = _mc.fused_query

    def spy(*args, **kwargs):
        captured["called"] = True
        captured["query"] = kwargs.get("query") if "query" in kwargs else args[0]
        return real_fused(*args, **kwargs)

    monkeypatch.setattr(_mc, "fused_query", spy)

    with _env(
        PALACE_USE_MULTI_ENCODER_RRF="1",
        PALACE_RRF_ENCODERS=None,
        PALACE_RRF_ENCODER_PATHS=None,
        PALACE_RRF_PALACES=None,
    ):
        from mempalace.searcher import search_memories

        result = search_memories("JWT authentication", palace_path)

    assert captured.get("called") is True
    assert captured.get("query") == "JWT authentication"
    # With only the default encoder against the single seeded palace, the
    # fused result should be equivalent to a single-encoder query and
    # return non-empty hits.
    assert result.get("results"), f"expected hits, got {result}"


def test_search_memories_skips_fused_query_when_env_unset(
    monkeypatch, palace_path, seeded_collection
):
    """Default path does NOT call fused_query — proves the feature is opt-in."""
    from mempalace import multi_encoder as _mc

    called = {"n": 0}

    def spy(*args, **kwargs):
        called["n"] += 1
        return _mc.fused_query(*args, **kwargs)

    monkeypatch.setattr(_mc, "fused_query", spy)

    with _env(PALACE_USE_MULTI_ENCODER_RRF=None):
        from mempalace.searcher import search_memories

        search_memories("JWT authentication", palace_path)

    assert called["n"] == 0
