"""``_ensure_drawer_unique_index`` skips the duplicate count once the index exists.

The count is a COUNT(DISTINCT) over every Drawer row (1.2M rows, 401 MB on
familiar, 2026-10-20), and it ran on every KG setup even though the index had
long been installed.
"""

from __future__ import annotations

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
