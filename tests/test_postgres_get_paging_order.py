"""``PostgresCollection.get`` pages in a total order, so offset walks see every row once (#553).

Without ``ORDER BY``, postgres may start each page at a different scan position
(``synchronize_seqscans``, concurrent writes). Measured on the palace host, an
offset walk over ``mempalace_drawers`` missed 27-31% of a wing's drawers and
duplicated about half, differently on each pass. That walk is the
``compute_hallways_for_wing`` loop, and the exporter, repair and migrate use
the same one.
"""

from __future__ import annotations

import os
import uuid

import pytest

from mempalace.backends import postgres as pg


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        rendered = sql.as_string(None) if hasattr(sql, "as_string") else str(sql)
        self.conn.executed.append((rendered, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.executed = []
        self.autocommit = None
        self.closed = 0

    def cursor(self):
        return _Cursor(self)


class _Mod:
    def __init__(self, conn):
        self._conn = conn

    def connect(self, dsn, **kwargs):
        return self._conn


@pytest.fixture()
def fake_collection(monkeypatch):
    sql = pytest.importorskip("psycopg.sql", reason="requires the postgres extra")
    conn = _Conn()
    monkeypatch.setattr(pg, "_load_psycopg2", lambda: (_Mod(conn), sql))
    col = pg.PostgresCollection.__new__(pg.PostgresCollection)
    col.dsn = "postgresql://fake/db"
    col.table_name = "mempalace_drawers"
    col._conn = None
    col._vec_type = "vector"
    col._table_am = "heap"
    col._index_am = "hnsw"
    col._setup_done = True
    col._vector_index_ready = True
    col._rows_since_index_check = 0
    col._local_row_estimate = 0
    return col, conn


def _select(conn) -> str:
    return next(q for q, _ in reversed(conn.executed) if q.lstrip().upper().startswith("SELECT"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 5000, "offset": 0},
        {"limit": 5000, "offset": 10000},
        {"offset": 10000},
        {"limit": 1000, "where": {"wing": "money"}},
    ],
)
def test_paged_get_orders_by_primary_key_before_limit(fake_collection, kwargs):
    col, conn = fake_collection
    col.get(include=["metadatas"], **kwargs)
    q = " ".join(_select(conn).split())
    assert "ORDER BY id" in q, q
    order_at = q.index("ORDER BY id")
    for clause in ("LIMIT", "OFFSET"):
        if clause in q:
            assert order_at < q.index(clause), q


def test_unpaged_get_keeps_its_unordered_plan(fake_collection):
    col, conn = fake_collection
    col.get(ids=["a", "b"], include=["metadatas"])
    assert "ORDER BY" not in _select(conn)


DSN = os.environ.get("TEST_POSTGRES_DSN")


@pytest.mark.skipif(DSN is None, reason="set TEST_POSTGRES_DSN to run against postgres")
def test_offset_walk_returns_every_row_once_in_id_order(tmp_path):
    """Real postgres: ids inserted in reverse, then updated so heap order differs from id order.

    The old unordered query returns heap order, so the walk is not id-sorted.
    It also has no guarantee of exactly-once under concurrent scans. The
    ordered query returns a sorted, duplicate-free walk.
    """
    from mempalace.backends import PalaceRef, get_backend

    backend = get_backend("postgres")
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    name = f"paging_{uuid.uuid4().hex[:8]}"
    try:
        col = backend.get_collection(
            palace=ref, collection_name=name, create=True, options={"dsn": DSN}
        )
        ids = [f"d{i:04d}" for i in range(250)]
        col.add(
            ids=list(reversed(ids)),
            documents=[f"doc {i}" for i in reversed(range(250))],
            metadatas=[{"wing": "w", "room": "r"} for _ in ids],
            embeddings=[[0.0] * 384 for _ in ids],
        )
        # An update writes a new heap tuple, which moves these rows to the end of the heap.
        col.update(ids=ids[::7], metadatas=[{"wing": "w", "room": "moved"} for _ in ids[::7]])

        walked, offset = [], 0
        while True:
            page = col.get(limit=40, offset=offset, include=[])
            if not page.ids:
                break
            walked += page.ids
            offset += len(page.ids)
        assert walked == sorted(ids)
    finally:
        import psycopg
        from psycopg import sql

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(name)))
