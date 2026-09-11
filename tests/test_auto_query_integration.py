"""Integration tests for the full auto-query pipeline.

These tests exercise the complete signal → route → format chain
without a real daemon, using ``_call_mcp`` patching.
"""

import json
from unittest.mock import patch

from mempalace.auto_query.decisions import read_decisions
from mempalace.auto_query.formatter import SENTINEL_CLOSE, SENTINEL_OPEN
from mempalace.auto_query.runner import run_auto_query
from mempalace.config import MempalaceConfig


def _cfg(tmp_path, mode="balanced"):
    """Shorthand config with auto-query enabled."""
    import os

    config_dir = str(tmp_path / "cfg")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "config.json"), "w") as f:
        json.dump(
            {
                "auto_query": {"enabled": True, "mode": mode},
                "daemon_url": "http://fake:9999",
            },
            f,
        )
    return MempalaceConfig(config_dir=config_dir)


def _patch_mcp(return_value):
    return patch("mempalace.auto_query.runner._call_mcp", return_value=return_value)


# ── explicit recall → search ────────────────────────────────────


class TestExplicitRecall:
    def test_explicit_recall_fires_search(self, tmp_path):
        mcp_result = {
            "results": [
                {
                    "wing": "wing_mempalace",
                    "room": "bugs",
                    "drawer_id": "d-100",
                    "text": "metadata reshape bug in chroma.py",
                    "created_at": "2026-05-20",
                },
                {
                    "wing": "wing_mempalace",
                    "room": "bugs",
                    "drawer_id": "d-101",
                    "text": "m dict conversion fix needed",
                    "created_at": "2026-05-21",
                },
            ]
        }
        log_dir = str(tmp_path / "log")
        with _patch_mcp(mcp_result):
            result = run_auto_query(
                prompt="remind me what we did with metadata reshape?",
                session_id="int-1",
                turn=2,
                config=_cfg(tmp_path),
                log_dir=log_dir,
            )
        assert result.injection is not None
        assert SENTINEL_OPEN in result.injection
        assert SENTINEL_CLOSE in result.injection
        assert "d-100" in result.injection
        assert result.decision.tool == "mempalace_search"
        assert result.decision.decision == "fire"
        assert result.decision.result_drawers == 2


# ── entity + temporal → kg_query ────────────────────────────────


class TestEntityTemporal:
    def test_entity_temporal_fires_kg(self, tmp_path):
        mcp_result = {
            "outgoing": [
                {
                    "subject": "AutoQuery",
                    "predicate": "implemented_by",
                    "object": "runner.py",
                    "valid_from": "2026-05-22",
                }
            ],
            "incoming": [],
        }
        log_dir = str(tmp_path / "log")
        with _patch_mcp(mcp_result):
            result = run_auto_query(
                prompt="check AutoQuery changes from last time",
                session_id="int-2",
                turn=2,
                known_wings={"wing_mempalace"},
                known_entities={"AutoQuery"},
                config=_cfg(tmp_path),
                log_dir=log_dir,
            )
        assert result.injection is not None
        assert "AutoQuery" in result.injection
        assert result.decision.tool == "mempalace_kg_query"


# ── resumption → diary_read ─────────────────────────────────────


class TestResumptionIntegration:
    def test_positional_resumption_fires_diary(self, tmp_path):
        """Turn 1 with no resumption *phrase* still reads the diary.

        A turn that asks to resume in words takes the content route instead
        (#364) — see ``test_auto_query_resumption.py``. This pins the
        positional half, which is unchanged.
        """
        mcp_result = {
            "entries": [
                {
                    "topic": "session",
                    "timestamp": "2026-05-22T10:00:00Z",
                    "entry": "SESSION:2026-05-22|built.auto-query.router+formatter",
                }
            ]
        }
        log_dir = str(tmp_path / "log")
        with _patch_mcp(mcp_result):
            result = run_auto_query(
                prompt="ok lets keep going",
                session_id="int-3",
                turn=1,
                project_wing="wing_mempalace",
                known_wings={"wing_mempalace"},
                has_recent_drawers=True,
                config=_cfg(tmp_path),
                log_dir=log_dir,
            )
        assert result.injection is not None
        assert "diary" in result.decision.tool
        assert "SESSION:2026-05-22" in result.injection


# ── decision log roundtrip ──────────────────────────────────────


class TestDecisionLogRoundtrip:
    def test_all_decisions_logged(self, tmp_path):
        log_dir = str(tmp_path / "log")
        config = _cfg(tmp_path, mode="dry-run")

        # Turn 1: explicit recall (should log dry-run-skip)
        run_auto_query(
            prompt="remind me about metadata?",
            session_id="log-1",
            turn=1,
            config=config,
            log_dir=log_dir,
        )
        # Turn 2: no signal (should log skip)
        run_auto_query(
            prompt="hello",
            session_id="log-1",
            turn=2,
            config=config,
            log_dir=log_dir,
        )

        entries = read_decisions(log_dir=log_dir)
        assert len(entries) == 2
        assert entries[0]["decision"] == "dry-run-skip"
        assert entries[1]["decision"] == "skip"
        # All entries share the same session
        assert all(e["session_id"] == "log-1" for e in entries)


# ── __main__ CLI ────────────────────────────────────────────────


class TestCLI:
    def test_main_returns_zero(self, tmp_path):
        from mempalace.auto_query.__main__ import main

        config_dir = str(tmp_path / "cfg")
        import os

        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            json.dump({"auto_query": {"enabled": False}}, f)

        with patch("mempalace.auto_query.__main__.MempalaceConfig") as mock_cls:
            mock_cls.return_value = MempalaceConfig(config_dir=config_dir)
            rc = main(["--prompt", "test", "--session-id", "cli-1", "--turn", "1"])
        assert rc == 0

    def test_main_outputs_injection(self, tmp_path, capsys):
        from mempalace.auto_query.__main__ import main

        config_dir = str(tmp_path / "cfg")
        import os

        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.json"), "w") as f:
            json.dump(
                {
                    "auto_query": {"enabled": True, "mode": "balanced"},
                    "daemon_url": "http://fake:1",
                },
                f,
            )

        mcp_result = {
            "results": [{"wing": "w", "room": "r", "drawer_id": "d-1", "text": "test content"}]
        }

        with (
            patch("mempalace.auto_query.__main__.MempalaceConfig") as mock_cls,
            patch("mempalace.auto_query.runner._call_mcp", return_value=mcp_result),
            patch("mempalace.auto_query.__main__._fetch_wings", return_value=set()),
        ):
            mock_cls.return_value = MempalaceConfig(config_dir=config_dir)
            rc = main(
                [
                    "--prompt",
                    "remind me about the metadata bug?",
                    "--session-id",
                    "cli-2",
                    "--turn",
                    "2",
                ]
            )

        assert rc == 0
        captured = capsys.readouterr()
        assert SENTINEL_OPEN in captured.out
