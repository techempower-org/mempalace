"""``PostgresCollection.get`` continues a page walk by keyset, not OFFSET.

OFFSET re-walks every earlier row, so a full walk is O(n^2) in pages. On
familiar (1.18M drawers, 2026-10-20) the miner's prefetch, the hallways pass and
the graph fallback each did that walk, and drove load to 49. A page that starts
where the previous page of the same filter ended now reads ``id > <last id>``.
A metadata-only read also stops fetching the document.
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
        self.conn.executed.append((" ".join(rendered.split()), list(params or [])))

    def fetchone(self):
        return None

    def fetchall(self):
        return self.conn.pages.pop(0) if self.conn.pages else []


class _Conn:
    def __init__(self):
        self.executed = []
        self.pages = []
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
def fake(monkeypatch):
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


def _rows(*ids):
    return [(i, None, "w", "r", {}) for i in ids]


def _last_select(conn):
    return next((q, p) for q, p in reversed(conn.executed) if q.upper().startswith("SELECT"))


def test_a_walk_continues_by_keyset(fake):
    col, conn = fake
    conn.pages = [_rows("a", "b"), _rows("c", "d")]
    assert col.get(limit=2, offset=0, include=["metadatas"]).ids == ["a", "b"]
    q1, _ = _last_select(conn)
    assert "OFFSET" not in q1 and "id >" not in q1

    assert col.get(limit=2, offset=2, include=["metadatas"]).ids == ["c", "d"]
    q2, p2 = _last_select(conn)
    assert "id > %s" in q2, q2
    assert "OFFSET" not in q2, q2
    assert p2 == ["b", 2]
    assert q2.index("id > %s") < q2.index("ORDER BY id") < q2.index("LIMIT")


def test_a_filtered_walk_keeps_its_filter_and_continues(fake):
    col, conn = fake
    conn.pages = [_rows("a", "b"), _rows("c")]
    col.get(where={"wing": "money"}, limit=2, offset=0, include=["metadatas"])
    col.get(where={"wing": "money"}, limit=2, offset=2, include=["metadatas"])
    q, p = _last_select(conn)
    assert "id > %s" in q and "OFFSET" not in q, q
    assert p == ["money", "b", 2]


def test_an_or_filter_is_parenthesised_before_the_keyset_clause(fake):
    """A top-level $or ANDed with ``id > %s`` unwrapped binds as
    "(a) OR ((b) AND id > x)": rows matching (a) come back on every page and
    the walk never ends (seen as a hung test-postgres job on #569)."""
    col, conn = fake
    where = {"$or": [{"wing": "b"}, {"room": "r2"}]}
    conn.pages = [_rows("a", "b")]
    col.get(where=where, limit=2, offset=0, include=[])
    col.get(where=where, limit=2, offset=2, include=[])
    q, _ = _last_select(conn)
    where_sql = q[q.index("WHERE") + len("WHERE") : q.index("ORDER BY")].strip()
    assert where_sql.startswith("((") and ") AND id > %s" in where_sql, where_sql


def test_an_offset_nobody_reached_uses_offset(fake):
    col, conn = fake
    col.get(limit=2, offset=10, include=["metadatas"])
    q, p = _last_select(conn)
    assert "OFFSET %s" in q and "id >" not in q, q
    assert p == [2, 10]


def test_another_filter_does_not_share_the_memo(fake):
    col, conn = fake
    conn.pages = [_rows("a", "b")]
    col.get(where={"wing": "money"}, limit=2, offset=0, include=["metadatas"])
    col.get(where={"wing": "2g"}, limit=2, offset=2, include=["metadatas"])
    q, _ = _last_select(conn)
    assert "OFFSET %s" in q and "id >" not in q, q


def test_an_expired_continuation_falls_back_to_offset(fake, monkeypatch):
    col, conn = fake
    conn.pages = [_rows("a", "b")]
    col.get(limit=2, offset=0, include=["metadatas"])
    real = pg.time.monotonic
    monkeypatch.setattr(pg.time, "monotonic", lambda: real() + col._PAGE_MEMO_TTL_S + 1)
    col.get(limit=2, offset=2, include=["metadatas"])
    q, _ = _last_select(conn)
    assert "OFFSET %s" in q and "id >" not in q, q


def test_the_memo_is_bounded(fake):
    col, conn = fake
    for n in range(col._PAGE_MEMO_MAX + 10):
        conn.pages = [_rows(f"id{n}")]
        col.get(where={"n": n}, limit=1, offset=0, include=["metadatas"])
    _, entries = col._page_memo()
    assert len(entries) == col._PAGE_MEMO_MAX


def test_an_empty_page_writes_no_continuation(fake):
    col, conn = fake
    col.get(limit=2, offset=0, include=["metadatas"])
    _, entries = col._page_memo()
    assert not entries


def test_an_id_lookup_never_uses_the_memo(fake):
    col, conn = fake
    conn.pages = [_rows("a", "b")]
    col.get(limit=2, offset=0, include=["metadatas"])
    col.get(ids=["x"], limit=2, offset=2, include=["metadatas"])
    q, _ = _last_select(conn)
    assert "id >" not in q, q


@pytest.mark.parametrize(
    ("include", "selects_document"),
    [
        (["metadatas"], False),
        ([], False),
        (["documents"], True),
        (["documents", "metadatas"], True),
    ],
)
def test_the_document_is_read_only_when_asked_for(fake, include, selects_document):
    col, conn = fake
    col.get(limit=2, include=include)
    q, _ = _last_select(conn)
    assert q.startswith("SELECT id, document," if selects_document else "SELECT id, NULL::text,"), q


DSN = os.environ.get("TEST_POSTGRES_DSN")


@pytest.mark.skipif(DSN is None, reason="set TEST_POSTGRES_DSN to run against postgres")
def test_keyset_walk_matches_offset_walk_on_postgres(tmp_path):
    """Real postgres: the keyset walk returns every row once, in id order, with documents."""
    from mempalace.backends import PalaceRef, get_backend

    backend = get_backend("postgres")
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    name = f"keyset_{uuid.uuid4().hex[:8]}"
    try:
        col = backend.get_collection(
            palace=ref, collection_name=name, create=True, options={"dsn": DSN}
        )
        ids = [f"d{i:04d}" for i in range(250)]
        col.add(
            ids=list(reversed(ids)),
            documents=[f"doc {i}" for i in reversed(range(250))],
            metadatas=[{"wing": "w" if i % 3 else "x", "room": "r"} for i in reversed(range(250))],
            embeddings=[[0.0] * 384 for _ in ids],
        )
        x_ids = [i for i in ids if int(i[1:]) % 3 == 0]
        cases = (
            (None, ids),
            ({"wing": "x"}, x_ids),
            ({"$or": [{"wing": "x"}, {"room": "q"}]}, x_ids),
        )
        for where, expect in cases:
            walked, docs, offset = [], [], 0
            for _ in range(100):  # 7 pages expected; bounded so a regression fails, not hangs
                page = col.get(where=where, limit=40, offset=offset, include=["documents"])
                if not page.ids:
                    break
                walked += page.ids
                docs += page.documents
                offset += len(page.ids)
            assert walked == expect
            assert docs == [f"doc {int(i[1:])}" for i in expect]
        meta_only = col.get(limit=5, include=["metadatas"])
        assert meta_only.ids == ids[:5] and all(m["room"] == "r" for m in meta_only.metadatas)
        # delete(ids=, where=$or) removes only the intersection.
        # Unwrapped, this would bind as "id IN (...) AND (x) OR (r)" and delete every row.
        col.delete(ids=["d0000", "d0001"], where={"$or": [{"wing": "x"}, {"room": "r"}]})
        left = set(col.get(limit=1000, include=[]).ids)
        assert left == set(ids[2:])
    finally:
        import psycopg
        from psycopg import sql

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(name)))
