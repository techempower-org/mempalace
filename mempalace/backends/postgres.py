"""Optional PostgreSQL-backed MemPalace storage backend.

The backend prefers ``pg_sorted_heap`` when available and falls back to
``pgvector``. Optional dependencies are imported lazily so the default Chroma
install remains zero-config.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from ..config import normalize_wing_name, strip_lone_surrogates
from .base import (
    BackendClosedError,
    BaseBackend,
    BaseCollection,
    GetResult,
    HealthStatus,
    PalaceNotFoundError,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    _IncludeSpec,
)

logger = logging.getLogger("mempalace.postgres")

# Where wing-less writes land. The drawers table's schema default for the
# wing column is ''::text, so before the _coerce_wing guard any writer that
# omitted the key filed drawers under an unreachable empty-string wing
# (54 smoke-test drawers leaked there on 2026-05-12, issue #381).
FALLBACK_WING = "general"

EMBEDDING_DIM = 384
EMBEDDING_MODEL = "chroma-default-all-MiniLM-L6-v2"
VECTOR_INDEX_MIN_ROWS = 5_000
VECTOR_INDEX_CHECK_INTERVAL_ROWS = 1_000

_embedder = None

# U+FFFD REPLACEMENT CHARACTER — the standard "this byte cannot be
# represented" marker, used for both unstorable classes so the
# substitution reads the same wherever it shows up in a drawer.
_UNSTORABLE_REPLACEMENT = "\ufffd"


def _load_psycopg2():
    """Return the psycopg driver module and its ``sql`` helper.

    Name retained for monkeypatch compatibility with the existing test
    surface; the driver underneath is psycopg3 (``import psycopg``) since
    the psycopg3 migration. Public API on the returned object — ``connect``,
    ``%s`` placeholders, ``errors.UndefinedTable``, ``sql.Identifier``,
    ``sql.SQL`` — is API-compatible with the psycopg2 usage in this
    package.
    """
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - exercised without the extra installed.
        raise RuntimeError(
            "PostgreSQL backend requires optional dependencies. "
            'Install with: pip install "mempalace[postgres]"'
        ) from exc
    return psycopg, sql


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed texts for PostgreSQL vector search.

    Reuse Chroma's default local embedding function so the PostgreSQL backend
    matches the zero-API embedding model already used by the default backend
    without adding a second ML dependency stack.
    """
    global _embedder
    if _embedder is None:
        try:
            from chromadb.utils import embedding_functions
        except ImportError as exc:  # pragma: no cover - chromadb is a core dependency.
            raise RuntimeError(
                "PostgreSQL backend text queries require ChromaDB's local embedding function."
            ) from exc

        _embedder = embedding_functions.DefaultEmbeddingFunction()
        logger.info("Loaded embedding model: %s", EMBEDDING_MODEL)

    vectors = _embedder(texts)
    return [[float(value) for value in vector] for vector in vectors]


def _vec_literal(vector: list[float]) -> str:
    """Convert a vector to a PostgreSQL vector/svec literal."""
    return "[" + ",".join(f"{float(value):.8f}" for value in vector) + "]"


def _parse_vector_literal(value: Any) -> list[float]:
    """Parse pgvector/svec text output into a Python list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [float(v) for v in value]
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return []
    return [float(part) for part in text.split(",")]


def _metadata_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coerce_wing(raw: Any) -> str:
    """Normalize a wing slug at the write choke point (issue #381).

    Every postgres write funnels through here, so this is where the two
    wing-hygiene classes get stopped at the door:

    - missing/empty/whitespace wing → ``FALLBACK_WING`` instead of the
      schema's ``''`` column default (which made drawers unreachable);
    - separator/case variants → the same ``normalize_wing_name`` rule that
      ``init`` applies, so ``Kiyo-XHCI-Fix`` and ``kiyo_xhci_fix`` can't
      fork into near-duplicate wings again.

    Reads stay literal: filters match stored values, which are normalized
    from here on and converged historically by scripts/wing_hygiene.py.
    """
    if raw is None:
        return FALLBACK_WING
    wing = _metadata_value(raw)
    if not wing.strip():
        return FALLBACK_WING
    return normalize_wing_name(wing) or FALLBACK_WING


def _scrub_text(value: str) -> str:
    """Replace both byte classes Postgres refuses, in one pass over a string.

    NUL first, lone surrogate second; each substitution is one character wide,
    so the result is the same length as the input and neither pass can create
    work for the other (U+FFFD is neither a NUL nor a surrogate).
    """
    return strip_lone_surrogates(value.replace("\x00", _UNSTORABLE_REPLACEMENT))


def _scrub_json_value(value: Any) -> Any:
    """Recursively scrub a JSON-shaped metadata value.

    ``json.dumps`` happily serializes both byte classes — a NUL becomes a
    ``\\u0000`` escape, a lone surrogate a ``\\udXXX`` escape — and the
    ``::jsonb`` cast then rejects the statement with "unsupported Unicode
    escape sequence". So the walk has to happen *before* serialization, and
    it has to reach nested containers and dict keys: the old top-level-only
    pass let a NUL one level down abort the batch exactly as an unscrubbed
    top-level one would.

    Scrubbing is not injective — two keys differing only by a NUL collapse to
    one, last wins. That does not arise in practice: metadata keys are fixed
    field names and only transcript-derived values ever actually change.
    """
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {_scrub_json_value(key): _scrub_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_json_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_json_value(item) for item in value)
    return value


def _scrub_unstorable(
    *,
    documents: Optional[list[str]] = None,
    ids: Optional[list[str]] = None,
    metadatas: Optional[list[Any]] = None,
) -> None:
    """Replace every unstorable byte in-place before the rows reach postgres.

    Postgres ``text`` and ``jsonb`` refuse two classes of input outright: a
    NUL (``psycopg.DataError``) and a lone UTF-16 surrogate (invalid UTF-8, so
    psycopg cannot even encode the parameter). Either one anywhere in a mined
    corpus aborts the whole batch, which is how a single stray byte in one
    transcript blocks an entire mine. ``backends/pgvector.py`` has stripped
    both since #1829/#1833; this backend had only the NUL half (#417), and
    only over documents and top-level metadata values.

    Verbatim storage of those bytes is physically impossible on this backend;
    the nearest-verbatim answer is U+FFFD (the Unicode replacement character —
    the standard "this byte cannot be represented" marker) plus an explicit
    ``nul_bytes_replaced`` / ``lone_surrogates_replaced`` count in the
    drawer's metadata, so the substitution is provenanced, never silent.
    Counts describe the *document*, which is the verbatim payload; metadata is
    scrubbed too but is bookkeeping, not the user's words.

    ids are scrubbed defensively — drawer ids are SHA-256 hashes in practice,
    so this is a no-op on the ``ON CONFLICT`` key — and read/delete paths
    scrub the same way, so an id stored through this scrub is still reachable
    by the id its caller holds.
    """
    if ids is not None:
        for i, doc_id in enumerate(ids):
            if isinstance(doc_id, str):
                ids[i] = _scrub_text(doc_id)

    if documents is not None:
        for i, doc in enumerate(documents):
            nuls = doc.count("\x00")
            without_nul = doc.replace("\x00", _UNSTORABLE_REPLACEMENT) if nuls else doc
            scrubbed = strip_lone_surrogates(without_nul)
            # Both substitutions are one character wide, so positions line up
            # and a zip-diff counts the surrogates without a second regex.
            surrogates = sum(1 for a, b in zip(without_nul, scrubbed) if a != b)
            if not nuls and not surrogates:
                continue
            documents[i] = scrubbed
            meta = metadatas[i] if metadatas is not None and i < len(metadatas) else None
            if not isinstance(meta, dict):
                continue
            if nuls:
                meta["nul_bytes_replaced"] = nuls
            if surrogates:
                meta["lone_surrogates_replaced"] = surrogates

    if metadatas is None:
        return
    for i, meta in enumerate(metadatas):
        if not isinstance(meta, dict):
            continue
        scrubbed_meta = _scrub_json_value(meta)
        if scrubbed_meta != meta:
            # Rewrite through the caller's dict rather than replacing the list
            # slot, so a caller holding a reference sees the same scrub the
            # database does.
            meta.clear()
            meta.update(scrubbed_meta)


def _validate_write_lengths(
    *,
    documents: list[str],
    ids: list[str],
    metadatas: Optional[list[dict[str, Any]]],
    embeddings: Optional[list[list[float]]],
) -> None:
    if len(documents) != len(ids):
        raise ValueError("documents and ids must have the same length")
    if metadatas is not None and len(metadatas) != len(documents):
        raise ValueError("metadatas and documents must have the same length")
    if embeddings is not None and len(embeddings) != len(documents):
        raise ValueError("embeddings and documents must have the same length")


class PostgresCollection(BaseCollection):
    """PostgreSQL collection adapter implementing the RFC 001 collection contract."""

    def __init__(self, dsn: str, table_name: str = "mempalace_drawers"):
        self.dsn = dsn
        self.table_name = table_name
        self._conn = None
        self._vec_type: Optional[str] = None
        self._table_am: Optional[str] = None
        self._index_am: Optional[str] = None
        self._setup_done = False
        self._vector_index_ready = False
        self._rows_since_index_check = VECTOR_INDEX_CHECK_INTERVAL_ROWS
        self._local_row_estimate = 0

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        embeddings = self._prepare_write_inputs(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        self._insert_rows(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
            update_on_conflict=False,
        )

    def upsert(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict[str, Any]]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        embeddings = self._prepare_write_inputs(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        self._insert_rows(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
            update_on_conflict=True,
        )

    def _prepare_write_inputs(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict[str, Any]]],
        embeddings: Optional[list[list[float]]],
    ) -> list[list[float]]:
        _validate_write_lengths(
            documents=documents,
            ids=ids,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        _scrub_unstorable(documents=documents, ids=ids, metadatas=metadatas)
        self._ensure_setup(create=True)
        if embeddings is None:
            embeddings = _embed(documents)
        if len(embeddings) != len(documents):
            raise ValueError("embeddings and documents must have the same length")
        return embeddings

    def _insert_rows(
        self,
        *,
        documents: list[str],
        ids: list[str],
        metadatas: Optional[list[dict[str, Any]]],
        embeddings: list[list[float]],
        update_on_conflict: bool,
    ) -> None:
        if update_on_conflict:
            conflict_clause = self._sql.SQL(
                "ON CONFLICT (id) DO UPDATE SET "
                "wing = EXCLUDED.wing, "
                "room = EXCLUDED.room, "
                "document = EXCLUDED.document, "
                "embedding = EXCLUDED.embedding, "
                "metadata = EXCLUDED.metadata"
            )
        else:
            conflict_clause = self._sql.SQL("ON CONFLICT (id) DO NOTHING")

        rows_by_id: dict[str, tuple[str, str, str, str, str, str]] = {}
        ordered_ids: list[str] = []
        # Track the *post-pop* metadata dict (wing/room already removed) so
        # the KG write-through hook block below can use it without
        # re-parsing ``json.dumps(metadata)`` back into a dict. Gemini PR
        # #101 review flagged the re-parse as redundant; we use a parallel
        # in-memory map rather than the *raw* ``metadatas`` argument because
        # the raw form still has wing/room and the hook contract expects
        # them already-popped.
        metadata_by_id: dict[str, dict[str, Any]] = {}
        for index, (doc_id, document) in enumerate(zip(ids, documents)):
            metadata = dict(metadatas[index]) if metadatas else {}
            wing = _coerce_wing(metadata.pop("wing", ""))
            room = _metadata_value(metadata.pop("room", ""))
            embedding = _vec_literal(embeddings[index])
            if doc_id not in rows_by_id:
                ordered_ids.append(doc_id)
                rows_by_id[doc_id] = (
                    wing,
                    room,
                    doc_id,
                    document,
                    embedding,
                    json.dumps(metadata),
                )
                metadata_by_id[doc_id] = metadata
            elif update_on_conflict:
                rows_by_id[doc_id] = (
                    wing,
                    room,
                    doc_id,
                    document,
                    embedding,
                    json.dumps(metadata),
                )
                metadata_by_id[doc_id] = metadata

        rows = [rows_by_id[doc_id] for doc_id in ordered_ids]
        if not rows:
            return
        self._local_row_estimate += len(rows)

        wings = [row[0] for row in rows]
        rooms = [row[1] for row in rows]
        doc_ids = [row[2] for row in rows]
        row_documents = [row[3] for row in rows]
        row_embeddings = [row[4] for row in rows]
        row_metadatas = [row[5] for row in rows]

        cur = self._get_conn().cursor()
        if self._table_am == "sorted_heap":
            cur.execute(
                self._sql.SQL(
                    "INSERT INTO {} (wing, room, id, document, embedding, metadata) "
                    "SELECT wing, room, id, document, embedding_text::{}, metadata_text::jsonb "
                    "FROM unnest("
                    "%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[]"
                    ") AS rows(wing, room, id, document, embedding_text, metadata_text) "
                    "{}"
                ).format(self._table_id, self._vec_type_sql, conflict_clause),
                (wings, rooms, doc_ids, row_documents, row_embeddings, row_metadatas),
            )
        else:
            cur.execute(
                self._sql.SQL(
                    "INSERT INTO {} (id, wing, room, document, embedding, metadata) "
                    "SELECT id, wing, room, document, embedding_text::{}, metadata_text::jsonb "
                    "FROM unnest("
                    "%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[]"
                    ") AS rows(id, wing, room, document, embedding_text, metadata_text) "
                    "{}"
                ).format(self._table_id, self._vec_type_sql, conflict_clause),
                (doc_ids, wings, rooms, row_documents, row_embeddings, row_metadatas),
            )

        self._maybe_create_vector_index(inserted_rows=len(rows))

        # ── KG write-through (AGE-integration inline enrichment) ──
        # If the postgres backend was configured with a KG hook (set via
        # ``set_kg_writethrough(hook)``), call it for each row we just
        # wrote so entities/relations land in the KG alongside the
        # drawer. Hook signature: ``hook(drawer_id, document, metadata)``.
        # Failures inside the hook are caught + logged but never raise —
        # KG enrichment is opportunistic, not mandatory.
        hook = getattr(self, "_kg_writethrough", None)
        if hook is not None:
            for row in rows:
                doc_id, document = row[2], row[3]
                # Use the in-memory post-pop metadata dict rather than
                # re-parsing ``row[5]`` (which is the JSON-serialized form).
                # Same contract — wing/room already popped — but no
                # round-trip through json.loads. (Gemini PR #101 review.)
                metadata = metadata_by_id.get(doc_id, {})
                try:
                    hook(drawer_id=doc_id, document=document, metadata=metadata)
                except Exception as e:  # noqa: BLE001 — opportunistic enrichment
                    logger.warning(
                        "KG write-through hook failed for drawer %s: %s",
                        doc_id,
                        e,
                    )

    def set_kg_writethrough(self, hook) -> None:
        """Register a callable invoked after each successful drawer write.

        Hook signature: ``hook(drawer_id: str, document: str, metadata: dict)``.
        Called once per drawer in ``_insert_rows`` after the row commits.
        Exceptions inside the hook are caught + logged; they never propagate.

        Set to ``None`` to disable. The default (no hook registered) is
        zero overhead — vector-only write path matches the pre-Phase-2
        behavior byte-identically.

        Typical use: configure an entity-extracting hook that populates
        the AGE KG. See ``mempalace.kg_writethrough.make_age_writethrough``
        for the canonical implementation.
        """
        self._kg_writethrough = hook

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        query_texts: Optional[list[str]] = None,
        query_embeddings: Optional[list[list[float]]] = None,
        n_results: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        include: Optional[list[str]] = None,
    ) -> QueryResult:
        if where_document is not None:
            raise UnsupportedFilterError("PostgreSQL backend does not support where_document")
        if (query_texts is None) == (query_embeddings is None):
            raise ValueError("query requires exactly one of query_texts or query_embeddings")
        chosen = query_texts if query_texts is not None else query_embeddings
        if not chosen:
            raise ValueError("query input must be a non-empty list")
        if n_results <= 0:
            raise ValueError("n_results must be positive")

        vectors = query_embeddings if query_embeddings is not None else _embed(query_texts or [])
        spec = _IncludeSpec.resolve(include, default_distances=True)
        self._ensure_setup(create=True)

        all_ids: list[list[str]] = []
        all_documents: list[list[str]] = []
        all_metadatas: list[list[dict]] = []
        all_distances: list[list[float]] = []
        all_embeddings: Optional[list[list[list[float]]]] = [] if spec.embeddings else None

        for query_embedding in vectors:
            ids, documents, metadatas, distances, embeddings = self._query_one(
                query_embedding=query_embedding,
                n_results=n_results,
                where=where,
                include_embeddings=spec.embeddings,
            )
            all_ids.append(ids)
            all_documents.append(documents if spec.documents else [])
            all_metadatas.append(metadatas if spec.metadatas else [])
            all_distances.append(distances if spec.distances else [])
            if all_embeddings is not None:
                all_embeddings.append(embeddings)

        return QueryResult(
            ids=all_ids,
            documents=all_documents,
            metadatas=all_metadatas,
            distances=all_distances,
            embeddings=all_embeddings,
        )

    def _query_one(
        self,
        *,
        query_embedding: list[float],
        n_results: int,
        where: Optional[dict],
        include_embeddings: bool,
    ) -> tuple[list[str], list[str], list[dict], list[float], list[list[float]]]:
        where_sql, where_params = self._where_to_sql(where)
        where_clause = (
            self._sql.SQL("WHERE {}").format(where_sql) if where_sql else self._sql.SQL("")
        )
        embedding_select = (
            self._sql.SQL(", embedding::text") if include_embeddings else self._sql.SQL("")
        )
        embedding = _vec_literal(query_embedding)

        cur = self._get_conn().cursor()
        cur.execute(
            self._sql.SQL(
                "SELECT id, document, wing, room, metadata, "
                "embedding <=> %s::{} AS distance{} "
                "FROM {} {} "
                "ORDER BY embedding <=> %s::{} "
                "LIMIT %s"
            ).format(
                self._vec_type_sql,
                embedding_select,
                self._table_id,
                where_clause,
                self._vec_type_sql,
            ),
            [embedding, *where_params, embedding, int(n_results)],
        )
        rows = cur.fetchall()

        result_ids: list[str] = []
        result_documents: list[str] = []
        result_metadatas: list[dict] = []
        result_distances: list[float] = []
        result_embeddings: list[list[float]] = []
        for row in rows:
            doc_id, document, wing, room, metadata, distance, *rest = row
            result_ids.append(doc_id)
            result_documents.append(document)
            result_metadatas.append(self._metadata_dict(wing, room, metadata))
            result_distances.append(float(distance))
            if include_embeddings:
                result_embeddings.append(_parse_vector_literal(rest[0] if rest else None))

        return (
            result_ids,
            result_documents,
            result_metadatas,
            result_distances,
            result_embeddings,
        )

    def get(
        self,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include: Optional[list[str]] = None,
    ) -> GetResult:
        if where_document is not None:
            raise UnsupportedFilterError("PostgreSQL backend does not support where_document")
        if ids is not None and not ids:
            raise ValueError("Expected ids to be a non-empty list in get")
        # Stored ids went through the write scrub, so the lookup key has to as
        # well or a scrubbed drawer becomes unreachable by the id its caller
        # holds -- and a raw NUL in a bound text parameter is a DataError.
        _scrub_unstorable(ids=ids)
        self._ensure_setup(create=True)

        spec = _IncludeSpec.resolve(include, default_distances=False)
        clauses = []
        params: list[Any] = []
        if ids is not None:
            placeholders = self._sql.SQL(", ").join(self._sql.Placeholder() for _ in ids)
            clauses.append(self._sql.SQL("id IN ({})").format(placeholders))
            params.extend(ids)
        if where:
            where_sql, where_params = self._where_to_sql(where)
            if where_sql:
                clauses.append(where_sql)
                params.extend(where_params)

        where_clause = (
            self._sql.SQL("WHERE {}").format(self._sql.SQL(" AND ").join(clauses))
            if clauses
            else self._sql.SQL("")
        )
        limit_clause = self._sql.SQL("LIMIT %s") if limit else self._sql.SQL("")
        offset_clause = self._sql.SQL("OFFSET %s") if offset else self._sql.SQL("")
        if limit:
            params.append(int(limit))
        if offset:
            params.append(int(offset))
        embedding_select = (
            self._sql.SQL(", embedding::text") if spec.embeddings else self._sql.SQL("")
        )

        cur = self._get_conn().cursor()
        cur.execute(
            self._sql.SQL("SELECT id, document, wing, room, metadata{} FROM {} {} {} {}").format(
                embedding_select,
                self._table_id,
                where_clause,
                limit_clause,
                offset_clause,
            ),
            params,
        )
        rows = cur.fetchall()

        result_ids = [row[0] for row in rows]
        documents = [row[1] for row in rows] if spec.documents else []
        metadatas = (
            [self._metadata_dict(row[2], row[3], row[4]) for row in rows] if spec.metadatas else []
        )
        embeddings = [_parse_vector_literal(row[5]) for row in rows] if spec.embeddings else None
        return GetResult(
            ids=result_ids, documents=documents, metadatas=metadatas, embeddings=embeddings
        )

    def delete(
        self,
        *,
        ids: Optional[list[str]] = None,
        where: Optional[dict] = None,
    ) -> None:
        if ids is not None and not ids:
            raise ValueError("Expected ids to be a non-empty list in delete")
        if not ids and not where:
            return
        _scrub_unstorable(ids=ids)
        self._ensure_setup(create=True)

        clauses = []
        params: list[Any] = []
        if ids:
            placeholders = self._sql.SQL(", ").join(self._sql.Placeholder() for _ in ids)
            clauses.append(self._sql.SQL("id IN ({})").format(placeholders))
            params.extend(ids)
        if where:
            where_sql, where_params = self._where_to_sql(where)
            if where_sql:
                clauses.append(where_sql)
                params.extend(where_params)

        if not clauses:
            return
        cur = self._get_conn().cursor()
        # Resolve the doomed ids BEFORE the DELETE so the delete-through
        # hook can propagate them to AGE. For id-only deletes this is just
        # ``ids``; for where-based deletes we have to query first because
        # the predicate refers to rows that won't exist post-DELETE.
        deleted_ids = list(ids) if ids else None
        if deleted_ids is None:
            cur.execute(
                self._sql.SQL("SELECT id FROM {} WHERE {}").format(
                    self._table_id, self._sql.SQL(" AND ").join(clauses)
                ),
                params,
            )
            deleted_ids = [row[0] for row in cur.fetchall()]
        cur.execute(
            self._sql.SQL("DELETE FROM {} WHERE {}").format(
                self._table_id, self._sql.SQL(" AND ").join(clauses)
            ),
            params,
        )

        # ── KG delete-through (AGE-integration drift prevention) ──
        # Symmetric to the write-through path: if a delete hook is
        # registered, notify it of the removed ids so AGE can drop the
        # corresponding Drawer nodes. Hook failures are caught + logged.
        hook = getattr(self, "_kg_deletethrough", None)
        if hook is not None and deleted_ids:
            try:
                hook(drawer_ids=deleted_ids)
            except Exception as e:  # noqa: BLE001 — opportunistic sync
                logger.warning(
                    "KG delete-through hook failed for %d ids: %s",
                    len(deleted_ids),
                    e,
                )

    def set_kg_deletethrough(self, hook) -> None:
        """Register a callable invoked after each successful drawer delete.

        Hook signature: ``hook(drawer_ids: list[str])``. Called once per
        ``delete`` call with the list of ids actually removed (resolved
        before the DELETE so ``where``-based deletes also propagate).
        Exceptions inside the hook are caught + logged.

        Set to ``None`` to disable. Pair with ``set_kg_writethrough`` —
        running one without the other drifts the graph out of sync with
        the relational table.
        """
        self._kg_deletethrough = hook

    def update(
        self,
        *,
        ids: list[str],
        documents: Optional[list[str]] = None,
        metadatas: Optional[list[dict]] = None,
        embeddings: Optional[list[list[float]]] = None,
    ) -> None:
        if documents is None and metadatas is None and embeddings is None:
            raise ValueError("update requires at least one of documents, metadatas, embeddings")
        if documents is not None or embeddings is not None:
            super().update(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
                embeddings=embeddings,
            )
            return

        n = len(ids)
        if metadatas is not None and len(metadatas) != n:
            raise ValueError(f"metadatas length {len(metadatas)} does not match ids length {n}")
        _scrub_unstorable(ids=ids, metadatas=metadatas)
        self._ensure_setup(create=True)

        cur = self._get_conn().cursor()
        for i, doc_id in enumerate(ids):
            meta = dict(metadatas[i]) if metadatas else {}
            raw_wing = meta.pop("wing", None)
            raw_room = meta.pop("room", None)
            set_parts = []
            params: list[Any] = []
            if raw_wing is not None:
                set_parts.append(self._sql.SQL("wing = %s"))
                params.append(_coerce_wing(raw_wing))
            if raw_room is not None:
                set_parts.append(self._sql.SQL("room = %s"))
                params.append(_metadata_value(raw_room))
            set_parts.append(self._sql.SQL("metadata = metadata || %s::jsonb"))
            params.append(json.dumps(meta))
            params.append(doc_id)
            cur.execute(
                self._sql.SQL("UPDATE {} SET {} WHERE id = %s").format(
                    self._table_id,
                    self._sql.SQL(", ").join(set_parts),
                ),
                params,
            )

    def rename_wing(self, *, from_wing: str, to_wing: str, batch_size: int = 500) -> dict:
        # from_wing stays literal so operators can rename AWAY from a
        # malformed wing (incl. the empty string); the destination gets the
        # same guard as writes so a rename can't mint a new malformed wing.
        to_wing = _coerce_wing(to_wing)
        self._ensure_setup(create=True)
        cur = self._get_conn().cursor()
        renamed = 0
        # Batch the UPDATE to stay within statement_timeout. Each batch
        # auto-commits independently (connection has autocommit=True).
        while True:
            cur.execute(
                self._sql.SQL(
                    "UPDATE {} SET wing = %s"
                    " WHERE id IN ("
                    "   SELECT id FROM {} WHERE wing = %s LIMIT %s"
                    " )"
                ).format(self._table_id, self._table_id),
                [to_wing, from_wing, batch_size],
            )
            if cur.rowcount == 0:
                break
            renamed += cur.rowcount
        return {"renamed": renamed, "errors": 0}

    def count(self) -> int:
        self._ensure_setup(create=True)
        cur = self._get_conn().cursor()
        # Public collection API: keep this exact. Use estimated_count() for
        # status/heuristic paths where stale PostgreSQL catalog stats are acceptable.
        cur.execute(self._sql.SQL("SELECT COUNT(*) FROM {}").format(self._table_id))
        return cur.fetchone()[0]

    def estimated_count(self) -> int:
        self._ensure_setup(create=True)
        return self._estimated_count()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Connection / DDL helpers
    # ------------------------------------------------------------------

    @property
    def _sql(self):
        _psycopg2, sql = _load_psycopg2()
        return sql

    @property
    def _table_id(self):
        return self._sql.Identifier(self.table_name)

    @property
    def _vec_type_sql(self):
        if not self._vec_type:
            raise RuntimeError("PostgreSQL vector type was not detected")
        return self._sql.SQL(self._vec_type)

    def _get_conn(self):
        psycopg2, _sql = _load_psycopg2()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.dsn)
            self._conn.autocommit = True
            self._apply_session_settings(self._conn)
        return self._conn

    def _apply_session_settings(self, conn) -> None:
        """Per-connection GUCs the kNN path depends on.

        ``hnsw.iterative_scan`` (pgvector >= 0.8): a filtered kNN
        (``WHERE wing = %s ORDER BY embedding <=> q LIMIT k``) is planned as
        an HNSW index scan *then* a filter. The index hands back its
        ``ef_search`` nearest rows palace-wide and the filter discards the
        ones outside the wing — so a wing that isn't in the global top-N
        gets ZERO rows, no matter how good its best match is. Measured
        2026-09-03 on a 757K-drawer palace: a 13K-drawer wing returned 0 for a
        well-formed prose query (``Rows Removed by Filter: 47``) while the
        same query unscoped returned 20 and an exact scan returned 5.
        ``relaxed_order`` makes the index keep scanning until LIMIT rows
        survive the filter. Session-scoped, so it must be set on every new
        connection (autocommit means no ``SET LOCAL``). Tolerated on older
        pgvector (unknown GUC) — logged once, never fatal.
        MEMPALACE_PG_HNSW_ITERATIVE_SCAN=off disables; any other value is
        passed through (``relaxed_order`` | ``strict_order``).
        """
        mode = os.environ.get("MEMPALACE_PG_HNSW_ITERATIVE_SCAN", "relaxed_order").strip()
        if not mode or mode.lower() == "off":
            return
        if mode not in ("relaxed_order", "strict_order"):
            logger.warning(
                "MEMPALACE_PG_HNSW_ITERATIVE_SCAN=%r not in (relaxed_order, strict_order); "
                "using relaxed_order",
                mode,
            )
            mode = "relaxed_order"
        try:
            cur = conn.cursor()
            cur.execute("SET hnsw.iterative_scan = " + mode)
        except Exception as exc:  # noqa: BLE001 — pgvector < 0.8 has no such GUC
            if not getattr(self, "_iterative_scan_warned", False):
                logger.info(
                    "postgres: hnsw.iterative_scan unavailable (%s); filtered kNN may "
                    "under-return on wing-scoped queries — upgrade pgvector to >= 0.8",
                    str(exc).splitlines()[0][:120],
                )
                self._iterative_scan_warned = True

    def _detect_extensions(self, *, create: bool = False) -> None:
        if self._vec_type:
            return

        cur = self._get_conn().cursor()
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('pg_sorted_heap', 'vector')"
        )
        installed = {row[0] for row in cur.fetchall()}

        if "pg_sorted_heap" in installed:
            self._vec_type = "svec"
            self._table_am = "sorted_heap"
            self._index_am = "sorted_hnsw"
        elif "vector" in installed:
            self._vec_type = "vector"
            self._table_am = "heap"
            self._index_am = "hnsw"
        elif create:
            for extension, vec_type, table_am, index_am in (
                ("pg_sorted_heap", "svec", "sorted_heap", "sorted_hnsw"),
                ("vector", "vector", "heap", "hnsw"),
            ):
                try:
                    cur.execute(
                        self._sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                            self._sql.Identifier(extension)
                        )
                    )
                    self._vec_type = vec_type
                    self._table_am = table_am
                    self._index_am = index_am
                    break
                except Exception:
                    logger.debug(
                        "Could not create PostgreSQL extension %s", extension, exc_info=True
                    )
                    continue

        if not self._vec_type:
            raise RuntimeError(
                "PostgreSQL backend requires pgvector or pg_sorted_heap. "
                "Install one of them with CREATE EXTENSION before opening read-only collections."
            )

    def _open(self, *, create: bool) -> None:
        self._detect_extensions(create=create)
        if create:
            self._ensure_setup(create=True)
            return
        if not self._table_exists():
            raise PalaceNotFoundError(f"PostgreSQL collection does not exist: {self.table_name}")
        self._setup_done = True

    def _table_exists(self) -> bool:
        cur = self._get_conn().cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (self.table_name,),
        )
        return cur.fetchone() is not None

    def _ensure_setup(self, *, create: bool = True) -> None:
        if self._setup_done:
            return

        self._detect_extensions(create=create)
        if not create and not self._table_exists():
            raise PalaceNotFoundError(f"PostgreSQL collection does not exist: {self.table_name}")

        if create:
            cur = self._get_conn().cursor()
            self._create_table(cur)
        self._setup_done = True

    def _create_table(self, cur) -> None:
        if self._table_exists():
            return

        vec_type = self._sql.SQL("{}({})").format(
            self._vec_type_sql, self._sql.SQL(str(EMBEDDING_DIM))
        )
        if self._table_am == "sorted_heap":
            cur.execute(
                self._sql.SQL(
                    "CREATE TABLE {} ("
                    "wing text COLLATE \"C\" NOT NULL DEFAULT '', "
                    "room text COLLATE \"C\" NOT NULL DEFAULT '', "
                    "id text NOT NULL, "
                    "document text NOT NULL, "
                    "embedding {}, "
                    "metadata jsonb DEFAULT '{{}}', "
                    "PRIMARY KEY (wing, room, id)"
                    ") USING sorted_heap"
                ).format(self._table_id, vec_type)
            )
            cur.execute(
                self._sql.SQL("CREATE UNIQUE INDEX {} ON {} USING btree (id)").format(
                    self._sql.Identifier(f"{self.table_name}_id_idx"), self._table_id
                )
            )
        else:
            # doc_tsv is a generated tsvector column for BM25 search via
            # plainto_tsquery/websearch_to_tsquery. Truncated to 100KB to
            # stay under postgres's 1MB tsvector cap (some drawers are
            # very large; the head 100KB is plenty for keyword surface).
            cur.execute(
                self._sql.SQL(
                    "CREATE TABLE {} ("
                    "id text PRIMARY KEY, "
                    "wing text NOT NULL DEFAULT '', "
                    "room text NOT NULL DEFAULT '', "
                    "document text NOT NULL, "
                    "embedding {}, "
                    "metadata jsonb DEFAULT '{{}}', "
                    "doc_tsv tsvector GENERATED ALWAYS AS ("
                    "    to_tsvector('english', substring(coalesce(document, '') for 100000))"
                    ") STORED"
                    ")"
                ).format(self._table_id, vec_type)
            )
            for column in ("wing", "room"):
                cur.execute(
                    self._sql.SQL("CREATE INDEX {} ON {} ({})").format(
                        self._sql.Identifier(f"{self.table_name}_{column}_idx"),
                        self._table_id,
                        self._sql.Identifier(column),
                    )
                )
            # GIN on doc_tsv — BM25 keyword search path. Index build is
            # incremental on the empty table; later writes auto-update it.
            cur.execute(
                self._sql.SQL("CREATE INDEX {} ON {} USING gin (doc_tsv)").format(
                    self._sql.Identifier(f"{self.table_name}_doc_tsv_idx"),
                    self._table_id,
                )
            )
            # GIN trigram on document — ILIKE substring fallback for
            # underscore-bearing identifiers (ts_rank_cd,
            # websearch_to_tsquery, etc.) that postgres's tsvector parser
            # splits into separate tokens. Requires the pg_trgm extension
            # — migrate-to-postgres' phase_1_schema installs it; an
            # in-process backend init in a fresh DB without pg_trgm will
            # skip this index gracefully via the try/except.
            try:
                cur.execute(
                    self._sql.SQL("CREATE INDEX {} ON {} USING gin (document gin_trgm_ops)").format(
                        self._sql.Identifier(f"{self.table_name}_doc_trgm_idx"),
                        self._table_id,
                    )
                )
            except Exception:
                logger.warning(
                    "pg_trgm extension not available; skipping trigram index "
                    "on %s.document — ILIKE substring search will be slow. "
                    "Install with: CREATE EXTENSION pg_trgm;",
                    self.table_name,
                )

        logger.info(
            "Created PostgreSQL collection %s (%s, %s)",
            self.table_name,
            self._table_am,
            self._vec_type,
        )

    def _maybe_create_vector_index(self, *, inserted_rows: int = 0) -> None:
        if self._vector_index_ready:
            return
        self._rows_since_index_check += inserted_rows
        if self._rows_since_index_check < VECTOR_INDEX_CHECK_INTERVAL_ROWS:
            return
        self._rows_since_index_check = 0

        index_name = f"{self.table_name}_vec_idx"

        # Open a side connection for the index-management transaction.
        # pg_advisory_xact_lock auto-releases at commit and requires a
        # real transaction; the main backend connection is autocommit=
        # True, and psycopg forbids toggling autocommit mid-flight (we
        # hit this earlier in phase_1_schema). A short-lived side
        # connection sidesteps both constraints.
        #
        # The advisory lock serializes check+create across every
        # connection on this database, preventing the lookup-vs-create
        # race that wedges Postgres when concurrent writers cross the
        # threshold simultaneously and each issue their own CREATE
        # INDEX (each takes ACCESS EXCLUSIVE; they stack and block all
        # other writes for the duration of the build).
        #
        # `IF NOT EXISTS` is belt-and-suspenders against any path that
        # slips past the advisory (e.g. an out-of-band loader that
        # already created an HNSW under this exact name).
        psycopg2, _sql = _load_psycopg2()
        ix_conn = psycopg2.connect(self.dsn)
        try:
            with ix_conn:
                with ix_conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"vec_idx:{self.table_name}",),
                    )
                    # Structure-based existence check (#73 priority 1).
                    # Earlier versions queried by literal index name —
                    # which missed out-of-band indexes created by
                    # migration tooling or operators under different
                    # names. The race got us once already: the
                    # canonical name `{table}_vec_idx` didn't match
                    # the migration's `{table}_embedding_hnsw`, so
                    # every threshold crossing fell through to CREATE
                    # INDEX and stacked duplicate full HNSW builds
                    # holding ACCESS EXCLUSIVE for 30+ min.
                    #
                    # The new check asks pg_index: "is there *any*
                    # valid `{index_am}` index covering the embedding
                    # column on this table?". Catches any name and
                    # filters out half-built / invalid indexes.
                    #
                    # The legacy name lookup remains as belt-and-
                    # suspenders below — covers edge cases where the
                    # structural check might be ambiguous (e.g. a
                    # partial-build that recorded indisvalid=true
                    # against the wrong amname after a backend
                    # upgrade).
                    cur.execute(
                        """
                        SELECT 1
                        FROM pg_class ix
                        JOIN pg_index idx ON idx.indexrelid = ix.oid
                        JOIN pg_class t ON t.oid = idx.indrelid
                        JOIN pg_namespace n ON n.oid = t.relnamespace
                        JOIN pg_am am ON am.oid = ix.relam
                        JOIN pg_attribute a
                            ON a.attrelid = t.oid AND a.attnum = ANY(idx.indkey)
                        WHERE n.nspname = current_schema()
                          AND t.relname = %s
                          AND a.attname = 'embedding'
                          AND am.amname = %s
                          AND idx.indisvalid
                        LIMIT 1
                        """,
                        (self.table_name, self._index_am),
                    )
                    if cur.fetchone():
                        self._vector_index_ready = True
                        return
                    # Legacy name lookup retained as belt-and-suspenders.
                    cur.execute(
                        "SELECT 1 FROM pg_indexes WHERE indexname = %s",
                        (index_name,),
                    )
                    if cur.fetchone():
                        self._vector_index_ready = True
                        return

                    if self._estimated_count() < VECTOR_INDEX_MIN_ROWS:
                        return

                    ops = "svec_cosine_ops" if self._vec_type == "svec" else "vector_cosine_ops"
                    cur.execute(
                        self._sql.SQL(
                            "CREATE INDEX IF NOT EXISTS {} ON {} USING {} (embedding {})"
                        ).format(
                            self._sql.Identifier(index_name),
                            self._table_id,
                            self._sql.SQL(self._index_am),
                            self._sql.SQL(ops),
                        )
                    )
                    self._vector_index_ready = True
        finally:
            ix_conn.close()

    def _estimated_count(self) -> int:
        cur = self._get_conn().cursor()
        cur.execute(
            """
            SELECT GREATEST(
                COALESCE(c.reltuples, 0),
                COALESCE(s.n_live_tup, 0)
            )::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
            WHERE n.nspname = 'public' AND c.relname = %s
            """,
            (self.table_name,),
        )
        row = cur.fetchone()
        if not row:
            return self._local_row_estimate
        return max(int(row[0]), self._local_row_estimate)

    def _where_to_sql(self, where: Optional[dict[str, Any]]):
        if not where:
            return None, []
        if not isinstance(where, dict):
            raise ValueError("PostgreSQL where filter must be a dictionary")

        if len(where) == 1 and next(iter(where)) in ("$and", "$or"):
            operator = next(iter(where))
            conditions = where[operator]
            if not isinstance(conditions, list) or not conditions:
                raise ValueError(f"PostgreSQL where operator {operator} requires a non-empty list")
            parts = []
            params = []
            for condition in conditions:
                clause, clause_params = self._where_to_sql(condition)
                if clause is None:
                    raise ValueError(
                        f"PostgreSQL where operator {operator} contains an empty filter"
                    )
                parts.append(self._sql.SQL("({})").format(clause))
                params.extend(clause_params)
            joiner = self._sql.SQL(" AND " if operator == "$and" else " OR ")
            return joiner.join(parts), params

        clauses = []
        params = []
        for key, value in where.items():
            if key.startswith("$"):
                raise UnsupportedFilterError(f"unsupported PostgreSQL where operator: {key}")
            clause, clause_params = self._field_filter_to_sql(key, value)
            clauses.append(clause)
            params.extend(clause_params)
        if not clauses:
            return None, []
        return self._sql.SQL(" AND ").join(clauses), params

    def _field_filter_to_sql(self, key: str, value: Any):
        # JSONB list-membership operators apply to fields that hold an
        # array (e.g. ``tags``). Detected before the scalar lhs is built
        # because ``metadata->>%s`` would coerce the array to text.
        if isinstance(value, dict) and len(value) == 1:
            inner_op, inner_operand = next(iter(value.items()))
            if inner_op in ("$contains_all", "$contains_any"):
                return self._jsonb_array_filter_to_sql(key, inner_op, inner_operand)

        if key in ("wing", "room"):
            lhs = self._sql.SQL("{}").format(self._sql.Identifier(key))
            lhs_params = []
        else:
            lhs = self._sql.SQL("metadata->>%s")
            lhs_params = [key]

        if not isinstance(value, dict):
            return self._sql.SQL("{} = %s").format(lhs), [*lhs_params, _metadata_value(value)]

        if len(value) != 1:
            raise ValueError(f"PostgreSQL where field {key!r} must contain exactly one operator")

        operator, operand = next(iter(value.items()))
        if operator == "$eq":
            return self._sql.SQL("{} = %s").format(lhs), [*lhs_params, _metadata_value(operand)]
        if operator == "$ne":
            return self._sql.SQL("{} <> %s").format(lhs), [*lhs_params, _metadata_value(operand)]
        if operator in ("$in", "$nin"):
            if not isinstance(operand, list) or not operand:
                raise ValueError(f"PostgreSQL where operator {operator} requires a non-empty list")
            placeholders = self._sql.SQL(", ").join(self._sql.Placeholder() for _ in operand)
            sql_operator = self._sql.SQL("IN" if operator == "$in" else "NOT IN")
            return (
                self._sql.SQL("{} {} ({})").format(lhs, sql_operator, placeholders),
                [*lhs_params, *(_metadata_value(item) for item in operand)],
            )
        raise UnsupportedFilterError(f"unsupported PostgreSQL where field operator: {operator}")

    def _jsonb_array_filter_to_sql(self, key: str, operator: str, operand: Any):
        """Compile ``$contains_all`` / ``$contains_any`` against a JSONB array field.

        ``$contains_all`` → JSONB ``@>`` containment (every element required).
        ``$contains_any`` → ``?|`` text-array overlap (any element matches).
        """
        if not isinstance(operand, list) or not operand:
            raise ValueError(f"PostgreSQL where operator {operator} requires a non-empty list")
        if not all(isinstance(item, str) for item in operand):
            raise ValueError(f"PostgreSQL where operator {operator} requires string elements")
        # ``metadata->'tags'`` keeps the JSONB type so ``@>``/``?|`` work.
        field_expr = self._sql.SQL("metadata->%s")
        if operator == "$contains_all":
            return (
                self._sql.SQL("{} @> %s::jsonb").format(field_expr),
                [key, json.dumps(operand)],
            )
        # $contains_any
        return (
            self._sql.SQL("{} ?| %s").format(field_expr),
            [key, operand],
        )

    @staticmethod
    def _metadata_dict(wing: str, room: str, metadata: Any) -> dict[str, Any]:
        result = dict(metadata) if isinstance(metadata, dict) else {}
        result["wing"] = wing
        result["room"] = room
        return result


class PostgresBackend(BaseBackend):
    """Factory for optional PostgreSQL collections."""

    name = "postgres"
    capabilities = frozenset(
        {
            "supports_embeddings_in",
            "supports_embeddings_passthrough",
            "supports_embeddings_out",
            "supports_metadata_filters",
            "server_mode",
        }
    )

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or _dsn_from_env()
        self._collections: dict[tuple[str, str, str], PostgresCollection] = {}
        self._closed = False

    def get_collection(
        self,
        *,
        palace: PalaceRef,
        collection_name: str,
        create: bool = False,
        options: Optional[dict] = None,
    ) -> PostgresCollection:
        if self._closed:
            raise BackendClosedError("PostgresBackend has been closed")

        options = options or {}
        dsn = options.get("dsn") or self.dsn or _dsn_from_env()
        if not dsn:
            raise RuntimeError(
                "PostgreSQL backend selected but no DSN is configured. "
                "Set MEMPALACE_POSTGRES_DSN or MEMPALACE_PG_DSN."
            )
        table_name = options.get("table_name") or collection_name
        cache_key = (str(dsn), palace.id, table_name)

        collection = self._collections.get(cache_key)
        if collection is None:
            collection = PostgresCollection(str(dsn), table_name=table_name)
            collection._open(create=create)
            self._collections[cache_key] = collection
        elif create:
            collection._ensure_setup(create=True)
        return collection

    def close_palace(self, palace: PalaceRef) -> None:
        for key, collection in list(self._collections.items()):
            if key[1] == palace.id:
                collection.close()
                self._collections.pop(key, None)

    def close(self) -> None:
        for collection in self._collections.values():
            collection.close()
        self._collections.clear()
        self._closed = True

    def health(self, palace: Optional[PalaceRef] = None) -> HealthStatus:
        del palace
        if self._closed:
            return HealthStatus.unhealthy("backend closed")
        dsn = self.dsn or _dsn_from_env()
        if not dsn:
            return HealthStatus.unhealthy("missing PostgreSQL DSN")
        try:
            psycopg2, _sql = _load_psycopg2()
            conn = psycopg2.connect(dsn)
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
            finally:
                conn.close()
        except Exception as exc:
            return HealthStatus.unhealthy(str(exc))
        return HealthStatus.healthy("PostgreSQL reachable")

    @classmethod
    def detect(cls, path: str) -> bool:
        del path
        return False


def _dsn_from_env() -> Optional[str]:
    return os.environ.get("MEMPALACE_POSTGRES_DSN") or os.environ.get("MEMPALACE_PG_DSN")
