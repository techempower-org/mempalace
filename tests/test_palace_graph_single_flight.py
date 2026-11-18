"""build_graph on postgres: one aggregate at a time, and never a row walk after a failure.

Measured on familiar 2026-10-20 (load 49): graph_stats ran with up to 8 calls in
flight, the aggregate hit the statement timeout, and each failure fell back to
an OFFSET walk over all 1.18M drawers.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from mempalace import palace_graph as pg
from mempalace.palace_graph import GraphUnavailableError, build_graph, invalidate_graph_cache

GOOD = ({"room_a": {"wings": ["w1", "w2"], "halls": [], "count": 3, "dates": []}}, [])


def _pg_col():
    """A postgres-shaped collection whose walk path is a trip-wire."""
    col = MagicMock()
    col.dsn = "postgresql://test@example/test"
    col.table_name = "mempalace_drawers"
    col.count.side_effect = AssertionError("row walk ran on a postgres collection")
    col.get.side_effect = AssertionError("row walk ran on a postgres collection")
    return col


@pytest.fixture(autouse=True)
def _cold_cache():
    invalidate_graph_cache()
    yield
    invalidate_graph_cache()


def test_failure_without_a_good_graph_raises_and_never_walks():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", side_effect=RuntimeError("statement timeout")):
        with pytest.raises(GraphUnavailableError, match="statement timeout"):
            build_graph(col=col)
    col.get.assert_not_called()
    col.count.assert_not_called()


def test_failure_after_a_write_serves_the_last_good_graph():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", return_value=GOOD):
        assert build_graph(col=col) == GOOD
    invalidate_graph_cache()  # a mine write
    with patch.object(pg, "_build_graph_postgres", side_effect=RuntimeError("statement timeout")):
        assert build_graph(col=col) == GOOD
    col.get.assert_not_called()


def test_a_recent_failure_is_not_retried_by_the_next_caller():
    col = _pg_col()
    agg = MagicMock(side_effect=RuntimeError("statement timeout"))
    with patch.object(pg, "_build_graph_postgres", agg):
        for _ in range(3):
            with pytest.raises(GraphUnavailableError):
                build_graph(col=col)
    assert agg.call_count == 1


def test_retry_after_the_window():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", side_effect=RuntimeError("down")):
        with pytest.raises(GraphUnavailableError):
            build_graph(col=col)
    pg._graph_pg_failed_at = time.time() - pg._GRAPH_PG_RETRY_AFTER - 1
    with patch.object(pg, "_build_graph_postgres", return_value=GOOD) as agg:
        assert build_graph(col=col) == GOOD
    assert agg.call_count == 1


def test_concurrent_callers_share_one_aggregate():
    col = _pg_col()
    calls = []
    release = threading.Event()

    def slow_aggregate(_col, _config=None):
        calls.append(1)
        release.wait(5)
        return GOOD

    results = []
    with patch.object(pg, "_build_graph_postgres", side_effect=slow_aggregate):
        threads = [
            threading.Thread(target=lambda: results.append(build_graph(col=col))) for _ in range(8)
        ]
        for t in threads:
            t.start()
        time.sleep(0.2)  # let every caller reach the build lock
        release.set()
        for t in threads:
            t.join(5)
    assert len(calls) == 1
    assert results == [GOOD] * 8


def test_unavailable_fast_path_still_walks():
    """No psycopg2 / no dsn (the aggregate returns None) keeps the walk."""
    col = MagicMock()
    col.dsn = "postgresql://test@example/test"
    col.table_name = "mempalace_drawers"
    col.count.return_value = 1
    col.get.side_effect = lambda limit=1000, offset=0, include=None: (
        {"ids": ["d1"], "metadatas": [{"wing": "w1", "room": "walked"}]}
        if offset == 0
        else {"ids": [], "metadatas": []}
    )
    with patch.object(pg, "_build_graph_postgres", return_value=None):
        nodes, _ = build_graph(col=col)
    assert "walked" in nodes


def test_a_write_within_the_window_does_not_rerun_the_aggregate():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", return_value=GOOD) as agg:
        assert build_graph(col=col) == GOOD
        for _ in range(5):
            invalidate_graph_cache()  # a mine write
            assert build_graph(col=col) == GOOD
    assert agg.call_count == 1


def _join_rebuild():
    t = pg._graph_refresh_thread
    if t is not None:
        t.join(5)
        assert not t.is_alive(), "background rebuild did not finish"


@pytest.fixture(autouse=True)
def _no_rebuild_state():
    pg._graph_last_good = None
    pg._graph_last_good_at = 0.0
    pg._graph_pg_failed_at = 0.0
    pg._graph_refreshing = False
    yield
    _join_rebuild()
    pg._graph_last_good = None
    pg._graph_last_good_at = 0.0
    pg._graph_pg_failed_at = 0.0
    pg._graph_refreshing = False
    pg._graph_refresh_thread = None


NEWER = ({"room_b": {"wings": ["w3"], "halls": [], "count": 1, "dates": []}}, [])


def _age_the_good_graph():
    invalidate_graph_cache()  # a mine write
    pg._graph_last_good_at = time.time() - pg._GRAPH_PG_SERVE_RECENT_S - 1


def test_after_the_window_a_caller_gets_the_last_good_graph_at_once_and_a_background_rebuild_replaces_it():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", return_value=GOOD):
        build_graph(col=col)
    _age_the_good_graph()
    release = threading.Event()

    def slow(_col, _config=None, statement_timeout_s=None):
        release.wait(5)
        return NEWER

    with patch.object(pg, "_build_graph_postgres", side_effect=slow) as agg:
        t0 = time.monotonic()
        assert build_graph(col=col) == GOOD  # did not wait for the rebuild
        assert time.monotonic() - t0 < 1.0
        release.set()
        _join_rebuild()
        assert build_graph(col=col) == NEWER
    assert agg.call_count == 1


def test_the_background_rebuild_has_its_own_longer_statement_timeout():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", return_value=GOOD) as cold:
        build_graph(col=col)
    # a caller that waits (no graph yet) keeps the role's limit
    assert cold.call_args.kwargs.get("statement_timeout_s") is None
    _age_the_good_graph()
    with patch.object(pg, "_build_graph_postgres", return_value=NEWER) as agg:
        build_graph(col=col)
        _join_rebuild()
    assert agg.call_args.kwargs["statement_timeout_s"] == pg._GRAPH_PG_BACKGROUND_TIMEOUT_S
    assert pg._GRAPH_PG_BACKGROUND_TIMEOUT_S > 30


def test_callers_during_a_rebuild_start_no_second_one():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", return_value=GOOD):
        build_graph(col=col)
    _age_the_good_graph()
    release = threading.Event()

    def slow(_col, _config=None, statement_timeout_s=None):
        release.wait(5)
        return NEWER

    with patch.object(pg, "_build_graph_postgres", side_effect=slow) as agg:
        results = [build_graph(col=col) for _ in range(6)]
        release.set()
        _join_rebuild()
    assert results == [GOOD] * 6
    assert agg.call_count == 1


def test_a_failed_background_rebuild_keeps_the_last_good_graph_and_backs_off():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", return_value=GOOD):
        build_graph(col=col)
    _age_the_good_graph()
    with patch.object(
        pg, "_build_graph_postgres", side_effect=RuntimeError("statement timeout")
    ) as agg:
        assert build_graph(col=col) == GOOD
        _join_rebuild()
        for _ in range(3):
            assert build_graph(col=col) == GOOD
            _join_rebuild()
    assert agg.call_count == 1  # no retry inside _GRAPH_PG_BACKGROUND_RETRY_S
    pg._graph_pg_failed_at = time.time() - pg._GRAPH_PG_BACKGROUND_RETRY_S - 1
    with patch.object(pg, "_build_graph_postgres", return_value=NEWER) as agg2:
        build_graph(col=col)
        _join_rebuild()
    assert agg2.call_count == 1
    col.get.assert_not_called()


def test_a_rebuild_whose_thread_cannot_start_is_retried_by_a_later_caller():
    col = _pg_col()
    with patch.object(pg, "_build_graph_postgres", return_value=GOOD):
        build_graph(col=col)
    _age_the_good_graph()
    with patch.object(pg.threading, "Thread", side_effect=RuntimeError("no more threads")):
        assert build_graph(col=col) == GOOD
    assert pg._graph_refreshing is False
    with patch.object(pg, "_build_graph_postgres", return_value=NEWER) as agg:
        build_graph(col=col)
        _join_rebuild()
    assert agg.call_count == 1
