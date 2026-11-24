"""``_ensure_drawer_unique_index`` skips the duplicate count once the index exists.

The count is a COUNT(DISTINCT) over every Drawer row (1.2M rows, 401 MB on
familiar, 2026-10-20), and it ran on every KG setup even though the index had
long been installed.
"""

from __future__ import annotations

import pytest

from mempalace.knowledge_graph_age import KnowledgeGraphAGE


class _Cur:
    def __init__(self, answers):
        self.answers = answers
        self.executed = []
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))
        self._last = next((v for k, v in self.answers.items() if k in sql), None)

    def fetchone(self):
        return self._last


class _Conn:
    def __init__(self, answers):
        self.cur = _Cur(answers)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def _kg(answers):
    kg = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)
    kg._conn = _Conn(answers)
    return kg


TABLE = "information_schema.tables"
INDEX = "pg_indexes"
COUNT = "COUNT(DISTINCT"


def test_an_installed_index_skips_the_duplicate_count():
    kg = _kg({TABLE: (1,), INDEX: (1,)})
    kg._ensure_drawer_unique_index()
    assert not any(COUNT in q for q in kg._conn.cur.executed)
    assert not any("CREATE UNIQUE INDEX" in q for q in kg._conn.cur.executed)


def test_a_missing_index_is_created_when_there_are_no_duplicates():
    kg = _kg({TABLE: (1,), INDEX: None, COUNT: (0,)})
    kg._ensure_drawer_unique_index()
    assert any(COUNT in q for q in kg._conn.cur.executed)
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS drawer_id_unique" in q for q in kg._conn.cur.executed
    )


def test_duplicates_still_block_the_index():
    kg = _kg({TABLE: (1,), INDEX: None, COUNT: (3,)})
    kg._ensure_drawer_unique_index()
    assert not any("CREATE UNIQUE INDEX" in q for q in kg._conn.cur.executed)


def test_no_drawer_table_does_nothing():
    kg = _kg({TABLE: None})
    kg._ensure_drawer_unique_index()
    assert kg._conn.cur.executed == [kg._conn.cur.executed[0]]


def test_every_path_commits_so_setup_never_sits_idle_in_transaction():
    """An early return used to skip the commit, so the setup connection sat
    "idle in transaction" holding a snapshot. That blocked a CREATE INDEX
    CONCURRENTLY on familiar (2026-10-20) and held back vacuum."""
    for answers in (
        {TABLE: None},  # no Drawer table
        {TABLE: (1,), INDEX: (1,)},  # index already installed
        {TABLE: (1,), INDEX: None, COUNT: (3,)},  # duplicates block it
        {TABLE: (1,), INDEX: None, COUNT: (0,)},  # index created
    ):
        kg = _kg(answers)
        kg._ensure_drawer_unique_index()
        assert kg._conn.commits == 1, answers


class _LockTimeout(Exception):
    """Stands in for psycopg's LockNotAvailable: only the SQLSTATE is read."""

    sqlstate = "55P03"


class _BusyCur(_Cur):
    def __init__(self, answers, exc):
        super().__init__(answers)
        self.exc = exc

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "CREATE UNIQUE INDEX" in sql:
            raise self.exc


class _BusyConn(_Conn):
    def __init__(self, answers, exc):
        self.cur = _BusyCur(answers, exc)
        self.commits = 0
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def _busy_kg(exc):
    kg = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)
    kg._conn = _BusyConn({TABLE: (1,), INDEX: None, COUNT: (0,)}, exc)
    return kg


def test_the_index_build_waits_for_the_drawer_table_only_briefly():
    """#556: the CREATE UNIQUE INDEX queued behind a write-through transaction
    that was minutes old, so every KG init (kg_stats among them) blocked until
    statement_timeout cancelled it. A lock_timeout is set before the build."""
    kg = _kg({TABLE: (1,), INDEX: None, COUNT: (0,)})
    kg._ensure_drawer_unique_index()
    executed = kg._conn.cur.executed
    create = next(i for i, q in enumerate(executed) if "CREATE UNIQUE INDEX" in q)
    timeout = next(i for i, q in enumerate(executed) if "SET LOCAL lock_timeout" in q)
    assert timeout < create


def test_a_busy_drawer_table_skips_the_index_and_rolls_back():
    """The lock timeout costs the index build, not the KG init: setup returns,
    and the rollback leaves the connection out of the aborted transaction."""
    kg = _busy_kg(_LockTimeout("canceling statement due to lock timeout"))
    kg._ensure_drawer_unique_index()
    assert kg._conn.rollbacks == 1
    assert kg._conn.commits == 0


def test_other_index_errors_still_raise():
    kg = _busy_kg(RuntimeError("disk full"))
    with pytest.raises(RuntimeError):
        kg._ensure_drawer_unique_index()
