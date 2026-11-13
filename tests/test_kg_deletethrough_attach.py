"""#558: the delete-through attaches independently of write-through.

Measured in production 2026-11-13: with MEMPALACE_KG_WRITETHROUGH=0 (paused for a
re-mine drain) the delete hook was never attached, so every drawer deleted through
the daemon left its AGE Drawer vertex behind.
"""

from mempalace import palace as col_mod


class _RecordingKG:
    def __init__(self):
        self.deleted = []

    def delete_drawers(self, ids):
        self.deleted.append(list(ids))


def _queue_factory(monkeypatch):
    """Replace the queue-hook factory; returns the DSNs it was asked for."""
    made = []

    def factory(dsn):
        made.append(dsn)

        def hook(*, drawer_ids):
            hook.queued.append(list(drawer_ids))

        hook.queued = []
        return hook

    monkeypatch.setattr("mempalace.kg_delete_queue.make_queued_deletethrough", factory)
    return made


class _Col:
    def __init__(self):
        self.delete_hook = None

    def set_kg_deletethrough(self, hook):
        self.delete_hook = hook


def _env(monkeypatch, write="0", delete=None):
    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", write)
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "0")
    if delete is None:
        monkeypatch.delenv("MEMPALACE_KG_DELETETHROUGH", raising=False)
    else:
        monkeypatch.setenv("MEMPALACE_KG_DELETETHROUGH", delete)


def _attach(col, dsn):
    col_mod._writethrough_attached.discard(id(col))
    col_mod._no_age_graph.pop(dsn, None)
    col_mod._maybe_attach_writethrough(col, dsn)


def test_delete_through_attaches_with_write_through_off(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: True)
    made = _queue_factory(monkeypatch)

    def no_inline_age(dsn):
        raise AssertionError("the delete path must not construct the AGE graph inline")

    monkeypatch.setattr("mempalace.knowledge_graph_age.KnowledgeGraphAGE", no_inline_age)
    col = _Col()
    _attach(col, "postgresql://fake/a")
    assert made == ["postgresql://fake/a"]
    col.delete_hook(drawer_ids=["d1", "d2"])
    assert col.delete_hook.queued == [["d1", "d2"]]


def test_no_graph_no_hook(monkeypatch):
    _env(monkeypatch)
    probes = []
    monkeypatch.setattr(
        "mempalace.kg_writethrough.age_graph_exists", lambda dsn: probes.append(dsn) or False
    )
    made = _queue_factory(monkeypatch)
    col = _Col()
    _attach(col, "postgresql://fake/b")
    assert col.delete_hook is None and made == []
    # a second collection on the same DSN does not probe again
    col_mod._maybe_attach_writethrough(_Col(), "postgresql://fake/b")
    assert probes == ["postgresql://fake/b"]


def test_explicit_opt_out_attaches_nothing(monkeypatch):
    _env(monkeypatch, delete="0")
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: True)
    _queue_factory(monkeypatch)
    col = _Col()
    _attach(col, "postgresql://fake/c")
    assert col.delete_hook is None


def test_age_graph_exists_cannot_tell_when_the_server_is_unreachable():
    from mempalace.kg_writethrough import age_graph_exists

    assert age_graph_exists("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1") is None


# --- the probe's real code, against a fake driver ---------------------------------
import pytest  # noqa: E402


class _PgError(Exception):
    """psycopg 3's interface (.sqlstate) by default; psycopg2's (.pgcode) on request."""

    def __init__(self, code, attr="sqlstate"):
        super().__init__(code)
        setattr(self, attr, code)


class _FakeDriver:
    """Records connect kwargs, every SQL statement, and whether the conn was closed."""

    def __init__(self, rows=None, error=None, connect_error=None):
        self.rows, self.error, self.connect_error = rows, error, connect_error
        self.kwargs, self.sql, self.closed = None, [], False

    def connect(self, dsn, **kw):
        if self.connect_error:
            raise self.connect_error
        self.kwargs = kw
        drv = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q, params=None):
                drv.sql.append(q)
                if drv.error:
                    raise drv.error

            def fetchone(self):
                return drv.rows

        class _Conn:
            def cursor(self):
                return _Cur()

            def close(self):
                drv.closed = True

        return _Conn()


@pytest.mark.parametrize(
    "rows,error,want",
    [
        ((1,), None, True),  # graph + Drawer vertex label listed
        (None, None, False),  # catalog answered: no such graph/label
        (None, _PgError("3F000"), False),  # no ag_catalog schema: AGE not installed
        (None, _PgError("42P01"), False),  # undefined table: AGE not installed
        (None, _PgError("3F000", "pgcode"), False),  # the same, psycopg2-style
        (None, _PgError("57014"), None),  # statement timeout: cannot tell
        (None, _PgError("42501"), None),  # permission denied: cannot tell
        (None, RuntimeError("boom"), None),  # anything else: cannot tell
    ],
)
def test_probe_reads_the_age_catalog_only_and_always_closes(monkeypatch, rows, error, want):
    from mempalace.kg_writethrough import age_graph_exists

    drv = _FakeDriver(rows=rows, error=error)
    monkeypatch.setattr("mempalace.knowledge_graph_age._load_psycopg2", lambda: drv)
    assert age_graph_exists("postgresql://fake/x") is want
    assert drv.closed, "the probe connection must be closed on every path"
    assert drv.kwargs["connect_timeout"] == 5 and "statement_timeout" in drv.kwargs["options"]
    assert len(drv.sql) == 1 and "ag_catalog.ag_graph" in drv.sql[0] and "ag_label" in drv.sql[0]
    assert drv.sql[0].lstrip().upper().startswith("SELECT"), "the probe must never create or alter"


def test_probe_connect_failure_cannot_tell(monkeypatch):
    from mempalace.kg_writethrough import age_graph_exists

    drv = _FakeDriver(connect_error=OSError("refused"))
    monkeypatch.setattr("mempalace.knowledge_graph_age._load_psycopg2", lambda: drv)
    assert age_graph_exists("postgresql://fake/y") is None


# --- attach: idempotence, transient failure, negative-cache expiry ---------------------
class _CountingCol(_Col):
    def __init__(self):
        super().__init__()
        self.sets = 0

    def set_kg_deletethrough(self, hook):
        self.sets += 1
        super().set_kg_deletethrough(hook)


def test_attach_is_idempotent_per_collection(monkeypatch):
    _env(monkeypatch)
    probes = []
    monkeypatch.setattr(
        "mempalace.kg_writethrough.age_graph_exists", lambda dsn: probes.append(dsn) or True
    )
    _queue_factory(monkeypatch)
    col = _CountingCol()
    _attach(col, "postgresql://fake/d")
    col_mod._maybe_attach_writethrough(col, "postgresql://fake/d")
    col_mod._maybe_attach_writethrough(col, "postgresql://fake/d")
    assert col.sets == 1 and len(probes) == 1


def test_a_transient_probe_failure_is_retried_not_cached(monkeypatch):
    _env(monkeypatch)
    answers = [None, True]  # server down, then back
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: answers.pop(0))
    _queue_factory(monkeypatch)
    first, second = _Col(), _Col()
    _attach(first, "postgresql://fake/e")
    assert first.delete_hook is None and "postgresql://fake/e" not in col_mod._no_age_graph
    col_mod._maybe_attach_writethrough(second, "postgresql://fake/e")
    assert second.delete_hook is not None


def test_a_graph_created_after_a_negative_probe_is_seen_once_the_ttl_expires(monkeypatch):
    _env(monkeypatch)
    answers = [False, True]
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: answers.pop(0))
    _queue_factory(monkeypatch)
    first = _Col()
    _attach(first, "postgresql://fake/f")
    assert first.delete_hook is None and col_mod._no_age_graph["postgresql://fake/f"] > 0
    col_mod._maybe_attach_writethrough(_Col(), "postgresql://fake/f")  # inside the TTL: no probe
    assert answers == [True]
    col_mod._no_age_graph["postgresql://fake/f"] = 0.0  # TTL expired
    later = _Col()
    col_mod._maybe_attach_writethrough(later, "postgresql://fake/f")
    assert later.delete_hook is not None


# --- live: the real driver, the real constructor, a real vertex (CI runs AGE) -----------
import os  # noqa: E402

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")
pgmark = pytest.mark.skipif(
    POSTGRES_DSN is None, reason="set TEST_POSTGRES_DSN (Postgres with AGE)"
)


def _drawer_vertices(did):
    import psycopg

    with psycopg.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT count(*) FROM mempalace_kg."Drawer" WHERE properties::text LIKE %s',
            ("%" + did + "%",),
        )
        return cur.fetchone()[0]


@pgmark
def test_live_delete_queues_then_the_sweep_removes_the_vertex(monkeypatch):
    from mempalace.kg_delete_queue import QUEUE_TABLE, sweep
    from mempalace.kg_writethrough import age_graph_exists
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    did = "drawer_558_live_%d" % os.getpid()
    kg._run_cypher("CREATE (:Drawer {id: $id})", {"id": did}, commit=True)
    assert _drawer_vertices(did) == 1
    assert age_graph_exists(POSTGRES_DSN) is True
    assert age_graph_exists(POSTGRES_DSN, graph="no_such_graph_558") is False

    _env(monkeypatch)  # write-through OFF, delete-through default
    col = _Col()
    _attach(col, POSTGRES_DSN)  # the real probe and the real queue hook
    assert col.delete_hook is not None
    col.delete_hook(drawer_ids=[did])
    assert _drawer_vertices(did) == 1, "the delete path only queues"

    import psycopg

    with psycopg.connect(POSTGRES_DSN) as conn:
        queued = conn.execute(
            f"SELECT count(*) FROM {QUEUE_TABLE} WHERE drawer_id = %s", (did,)
        ).fetchone()[0]
    assert queued == 1
    out = sweep(POSTGRES_DSN, kg=kg)
    assert _drawer_vertices(did) == 0 and out["removed"] >= 1 and out["left"] == 0


@pgmark
def test_live_a_database_without_age_is_a_confirmed_absence():
    import psycopg
    from psycopg.conninfo import make_conninfo

    from mempalace.kg_writethrough import age_graph_exists

    name = "mp558_noage_%d" % os.getpid()
    try:
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
            admin.execute("CREATE DATABASE %s" % name)
    except Exception as e:  # noqa: BLE001
        pytest.skip("cannot create a scratch database: %s" % e)
    try:
        assert age_graph_exists(make_conninfo(POSTGRES_DSN, dbname=name)) is False
    finally:
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS %s" % name)


def test_a_new_collection_reusing_a_dead_ones_id_still_gets_its_hook(monkeypatch):
    """xreview of #609: a stale id() in the attached registry must not skip a new object."""
    _env(monkeypatch)
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: True)
    _queue_factory(monkeypatch)
    fresh = _Col()
    col_mod._writethrough_attached.add(id(fresh))  # as if a dead collection had this id
    col_mod._no_age_graph.pop("postgresql://fake/g", None)
    try:
        col_mod._maybe_attach_writethrough(fresh, "postgresql://fake/g")
    finally:
        col_mod._writethrough_attached.discard(id(fresh))
    assert fresh.delete_hook is not None


# --- the queue hook and the sweep, against an in-memory fake -----------------------------
class _QueueDB:
    """A tiny stand-in for the queue table: understands exactly the statements
    kg_delete_queue issues, and records every one."""

    def __init__(self, fail_insert=False):
        self.rows = {}  # drawer_id -> order
        self.sql = []
        self.fail_insert = fail_insert
        self.connects = 0
        self.closed_count = 0

    def connect(self, dsn):
        self.connects += 1
        db = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q, params=None):
                db.sql.append(" ".join(q.split())[:40])
                self._rows = []
                if q.lstrip().startswith("INSERT"):
                    if db.fail_insert:
                        raise RuntimeError("db down")
                    for i in params[0]:
                        db.rows.setdefault(i, len(db.rows))
                elif q.lstrip().startswith("SELECT drawer_id"):
                    ordered = sorted(db.rows, key=lambda k: (db.rows[k], k))
                    self._rows = [(i,) for i in ordered[: params[0]]]
                elif q.lstrip().startswith("DELETE"):
                    for i in params[0]:
                        db.rows.pop(i, None)
                elif q.lstrip().startswith("SELECT count"):
                    self._rows = [(len(db.rows),)]

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0]

        class _Conn:
            closed = 0

            def cursor(self):
                return _Cur()

            def close(self):
                db.closed_count += 1
                self.closed = 1

        return _Conn()


def test_the_hook_only_enqueues_unique_ids_and_creates_the_table_once():
    from mempalace.kg_delete_queue import make_queued_deletethrough

    db = _QueueDB()
    hook = make_queued_deletethrough("postgresql://fake/q", connect=db.connect)
    hook(drawer_ids=["b", "a", "b"])
    hook(drawer_ids=["c"])
    hook(drawer_ids=[])
    assert sorted(db.rows) == ["a", "b", "c"]
    assert sum(1 for q in db.sql if q.startswith("CREATE TABLE")) == 1
    assert db.connects == 1
    assert not any(q.upper().startswith(("MATCH", "SELECT * FROM CYPHER")) for q in db.sql)


def test_a_failed_enqueue_never_raises_and_reconnects_next_time(caplog):
    from mempalace.kg_delete_queue import make_queued_deletethrough

    db = _QueueDB(fail_insert=True)
    hook = make_queued_deletethrough("postgresql://fake/r", connect=db.connect)
    hook(drawer_ids=["a"])  # must not raise
    assert "could not queue 1 id(s)" in caplog.text and db.closed_count == 1
    db.fail_insert = False
    hook(drawer_ids=["a"])
    assert db.connects == 2 and list(db.rows) == ["a"]


def test_the_sweep_deletes_in_batches_oldest_first_and_dequeues_only_after_success():
    from mempalace.kg_delete_queue import sweep

    db = _QueueDB()
    for i in ["d1", "d2", "d3", "d4", "d5"]:
        db.rows[i] = len(db.rows)
    kg = _RecordingKG()
    kg.delete_drawers = lambda ids: (kg.deleted.append(list(ids)), len(ids))[1]
    out = sweep("postgresql://fake/s", kg=kg, batch=2, connect=db.connect)
    assert kg.deleted == [["d1", "d2"], ["d3", "d4"], ["d5"]]
    assert out["batches"] == 3 and out["ids"] == 5 and out["removed"] == 5 and out["left"] == 0
    assert len(out["seconds"]) == 3 and db.closed_count == 1


def test_a_failing_batch_stops_the_sweep_and_keeps_its_ids_queued():
    import pytest as _pt

    from mempalace.kg_delete_queue import sweep

    db = _QueueDB()
    for i in ["d1", "d2", "d3"]:
        db.rows[i] = len(db.rows)

    def boom(ids):
        raise RuntimeError("statement timeout")

    kg = _RecordingKG()
    kg.delete_drawers = boom
    with _pt.raises(RuntimeError):
        sweep("postgresql://fake/t", kg=kg, batch=2, connect=db.connect)
    assert sorted(db.rows) == ["d1", "d2", "d3"] and db.closed_count == 1


def test_sweep_from_env_skips_without_a_dsn_or_a_graph(monkeypatch):
    from mempalace import kg_delete_queue as q

    monkeypatch.delenv("MEMPALACE_KG_DELETETHROUGH", raising=False)
    assert q.sweep_from_env(env={}) == {"skipped": "no MEMPALACE_POSTGRES_DSN"}
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: False)
    assert q.sweep_from_env(env={"MEMPALACE_POSTGRES_DSN": "x"}) == {"skipped": "no AGE graph"}
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: None)
    assert q.sweep_from_env(env={"MEMPALACE_POSTGRES_DSN": "x"}) == {
        "skipped": "graph probe failed"
    }
    monkeypatch.setenv("MEMPALACE_KG_DELETETHROUGH", "0")
    assert "off" in q.sweep_from_env(env={"MEMPALACE_POSTGRES_DSN": "x"})["skipped"]
