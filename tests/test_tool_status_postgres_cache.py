"""``_tool_status_via_postgres`` caches, and serves a stale result on failure.

On familiar (2026-10-20) the three count queries missed their 3 s timeout
whenever the disk was busy. Each miss fell back to the full metadata sweep,
a whole-table read, exactly when the disk was saturated.
"""

import time
from unittest.mock import patch

from mempalace import mcp_server

GOOD = {"total_drawers": 5, "wings": {"w": 5}, "rooms": {"r": 5}}


def test_a_fresh_result_is_reused_without_querying():
    with patch.object(mcp_server, "_tool_status_via_postgres_uncached", return_value=GOOD) as q:
        assert mcp_server._tool_status_via_postgres() == GOOD
        assert mcp_server._tool_status_via_postgres() == GOOD
    assert q.call_count == 1


def test_a_failure_serves_the_last_result_marked_stale():
    with patch.object(mcp_server, "_tool_status_via_postgres_uncached", return_value=GOOD):
        mcp_server._tool_status_via_postgres()
    mcp_server._pg_status_cache = (GOOD, time.time() - mcp_server._PG_STATUS_TTL_S - 1)
    with patch.object(mcp_server, "_tool_status_via_postgres_uncached", return_value=None):
        out = mcp_server._tool_status_via_postgres()
    assert out["total_drawers"] == 5 and out["stale"] is True and out["stale_age_s"] >= 60


def test_a_failure_with_nothing_cached_still_returns_none():
    with patch.object(mcp_server, "_tool_status_via_postgres_uncached", return_value=None):
        assert mcp_server._tool_status_via_postgres() is None


def test_a_result_past_the_stale_limit_is_not_served():
    mcp_server._pg_status_cache = (GOOD, time.time() - mcp_server._PG_STATUS_STALE_MAX_S - 1)
    with patch.object(mcp_server, "_tool_status_via_postgres_uncached", return_value=None):
        assert mcp_server._tool_status_via_postgres() is None


def test_the_cached_result_is_not_mutated_by_callers():
    with patch.object(mcp_server, "_tool_status_via_postgres_uncached", return_value=dict(GOOD)):
        mcp_server._tool_status_via_postgres()
        hit = mcp_server._tool_status_via_postgres()  # served from the cache
        hit["total_drawers"] = 999
        assert mcp_server._tool_status_via_postgres()["total_drawers"] == 5


def test_tool_status_on_postgres_does_not_sweep_when_a_stale_result_exists(monkeypatch):
    monkeypatch.setattr(mcp_server, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)
    mcp_server._pg_status_cache = (GOOD, time.time() - mcp_server._PG_STATUS_TTL_S - 1)
    with (
        patch.object(type(mcp_server._config), "backend", "postgres", create=True),
        patch.object(mcp_server, "_tool_status_via_postgres_uncached", return_value=None),
        patch.object(mcp_server, "_get_cached_metadata") as sweep,
        patch.object(mcp_server, "_backend_db_exists", return_value=True),
        patch.object(mcp_server, "_ensure_sqlite_integrity_status"),
        patch.object(mcp_server, "_refresh_vector_disabled_flag"),
    ):
        out = mcp_server.tool_status()
    sweep.assert_not_called()
    assert out["stale"] is True
