"""Integration smoke tests for the postgres backend.

These tests require a live Postgres with the `vector` extension installed.
Set `TEST_POSTGRES_DSN` to the connection string (e.g.
`postgresql://palace@disks.jphe.in:5432/mempalace_test` or a local DSN);
without it, the tests are skipped so the default suite stays green on
machines that don't have a Postgres at hand.

The substrate the fork uses for these tests is the docker container
documented in `scratch/postgres-preflight-2026-05-10.md` —
`apache/age:release_PG16_1.6.0` with `postgresql-16-pgvector` apt-installed
on top. The AGE graph extension is loaded but not exercised by these
tests; knowledge-graph coverage lives in its own test module.

Pairs with the postgres backend code in `mempalace/backends/postgres.py`
(upstream PR #665) and the BaseCollection contract in
`mempalace/backends/base.py`.
"""

import os

import pytest

from mempalace.backends import get_backend
from mempalace.backends.base import BaseCollection
from mempalace.backends.postgres import PostgresBackend, PostgresCollection

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    POSTGRES_DSN is None,
    reason="set TEST_POSTGRES_DSN to run postgres backend tests",
)


def _palace_ref(path: str):
    """Build a PalaceRef compatible with the postgres backend's get_collection."""
    from mempalace.backends import PalaceRef

    return PalaceRef(id=path, local_path=path)


def test_postgres_backend_is_registered():
    """`get_backend("postgres")` returns a PostgresBackend singleton."""
    backend = get_backend("postgres")
    assert isinstance(backend, PostgresBackend)
    # Singleton property: repeated calls return the same instance.
    assert get_backend("postgres") is backend


def test_postgres_backend_smoke():
    """End-to-end: get a collection, add a drawer, read it back, clean up by id."""
    backend = get_backend("postgres")
    palace = _palace_ref("smoke_test_palace")
    collection_name = "smoke_test_drawers"

    col = backend.get_collection(
        palace=palace,
        collection_name=collection_name,
        create=True,
        options={"dsn": POSTGRES_DSN},
    )
    assert isinstance(col, (BaseCollection, PostgresCollection))

    # Idempotent setup: delete any leftover row from a prior run, then add.
    try:
        col.delete(ids=["smoke_d1"])
    except Exception:
        pass

    col.add(
        ids=["smoke_d1"],
        documents=["hello world from the postgres smoke test"],
        embeddings=[[0.1] * 384],
        metadatas=[{"wing": "test", "room": "smoke", "filed_at": "2026-05-11T00:00:00Z"}],
    )

    res = col.get(ids=["smoke_d1"])
    assert res["documents"] == ["hello world from the postgres smoke test"]
    assert res["metadatas"][0]["wing"] == "test"

    # Clean up the row we added — leaves the table in place for the next test.
    col.delete(ids=["smoke_d1"])


def test_postgres_vector_distance_query():
    """A vector query returns rows ordered by L2 distance to the query embedding."""
    backend = get_backend("postgres")
    palace = _palace_ref("smoke_test_palace")
    collection_name = "smoke_test_distance"

    col = backend.get_collection(
        palace=palace,
        collection_name=collection_name,
        create=True,
        options={"dsn": POSTGRES_DSN},
    )

    # Idempotent setup
    try:
        col.delete(ids=["near", "far"])
    except Exception:
        pass

    col.add(
        ids=["near", "far"],
        documents=["close to the query", "very different"],
        embeddings=[[0.1] * 384, [0.9] * 384],
        metadatas=[{"wing": "test"}, {"wing": "test"}],
    )

    query_emb = [[0.1] * 384]
    qres = col.query(query_embeddings=query_emb, n_results=2)
    assert qres["ids"][0][0] == "near", "nearest neighbor should be 'near' drawer"
    assert qres["ids"][0][1] == "far"

    col.delete(ids=["near", "far"])


def test_maybe_create_vector_index_recognizes_offname_hnsw():
    """A pre-existing HNSW index under a non-canonical name is recognized.

    Regression test for techempower-org/mempalace#73 priority 1. Before the
    structure-based check, _maybe_create_vector_index queried only by literal
    name (`{table}_vec_idx`). An HNSW index created out-of-band by the
    migration tool under e.g. `{table}_embedding_hnsw` was invisible to it,
    so every threshold crossing fell through to CREATE INDEX and stacked
    duplicate full HNSW builds holding ACCESS EXCLUSIVE.
    """
    import psycopg2

    backend = get_backend("postgres")
    palace = _palace_ref("smoke_test_palace")
    collection_name = "smoke_test_offname_idx"

    col = backend.get_collection(
        palace=palace,
        collection_name=collection_name,
        create=True,
        options={"dsn": POSTGRES_DSN},
    )

    # Wipe any prior state on this table (idempotent re-runs).
    conn = psycopg2.connect(POSTGRES_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX IF EXISTS "{col.table_name}_vec_idx"')
            cur.execute(f'DROP INDEX IF EXISTS "{col.table_name}_offname_hnsw"')

        # Need at least one row so HNSW build succeeds.
        try:
            col.delete(ids=["seed"])
        except Exception:
            pass
        col.add(
            ids=["seed"],
            documents=["just to keep the table non-empty"],
            embeddings=[[0.0] * 384],
            metadatas=[{"wing": "test"}],
        )

        # Create the HNSW index under a non-canonical name (mimics migration
        # tool / operator pre-creating it). Same column + amname + ops as
        # _maybe_create_vector_index would have used.
        offname = f"{col.table_name}_offname_hnsw"
        with conn.cursor() as cur:
            cur.execute(
                f'CREATE INDEX "{offname}" '
                f'ON "{col.table_name}" USING hnsw (embedding vector_cosine_ops)'
            )

        # Force the helper past its row-budget gate without needing 5k rows.
        # (The structural check we're exercising runs BEFORE _estimated_count.)
        col._rows_since_index_check = 10_000
        col._vector_index_ready = False
        col._maybe_create_vector_index(inserted_rows=0)

        # After the call: ready flag flips True and no second index appears.
        assert col._vector_index_ready, "structural check should mark index ready"
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM pg_index idx
                JOIN pg_class ix ON ix.oid = idx.indexrelid
                JOIN pg_class t ON t.oid = idx.indrelid
                JOIN pg_am am ON am.oid = ix.relam
                WHERE t.relname = %s AND am.amname = 'hnsw' AND idx.indisvalid
                """,
                (col.table_name,),
            )
            count = cur.fetchone()[0]
        assert count == 1, (
            f"expected exactly 1 HNSW index after structural-recognition path, "
            f"got {count} — duplicate-build regression?"
        )

        # Cleanup
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX IF EXISTS "{offname}"')
        col.delete(ids=["seed"])
    finally:
        conn.close()
