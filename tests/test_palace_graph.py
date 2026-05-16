"""Tests for mempalace.palace_graph — graph traversal layer.

All ChromaDB access is mocked — no real database needed.
"""

import pytest
from unittest.mock import MagicMock, patch

from mempalace.palace_graph import invalidate_graph_cache


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    """Ensure each test starts with a fresh graph cache."""
    invalidate_graph_cache()
    yield
    invalidate_graph_cache()


def _make_fake_collection(metadatas, ids=None):
    """Create a mock collection that returns the given metadata in batches."""
    if ids is None:
        ids = [f"id_{i}" for i in range(len(metadatas))]

    col = MagicMock()
    col.count.return_value = len(metadatas)

    def fake_get(limit=1000, offset=0, include=None):
        batch_meta = metadatas[offset : offset + limit]
        batch_ids = ids[offset : offset + limit]
        return {"ids": batch_ids, "metadatas": batch_meta}

    col.get.side_effect = fake_get
    return col


# Patch chromadb at import time so palace_graph can be imported
with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    from mempalace.palace_graph import (
        _fuzzy_match,
        build_graph,
        find_tunnels,
        graph_stats,
        traverse,
    )


# --- build_graph ---


class TestBuildGraph:
    def setup_method(self):
        invalidate_graph_cache()

    def test_empty_collection(self):
        col = _make_fake_collection([])
        nodes, edges = build_graph(col=col)
        assert nodes == {}
        assert edges == []

    def test_falsy_collection(self):
        """When col is explicitly falsy, build_graph returns empty."""
        nodes, edges = build_graph(col=0)
        assert nodes == {}
        assert edges == []

    def test_none_metadata_does_not_crash(self):
        """ChromaDB can return None for drawers without metadata (legacy
        data, partial writes — upstream #1020 territory). build_graph
        must skip None entries silently rather than crash the whole
        graph build with AttributeError. Caught 2026-04-25 by
        palace-daemon's verify-routes.sh smoke test against the
        canonical 151K palace; /stats was 500-ing on a single None
        drawer and taking out every consumer of build_graph for the
        whole call path."""
        col = _make_fake_collection(
            [
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
                None,  # legacy / partial-write drawer with no metadata
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-02"},
            ]
        )
        nodes, edges = build_graph(col=col)
        # The two real drawers were processed; the None one was skipped.
        assert "auth" in nodes
        assert nodes["auth"]["count"] == 2

    def test_single_wing_no_edges(self):
        col = _make_fake_collection(
            [
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-02"},
            ]
        )
        nodes, edges = build_graph(col=col)
        assert "auth" in nodes
        assert nodes["auth"]["count"] == 2
        assert edges == []

    def test_multi_wing_creates_edges(self):
        col = _make_fake_collection(
            [
                {
                    "room": "chromadb",
                    "wing": "wing_code",
                    "hall": "databases",
                    "date": "2026-01-01",
                },
                {
                    "room": "chromadb",
                    "wing": "wing_project",
                    "hall": "databases",
                    "date": "2026-01-02",
                },
            ]
        )
        nodes, edges = build_graph(col=col)
        assert "chromadb" in nodes
        assert len(edges) == 1
        assert edges[0]["wing_a"] == "wing_code"
        assert edges[0]["wing_b"] == "wing_project"
        assert edges[0]["hall"] == "databases"

    def test_general_room_excluded(self):
        col = _make_fake_collection(
            [
                {"room": "general", "wing": "wing_code", "hall": "misc", "date": ""},
            ]
        )
        nodes, edges = build_graph(col=col)
        assert "general" not in nodes

    def test_missing_wing_excluded(self):
        col = _make_fake_collection(
            [
                {"room": "orphan", "wing": "", "hall": "misc", "date": ""},
            ]
        )
        nodes, edges = build_graph(col=col)
        assert "orphan" not in nodes

    def test_dates_capped_at_five(self):
        col = _make_fake_collection(
            [
                {"room": "busy", "wing": "w", "hall": "h", "date": f"2026-01-{i:02d}"}
                for i in range(1, 10)
            ]
        )
        nodes, _ = build_graph(col=col)
        assert len(nodes["busy"]["dates"]) <= 5

    def test_cache_returns_same_result(self):
        """Second call within TTL returns cached nodes without re-scanning.

        The cache intentionally ignores col/config args when warm — this is
        correct for the MCP server's single-palace use case. Callers that
        switch collections must call invalidate_graph_cache() first.
        """
        col = _make_fake_collection(
            [{"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"}]
        )
        nodes1, edges1 = build_graph(col=col)
        # Second call with a *different* collection — should still return cached result
        col2 = _make_fake_collection([])
        nodes2, edges2 = build_graph(col=col2)
        assert nodes1 == nodes2
        assert edges1 == edges2

    def test_invalidate_clears_cache(self):
        """invalidate_graph_cache() forces a fresh scan on next call."""
        col = _make_fake_collection(
            [{"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"}]
        )
        build_graph(col=col)
        invalidate_graph_cache()
        col_empty = _make_fake_collection([])
        nodes, edges = build_graph(col=col_empty)
        assert nodes == {}
        assert edges == []


# --- traverse ---


class TestTraverse:
    def setup_method(self):
        invalidate_graph_cache()

    def _build_col(self):
        return _make_fake_collection(
            [
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
                {"room": "login", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
                {"room": "deploy", "wing": "wing_ops", "hall": "infra", "date": "2026-01-01"},
            ]
        )

    def test_traverse_known_room(self):
        col = self._build_col()
        result = traverse("auth", col=col)
        assert isinstance(result, list)
        rooms = [r["room"] for r in result]
        assert "auth" in rooms
        # login shares wing_code with auth
        assert "login" in rooms

    def test_traverse_unknown_room(self):
        col = self._build_col()
        result = traverse("nonexistent", col=col)
        assert isinstance(result, dict)
        assert "error" in result
        assert "suggestions" in result

    def test_traverse_max_hops(self):
        col = self._build_col()
        result = traverse("auth", col=col, max_hops=0)
        # Only the start room itself at hop 0
        assert len(result) == 1
        assert result[0]["room"] == "auth"


# --- find_tunnels ---


class TestFindTunnels:
    def setup_method(self):
        invalidate_graph_cache()

    def _build_tunnel_col(self):
        return _make_fake_collection(
            [
                {"room": "chromadb", "wing": "wing_code", "hall": "db", "date": "2026-01-01"},
                {"room": "chromadb", "wing": "wing_project", "hall": "db", "date": "2026-01-02"},
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
            ]
        )

    def test_find_all_tunnels(self):
        col = self._build_tunnel_col()
        tunnels = find_tunnels(col=col)
        assert len(tunnels) == 1
        assert tunnels[0]["room"] == "chromadb"

    def test_find_tunnels_with_wing_filter(self):
        col = self._build_tunnel_col()
        tunnels = find_tunnels(wing_a="wing_code", col=col)
        assert len(tunnels) == 1

    def test_find_tunnels_no_match(self):
        col = self._build_tunnel_col()
        tunnels = find_tunnels(wing_a="wing_nonexistent", col=col)
        assert tunnels == []

    def test_find_tunnels_both_wings(self):
        col = self._build_tunnel_col()
        tunnels = find_tunnels(wing_a="wing_code", wing_b="wing_project", col=col)
        assert len(tunnels) == 1
        assert tunnels[0]["room"] == "chromadb"


# --- graph_stats ---


class TestGraphStats:
    def setup_method(self):
        invalidate_graph_cache()

    def test_empty_graph(self):
        col = _make_fake_collection([])
        stats = graph_stats(col=col)
        assert stats["total_rooms"] == 0
        assert stats["tunnel_rooms"] == 0
        assert stats["total_edges"] == 0

    def test_stats_with_data(self):
        col = _make_fake_collection(
            [
                {"room": "chromadb", "wing": "wing_code", "hall": "db", "date": "2026-01-01"},
                {"room": "chromadb", "wing": "wing_project", "hall": "db", "date": "2026-01-02"},
                {"room": "auth", "wing": "wing_code", "hall": "security", "date": "2026-01-01"},
            ]
        )
        stats = graph_stats(col=col)
        assert stats["total_rooms"] == 2
        assert stats["tunnel_rooms"] == 1
        assert stats["total_edges"] == 1
        assert "wing_code" in stats["rooms_per_wing"]


# --- _fuzzy_match ---


class TestFuzzyMatch:
    def test_exact_substring(self):
        nodes = {"chromadb-setup": {}, "auth-module": {}, "deploy-config": {}}
        result = _fuzzy_match("chromadb", nodes)
        assert "chromadb-setup" in result

    def test_partial_word_match(self):
        nodes = {"chromadb-setup": {}, "auth-module": {}, "deploy-config": {}}
        result = _fuzzy_match("auth", nodes)
        assert "auth-module" in result

    def test_no_match(self):
        nodes = {"chromadb-setup": {}, "auth-module": {}}
        result = _fuzzy_match("zzzzz", nodes)
        assert result == []

    def test_hyphenated_query(self):
        nodes = {"riley-college-apps": {}, "college-prep": {}}
        result = _fuzzy_match("riley-college", nodes)
        assert "riley-college-apps" in result

    def test_max_results(self):
        nodes = {f"room-{i}": {} for i in range(20)}
        result = _fuzzy_match("room", nodes, n=3)
        assert len(result) <= 3


# --- Postgres aggregate fast path (#95) ---


import os  # noqa: E402

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")
pgmark = pytest.mark.skipif(
    POSTGRES_DSN is None,
    reason="set TEST_POSTGRES_DSN to run postgres-backed graph tests",
)


class TestPostgresFastPath:
    """Verifies the postgres-direct aggregate path replaces the row walk on
    postgres collections — closes the OOM that bit the 271k-drawer production
    palace (issue #95). Walk path stays available as fallback on any failure.
    """

    @pgmark
    def test_postgres_collection_routes_to_aggregate_path(self, monkeypatch):
        """PostgresCollection skips the row walk; build_graph returns the
        expected aggregate without ever calling col.get."""
        import psycopg2
        from mempalace.backends import get_backend
        from mempalace.backends.base import PalaceRef

        invalidate_graph_cache()
        backend = get_backend("postgres")
        col = backend.get_collection(
            palace=PalaceRef(id="test_fastpath", local_path="test_fastpath"),
            collection_name="test_graph_fastpath",
            create=True,
            options={"dsn": POSTGRES_DSN},
        )
        try:
            col.delete(ids=[f"d{i}" for i in range(1, 6)])
        except Exception:
            pass

        col.add(
            ids=["d1", "d2", "d3", "d4", "d5"],
            documents=["a", "b", "c", "d", "e"],
            embeddings=[[0.0] * 384] * 5,
            metadatas=[
                {"wing": "wing_a", "room": "shared_room", "hall": "h1", "date": "2026-01-01"},
                {"wing": "wing_b", "room": "shared_room", "hall": "h2", "date": "2026-02-01"},
                {"wing": "wing_a", "room": "solo_room_a", "date": "2026-03-01"},
                {"wing": "wing_b", "room": "solo_room_b"},
                # `general` is filtered out per the chroma-path spec.
                {"wing": "wing_a", "room": "general"},
            ],
        )

        # Trip-wire: if the walk path runs, col.get gets called. Spy on it.
        orig_get = col.get
        get_call_count = {"n": 0}

        def counting_get(*args, **kwargs):
            get_call_count["n"] += 1
            return orig_get(*args, **kwargs)

        monkeypatch.setattr(col, "get", counting_get)

        nodes, edges = build_graph(col=col)

        # Tripwire: NOT called — postgres fast path doesn't paginate.
        assert get_call_count["n"] == 0, (
            "col.get was called — postgres fast path didn't run, walk path " "took over instead"
        )

        # shared_room appears in 2 wings → tunnel node with edges.
        assert "shared_room" in nodes
        assert sorted(nodes["shared_room"]["wings"]) == ["wing_a", "wing_b"]
        assert nodes["shared_room"]["count"] == 2
        assert "2026-01-01" in nodes["shared_room"]["dates"]
        assert "2026-02-01" in nodes["shared_room"]["dates"]

        # Solo rooms exist with single-wing arrays.
        assert "solo_room_a" in nodes
        assert nodes["solo_room_a"]["wings"] == ["wing_a"]

        # `general` filtered out — same spec as the chroma path.
        assert "general" not in nodes

        # Edges only for rooms appearing in 2+ wings.
        tunnel_edges = [e for e in edges if e["room"] == "shared_room"]
        assert len(tunnel_edges) >= 1
        assert not any(e["room"] in ("solo_room_a", "solo_room_b") for e in edges)

        # Cleanup so re-runs don't accumulate.
        col.delete(ids=["d1", "d2", "d3", "d4", "d5"])
        conn = psycopg2.connect(POSTGRES_DSN)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "{col.table_name}" CASCADE')
        finally:
            conn.close()

    def test_chroma_collection_keeps_walk_path(self):
        """Chroma collections don't have a dsn attribute, so the postgres
        fast path correctly skips. MagicMock collections in unit tests also
        don't trigger the fast path (str-typed isinstance check)."""
        invalidate_graph_cache()
        col = _make_fake_collection(
            [
                {"wing": "wing_a", "room": "shared", "hall": "h1"},
                {"wing": "wing_b", "room": "shared", "hall": "h2"},
            ]
        )
        nodes, edges = build_graph(col=col)
        # Walk path returns the same shape.
        assert "shared" in nodes
        assert sorted(nodes["shared"]["wings"]) == ["wing_a", "wing_b"]

    def test_postgres_fast_path_shapes_results_correctly(self):
        """Unit-level coverage for _build_graph_postgres without real postgres.

        Postgres-CI exercises this end-to-end against a service container,
        but the no-postgres CI job lost test coverage when the function
        landed (79.34% vs 80% threshold). This test mocks psycopg2.connect
        so the function's result-shaping (room nodes + tunnel edges with
        cartesian wing×hall expansion) is verified without a real DB.
        """
        from mempalace import palace_graph as pg

        # Fake postgres collection — has dsn + table_name to trigger the
        # fast-path dispatch.
        col = MagicMock()
        col.dsn = "postgresql://test@example/test"
        col.table_name = "mempalace_drawers"

        # Mock cursor.fetchall returns rows shaped like the aggregate query.
        # Order: room, wings[], halls[], cnt, dates_sample[]
        fake_rows = [
            ("shared_room", ["wing_a", "wing_b"], ["h1", "h2"], 4, ["2026-01-01", "2026-02-01"]),
            ("solo_room", ["wing_a"], None, 2, None),
            ("triple_room", ["wing_x", "wing_y", "wing_z"], ["h_only"], 3, None),
        ]
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = fake_rows
        fake_cursor.__enter__ = lambda s: s
        fake_cursor.__exit__ = lambda s, *a: None

        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_conn.__enter__ = lambda s: s
        fake_conn.__exit__ = lambda s, *a: None

        # Patch psycopg2 inside _load_psycopg2's return so connect → our mock.
        fake_psycopg2 = MagicMock()
        fake_psycopg2.connect.return_value = fake_conn

        with patch(
            "mempalace.backends.postgres._load_psycopg2", return_value=(fake_psycopg2, None)
        ):
            invalidate_graph_cache()
            nodes, edges = pg._build_graph_postgres(col)

        # Multi-wing room: appears in nodes with sorted wings, halls, count, dates.
        assert "shared_room" in nodes
        assert nodes["shared_room"]["wings"] == ["wing_a", "wing_b"]
        assert nodes["shared_room"]["halls"] == ["h1", "h2"]
        assert nodes["shared_room"]["count"] == 4
        assert sorted(nodes["shared_room"]["dates"]) == ["2026-01-01", "2026-02-01"]

        # Solo room: halls None handled, empty list returned.
        assert nodes["solo_room"]["halls"] == []
        assert nodes["solo_room"]["dates"] == []

        # Edges: cartesian wing-cross × halls. shared_room has C(2,2)=1
        # wing pair × 2 halls = 2 edges. triple_room has C(3,2)=3 wing
        # pairs × 1 hall = 3 edges. solo_room has 0 (single wing).
        shared_edges = [e for e in edges if e["room"] == "shared_room"]
        triple_edges = [e for e in edges if e["room"] == "triple_room"]
        solo_edges = [e for e in edges if e["room"] == "solo_room"]
        assert len(shared_edges) == 2
        assert len(triple_edges) == 3
        assert solo_edges == []
        # Each shared_room edge connects the two wings.
        for e in shared_edges:
            assert e["wing_a"] == "wing_a"
            assert e["wing_b"] == "wing_b"
            assert e["hall"] in {"h1", "h2"}

    def test_postgres_fast_path_handles_missing_psycopg2(self):
        """If psycopg2 isn't importable, the fast path returns None and the
        caller falls through to the chroma walk. Verifies the import-error
        early-return branch is covered."""
        from mempalace import palace_graph as pg

        col = MagicMock()
        col.dsn = "postgresql://test@example/test"
        col.table_name = "mempalace_drawers"

        with patch(
            "mempalace.backends.postgres._load_psycopg2",
            side_effect=RuntimeError("psycopg2 missing"),
        ):
            result = pg._build_graph_postgres(col)
        assert result is None

    def test_postgres_fast_path_handles_missing_dsn(self):
        """If col has no dsn attribute (or dsn is empty), fast path skips
        and returns None to fall through to the walk."""
        from mempalace import palace_graph as pg

        col = MagicMock(spec=[])  # no attributes at all
        result = pg._build_graph_postgres(col)
        assert result is None

    def test_build_graph_falls_back_to_walk_on_postgres_failure(self):
        """If the postgres fast path raises mid-execution (network blip,
        schema drift, etc.) build_graph catches and falls through to the
        walk path. Worst case == prior behavior."""
        invalidate_graph_cache()
        # A walk-compatible col with a real string dsn so the fast-path
        # dispatch fires, then we make psycopg2.connect raise so the
        # try/except catches and falls through.
        col = _make_fake_collection(
            [{"wing": "wing_a", "room": "fallback_room", "hall": "h1", "date": "2026-01-01"}]
        )
        col.dsn = "postgresql://nope@nowhere/x"
        col.table_name = "mempalace_drawers"

        fake_psycopg2 = MagicMock()
        fake_psycopg2.connect.side_effect = RuntimeError("connection refused")

        with patch(
            "mempalace.backends.postgres._load_psycopg2",
            return_value=(fake_psycopg2, None),
        ):
            nodes, _ = build_graph(col=col)

        # Walk path took over — fallback_room is present.
        assert "fallback_room" in nodes
        assert nodes["fallback_room"]["wings"] == ["wing_a"]

    def test_postgres_fast_path_caches_when_nonempty(self):
        """A non-empty postgres result populates the module-level cache so
        subsequent build_graph calls hit it without re-querying."""
        invalidate_graph_cache()
        col = MagicMock()
        col.dsn = "postgresql://test@example/test"
        col.table_name = "mempalace_drawers"

        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = [
            ("cached_room", ["wing_q", "wing_r"], None, 2, None),
        ]
        fake_cursor.__enter__ = lambda s: s
        fake_cursor.__exit__ = lambda s, *a: None
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_conn.__enter__ = lambda s: s
        fake_conn.__exit__ = lambda s, *a: None
        fake_psycopg2 = MagicMock()
        fake_psycopg2.connect.return_value = fake_conn

        with patch(
            "mempalace.backends.postgres._load_psycopg2",
            return_value=(fake_psycopg2, None),
        ):
            # First call: hits postgres path, populates cache.
            nodes1, _ = build_graph(col=col)
            assert "cached_room" in nodes1
            # Second call: cache hit; psycopg2.connect should NOT fire again.
            fake_psycopg2.connect.reset_mock()
            # Make connect raise so any non-cached run would fail loudly.
            fake_psycopg2.connect.side_effect = RuntimeError("would only fire on cache miss")
            nodes2, _ = build_graph(col=col)
            assert nodes1 == nodes2
            fake_psycopg2.connect.assert_not_called()
