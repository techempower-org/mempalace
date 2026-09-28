"""The postgres hallway store streams a full pass through a server-side cursor.

``iter_hallways()`` feeds the entity-tunnel step of every derived-graph
rebuild. On the postgres store it used to fall back to ``list()``, which sorts
all rows by co-occurrence and fetches them at once. On the palace host that is
2.6M rows. It hit the statement timeout, so every entity-tunnel rebuild failed,
and without the timeout it would hold the table in memory, the #551 cliff.
"""

from __future__ import annotations

import pytest

from mempalace import hallway_store as hs


class _NamedCursor:
    def __init__(self, conn, name):
        self.conn, self.name, self.itersize = conn, name, None
        conn.cursors.append(self)

    def execute(self, sql, params=None):
        self.conn.executed.append(" ".join(str(sql).split()))
        if self.conn.raise_on_execute is not None:
            raise self.conn.raise_on_execute

    def __iter__(self):
        return iter(self.conn.rows)

    def fetchall(self):
        return list(self.conn.rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows=(), raise_on_execute=None):
        self.rows, self.raise_on_execute = list(rows), raise_on_execute
        self.executed, self.cursors = [], []
        self.autocommit, self.rolled_back, self.closed = True, 0, 0

    def cursor(self, name=None):
        return _NamedCursor(self, name)

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed += 1


def _row(i: int):
    rec = {
        "id": f"h{i}",
        "wing": "w",
        "entity_a": f"a{i}",
        "entity_b": f"b{i}",
        "co_occurrence_count": i,
        "rooms": ["r"],
        "label": "l",
        "created_at": "2026-09-28T00:00:00+00:00",
        "created_by": "auto",
        "strength": 0.5,
    }
    row = hs._record_to_row(rec)
    return tuple(row[c] for c in (*hs._PROMOTED, "dynamics", "extra")), rec


@pytest.fixture()
def store(monkeypatch):
    def _make(**kw):
        conn = _Conn(**kw)
        s = hs.PostgresHallwayStore(dsn="postgresql://fake/db")
        monkeypatch.setattr(s, "_connect", lambda: conn)
        return s, conn

    return _make


def test_iter_uses_a_named_server_side_cursor_without_a_sort(store):
    rows = [_row(i) for i in range(5)]
    s, conn = store(rows=[r for r, _ in rows])
    got = list(s.iter(batch_size=2))
    assert got == [rec for _, rec in rows]
    (cur,) = conn.cursors
    assert cur.name, "a server-side (named) cursor keeps the client at batch_size rows"
    assert cur.itersize == 2
    assert conn.autocommit is False  # a named cursor needs a transaction
    assert "ORDER BY" not in conn.executed[0]  # a full-table sort is what timed out
    assert conn.rolled_back == 1 and conn.closed == 1


def test_iter_on_a_missing_table_yields_nothing_and_warns_once(store, caplog):
    s, conn = store(raise_on_execute=hs._undefined_table_error())
    assert list(s.iter()) == []
    assert list(s.iter()) == []
    assert sum("does not exist" in r.getMessage() for r in caplog.records) == 1
    assert conn.closed == 2


def test_iter_hallways_on_postgres_never_falls_back_to_list(monkeypatch):
    """RED on the old code: iter_hallways() called store.list() for postgres."""
    from mempalace import hallways

    s = hs.PostgresHallwayStore(dsn="postgresql://fake/db")

    def _no_list(*a, **k):
        raise AssertionError("list() sorts and fetches every row at once")

    monkeypatch.setattr(s, "list", _no_list)
    monkeypatch.setattr(s, "iter", lambda: iter([{"id": "x"}]))
    monkeypatch.setattr(hallways, "_hallway_store", lambda config=None: s)
    assert list(hallways.iter_hallways()) == [{"id": "x"}]
