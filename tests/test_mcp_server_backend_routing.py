"""Unit tests for the backend-routing logic in mcp_server._get_collection.

Tests routing only — doesn't hit postgres or chromadb. Verifies that
when _config.backend changes (via MEMPALACE_BACKEND env), _get_collection
dispatches to the right backend-specific helper.
"""

import os
from unittest.mock import patch, MagicMock

from mempalace import mcp_server


def _reset_caches():
    mcp_server._client_cache = None
    mcp_server._collection_cache = None
    mcp_server._postgres_backend_cache = None
    mcp_server._metadata_cache = None
    mcp_server._metadata_cache_time = 0


def test_get_collection_dispatches_to_chroma_by_default():
    """When MEMPALACE_BACKEND unset (chroma default), dispatch to chroma path."""
    _reset_caches()
    sentinel = MagicMock(name="chroma-collection-sentinel")
    env = {k: v for k, v in os.environ.items() if k != "MEMPALACE_BACKEND"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(mcp_server, "_get_collection_chroma", return_value=sentinel) as mock_chroma,
        patch.object(mcp_server, "_get_collection_postgres") as mock_pg,
    ):
        result = mcp_server._get_collection()
        assert result is sentinel
        mock_chroma.assert_called_once_with(create=False)
        mock_pg.assert_not_called()


def test_get_collection_dispatches_to_postgres_when_env_set():
    """When MEMPALACE_BACKEND=postgres, dispatch to postgres path."""
    _reset_caches()
    sentinel = MagicMock(name="postgres-collection-sentinel")
    with (
        patch.dict(os.environ, {"MEMPALACE_BACKEND": "postgres"}),
        patch.object(mcp_server, "_get_collection_postgres", return_value=sentinel) as mock_pg,
        patch.object(mcp_server, "_get_collection_chroma") as mock_chroma,
    ):
        result = mcp_server._get_collection()
        assert result is sentinel
        mock_pg.assert_called_once_with(create=False)
        mock_chroma.assert_not_called()


def test_get_collection_passes_create_flag_to_chroma():
    _reset_caches()
    env = {k: v for k, v in os.environ.items() if k != "MEMPALACE_BACKEND"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(mcp_server, "_get_collection_chroma") as mock_chroma,
    ):
        mcp_server._get_collection(create=True)
        mock_chroma.assert_called_once_with(create=True)


def test_get_collection_passes_create_flag_to_postgres():
    _reset_caches()
    with (
        patch.dict(os.environ, {"MEMPALACE_BACKEND": "postgres"}),
        patch.object(mcp_server, "_get_collection_postgres") as mock_pg,
    ):
        mcp_server._get_collection(create=True)
        mock_pg.assert_called_once_with(create=True)


def test_postgres_branch_lazy_constructs_backend():
    """First call constructs PostgresBackend; second reuses it."""
    _reset_caches()
    fake_backend = MagicMock(name="fake-pg-backend")
    fake_collection = MagicMock(name="fake-pg-col")
    fake_backend.get_collection.return_value = fake_collection

    with (
        patch.dict(
            os.environ,
            {
                "MEMPALACE_BACKEND": "postgres",
                "MEMPALACE_POSTGRES_DSN": "postgresql://stub",
            },
        ),
        patch("mempalace.backends.postgres.PostgresBackend", return_value=fake_backend),
    ):
        result1 = mcp_server._get_collection_postgres()
        result2 = mcp_server._get_collection_postgres()

    assert result1 is fake_collection
    assert result2 is fake_collection
    # Backend constructed once, used twice (create=False path doesn't recreate collection cache)
    # NOTE: _collection_cache is reused on second call, so get_collection might only be called once.
    # Verify backend itself only built once via the cache.
    assert mcp_server._postgres_backend_cache is fake_backend


def test_postgres_branch_returns_none_on_error():
    """When PostgresBackend.get_collection raises, log + return None."""
    _reset_caches()
    fake_backend = MagicMock(name="fake-pg-backend")
    fake_backend.get_collection.side_effect = RuntimeError("simulated DSN failure")

    with (
        patch.dict(
            os.environ,
            {
                "MEMPALACE_BACKEND": "postgres",
                "MEMPALACE_POSTGRES_DSN": "postgresql://stub",
            },
        ),
        patch("mempalace.backends.postgres.PostgresBackend", return_value=fake_backend),
    ):
        result = mcp_server._get_collection_postgres()

    assert result is None
