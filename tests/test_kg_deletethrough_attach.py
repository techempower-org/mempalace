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

    def factory(dsn, table=None):
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
    col_mod._deletethrough_attached.discard(id(col))
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
import pytest as _pt  # noqa: E402


class _QueueDB:
    """Stand-in for the queue table and the drawer table: understands exactly the
    statements kg_delete_queue issues, records them, and can fail on request."""

    def __init__(self):
        self.queue = {}  # drawer_id -> [order, source_table, version, settled]
        self.drawers = set()  # ids that have a live drawer row
        self.vertices = None  # ids that have a Drawer vertex (None: every id does)
        self.sql = []
        self.fail = set()  # statement kinds to fail: "insert", "dequeue"
        self.on_select = None  # callback run right after a batch SELECT
        self.connects = 0
        self.closed_count = 0
        self._order = 0
        self._seq = 0
        self.lock_owner = None

    def enqueue(self, *ids, table="mempalace_drawers", settled=True, sequence=True):
        """``sequence``: versions from a global sequence (the shipped SQL); False
        mimics a per-row counter that restarts at 1 when the row is re-created."""
        for i in ids:
            self._seq += 1
            if i in self.queue:
                self.queue[i][2] = self._seq if sequence else self.queue[i][2] + 1
                self.queue[i][3] = settled
            else:
                self._order += 1
                self.queue[i] = [self._order, table, self._seq if sequence else 1, settled]

    def connect(self, dsn):
        self.connects += 1
        db = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q, params=None):
                q1 = " ".join(q.split())
                db.sql.append(q1[:60])
                self._rows = []
                if q1.startswith("SELECT pg_try_advisory_lock"):
                    ok = db.lock_owner in (None, conn)
                    if ok:
                        db.lock_owner = conn
                    self._rows = [(ok,)]
                elif q1.startswith("INSERT"):
                    if "insert" in db.fail:
                        raise RuntimeError("db down")
                    db.enqueue(*params[0], table=params[1], settled=False, sequence="nextval" in q1)
                elif q1.startswith("SELECT drawer_id"):
                    _hours, held, limit = params
                    ordered = [
                        k
                        for k in sorted(db.queue, key=lambda k: (db.queue[k][0], k))
                        if k not in held
                    ]
                    self._rows = [
                        (i, db.queue[i][1], db.queue[i][2], db.queue[i][3]) for i in ordered[:limit]
                    ]
                    if db.on_select:
                        db.on_select()
                elif q1.startswith("SELECT ((properties"):
                    if "vertex" in db.fail:
                        raise RuntimeError("vertex lookup failed")
                    have = params[0] if db.vertices is None else db.vertices
                    self._rows = [(i,) for i in params[0] if i in have]
                elif q1.startswith("SELECT id FROM"):
                    self._rows = [(i,) for i in params[0] if i in db.drawers]
                elif q1.startswith("DELETE FROM mempalace_kg_delete_queue"):
                    if "dequeue" in db.fail:
                        raise RuntimeError("dequeue failed")
                    by_version = "q.version = a.version" in q1  # honour the SQL as sent
                    for i, v in zip(params[0], params[1]):
                        if i in db.queue and (db.queue[i][2] == v or not by_version):
                            del db.queue[i]
                elif q1.startswith("SELECT count"):
                    self._rows = [(len(db.queue),)]

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
                if db.lock_owner is self:
                    db.lock_owner = None  # a session advisory lock ends with its session

        conn = _Conn()
        return conn


class _SweepKG:
    def __init__(self, fail_on_call=None):
        self.calls, self.fail_on_call = [], fail_on_call

    def delete_drawers(self, ids):
        self.calls.append(list(ids))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("statement timeout")
        return len(ids)


def test_the_hook_only_enqueues_unique_ids_and_creates_the_table_once():
    from mempalace.kg_delete_queue import make_queued_deletethrough

    db = _QueueDB()
    hook = make_queued_deletethrough("postgresql://fake/q", connect=db.connect)
    hook(drawer_ids=["b", "a", "b"])
    hook(drawer_ids=["c"])
    hook(drawer_ids=[])
    assert sorted(db.queue) == ["a", "b", "c"]
    assert sum(1 for q in db.sql if q.startswith("CREATE SEQUENCE")) == 1  # the DDL, once
    assert db.connects == 1
    assert not any("cypher" in q.lower() or q.startswith("MATCH") for q in db.sql)


def test_a_failed_enqueue_never_raises_and_reconnects_next_time(caplog):
    from mempalace.kg_delete_queue import make_queued_deletethrough

    db = _QueueDB()
    db.fail.add("insert")
    hook = make_queued_deletethrough("postgresql://fake/r", connect=db.connect)
    hook(drawer_ids=["a"])  # must not raise
    assert "could not queue 1 id(s)" in caplog.text and db.closed_count == 1
    db.fail.clear()
    hook(drawer_ids=["a"])
    assert db.connects == 2 and list(db.queue) == ["a"]


def test_a_hostile_ids_iterable_never_raises(caplog):
    from mempalace.kg_delete_queue import make_queued_deletethrough

    def bad():
        yield "a"
        raise RuntimeError("iterator broke")

    db = _QueueDB()
    make_queued_deletethrough("postgresql://fake/h", connect=db.connect)(drawer_ids=bad())
    assert "could not queue ? id(s)" in caplog.text and db.queue == {}


def test_an_unsafe_drawer_table_name_is_refused():
    from mempalace.kg_delete_queue import make_queued_deletethrough

    with _pt.raises(ValueError):
        make_queued_deletethrough("postgresql://fake/x", table='drawers"; DROP TABLE x; --')


def test_the_sweep_batches_oldest_first_and_enforces_max_batches():
    from mempalace.kg_delete_queue import sweep

    db = _QueueDB()
    db.enqueue("d1", "d2", "d3", "d4", "d5")
    kg = _SweepKG()
    out = sweep("postgresql://fake/s", kg=kg, batch=2, max_batches=2, connect=db.connect)
    assert kg.calls == [["d1", "d2"], ["d3", "d4"]]
    assert out["batches"] == 2 and out["removed"] == 4 and out["left"] == 1
    assert sorted(db.queue) == ["d5"] and len(out["seconds"]) == 2 and db.closed_count == 1


def test_a_settled_live_drawer_is_acknowledged_without_touching_its_vertex():
    from mempalace.kg_delete_queue import sweep

    db = _QueueDB()
    db.enqueue("gone", "back")  # entries older than the settling window
    db.drawers.add("back")  # re-mined under the same id, or the delete rolled back
    kg = _SweepKG()
    out = sweep("postgresql://fake/l", kg=kg, connect=db.connect)
    assert kg.calls == [["gone"]] and out["live_skipped"] == 1 and db.queue == {}


def test_an_uncommitted_delete_stays_queued_until_it_lands():
    """Defence in depth: for a caller that held its delete open, a fresh entry
    whose row is still visible waits instead of being acknowledged as live."""
    from mempalace.kg_delete_queue import make_queued_deletethrough, sweep

    db = _QueueDB()
    db.drawers.add("d1")  # the DELETE has not committed yet
    make_queued_deletethrough("postgresql://fake/u2", connect=db.connect)(drawer_ids=["d1"])
    kg = _SweepKG()
    out = sweep("postgresql://fake/u2", kg=kg, batch=10, max_batches=5, connect=db.connect)
    assert kg.calls == [] and list(db.queue) == ["d1"] and out["waiting"] == 1
    assert out["batches"] == 1, "a held id is not re-read by later batches of the same sweep"
    db.drawers.discard("d1")  # the transaction commits
    sweep("postgresql://fake/u2", kg=kg, connect=db.connect)
    assert kg.calls == [["d1"]] and db.queue == {}


def test_a_re_delete_during_a_batch_stays_queued_for_the_next_sweep():
    from mempalace.kg_delete_queue import make_queued_deletethrough, sweep

    db = _QueueDB()
    db.enqueue("d1", "d2")
    hook = make_queued_deletethrough("postgresql://fake/v", connect=db.connect)
    fired = []

    def redelete():
        if not fired:
            fired.append(1)
            hook(drawer_ids=["d1"])  # the same id deleted again while the batch is in flight

    db.on_select = redelete
    sweep("postgresql://fake/v", kg=_SweepKG(), batch=10, max_batches=1, connect=db.connect)
    assert list(db.queue) == ["d1"]


def test_a_stale_ack_cannot_remove_a_re_created_entry():
    """Another sweeper acknowledged d1 and a new delete re-created it while this
    sweep's batch was in flight; versions never repeat, so this sweep's ack misses."""
    from mempalace.kg_delete_queue import make_queued_deletethrough, sweep

    db = _QueueDB()
    db.enqueue("d1")
    hook = make_queued_deletethrough("postgresql://fake/o", connect=db.connect)
    fired = []

    def other_sweep_acked_then_new_delete():
        if not fired:
            fired.append(1)
            del db.queue["d1"]
            hook(drawer_ids=["d1"])

    db.on_select = other_sweep_acked_then_new_delete
    sweep("postgresql://fake/o", kg=_SweepKG(), batch=10, max_batches=1, connect=db.connect)
    assert list(db.queue) == ["d1"]


def test_only_one_sweeper_runs_at_a_time():
    from mempalace.kg_delete_queue import sweep

    db = _QueueDB()
    db.enqueue("d1")
    holder = db.connect("x")
    with holder.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (1,))
    kg = _SweepKG()
    assert sweep("postgresql://fake/p", kg=kg, connect=db.connect) == {
        "skipped": "another sweep is running"
    }
    assert kg.calls == [] and list(db.queue) == ["d1"]
    holder.close()
    sweep("postgresql://fake/p", kg=kg, connect=db.connect)
    assert kg.calls == [["d1"]] and db.lock_owner is None


def test_a_second_batch_failure_keeps_it_and_the_rest_queued():
    from mempalace.kg_delete_queue import sweep

    db = _QueueDB()
    db.enqueue("d1", "d2", "d3", "d4", "d5")
    kg = _SweepKG(fail_on_call=2)
    with _pt.raises(RuntimeError):
        sweep("postgresql://fake/t", kg=kg, batch=2, connect=db.connect)
    assert sorted(db.queue) == ["d3", "d4", "d5"] and db.closed_count == 1


def test_a_failed_dequeue_after_the_graph_delete_is_retried_harmlessly():
    from mempalace.kg_delete_queue import sweep

    db = _QueueDB()
    db.enqueue("d1", "d2")
    db.fail.add("dequeue")
    with _pt.raises(RuntimeError):
        sweep("postgresql://fake/u", kg=_SweepKG(), connect=db.connect)
    assert sorted(db.queue) == ["d1", "d2"]
    db.fail.clear()

    class _AlreadyGone(_SweepKG):
        def delete_drawers(self, ids):
            self.calls.append(list(ids))
            return 0  # the vertices went last time

    kg = _AlreadyGone()
    out = sweep("postgresql://fake/u", kg=kg, connect=db.connect)
    assert kg.calls == [["d1", "d2"]] and out["removed"] == 0 and db.queue == {}


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


# --- attach with write-through ON: the delete side is still retried -------------------
class _BothCol(_Col):
    def __init__(self):
        super().__init__()
        self.write_hook = None

    def set_kg_writethrough(self, hook):
        self.write_hook = hook


def test_with_write_through_on_a_failed_probe_is_retried_for_the_delete_side(monkeypatch):
    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "null")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "0")
    monkeypatch.delenv("MEMPALACE_KG_DELETETHROUGH", raising=False)
    monkeypatch.setattr(
        "mempalace.knowledge_graph_age.KnowledgeGraphAGE", lambda dsn: _RecordingKG()
    )
    answers = [None, True]
    monkeypatch.setattr("mempalace.kg_writethrough.age_graph_exists", lambda dsn: answers.pop(0))
    made = _queue_factory(monkeypatch)
    col = _BothCol()
    _attach(col, "postgresql://fake/w")
    assert col.write_hook is not None and col.delete_hook is None
    col_mod._maybe_attach_writethrough(col, "postgresql://fake/w")
    assert col.delete_hook is not None and made == ["postgresql://fake/w"]
    first_write = col.write_hook
    col_mod._maybe_attach_writethrough(col, "postgresql://fake/w")
    assert col.write_hook is first_write, "write-through must not be re-attached"


@pgmark
def test_live_an_uncommitted_delete_is_held_then_swept_after_commit():
    import psycopg

    from mempalace.kg_delete_queue import make_queued_deletethrough, sweep
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    table = "mp558_drawers_%d" % os.getpid()
    did = "drawer_558_txn_%d" % os.getpid()
    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    kg._run_cypher("CREATE (:Drawer {id: $id})", {"id": did}, commit=True)
    with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
        admin.execute("CREATE TABLE %s (id TEXT PRIMARY KEY)" % table)
        admin.execute("INSERT INTO %s VALUES (%%s)" % table, (did,))
    deleter = psycopg.connect(POSTGRES_DSN)  # a transaction left open on purpose
    try:
        deleter.execute("DELETE FROM %s WHERE id = %%s" % table, (did,))
        make_queued_deletethrough(POSTGRES_DSN, table=table)(drawer_ids=[did])
        held = sweep(POSTGRES_DSN, kg=kg)
        assert held["waiting"] >= 1 and _drawer_vertices(did) == 1
        deleter.commit()
        swept = sweep(POSTGRES_DSN, kg=kg)
        assert _drawer_vertices(did) == 0 and swept["removed"] >= 1
    finally:
        deleter.close()
        with psycopg.connect(POSTGRES_DSN, autocommit=True) as admin:
            admin.execute("DROP TABLE IF EXISTS %s" % table)


def test_the_postgres_collection_connection_is_autocommit(monkeypatch):
    """The production caller's invariant the sweep relies on: the collection's
    connection is autocommit, so a delete has committed when the hook fires."""
    import mempalace.backends.postgres as pg

    made = []

    class _FakePsycopg:
        @staticmethod
        def connect(dsn):
            class _C:
                closed = 0
                autocommit = False

            made.append(_C())
            return made[-1]

    monkeypatch.setattr(pg, "_load_psycopg2", lambda: (_FakePsycopg, None))
    col = pg.PostgresCollection.__new__(pg.PostgresCollection)
    col._conn, col.dsn = None, "postgresql://fake/ac"
    monkeypatch.setattr(col, "_apply_session_settings", lambda conn: None, raising=False)
    assert col._get_conn().autocommit is True


# --- the vertex filter, the lazy AGE connection and the time budget (2026-11-13 live) ---
def test_ids_without_a_vertex_never_reach_the_graph_and_build_no_connection(monkeypatch):
    from mempalace.kg_delete_queue import sweep

    db = _QueueDB()
    db.enqueue("v1", "n1", "n2")
    db.vertices = {"v1"}
    built = []

    class _KG(_SweepKG):
        def __init__(self, dsn):
            super().__init__()
            built.append(dsn)

    monkeypatch.setattr("mempalace.knowledge_graph_age.KnowledgeGraphAGE", _KG)
    out = sweep("postgresql://fake/nv", connect=db.connect)
    assert out["no_vertex"] == 2 and out["removed"] == 1 and db.queue == {}
    assert len(built) == 1 and "statement_timeout=900000" in built[0]

    db.enqueue("n3", "n4")  # none of these has a vertex: no AGE connection at all
    built.clear()
    out = sweep("postgresql://fake/nv", connect=db.connect)
    assert built == [] and out["removed"] == 0 and out["no_vertex"] == 2 and db.queue == {}


def test_the_time_budget_lets_a_started_batch_finish_and_starts_no_more(monkeypatch):
    from mempalace import kg_delete_queue as q

    clock = [0.0]
    monkeypatch.setattr(q.time, "monotonic", lambda: clock[0])
    db = _QueueDB()
    db.enqueue("d1", "d2", "d3")

    class _SlowKG(_SweepKG):
        def delete_drawers(self, ids):
            clock[0] += 100.0  # each batch takes 100 s
            return super().delete_drawers(ids)

    kg = _SlowKG()
    out = q.sweep("postgresql://fake/b", kg=kg, batch=1, budget_s=150, connect=db.connect)
    assert kg.calls == [["d1"], ["d2"]] and out["stopped"] == "budget"
    assert sorted(db.queue) == ["d3"], "finished batches are acknowledged; the rest stay queued"


class _ErrCur:
    def __init__(self, err):
        self.err = err

    def execute(self, q, params=None):
        raise self.err

    def fetchall(self):
        return []


@_pt.mark.parametrize("code", ["42P01", "3F000"])
def test_vertex_lookup_reads_a_missing_label_table_as_no_vertices(code):
    from mempalace.kg_delete_queue import _ids_with_vertex

    assert _ids_with_vertex(_ErrCur(_PgError(code)), ["a"]) == []


def test_vertex_lookup_failure_keeps_the_batch_queued():
    from mempalace import kg_delete_queue as q

    with _pt.raises(_PgError):
        q._ids_with_vertex(_ErrCur(_PgError("57014")), ["a"])
    db = _QueueDB()
    db.enqueue("d1")
    db.fail.add("vertex")
    with _pt.raises(RuntimeError):
        q.sweep("postgresql://fake/vf", kg=_SweepKG(), connect=db.connect)
    assert list(db.queue) == ["d1"]


@_pt.mark.parametrize(
    "dsn,want",
    [
        ("postgresql://u@h/db", "postgresql://u@h/db?options=-c%20statement_timeout=900000"),
        ("postgresql://u@h/db?sslmode=disable", "&options=-c%20statement_timeout=900000"),
        ("host=h dbname=db", "host=h dbname=db options='-c statement_timeout=900000'"),
    ],
)
def test_long_statements_without_the_driver(monkeypatch, dsn, want):
    import builtins

    from mempalace import kg_delete_queue as q

    real = builtins.__import__

    def no_psycopg(name, *a, **k):
        if name.startswith("psycopg"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_psycopg)
    assert want in q._long_statements(dsn)
