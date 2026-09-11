"""A cached postgres connection must survive a server-side disconnect (#385).

Measured live on the production daemon 2026-08-08: after any server-side
disconnect (DB restart, ``pg_terminate_backend``, or the ``idle_session_timeout
= 10min`` now set on the palace DB), the *first* call on an unguarded
cached-connection path raises raw and surfaces as a 500; the call after that
heals.

The mechanism, empirically confirmed on psycopg 3::

    closed before:                              False
    closed immediately after server-side kill:  False | broken: False
    query after kill raised:                    AdminShutdown
    closed AFTER failed query:                  True  | broken: True

``.closed`` is a client-side flag updated only on I/O, so ``_get_conn()``
cheerfully hands back a dead connection and the next ``execute()`` raises.
These tests drive that exact sequence through a fake connection.
"""

import pytest

from mempalace.backends import postgres as pg


def _real_sql():
    """Real psycopg SQL composition — these tests drive real collection methods.

    The ``test-linux`` CI job installs mempalace without the ``postgres``
    extra, so psycopg is absent there and a module-scope import would fail
    collection outright (it did, on main, for #453's bind-site tests).
    """
    return pytest.importorskip("psycopg.sql", reason="requires the postgres extra")


class _FakePsycopgError(Exception):
    """Stands in for ``psycopg.Error`` — the base of every driver error."""


class _FakeOperationalError(_FakePsycopgError):
    """Stands in for ``psycopg.OperationalError`` (AdminShutdown's base)."""


class _FakeInterfaceError(_FakePsycopgError):
    """Stands in for ``psycopg.InterfaceError`` (the connection is closed)."""


class _FakeProgrammingError(_FakePsycopgError):
    """A statement-level error: the query was wrong, the socket is fine."""


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        text = str(sql)
        self.conn.executed.append(text)
        # Session GUCs land at connect time, before the server kills anything;
        # scripting a failure onto them would test the wrong moment.
        if text.startswith("SET "):
            return self
        exc = self.conn.raise_next.pop(0) if self.conn.raise_next else None
        if exc is not None:
            # A server-side kill marks the connection closed only *after* the
            # failed statement — which is the whole bug.
            if isinstance(exc, (_FakeOperationalError, _FakeInterfaceError)):
                self.conn.closed = 1
            raise exc
        return self

    def fetchone(self):
        return self.conn.fetchone_value

    def fetchall(self):
        return self.conn.fetchall_value


class _FakeConn:
    def __init__(self, raise_next=None, fetchone_value=(7,)):
        self.executed = []
        self.autocommit = None
        self.closed = 0
        self.raise_next = list(raise_next or [])
        self.fetchone_value = fetchone_value
        self.fetchall_value = []
        self.close_calls = 0

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.close_calls += 1
        self.closed = 1


class _FakeMod:
    """A psycopg stand-in that hands out a scripted series of connections."""

    OperationalError = _FakeOperationalError
    InterfaceError = _FakeInterfaceError
    ProgrammingError = _FakeProgrammingError
    Error = _FakePsycopgError

    def __init__(self, conns):
        self.conns = list(conns)
        self.handed_out = []

    def connect(self, dsn, **kwargs):
        conn = self.conns.pop(0) if self.conns else _FakeConn()
        self.handed_out.append(conn)
        return conn


@pytest.fixture()
def collection(monkeypatch):
    """Return a factory: give it connections, get a wired-up collection."""

    def _make(*conns):
        mod = _FakeMod(conns)
        monkeypatch.setattr(pg, "_load_psycopg2", lambda: (mod, _real_sql()))
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
        return col, mod

    return _make


# ---------------------------------------------------------------------------
# The reported symptom
# ---------------------------------------------------------------------------


def test_count_survives_a_server_side_disconnect(collection):
    """``count()`` is the path that produced raw 500s during the outage."""
    dead = _FakeConn(raise_next=[_FakeOperationalError("server closed the connection")])
    live = _FakeConn(fetchone_value=(42,))
    col, mod = collection(dead, live)

    col._get_conn()  # prime the cache with the soon-to-be-dead connection
    assert col.count() == 42
    assert len(mod.handed_out) == 2, "expected exactly one reconnect"


def test_get_survives_a_server_side_disconnect(collection):
    """Every cached-conn read path shares the seam, not just count()."""
    dead = _FakeConn(raise_next=[_FakeInterfaceError("the connection is closed")])
    live = _FakeConn()
    col, mod = collection(dead, live)

    col._get_conn()
    col.get(ids=["d1"])
    assert len(mod.handed_out) == 2


def test_dead_connection_is_closed_not_leaked(collection):
    """The 37.5h postmaster wedge was two forever-cached idle connections."""
    dead = _FakeConn(raise_next=[_FakeOperationalError("terminating connection")])
    live = _FakeConn(fetchone_value=(1,))
    col, _mod = collection(dead, live)

    col._get_conn()
    col.count()
    assert dead.close_calls == 1, "the dead socket must be released, not abandoned"


# ---------------------------------------------------------------------------
# The retry must not mask real errors, and must not loop
# ---------------------------------------------------------------------------


def test_statement_error_is_reraised_without_reconnecting(collection):
    """A bad query is the caller's fault — the connection is perfectly fine."""
    conn = _FakeConn(raise_next=[_FakeProgrammingError('relation "nope" does not exist')])
    col, mod = collection(conn)

    col._get_conn()
    with pytest.raises(_FakeProgrammingError):
        col.count()
    assert len(mod.handed_out) == 1, "a statement error must not churn the connection"


def test_only_one_retry_then_the_error_surfaces(collection):
    """If the database is genuinely down, fail — do not spin."""
    dead1 = _FakeConn(raise_next=[_FakeOperationalError("server closed the connection")])
    dead2 = _FakeConn(raise_next=[_FakeOperationalError("connection refused")])
    col, mod = collection(dead1, dead2)

    col._get_conn()
    with pytest.raises(_FakeOperationalError):
        col.count()
    assert len(mod.handed_out) == 2, "exactly one retry, not a loop"


def test_connection_error_detected_by_message_when_class_is_unfamiliar(collection):
    """Drivers wrap some disconnects in classes we do not import by name."""
    assert pg._is_connection_error(RuntimeError("server closed the connection unexpectedly"))
    assert pg._is_connection_error(RuntimeError("the connection is closed"))
    assert not pg._is_connection_error(RuntimeError("division by zero"))


# ---------------------------------------------------------------------------
# The replacement connection must be a *complete* connection
# ---------------------------------------------------------------------------


def test_reconnect_reapplies_session_settings(monkeypatch, collection):
    """hnsw.iterative_scan (#446) is session-scoped — a silent loss is a
    wing-scoped search that starts returning zero rows."""
    monkeypatch.delenv("MEMPALACE_PG_HNSW_ITERATIVE_SCAN", raising=False)
    dead = _FakeConn(raise_next=[_FakeOperationalError("server closed the connection")])
    live = _FakeConn(fetchone_value=(1,))
    col, _mod = collection(dead, live)

    col._get_conn()
    col.count()

    assert "SET hnsw.iterative_scan = relaxed_order" in live.executed
    assert live.autocommit is True
