"""The postgres backend must embed through get_embedding_function() (#413).

``backends/postgres.py::_embed`` built its own
``chromadb.utils.embedding_functions.DefaultEmbeddingFunction()`` and never
consulted the resolver, so every embedding-layer improvement in the project
missed this backend. Three defects compounded in one eight-line function:

1. ``MEMPALACE_EMBEDDING_MODEL`` was ignored on both the write and query
   paths. Measured on two long-running installs: a configured remote
   embedding server served **9 texts in 18.3 hours** of near-continuous
   mining, because nothing on the postgres path ever asked it.
2. The ORT ``intra_op_num_threads`` cap (#1068, fixed in ``_build_ef_class``)
   is wired up only inside the resolver. Measured on a 48-core host: one such
   embedder adds ~49 OS threads; daemon CPU averaged 210-265% over 18 hours
   and was sampled at 2550% mid-mine.
3. In chromadb 1.5.9 ``DefaultEmbeddingFunction`` is *not* ``ONNXMiniLM_L6_V2``
   — its whole ``__call__`` body is ``return ONNXMiniLM_L6_V2()(input)``, and
   ``.model`` is a per-*instance* ``cached_property``. So the module-global
   ``_embedder`` cached an object holding no model and every call rebuilt the
   entire ONNX session. Measured, three consecutive identical calls:
   0.706s / 0.711s / 0.713s (flat — no warm-up ever) against
   1.257s / 0.532s / 0.539s for a genuinely reused instance.
"""

import pytest

from mempalace import embedding as emb
from mempalace.backends import embedding_wrapper as ew
from mempalace.backends import postgres as pg

# conftest's autouse ``_stable_embedding_function_for_tests`` replaces
# ``embedding_wrapper._embed_texts`` with a deterministic stand-in for every
# module outside its opt-out list, to keep ordinary tests off native ONNX.
# That stub sits exactly where this module's subject does, so it would hide
# every assertion here. Capture the real function at import time — collection
# runs before autouse fixtures — and put it back for these tests only. No
# native ONNX is loaded regardless: each test below supplies its own embedder.
_REAL_EMBED_TEXTS = ew._embed_texts
# Same reason: conftest also points ``embedding.get_embedding_function`` at
# the stand-in, which would bypass the resolver whose caching is the subject
# of the session-built-once test.
_REAL_GET_EMBEDDING_FUNCTION = emb.get_embedding_function


@pytest.fixture(autouse=True)
def _use_the_real_embed_texts(monkeypatch):
    monkeypatch.setattr(ew, "_embed_texts", _REAL_EMBED_TEXTS)


class _Vectorish(list):
    """A stand-in for the numpy array real embedders return."""

    def tolist(self):
        return [float(x) for x in self]


class _CountingEF:
    """Counts constructions and calls, class-wide."""

    constructions = 0
    calls = 0

    def __init__(self, *args, **kwargs):
        type(self).constructions += 1

    def __call__(self, input):  # noqa: A002 — ChromaDB EF protocol uses `input`
        type(self).calls += 1
        return [_Vectorish([0.5] * 384) for _ in input]

    @staticmethod
    def name() -> str:
        return "default"


@pytest.fixture()
def counting_ef(monkeypatch):
    """Route the real resolver at a counting embedder, with a clean cache."""

    class _EF(_CountingEF):
        constructions = 0
        calls = 0

    saved = dict(emb._EF_CACHE)
    emb._EF_CACHE.clear()
    monkeypatch.setattr(emb, "get_embedding_function", _REAL_GET_EMBEDDING_FUNCTION)
    monkeypatch.setattr(emb, "_build_ef_class", lambda: _EF)
    monkeypatch.delenv("MEMPALACE_EMBEDDING_MODEL", raising=False)
    # Neutralize the legacy module-global embedder so one test cannot prime it
    # for the next. A no-op once _embed stops keeping its own cache.
    if hasattr(pg, "_embedder"):
        monkeypatch.setattr(pg, "_embedder", None)
    try:
        yield _EF
    finally:
        emb._EF_CACHE.clear()
        emb._EF_CACHE.update(saved)


# ---------------------------------------------------------------------------
# Defect 3 — the session must be built once, not once per call
# ---------------------------------------------------------------------------


def test_onnx_session_is_built_once_across_calls(counting_ef):
    """Three embeds, one embedder. This is the 0.706s-flat measurement."""
    pg._embed(["one"])
    pg._embed(["two"])
    pg._embed(["three"])

    assert counting_ef.constructions == 1, "the embedder must be constructed once"
    assert counting_ef.calls == 3


def test_embed_never_constructs_chromadb_default_directly(monkeypatch, counting_ef):
    """DefaultEmbeddingFunction rebuilds a session per call — never touch it."""
    from chromadb.utils import embedding_functions

    def _explode(*args, **kwargs):
        raise AssertionError(
            "postgres._embed constructed DefaultEmbeddingFunction directly; "
            "it must go through embedding.get_embedding_function()"
        )

    monkeypatch.setattr(embedding_functions, "DefaultEmbeddingFunction", _explode)

    assert pg._embed(["text"])


# ---------------------------------------------------------------------------
# Defect 1 — the configured embedder must actually be used
# ---------------------------------------------------------------------------


def test_embed_uses_the_configured_embedding_function(monkeypatch):
    """Whatever the resolver returns is what embeds — that is the whole point.

    A configured ``openai-compat`` endpoint resolves correctly at the config
    layer and was still bypassed on this backend, which is how a remote GPU
    endpoint served 9 texts in 18.3 hours.
    """
    seen = {}

    class _ConfiguredEF:
        def __call__(self, input):  # noqa: A002 — ChromaDB EF protocol
            seen["texts"] = list(input)
            return [[0.25] * 384 for _ in input]

    monkeypatch.setattr(emb, "get_embedding_function", lambda *a, **k: _ConfiguredEF())

    vectors = pg._embed(["hello"])

    assert seen["texts"] == ["hello"]
    assert vectors == [[0.25] * 384]


# ---------------------------------------------------------------------------
# The contract the postgres write path depends on
# ---------------------------------------------------------------------------


def test_embed_returns_plain_python_floats(counting_ef):
    """_vec_literal formats with %f — numpy scalars must be converted first."""
    vectors = pg._embed(["text"])

    assert isinstance(vectors, list)
    assert isinstance(vectors[0], list)
    assert all(type(v) is float for v in vectors[0])


def test_empty_input_never_touches_the_resolver(counting_ef):
    """An empty batch must not pay for (or trigger) a model load."""
    assert pg._embed([]) == []
    assert counting_ef.constructions == 0


# ---------------------------------------------------------------------------
# The new failure mode this change makes possible
# ---------------------------------------------------------------------------


def test_dimension_mismatch_is_named_once(monkeypatch, caplog):
    """A configured embedder of the wrong width must say so legibly.

    Before this change _embed could only ever produce 384-dim vectors. Routing
    through the resolver means an operator can now configure a 768-dim model
    against a ``vector(384)`` column, where pgvector's own error names neither
    the model nor the configuration that selected it.
    """
    import logging

    class _WideEF:
        def __call__(self, input):  # noqa: A002 — ChromaDB EF protocol
            return [[0.1] * 768 for _ in input]

    monkeypatch.setattr(emb, "get_embedding_function", lambda *a, **k: _WideEF())
    monkeypatch.setattr(pg, "_dim_warned", False, raising=False)

    with caplog.at_level(logging.WARNING, logger="mempalace.postgres"):
        pg._embed(["a"])
        pg._embed(["b"])

    warnings = [r for r in caplog.records if "768" in r.getMessage()]
    assert len(warnings) == 1, "name the mismatch, once, not on every batch"
    assert "384" in warnings[0].getMessage()
