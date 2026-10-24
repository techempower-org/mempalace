"""Tests for the postgres SQL group-by fast path in ``tool_status`` (#267).

``tool_status`` previously swept every drawer's metadata in Python — ~29s
on the production palace (~370k drawers under postgres), well past curl's
default 15s timeout. The fast path branches on ``_config.backend ==
"postgres"`` and runs three SQL queries against ``mempalace_drawers``
wrapped in a 3s ``statement_timeout``. These tests verify the SQL path
is taken on postgres, the envelope shape matches the chroma path, the
chroma path still works (regression guard), and that the fast path falls
back gracefully when postgres is configured but unreachable.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from mempalace import mcp_server


def _reset_caches() -> None:
    mcp_server._client_cache = None
    mcp_server._collection_cache = None
    mcp_server._postgres_backend_cache = None
    mcp_server._metadata_cache = None
    mcp_server._metadata_cache_time = 0
    mcp_server._vector_disabled = False
    mcp_server._vector_disabled_reason = ""
    mcp_server._vector_capacity_status = None


def _postgres_env() -> dict:
    """Env that selects the postgres backend + provides a stub DSN."""
    env = {k: v for k, v in os.environ.items() if k != "MEMPALACE_BACKEND"}
    env["MEMPALACE_BACKEND"] = "postgres"
    env["MEMPALACE_POSTGRES_DSN"] = "postgresql://stub/test"
    return env


def _chroma_env() -> dict:
    """Env that selects the chroma backend."""
    return {k: v for k, v in os.environ.items() if k != "MEMPALACE_BACKEND"}


class _FakeCursor:
    """psycopg-style cursor stub recording SQL + returning canned rows."""

    def __init__(self, total: int, wing_rows, room_rows, recorder):
        self._total = total
        self._wing_rows = list(wing_rows)
        self._room_rows = list(room_rows)
        self._recorder = recorder
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql_obj, params=None):
        text = sql_obj.as_string({}) if hasattr(sql_obj, "as_string") else str(sql_obj)
        self._recorder.append(text)
        self._last = text

    def fetchone(self):
        return (self._total,)

    def fetchall(self):
        if "GROUP BY wing" in (self._last or ""):
            return self._wing_rows
        if "GROUP BY room" in (self._last or ""):
            return self._room_rows
        return []


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return self._cursor


def _stub_psycopg_connect(total, wing_rows, room_rows, recorder):
    """Return a ``psycopg.connect`` replacement yielding the fake conn."""

    def _connect(*_args, **_kwargs):
        return _FakeConn(_FakeCursor(total, wing_rows, room_rows, recorder))

    return _connect


def test_tool_status_postgres_uses_sql_group_by(monkeypatch):
    """When _config.backend == "postgres", tool_status takes the SQL path
    (three queries: count, GROUP BY wing, GROUP BY room) and does NOT
    call _get_cached_metadata."""
    _reset_caches()
    sql_log: list[str] = []
    monkeypatch.setattr(os, "environ", _postgres_env())

    psycopg = pytest.importorskip("psycopg")

    monkeypatch.setattr(
        psycopg,
        "connect",
        _stub_psycopg_connect(
            total=370_000,
            wing_rows=[("wing_user", 100_000), ("wing_code", 270_000)],
            room_rows=[("hall_facts", 50_000), ("hall_events", 320_000)],
            recorder=sql_log,
        ),
    )

    with patch.object(mcp_server, "_get_cached_metadata") as cached_meta:
        result = mcp_server.tool_status()

    cached_meta.assert_not_called()
    # statement_timeout guard + three SELECTs against mempalace_drawers.
    assert any("statement_timeout" in s for s in sql_log)
    assert any("count(*)" in s and "FROM" in s for s in sql_log)
    assert any("GROUP BY wing" in s for s in sql_log)
    assert any("GROUP BY room" in s for s in sql_log)
    # Table name comes from config and lands in the SQL identifier.
    assert any("mempalace_drawers" in s for s in sql_log)
    # Envelope matches chroma path.
    assert result["total_drawers"] == 370_000
    assert result["wings"] == {"wing_user": 100_000, "wing_code": 270_000}
    assert result["rooms"] == {"hall_facts": 50_000, "hall_events": 320_000}
    assert result["protocol"] == mcp_server.PALACE_PROTOCOL
    assert result["aaak_dialect"] == mcp_server.AAAK_SPEC


def test_tool_status_postgres_envelope_keys_match_chroma_shape(monkeypatch):
    """Fast-path envelope must carry the exact same top-level keys the
    legacy chroma path returns (``total_drawers``, ``wings``, ``rooms``,
    ``protocol``, ``aaak_dialect``). Existing callers — including
    palace-daemon's fork-side fast-intercept — depend on byte-compat."""
    _reset_caches()
    monkeypatch.setattr(os, "environ", _postgres_env())

    psycopg = pytest.importorskip("psycopg")

    monkeypatch.setattr(
        psycopg,
        "connect",
        _stub_psycopg_connect(total=0, wing_rows=[], room_rows=[], recorder=[]),
    )

    result = mcp_server.tool_status()
    assert set(result.keys()) == {
        "total_drawers",
        "wings",
        "rooms",
        "protocol",
        "aaak_dialect",
    }


def test_tool_status_postgres_falls_back_when_dsn_missing(monkeypatch):
    """If backend == postgres but DSN isn't set, the fast path returns
    None and tool_status must fall through to the legacy metadata sweep
    rather than raising."""
    _reset_caches()
    env = _postgres_env()
    env.pop("MEMPALACE_POSTGRES_DSN", None)
    env.pop("MEMPALACE_PG_DSN", None)
    monkeypatch.setattr(os, "environ", env)
    # Pretend chroma.sqlite3 doesn't exist on disk so the legacy path
    # uses create=False.
    monkeypatch.setattr(mcp_server.os.path, "isfile", lambda _p: False)

    fake_col = MagicMock()
    fake_col.count.return_value = 2
    with (
        patch.object(mcp_server, "_get_collection", return_value=fake_col),
        patch.object(
            mcp_server,
            "_get_cached_metadata",
            return_value=[{"wing": "w", "room": "r"}, {"wing": "w", "room": "r"}],
        ),
    ):
        result = mcp_server.tool_status()

    assert "error" not in result
    assert result["total_drawers"] == 2
    assert result["wings"] == {"w": 2}
    assert result["rooms"] == {"r": 2}


def test_tool_status_postgres_falls_back_when_query_raises(monkeypatch):
    """If the SQL fast path raises (connection refused, schema mismatch,
    statement_timeout hit), tool_status falls back to the legacy chroma
    path so the caller still gets a response."""
    _reset_caches()
    monkeypatch.setattr(os, "environ", _postgres_env())
    monkeypatch.setattr(mcp_server.os.path, "isfile", lambda _p: False)

    psycopg = pytest.importorskip("psycopg")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated connection failure")

    monkeypatch.setattr(psycopg, "connect", _boom)

    fake_col = MagicMock()
    fake_col.count.return_value = 1
    with (
        patch.object(mcp_server, "_get_collection", return_value=fake_col),
        patch.object(
            mcp_server,
            "_get_cached_metadata",
            return_value=[{"wing": "fallback", "room": "r"}],
        ),
    ):
        result = mcp_server.tool_status()

    assert result["total_drawers"] == 1
    assert result["wings"] == {"fallback": 1}


def test_tool_status_chroma_path_unchanged(monkeypatch):
    """Regression guard: when _config.backend != "postgres", tool_status
    must still use _get_collection + _get_cached_metadata (the legacy
    chroma path) and never reach into psycopg."""
    _reset_caches()
    monkeypatch.setattr(os, "environ", _chroma_env())
    monkeypatch.setattr(mcp_server.os.path, "isfile", lambda _p: False)

    fake_col = MagicMock()
    fake_col.count.return_value = 3
    cached_meta = [
        {"wing": "wing_user", "room": "hall_facts"},
        {"wing": "wing_user", "room": "hall_events"},
        {"wing": "wing_code", "room": "hall_facts"},
    ]
    with (
        patch.object(mcp_server, "_get_collection", return_value=fake_col),
        patch.object(mcp_server, "_get_cached_metadata", return_value=cached_meta),
        patch.object(mcp_server, "_tool_status_via_postgres") as fastpath,
    ):
        result = mcp_server.tool_status()

    fastpath.assert_not_called()
    assert result["total_drawers"] == 3
    assert result["wings"] == {"wing_user": 2, "wing_code": 1}
    assert result["rooms"] == {"hall_facts": 2, "hall_events": 1}


def test_tool_status_reports_the_configured_backend_when_resolution_refuses(monkeypatch):
    """A palace whose files belong to another backend makes resolve_backend_name raise BackendMismatchError.
    The status payload names the configured backend instead of failing (fork; seen in CI, where the test palace
    path held chroma artifacts while MEMPALACE_BACKEND=postgres)."""
    from mempalace.backends.base import BackendMismatchError

    _reset_caches()
    monkeypatch.setattr(os, "environ", _chroma_env() | {"MEMPALACE_BACKEND": "postgres"})

    def refuse(*_a, **_k):
        raise BackendMismatchError(
            "palace contains 'chroma' backend artifacts, but 'postgres' was selected"
        )

    monkeypatch.setattr(mcp_server, "_selected_backend_name", refuse)
    fake_col = MagicMock()
    fake_col.count.return_value = 1
    with (
        patch.object(mcp_server, "_get_collection", return_value=fake_col),
        patch.object(mcp_server, "_get_cached_metadata", return_value=[{"wing": "w", "room": "r"}]),
    ):
        result = mcp_server.tool_status()
    assert "error" not in result
    assert result["backend"] == "postgres"
