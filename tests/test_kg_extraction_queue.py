"""Unit tests for the KG extraction queue writethrough.

These tests use the same psycopg2-cursor-shaped fake as
``test_age_kg_units.py`` — they verify SQL shape and composition without
needing a live postgres. A separate live-postgres integration suite can
exercise the actual DDL when a DSN is available.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ──────────────────────────────────────────────────────────────────────
# Test doubles — psycopg2 cursor/connection mocks
# ──────────────────────────────────────────────────────────────────────


class _FakeCursor:
    """Records every execute() call. Mirrors the pattern in test_age_kg_units."""

    def __init__(self):
        self.executes: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executes.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        self._cursor = _FakeCursor()

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class _FakeModule:
    """Stand-in for the (psycopg2, sql) tuple returned by _load_psycopg2."""

    last_dsn = None
    next_conn: _FakeConn | None = None

    @classmethod
    def connect(cls, dsn):
        cls.last_dsn = dsn
        conn = cls.next_conn if cls.next_conn is not None else _FakeConn()
        cls.next_conn = None
        return conn


@pytest.fixture
def fake_psycopg(monkeypatch):
    """Patch ``mempalace.backends.postgres._load_psycopg2`` to return a fake module.

    Yields a callable that produces a fresh _FakeConn and registers it
    as the next connection ``_FakeModule.connect`` will return — tests
    can collect the conn afterwards via the returned reference.
    """
    from mempalace.backends import postgres as pg_mod

    monkeypatch.setattr(pg_mod, "_load_psycopg2", lambda: (_FakeModule, None))

    created: list[_FakeConn] = []

    def _make_conn():
        conn = _FakeConn()
        created.append(conn)
        _FakeModule.next_conn = conn
        return conn

    yield _make_conn, created


# ──────────────────────────────────────────────────────────────────────
# make_extraction_enqueue_writethrough — SQL shape + idempotency
# ──────────────────────────────────────────────────────────────────────


def test_enqueue_inserts_row(fake_psycopg):
    """First call ensures the table, then INSERTs the drawer row."""
    from mempalace.kg_writethrough import make_extraction_enqueue_writethrough

    make_conn, created = fake_psycopg
    conn = make_conn()

    hook = make_extraction_enqueue_writethrough("postgresql://fake/db")
    hook(
        drawer_id="drw-1",
        document="Some text",
        metadata={"wing": "memorypalace", "room": "decisions"},
    )

    statements = [sql for sql, _ in conn._cursor.executes]
    # First-call table-ensure path runs the CREATE TABLE + index.
    assert any("CREATE TABLE IF NOT EXISTS mempalace_kg_extraction_queue" in s for s in statements)
    assert any("idx_kg_extraction_pending" in s for s in statements)
    # Then the INSERT lands.
    assert any("INSERT INTO mempalace_kg_extraction_queue" in s for s in statements)
    # And the params carry the drawer_id + wing + room.
    insert_params = next(
        params for sql, params in conn._cursor.executes if sql.lstrip().startswith("INSERT")
    )
    assert insert_params == ("drw-1", "memorypalace", "decisions")
    # Connection was committed and closed.
    assert conn.commits >= 1
    assert conn.closed


def test_enqueue_idempotent_on_conflict(fake_psycopg):
    """A second insert with the same drawer_id uses ON CONFLICT DO UPDATE."""
    from mempalace.kg_writethrough import make_extraction_enqueue_writethrough

    make_conn, created = fake_psycopg
    hook = make_extraction_enqueue_writethrough("postgresql://fake/db")

    make_conn()  # register first connection for the initial hook call
    hook(drawer_id="drw-1", document="v1", metadata={"wing": "w", "room": "r"})
    conn_b = make_conn()
    hook(drawer_id="drw-1", document="v2", metadata={"wing": "w", "room": "r"})

    # Second connection should still issue the INSERT ... ON CONFLICT.
    insert_sql = next(
        sql for sql, _ in conn_b._cursor.executes if sql.lstrip().startswith("INSERT")
    )
    assert "ON CONFLICT (drawer_id) DO UPDATE" in insert_sql
    # And it should clear started_at/completed_at/error/worker_id so the
    # worker re-processes the row.
    assert "started_at   = NULL" in insert_sql
    assert "completed_at = NULL" in insert_sql
    assert "error        = NULL" in insert_sql
    assert "worker_id    = NULL" in insert_sql


def test_re_enqueue_clears_completion(fake_psycopg):
    """Re-mine after completion bumps queued_at to NOW() (not the old value)."""
    from mempalace.kg_writethrough import make_extraction_enqueue_writethrough

    make_conn, _created = fake_psycopg
    hook = make_extraction_enqueue_writethrough("postgresql://fake/db")

    conn = make_conn()
    hook(drawer_id="drw-X", document="anything", metadata={"wing": "w", "room": "r"})

    insert_sql = next(sql for sql, _ in conn._cursor.executes if sql.lstrip().startswith("INSERT"))
    # queued_at must be reset on conflict — guarantees the worker treats
    # the row as freshly pending.
    assert "queued_at    = NOW()" in insert_sql


def test_env_flag_off_means_no_writethrough_call(monkeypatch):
    """Composition: with both env flags off, make_writethrough_from_env returns None."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.delenv("MEMPALACE_KG_WRITETHROUGH", raising=False)
    monkeypatch.delenv("MEMPALACE_KG_EXTRACTION_QUEUE", raising=False)

    assert make_writethrough_from_env(kg=None, dsn=None) is None


def test_env_flag_on_adds_to_existing_chain(monkeypatch, fake_psycopg):
    """Both flags on: MENTIONS hook fires AND the enqueue hook fires per drawer."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "regex")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "1")

    make_conn, created = fake_psycopg
    queue_conn = make_conn()

    kg = MagicMock()  # captures add_mention calls — proves MENTIONS still runs
    hook = make_writethrough_from_env(kg=kg, dsn="postgresql://fake/db")
    assert hook is not None

    hook(
        drawer_id="drw-chain",
        document="Anthropic ships Claude and palace-daemon.",
        metadata={"wing": "memorypalace", "room": "decisions"},
    )

    # MENTIONS stage ran (regex extractor → kg.replace_mentions, one call for the drawer).
    assert kg.replace_mentions.called
    # Queue stage ran on the patched fake connection.
    insert_sqls = [
        sql for sql, _ in queue_conn._cursor.executes if sql.lstrip().startswith("INSERT")
    ]
    assert any("INSERT INTO mempalace_kg_extraction_queue" in s for s in insert_sqls)


def test_env_queue_only_skips_mentions(monkeypatch, fake_psycopg):
    """Queue-only mode: enqueue runs without an AGE kg instance."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.delenv("MEMPALACE_KG_WRITETHROUGH", raising=False)
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "1")
    monkeypatch.setenv("MEMPALACE_POSTGRES_DSN", "postgresql://fake/db")

    make_conn, _created = fake_psycopg
    conn = make_conn()

    # No kg required when MENTIONS is off.
    hook = make_writethrough_from_env(kg=None)
    assert hook is not None
    hook(drawer_id="drw-queue", document="x", metadata={"wing": "w", "room": "r"})

    insert_sqls = [sql for sql, _ in conn._cursor.executes if sql.lstrip().startswith("INSERT")]
    assert any("INSERT INTO mempalace_kg_extraction_queue" in s for s in insert_sqls)


def test_env_queue_on_requires_dsn(monkeypatch):
    """Missing both ``dsn=`` and MEMPALACE_POSTGRES_DSN raises."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.delenv("MEMPALACE_KG_WRITETHROUGH", raising=False)
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "1")
    monkeypatch.delenv("MEMPALACE_POSTGRES_DSN", raising=False)

    with pytest.raises(ValueError, match="requires a dsn"):
        make_writethrough_from_env(kg=None, dsn=None)


# ──────────────────────────────────────────────────────────────────────
# Per-stage attachment logging — make silent-OFF visible (issue #286)
# ──────────────────────────────────────────────────────────────────────


def test_silent_off_extraction_queue_logs_only_mentions(monkeypatch, caplog):
    """Writethrough on + extraction-queue env unset: only the MENTIONS log line fires.

    Regression for techempower-org/mempalace#286 — on 2026-05-27 the
    extraction-queue stage was silently OFF and ~12,300 drawers were
    written without enqueueing. One INFO log line per stage attached
    makes that condition observable at startup.
    """
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.delenv("MEMPALACE_KG_EXTRACTION_QUEUE", raising=False)

    caplog.set_level("INFO", logger="mempalace.kg_writethrough")
    hook = make_writethrough_from_env(kg=MagicMock())
    assert hook is not None

    attach_records = [r for r in caplog.records if r.getMessage().startswith("kg_writethrough: ")]
    assert len(attach_records) == 1, [r.getMessage() for r in attach_records]
    msg = attach_records[0].getMessage()
    assert "MENTIONS stage attached" in msg
    assert "extraction-queue stage attached" not in msg


def test_both_stages_log_independently(monkeypatch, caplog, fake_psycopg):
    """Both env flags on: two INFO log lines, one per stage, each naming its flag."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.setenv("MEMPALACE_KG_WRITETHROUGH", "1")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTOR", "regex")
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "1")

    caplog.set_level("INFO", logger="mempalace.kg_writethrough")
    hook = make_writethrough_from_env(kg=MagicMock(), dsn="postgresql://fake/db")
    assert hook is not None

    messages = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("kg_writethrough: ")
    ]
    assert len(messages) == 2, messages
    assert any(
        "MENTIONS stage attached" in m and "MEMPALACE_KG_WRITETHROUGH=1" in m for m in messages
    )
    assert any(
        "extraction-queue stage attached" in m and "MEMPALACE_KG_EXTRACTION_QUEUE=1" in m
        for m in messages
    )


def test_no_stages_no_attach_logs(monkeypatch, caplog):
    """Both flags off: composer returns None and emits no attach logs."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.delenv("MEMPALACE_KG_WRITETHROUGH", raising=False)
    monkeypatch.delenv("MEMPALACE_KG_EXTRACTION_QUEUE", raising=False)

    caplog.set_level("INFO", logger="mempalace.kg_writethrough")
    assert make_writethrough_from_env(kg=None, dsn=None) is None

    attach_records = [r for r in caplog.records if r.getMessage().startswith("kg_writethrough: ")]
    assert attach_records == []


def test_silent_off_mentions_logs_only_extraction_queue(monkeypatch, caplog, fake_psycopg):
    """Queue env on + writethrough unset: only the extraction-queue log line fires."""
    from mempalace.kg_writethrough import make_writethrough_from_env

    monkeypatch.delenv("MEMPALACE_KG_WRITETHROUGH", raising=False)
    monkeypatch.setenv("MEMPALACE_KG_EXTRACTION_QUEUE", "1")
    monkeypatch.setenv("MEMPALACE_POSTGRES_DSN", "postgresql://fake/db")

    caplog.set_level("INFO", logger="mempalace.kg_writethrough")
    hook = make_writethrough_from_env(kg=None)
    assert hook is not None

    attach_records = [r for r in caplog.records if r.getMessage().startswith("kg_writethrough: ")]
    assert len(attach_records) == 1, [r.getMessage() for r in attach_records]
    msg = attach_records[0].getMessage()
    assert "extraction-queue stage attached" in msg
    assert "MENTIONS stage attached" not in msg
