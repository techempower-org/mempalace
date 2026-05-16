"""Extra tests for mempalace.config to cover remaining gaps."""

import json
import os

from mempalace.config import MempalaceConfig


def test_config_bad_json(tmp_path):
    """Bad JSON in config file falls back to empty."""
    (tmp_path / "config.json").write_text("not json", encoding="utf-8")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.palace_path  # still returns default


def test_people_map_from_file(tmp_path):
    (tmp_path / "people_map.json").write_text(json.dumps({"bob": "Robert"}), encoding="utf-8")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.people_map == {"bob": "Robert"}


def test_people_map_bad_json(tmp_path):
    (tmp_path / "people_map.json").write_text("bad", encoding="utf-8")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.people_map == {}


def test_people_map_missing(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.people_map == {}


def test_topic_wings_default(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert isinstance(cfg.topic_wings, list)
    assert "emotions" in cfg.topic_wings


def test_hall_keywords_default(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert isinstance(cfg.hall_keywords, dict)
    assert "technical" in cfg.hall_keywords


def test_init_idempotent(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg.init()
    cfg.init()  # second call should not overwrite
    with open(tmp_path / "config.json") as f:
        data = json.load(f)
    assert "palace_path" in data


def test_save_people_map(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    result = cfg.save_people_map({"alice": "Alice Smith"})
    assert result.exists()
    with open(result) as f:
        data = json.load(f)
    assert data["alice"] == "Alice Smith"


def test_env_mempal_palace_path(tmp_path):
    """MEMPAL_PALACE_PATH (legacy) should also work."""
    os.environ.pop("MEMPALACE_PALACE_PATH", None)
    raw = "/legacy/path"
    os.environ["MEMPAL_PALACE_PATH"] = raw
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        # palace_path is normalized via abspath + expanduser — compare
        # against the normalized form so the test is portable between
        # POSIX (no-op) and Windows (prepends current drive letter).
        assert cfg.palace_path == os.path.abspath(os.path.expanduser(raw))
    finally:
        del os.environ["MEMPAL_PALACE_PATH"]


def test_collection_name_from_config(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"collection_name": "custom_col"}), encoding="utf-8"
    )
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.collection_name == "custom_col"


def test_collection_name_from_env_overrides_config(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"collection_name": "custom_col"}), encoding="utf-8"
    )
    os.environ["MEMPALACE_COLLECTION_NAME"] = "env_col"
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        assert cfg.collection_name == "env_col"
    finally:
        del os.environ["MEMPALACE_COLLECTION_NAME"]


def test_backend_and_postgres_dsn_from_env(tmp_path):
    os.environ["MEMPALACE_BACKEND"] = "pg"
    os.environ["MEMPALACE_POSTGRES_DSN"] = "postgresql://example"
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        assert cfg.backend == "postgres"
        assert cfg.postgres_dsn == "postgresql://example"
    finally:
        del os.environ["MEMPALACE_BACKEND"]
        del os.environ["MEMPALACE_POSTGRES_DSN"]


def test_backend_override_is_none_without_explicit_config(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.backend == "chroma"
    assert cfg.backend_override is None


# ── daemon_url + daemon_strict resolution (issue #49) ──────────────────────


def test_daemon_url_defaults_to_none(tmp_path):
    """No env, no config — daemon_url is None and daemon_strict is False."""
    os.environ.pop("PALACE_DAEMON_URL", None)
    os.environ.pop("PALACE_DAEMON_STRICT", None)
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.daemon_url is None
    assert cfg.daemon_strict is False


def test_daemon_url_from_env(tmp_path):
    """PALACE_DAEMON_URL env var sets daemon_url and enables strict by default."""
    os.environ.pop("PALACE_DAEMON_STRICT", None)
    os.environ["PALACE_DAEMON_URL"] = "http://disks:8085"
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        assert cfg.daemon_url == "http://disks:8085"
        assert cfg.daemon_strict is True
    finally:
        del os.environ["PALACE_DAEMON_URL"]


def test_daemon_url_strips_trailing_slash(tmp_path):
    """Trailing slashes are stripped so endpoint joins like f'{url}/mcp' work."""
    os.environ["PALACE_DAEMON_URL"] = "http://disks:8085/"
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        assert cfg.daemon_url == "http://disks:8085"
    finally:
        del os.environ["PALACE_DAEMON_URL"]


def test_daemon_url_from_config_file_fallback(tmp_path):
    """config.json's daemon_url is used when env var is unset.

    This is the central fix in issue #49 — Claude Code's MCP spawn context
    can fail to propagate env vars, silently dropping the routing decision
    to local. config.json fallback closes that gap.
    """
    os.environ.pop("PALACE_DAEMON_URL", None)
    os.environ.pop("PALACE_DAEMON_STRICT", None)
    (tmp_path / "config.json").write_text(
        json.dumps({"daemon_url": "http://disks.jphe.in:8085"}), encoding="utf-8"
    )
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.daemon_url == "http://disks.jphe.in:8085"
    assert cfg.daemon_strict is True


def test_daemon_url_env_overrides_config(tmp_path):
    """Env wins over config (palace_path's resolution shape)."""
    (tmp_path / "config.json").write_text(
        json.dumps({"daemon_url": "http://from-config:8085"}), encoding="utf-8"
    )
    os.environ["PALACE_DAEMON_URL"] = "http://from-env:8085"
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        assert cfg.daemon_url == "http://from-env:8085"
    finally:
        del os.environ["PALACE_DAEMON_URL"]


def test_daemon_strict_env_zero_disables(tmp_path):
    """PALACE_DAEMON_STRICT=0 forces local even when daemon_url is set."""
    os.environ["PALACE_DAEMON_URL"] = "http://disks:8085"
    os.environ["PALACE_DAEMON_STRICT"] = "0"
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        assert cfg.daemon_url == "http://disks:8085"
        assert cfg.daemon_strict is False
    finally:
        del os.environ["PALACE_DAEMON_URL"]
        del os.environ["PALACE_DAEMON_STRICT"]


def test_daemon_strict_false_in_config_disables(tmp_path):
    """config.json {"daemon_strict": false} also forces local."""
    os.environ.pop("PALACE_DAEMON_URL", None)
    os.environ.pop("PALACE_DAEMON_STRICT", None)
    (tmp_path / "config.json").write_text(
        json.dumps({"daemon_url": "http://disks:8085", "daemon_strict": False}),
        encoding="utf-8",
    )
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.daemon_url == "http://disks:8085"
    assert cfg.daemon_strict is False
