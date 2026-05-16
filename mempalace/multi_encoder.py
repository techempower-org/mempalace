"""Multi-encoder retrieval — query N encoder-bound palaces, RRF-fuse.

RESEARCH FEATURE. Default off. Gated by ``PALACE_USE_MULTI_ENCODER_RRF=1``.

Background
----------

The 2026-05-15 chunking×encoder reproduction (`techempower-org/mempalace#82`)
measured **+0.0841 MRR vs. best solo** when fusing default ONNX MiniLM
with two adaptmem FT-Code SentenceTransformer checkpoints under 3-way
RRF on the n=200 git-derived probe set. HyDE — the cheaper alternative
for the same vocabulary-bridging problem — was ruled out for our
institutional-memory corpus (see ``reference-hyde-institutional-memory``
memo). This module is the next lever.

Design
------

Multi-encoder retrieval requires that each encoder's query embedding
land in a vector space populated with vectors *from that same encoder*.
That means **one mined palace per encoder** — single-palace mode would
have FT-encoded queries cosine-matched against ONNX-encoded drawer
vectors, which is noise. Production deployment therefore requires
N parallel mines. The eval harness automates this; production users
opt-in via env.

This module covers the *query side* only:

1. Read the encoder roster from env.
2. For each encoder: load model, encode the query, call
   ``collection.query(query_embeddings=[v], n_results=K)`` against the
   matching palace.
3. Fuse the resulting ranked lists via Reciprocal Rank Fusion (60/k).
4. Return a result shaped like a single ``chromadb`` query result —
   one ``documents``/``metadatas``/``distances`` row — so the caller
   (``mempalace.searcher.search_memories``) can stay unchanged
   downstream.

Configuration
-------------

* ``PALACE_USE_MULTI_ENCODER_RRF=1`` — master switch.
* ``PALACE_RRF_ENCODERS=default,ft-code-1000,ft-code-5000`` — encoder
  names. The literal ``default`` is the built-in ONNX MiniLM (no model
  path needed). Other names map to model paths via the next variable.
* ``PALACE_RRF_ENCODER_PATHS=ft-code-1000=/path/to/ft1000/model,ft-code-5000=/path/to/adaptmem-cache/model``
  — comma-separated ``name=path`` pairs. The path is loaded as a
  ``sentence_transformers.SentenceTransformer``.
* ``PALACE_RRF_PALACES=default=/var/lib/palace/main,ft-code-1000=/var/lib/palace/ft1000,ft-code-5000=/var/lib/palace/ft5k``
  — comma-separated ``name=palace_path`` pairs. Each palace was mined
  with the matching encoder. If a name is missing here, the request
  falls back to the palace passed into ``search_memories`` (with a
  warning the first time per process) — useful for benchmarking.
* ``PALACE_RRF_K=60`` — RRF smoothing constant. Cormack 2009 default.
* ``PALACE_RRF_OVERFETCH=3`` — per-encoder ``n_results`` multiplier.
  Larger overfetch = more chance the right doc shows up in at least one
  list; rapidly diminishing returns past 3x.

Cost
----

Query latency goes up roughly Nx — each encoder embeds the query, each
palace runs a vector probe. Encoders run in series (cheap; query
encoding is one forward pass through a 22M-parameter model, ~50ms CPU).
Storage: Nx (one palace per encoder); ingest is Nx (mine once per
encoder). These costs make this a **research lever**, not a default.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .rrf import DEFAULT_K, rrf_fuse

logger = logging.getLogger(__name__)


ENV_ENABLE = "PALACE_USE_MULTI_ENCODER_RRF"
ENV_ENCODERS = "PALACE_RRF_ENCODERS"
ENV_PATHS = "PALACE_RRF_ENCODER_PATHS"
ENV_PALACES = "PALACE_RRF_PALACES"
ENV_K = "PALACE_RRF_K"
ENV_OVERFETCH = "PALACE_RRF_OVERFETCH"

_DEFAULT_NAME = "default"

# Process-wide cache: encoder name → callable(text) → list[float].
_ENCODER_CACHE: dict[str, Callable[[str], list[float]]] = {}
_ENCODER_CACHE_LOCK = threading.Lock()

# One-shot warnings to avoid log spam when a palace fallback fires.
_WARNED_FALLBACK: set[str] = set()


@dataclass(frozen=True)
class EncoderSpec:
    """One encoder roster entry."""

    name: str
    model_path: Optional[str]  # None for the built-in ``default`` ONNX MiniLM
    palace_path: Optional[str]  # None → use the call-site palace as fallback


def is_enabled() -> bool:
    """True iff the multi-encoder feature flag is set."""
    return os.getenv(ENV_ENABLE, "").strip().lower() in ("1", "true", "yes", "on")


def _parse_kv_list(raw: str) -> dict[str, str]:
    """Parse ``name1=val1,name2=val2`` env strings.

    Empty input → empty dict. Whitespace is stripped. Values may
    contain ``=`` (e.g. file paths on Windows); we only split on the
    *first* ``=``.
    """
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            logger.warning("multi_encoder: dropping malformed entry %r (no '=')", part)
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        out[name] = value
    return out


def load_roster() -> list[EncoderSpec]:
    """Read encoder roster from env.

    Returns a list of :class:`EncoderSpec`. Always includes
    ``default`` first if the env roster is empty; otherwise honors
    the requested order (RRF doesn't depend on order, but consistent
    ordering helps reproducibility of debug logs).
    """
    names_raw = os.getenv(ENV_ENCODERS, "").strip()
    paths = _parse_kv_list(os.getenv(ENV_PATHS, ""))
    palaces = _parse_kv_list(os.getenv(ENV_PALACES, ""))
    if not names_raw:
        return [EncoderSpec(_DEFAULT_NAME, None, palaces.get(_DEFAULT_NAME))]
    names = [n.strip() for n in names_raw.split(",") if n.strip()]
    specs: list[EncoderSpec] = []
    for name in names:
        if name == _DEFAULT_NAME:
            specs.append(EncoderSpec(name, None, palaces.get(name)))
        else:
            path = paths.get(name)
            if not path:
                logger.warning(
                    "multi_encoder: encoder %r listed in %s but no model path in %s — skipping",
                    name,
                    ENV_ENCODERS,
                    ENV_PATHS,
                )
                continue
            specs.append(EncoderSpec(name, path, palaces.get(name)))
    return specs


def _build_default_encoder() -> Callable[[str], list[float]]:
    """Return a callable that encodes one string with the built-in ONNX MiniLM."""
    from .embedding import get_embedding_function

    ef = get_embedding_function()

    def encode(text: str) -> list[float]:
        # ChromaDB's ONNX EF returns a numpy ndarray of float32 (or a list
        # of such). When we hand the raw shape to chromadb's
        # ``query(query_embeddings=...)`` it rejects it: "Expected
        # embeddings to be a list of floats or ints…". Normalize via
        # tolist() so the dtype is plain Python float, regardless of
        # whether the EF gave us an ndarray or list-of-ndarray.
        vecs = ef([text])
        first = vecs[0]
        try:
            return first.tolist()  # numpy.ndarray
        except AttributeError:
            return [float(x) for x in first]

    return encode


def _build_sentence_transformer_encoder(model_path: str) -> Callable[[str], list[float]]:
    """Return a callable that encodes one string with a local SentenceTransformer."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_path)

    def encode(text: str) -> list[float]:
        vec = model.encode([text], convert_to_numpy=True, normalize_embeddings=True)
        return vec[0].tolist()

    return encode


def get_encoder(spec: EncoderSpec) -> Callable[[str], list[float]]:
    """Return a cached query-encoding callable for ``spec``.

    Loading a SentenceTransformer costs ~1.5s and ~100MB RAM per
    model; cache aggressively. Cache key is the encoder name, not the
    model path — two specs that disagree on path under the same name
    is a config bug we'd rather catch than silently honor.
    """
    cached = _ENCODER_CACHE.get(spec.name)
    if cached is not None:
        return cached
    with _ENCODER_CACHE_LOCK:
        cached = _ENCODER_CACHE.get(spec.name)
        if cached is not None:
            return cached
        if spec.name == _DEFAULT_NAME:
            enc = _build_default_encoder()
        else:
            assert spec.model_path, f"non-default encoder {spec.name!r} requires model_path"
            enc = _build_sentence_transformer_encoder(spec.model_path)
        _ENCODER_CACHE[spec.name] = enc
        logger.info(
            "multi_encoder: loaded encoder %s (path=%s)", spec.name, spec.model_path or "<built-in>"
        )
        return enc


def reset_encoder_cache() -> None:
    """Drop all cached encoders. Test/eval-only — not used in the hot path."""
    with _ENCODER_CACHE_LOCK:
        _ENCODER_CACHE.clear()


def _resolve_k() -> int:
    raw = os.getenv(ENV_K, "").strip()
    if not raw:
        return DEFAULT_K
    try:
        v = int(raw)
        if v <= 0:
            raise ValueError
        return v
    except ValueError:
        logger.warning("multi_encoder: invalid %s=%r — falling back to %d", ENV_K, raw, DEFAULT_K)
        return DEFAULT_K


def _resolve_overfetch() -> int:
    raw = os.getenv(ENV_OVERFETCH, "").strip()
    if not raw:
        return 3
    try:
        v = int(raw)
        if v < 1:
            raise ValueError
        return v
    except ValueError:
        logger.warning("multi_encoder: invalid %s=%r — falling back to 3", ENV_OVERFETCH, raw)
        return 3


def _palace_for(spec: EncoderSpec, fallback_palace: str) -> str:
    """Return the palace path this encoder should query.

    Honors :attr:`EncoderSpec.palace_path` first; falls back to the
    palace the caller passed in (with a one-shot warning per process
    per encoder). Eval harnesses that mine one temp palace per
    encoder always set palace_path; the fallback exists so single-
    palace dev environments can still A/B with a no-op fusion.
    """
    if spec.palace_path:
        return spec.palace_path
    if spec.name not in _WARNED_FALLBACK:
        logger.warning(
            "multi_encoder: encoder %r has no palace in %s; falling back to %s — "
            "fusion lift is unmeasurable in this mode since both encoders see the "
            "same single-encoder index",
            spec.name,
            ENV_PALACES,
            fallback_palace,
        )
        _WARNED_FALLBACK.add(spec.name)
    return fallback_palace


def fused_query(  # noqa: C901 — N-encoder fan-out + RRF fusion; complexity is the cost of orchestration
    query: str,
    palace_path: str,
    n_results: int,
    where: Optional[dict] = None,
    collection_getter: Optional[Callable[[str], Any]] = None,
) -> dict:
    """Run one query across N encoders, fuse via RRF, return chromadb-shape result.

    Parameters
    ----------
    query
        Query text. Encoded once per encoder.
    palace_path
        Fallback palace path when an encoder spec has no ``palace_path``.
        In eval mode every spec has its own palace and this fallback is
        unused.
    n_results
        Final desired result size. Per-encoder fetch is ``overfetch *
        n_results``.
    where
        ChromaDB metadata filter dict. Forwarded unchanged to each
        backend ``.query()``.
    collection_getter
        ``palace_path -> collection`` resolver. Defaults to
        :func:`mempalace.palace.get_collection`. Injected for tests.

    Returns
    -------
    dict
        ``{"documents": [[...]], "metadatas": [[...]], "distances":
        [[...]], "ids": [[...]]}`` — the same shape ChromaDB's
        ``collection.query()`` returns for one query text. The outer
        list has one element (one query). Order matches the fused
        ranking; size is ``min(len(fused), n_results)``.

    Notes
    -----
    Fusion identity is ``meta.source_file + meta.chunk_index`` when
    chunk_index is present; otherwise the drawer id. This collapses
    duplicate drawers across encoders (the same source_file at the
    same chunk_index lands in multiple encoder palaces with the same
    text but potentially different cosine distances).

    Distance handling: each encoder's cosine space is its own. We
    surface the distance from whichever encoder ranked the winning
    item *highest* (i.e. the representative chosen by ``rrf_fuse``).
    Downstream code uses distance for the ``max_distance`` gate and
    for ``similarity = 1 - distance``; the convention here is
    consistent with single-encoder mode for the default encoder and
    approximate but useful for the others.
    """
    specs = load_roster()
    if not specs:
        raise RuntimeError(
            "multi_encoder: roster is empty after parsing — check " f"{ENV_ENCODERS}/{ENV_PATHS}"
        )

    if collection_getter is None:
        from .palace import get_collection

        def collection_getter(p: str) -> Any:
            return get_collection(p, create=False)

    overfetch = _resolve_overfetch()
    k_rrf = _resolve_k()
    per_encoder_pull = n_results * overfetch

    # Per-encoder rank lists of (identity, payload) tuples.
    rank_lists: list[list[tuple[str, dict]]] = []
    list_names: list[str] = []
    for spec in specs:
        target_palace = _palace_for(spec, palace_path)
        try:
            col = collection_getter(target_palace)
        except Exception as e:
            logger.warning(
                "multi_encoder: skipping encoder %s — collection at %s unavailable: %s",
                spec.name,
                target_palace,
                e,
            )
            continue
        encoder = get_encoder(spec)
        try:
            vec = encoder(query)
        except Exception as e:
            logger.warning(
                "multi_encoder: skipping encoder %s — query encoding failed: %s", spec.name, e
            )
            continue
        kwargs: dict[str, Any] = {
            "query_embeddings": [vec],
            "n_results": per_encoder_pull,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        try:
            raw = col.query(**kwargs)
        except Exception as e:
            logger.warning(
                "multi_encoder: skipping encoder %s — vector query failed: %s", spec.name, e
            )
            continue
        ids = _first_or_empty(raw, "ids")
        docs = _first_or_empty(raw, "documents")
        metas = _first_or_empty(raw, "metadatas")
        dists = _first_or_empty(raw, "distances")
        if docs and not ids:
            ids = [None] * len(docs)
        ranked: list[tuple[str, dict]] = []
        for did, doc, meta, dist in zip(ids, docs, metas, dists):
            meta = meta or {}
            payload = {
                "id": did,
                "document": doc,
                "metadata": meta,
                "distance": dist,
                "encoder": spec.name,
            }
            ranked.append((_identity(payload), payload))
        rank_lists.append(ranked)
        list_names.append(spec.name)

    if not rank_lists:
        # Every encoder failed — bubble up empty result. Caller will
        # log/warn via the existing single-encoder error path.
        return {
            "ids": [[]],
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }

    fused = rrf_fuse(
        rank_lists,
        key=lambda pair: pair[0],
        representative=lambda items: items[0],  # first occurrence = best-ranked-list copy
        k=k_rrf,
    )
    fused_top = fused[:n_results]

    ids_out: list = []
    docs_out: list = []
    metas_out: list = []
    dists_out: list = []
    for _ident, _score, (_id_str, payload) in fused_top:
        ids_out.append(payload["id"])
        docs_out.append(payload["document"])
        metas_out.append(payload["metadata"])
        dists_out.append(payload["distance"])

    logger.debug(
        "multi_encoder: fused %d encoders (%s) → %d results (k=%d, overfetch=%d)",
        len(rank_lists),
        ",".join(list_names),
        len(fused_top),
        k_rrf,
        overfetch,
    )

    return {
        "ids": [ids_out],
        "documents": [docs_out],
        "metadatas": [metas_out],
        "distances": [dists_out],
    }


def _identity(payload: dict) -> str:
    """Stable identity key for fusion.

    Prefers ``(source_file, chunk_index)`` when both are present;
    falls back to the drawer id, then the document hash. The goal is
    that the same logical drawer in two encoder palaces collapses to
    one fused entry.
    """
    meta = payload.get("metadata") or {}
    source = meta.get("source_file")
    chunk = meta.get("chunk_index")
    if source and chunk is not None:
        return f"{source}#{chunk}"
    if source:
        return f"{source}#?"
    did = payload.get("id")
    if did:
        return f"id:{did}"
    doc = payload.get("document") or ""
    return f"doc:{hash(doc)}"


def _first_or_empty(result: Any, key: str) -> list:
    """Read ``result[key][0]`` or fall back to ``[]``.

    Accepts both dict-shaped chromadb responses and QueryResult
    objects (which expose the same keys as attributes).
    """
    val: Any = None
    if isinstance(result, dict):
        val = result.get(key)
    else:
        val = getattr(result, key, None)
    if val is None:
        return []
    if not val:
        return []
    return val[0] or []


__all__ = [
    "EncoderSpec",
    "ENV_ENABLE",
    "ENV_ENCODERS",
    "ENV_PATHS",
    "ENV_PALACES",
    "ENV_K",
    "ENV_OVERFETCH",
    "fused_query",
    "get_encoder",
    "is_enabled",
    "load_roster",
    "reset_encoder_cache",
]
