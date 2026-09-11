"""
test_cli_json.py — Agent-shaped CLI output (issue #44).

Covers ``--json`` / ``-j`` and ``--quiet`` / ``-q`` for the commands
that compose into shell pipelines (``status``, ``search``, ``mined``).
Each command's JSON output must be valid JSON, must contain the keys
agents expect, and must match the MCP tool response shape where one
exists so an agent can switch between MCP and CLI without rewriting
parsers.

The local-path tests stub ``search_memories`` / collection access so
they run without a real palace. The daemon-routing tests opt back into
``PALACE_DAEMON_URL`` via ``patch.dict`` (the autouse scrub in
``conftest.py`` clears it for the session).
"""

import argparse
import json
import sys
from unittest.mock import MagicMock, patch

import pytest


# ── _resolve_quiet ───────────────────────────────────────────────────


class TestResolveQuiet:
    def test_json_implies_quiet(self):
        from mempalace.cli import _resolve_quiet

        args = argparse.Namespace(json=True, quiet=False)
        assert _resolve_quiet(args) is True

    def test_quiet_flag_explicit(self):
        from mempalace.cli import _resolve_quiet

        args = argparse.Namespace(json=False, quiet=True)
        assert _resolve_quiet(args) is True

    def test_neither_flag_set_with_tty(self):
        from mempalace.cli import _resolve_quiet

        args = argparse.Namespace(json=False, quiet=False)
        fake_stdout = MagicMock()
        fake_stdout.isatty.return_value = True
        with patch.object(sys, "stdout", fake_stdout):
            assert _resolve_quiet(args) is False

    def test_non_tty_defaults_to_quiet(self):
        from mempalace.cli import _resolve_quiet

        args = argparse.Namespace(json=False, quiet=False)
        fake_stdout = MagicMock()
        fake_stdout.isatty.return_value = False
        with patch.object(sys, "stdout", fake_stdout):
            assert _resolve_quiet(args) is True

    def test_stdout_without_isatty_treated_as_no_tty(self):
        """A stdout replacement without ``isatty`` should still resolve.

        Test harnesses sometimes substitute ``sys.stdout`` with a
        ``StringIO`` (which does have ``isatty``) or a bare buffer
        (which does not). The helper must not crash on either.
        """
        from mempalace.cli import _resolve_quiet

        args = argparse.Namespace(json=False, quiet=False)

        class _Bare:
            pass

        with patch.object(sys, "stdout", _Bare()):
            assert _resolve_quiet(args) is True

    def test_args_without_json_quiet_attrs(self):
        """``getattr`` defaults must shield us from old call sites that
        build a ``Namespace`` without the new fields."""
        from mempalace.cli import _resolve_quiet

        args = argparse.Namespace()  # no json, no quiet
        fake_stdout = MagicMock()
        fake_stdout.isatty.return_value = True
        with patch.object(sys, "stdout", fake_stdout):
            assert _resolve_quiet(args) is False


# ── _emit_json ───────────────────────────────────────────────────────


class TestEmitJson:
    def test_writes_valid_json_with_trailing_newline(self, capsys):
        from mempalace.cli import _emit_json

        _emit_json({"hello": "world", "n": 3})
        out = capsys.readouterr().out
        assert out.endswith("\n")
        parsed = json.loads(out)
        assert parsed == {"hello": "world", "n": 3}

    def test_preserves_insertion_order(self, capsys):
        """``json.dump`` with ``sort_keys=False`` (the default) should
        preserve insertion order — important so agents can rely on
        ``query`` appearing where the schema documents it."""
        from mempalace.cli import _emit_json

        _emit_json({"zzz": 1, "aaa": 2, "mmm": 3})
        out = capsys.readouterr().out
        # The first key after the opening brace should be "zzz"
        first_key_position = out.find('"zzz"')
        last_key_position = out.find('"aaa"')
        assert 0 < first_key_position < last_key_position

    def test_non_serializable_falls_back_to_str(self, capsys):
        from mempalace.cli import _emit_json

        # ``default=str`` lets paths/Decimals/etc. serialize cleanly.
        from pathlib import Path

        _emit_json({"path": Path("/tmp/x")})
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed == {"path": "/tmp/x"}


# ── cmd_status JSON ──────────────────────────────────────────────────


class TestCmdStatusJson:
    def test_local_palace_emits_total_drawers_wings_rooms(self, capsys, tmp_path):
        """``status --json`` (local path) must mirror the MCP
        ``tool_status`` shape: ``total_drawers``, ``wings``, ``rooms``."""
        from mempalace.cli import _emit_local_status_json

        # Build a fake collection with two drawers across two wings.
        fake_col = MagicMock()
        fake_col.count.return_value = 2
        fake_col.get.side_effect = [
            {
                "metadatas": [
                    {"wing": "wing_a", "room": "room_1"},
                    {"wing": "wing_b", "room": "room_2"},
                ]
            },
            {"metadatas": []},
        ]

        with patch("mempalace.miner._open_collection_or_explain", return_value=fake_col):
            _emit_local_status_json(str(tmp_path))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["total_drawers"] == 2
        assert payload["wings"] == {"wing_a": 1, "wing_b": 1}
        assert payload["rooms"] == {"room_1": 1, "room_2": 1}
        assert payload["palace_path"] == str(tmp_path)

    def test_missing_palace_returns_error_and_exit_2(self, capsys, tmp_path):
        """When the palace can't be opened, JSON must include
        ``error: palace_unavailable`` and the CLI must exit with code 2
        (palace-unavailable exit code from issue #44)."""
        from mempalace.cli import _emit_local_status_json

        with patch("mempalace.miner._open_collection_or_explain", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                _emit_local_status_json(str(tmp_path / "missing"))

        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["error"] == "palace_unavailable"
        assert "hint" in payload
        assert "palace_path" in payload

    @patch("mempalace.cli.MempalaceConfig")
    def test_daemon_routing_emits_daemon_payload_as_json(self, mock_cfg, capsys):
        """When daemon routing is on, ``status --json`` must emit the
        daemon's JSON payload verbatim (no prose chrome)."""
        mock_cfg.return_value.palace_path = "/fake/palace"
        mock_cfg.return_value.daemon_strict = True

        args = argparse.Namespace(palace=None, json=True, quiet=False)
        daemon_payload = {"total_drawers": 7, "wings": {"wing_x": 7}}

        with patch("mempalace.cli._call_daemon_rest", return_value=None):
            with patch("mempalace.cli._call_daemon_tool", return_value=daemon_payload):
                with patch("mempalace.cli._daemon_strict", return_value=True):
                    from mempalace.cli import cmd_status

                    cmd_status(args)

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload == daemon_payload

    @patch("mempalace.cli.MempalaceConfig")
    def test_daemon_error_emits_json_error_and_exit_2(self, mock_cfg, capsys):
        """Daemon unreachable → JSON error envelope + exit 2."""
        mock_cfg.return_value.palace_path = "/fake/palace"
        mock_cfg.return_value.daemon_strict = True

        args = argparse.Namespace(palace=None, json=True, quiet=False)

        from mempalace.cli import DaemonError, cmd_status

        with patch("mempalace.cli._daemon_strict", return_value=True):
            with patch(
                "mempalace.cli._call_daemon_rest",
                side_effect=DaemonError("connection refused"),
            ):
                with patch(
                    "mempalace.cli._call_daemon_tool",
                    side_effect=DaemonError("connection refused"),
                ):
                    with pytest.raises(SystemExit) as exc_info:
                        cmd_status(args)

        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["error"] == "connection refused"
        assert payload["source"] == "daemon"


# ── cmd_search JSON ──────────────────────────────────────────────────


class TestCmdSearchJson:
    def test_local_search_returns_results_envelope(self, capsys, tmp_path):
        """``search --json`` (local path) must emit the MCP-shaped
        envelope ``{results, query, warnings, available_in_scope}``."""
        from mempalace.cli import _emit_local_search_json

        # Build a palace dir + chroma.sqlite3 so the early-out probes
        # don't trip.
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")

        fake_search = MagicMock(
            return_value={
                "results": [
                    {
                        "wing": "wing_a",
                        "room": "room_1",
                        "text": "a verbatim drawer",
                        "similarity": 0.91,
                        "source_file": "/path/to/file.md",
                    }
                ],
                "warnings": [],
                "available_in_scope": 1,
            }
        )

        with pytest.raises(SystemExit) as exc_info:
            _emit_local_search_json(
                query="needle",
                palace_path=str(palace),
                wing=None,
                room=None,
                n_results=5,
                search_memories=fake_search,
            )

        # exit 0 = at least one result
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["query"] == "needle"
        assert len(payload["results"]) == 1
        assert payload["results"][0]["text"] == "a verbatim drawer"
        assert payload["available_in_scope"] == 1

    def test_no_results_exits_with_code_1(self, capsys, tmp_path):
        """Empty result set → exit 1 (so shell scripts can branch on
        ``if mempalace search X --json | jq ...; then``)."""
        from mempalace.cli import _emit_local_search_json

        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")

        fake_search = MagicMock(return_value={"results": [], "warnings": []})

        with pytest.raises(SystemExit) as exc_info:
            _emit_local_search_json(
                query="nothing matches",
                palace_path=str(palace),
                wing=None,
                room=None,
                n_results=5,
                search_memories=fake_search,
            )

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["results"] == []
        assert payload["query"] == "nothing matches"

    def test_missing_palace_returns_palace_unavailable(self, capsys, tmp_path):
        """No palace dir → JSON error + exit 2."""
        from mempalace.cli import _emit_local_search_json

        fake_search = MagicMock()  # should never be called

        with pytest.raises(SystemExit) as exc_info:
            _emit_local_search_json(
                query="X",
                palace_path=str(tmp_path / "nonexistent"),
                wing=None,
                room=None,
                n_results=5,
                search_memories=fake_search,
            )

        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["error"] == "palace_unavailable"
        assert payload["query"] == "X"
        fake_search.assert_not_called()

    def test_palace_dir_without_chroma_sqlite_returns_palace_unavailable(self, capsys, tmp_path):
        """Palace dir exists but no chroma.sqlite3 → JSON error + exit 2.
        Mirrors searcher.search's State B / State C distinction."""
        from mempalace.cli import _emit_local_search_json

        palace = tmp_path / "empty_palace"
        palace.mkdir()  # but no chroma.sqlite3

        fake_search = MagicMock()

        with pytest.raises(SystemExit) as exc_info:
            _emit_local_search_json(
                query="X",
                palace_path=str(palace),
                wing=None,
                room=None,
                n_results=5,
                search_memories=fake_search,
            )

        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["error"] == "palace_unavailable"

    @patch("mempalace.cli.MempalaceConfig")
    def test_daemon_search_emits_results_with_query_key(self, mock_cfg, capsys):
        """Daemon routing → JSON output must have ``query`` set even if
        the daemon's payload didn't include it."""
        mock_cfg.return_value.palace_path = "/fake/palace"
        mock_cfg.return_value.daemon_strict = True

        args = argparse.Namespace(
            palace=None,
            json=True,
            quiet=False,
            query="needle",
            wing=None,
            room=None,
            results=5,
        )
        # Daemon-returned payload sans ``query`` key — _emit_json should add it.
        daemon_payload = {
            "results": [{"wing": "a", "room": "b", "text": "hit", "similarity": 0.8}],
            "warnings": [],
        }

        with patch("mempalace.cli._daemon_strict", return_value=True):
            with patch("mempalace.cli._call_daemon_rest", return_value=None):
                with patch("mempalace.cli._call_daemon_tool", return_value=daemon_payload):
                    with pytest.raises(SystemExit) as exc_info:
                        from mempalace.cli import cmd_search

                        cmd_search(args)

        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["query"] == "needle"
        assert len(payload["results"]) == 1


# ── cmd_mined JSON ───────────────────────────────────────────────────


class TestCmdMinedJson:
    def test_palace_missing_emits_palace_unavailable(self, capsys, tmp_path):
        """``mined --json`` against a missing palace must emit a JSON
        error envelope and exit 2."""
        from mempalace.cli import cmd_mined

        args = argparse.Namespace(
            palace=str(tmp_path / "missing"),
            json=True,
            quiet=False,
            wing=None,
            limit=50,
        )
        with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
            mock_cfg.return_value.palace_path = str(tmp_path / "missing")
            with pytest.raises(SystemExit) as exc_info:
                cmd_mined(args)

        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["error"] == "palace_unavailable"

    def test_empty_palace_emits_empty_envelope_and_exit_1(self, capsys, tmp_path):
        """A palace with no mined source files → empty envelope + exit 1."""
        from mempalace.cli import cmd_mined

        # Build a stand-in palace
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")

        fake_col = MagicMock()
        fake_col.get.return_value = {"metadatas": []}
        args = argparse.Namespace(palace=str(palace), json=True, quiet=False, wing=None, limit=50)

        # `mined` opens through palace.get_collection since #459 (it used to
        # construct ChromaBackend directly); patch the seam it actually uses.
        with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
            mock_cfg.return_value.palace_path = str(palace)
            with patch("mempalace.palace.get_collection", return_value=fake_col):
                with patch("mempalace.migrate.contains_palace_database", return_value=True):
                    with pytest.raises(SystemExit) as exc_info:
                        cmd_mined(args)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["sources_by_wing"] == {}
        assert payload["total_wings"] == 0

    def test_populated_palace_emits_grouped_sources(self, capsys, tmp_path):
        """JSON output groups source files by wing with per-wing totals
        and a truncated flag when --limit cuts the list."""
        from mempalace.cli import cmd_mined

        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_text("")

        # Two wings, multiple sources per wing.
        fake_col = MagicMock()
        fake_col.get.side_effect = [
            {
                "metadatas": [
                    {"wing": "wing_a", "source_file": "/p/a.md"},
                    {"wing": "wing_a", "source_file": "/p/a.md"},
                    {"wing": "wing_a", "source_file": "/p/b.md"},
                    {"wing": "wing_b", "source_file": "/q/c.md"},
                    {"wing": "wing_a"},  # no source_file → skipped
                ]
            },
            {"metadatas": []},  # next batch empty → terminate loop
        ]
        args = argparse.Namespace(palace=str(palace), json=True, quiet=False, wing=None, limit=50)

        # `mined` opens through palace.get_collection since #459 (it used to
        # construct ChromaBackend directly); patch the seam it actually uses.
        with patch("mempalace.cli.MempalaceConfig") as mock_cfg:
            mock_cfg.return_value.palace_path = str(palace)
            with patch("mempalace.palace.get_collection", return_value=fake_col):
                with patch("mempalace.migrate.contains_palace_database", return_value=True):
                    cmd_mined(args)

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "wing_a" in payload["sources_by_wing"]
        assert "wing_b" in payload["sources_by_wing"]
        wing_a = payload["sources_by_wing"]["wing_a"]
        assert wing_a["total_sources"] == 2  # /p/a.md and /p/b.md
        assert wing_a["total_drawers"] == 3  # 2 + 1
        # Sources are ordered by drawer count desc
        assert wing_a["sources"][0] == {"source_file": "/p/a.md", "drawer_count": 2}
        assert wing_a["truncated"] is False


# ── Quiet flag suppresses chrome ─────────────────────────────────────


class TestQuietSuppressesChrome:
    def test_quiet_passed_explicitly(self):
        """``args.quiet=True`` resolves to quiet even with a real TTY."""
        from mempalace.cli import _resolve_quiet

        args = argparse.Namespace(json=False, quiet=True)
        fake_stdout = MagicMock()
        fake_stdout.isatty.return_value = True
        with patch.object(sys, "stdout", fake_stdout):
            assert _resolve_quiet(args) is True


# ── End-to-end: parser accepts --json/--quiet pre- and post-subcommand


class TestParserAcceptsFlags:
    def _parser_attrs(self, argv):
        """Run ``main()`` up to dispatch and capture the parsed args.

        Stubs every dispatched ``cmd_*`` so the test doesn't touch a
        real palace.
        """
        from mempalace import cli

        captured = {}

        def _capture(name):
            def _stub(args):
                captured["name"] = name
                captured["args"] = args

            return _stub

        with patch.dict(
            "os.environ",
            {k: v for k, v in {}.items()},  # leave env alone
            clear=False,
        ):
            with patch.object(cli, "cmd_status", _capture("status")):
                with patch.object(cli, "cmd_mined", _capture("mined")):
                    with patch.object(cli, "cmd_search", _capture("search")):
                        with patch.object(sys, "argv", argv):
                            try:
                                cli.main()
                            except SystemExit:
                                pass
        return captured

    def test_json_pre_subcommand(self):
        captured = self._parser_attrs(["mempalace", "--json", "status"])
        assert captured["args"].json is True
        assert captured["name"] == "status"

    def test_json_post_subcommand(self):
        captured = self._parser_attrs(["mempalace", "status", "--json"])
        assert captured["args"].json is True

    def test_short_j_flag(self):
        captured = self._parser_attrs(["mempalace", "status", "-j"])
        assert captured["args"].json is True

    def test_quiet_post_subcommand(self):
        captured = self._parser_attrs(["mempalace", "status", "--quiet"])
        assert captured["args"].quiet is True

    def test_short_q_flag(self):
        captured = self._parser_attrs(["mempalace", "status", "-q"])
        assert captured["args"].quiet is True

    def test_neither_flag(self):
        captured = self._parser_attrs(["mempalace", "status"])
        assert captured["args"].json is False
        assert captured["args"].quiet is False

    def test_search_accepts_json_post_subcommand(self):
        captured = self._parser_attrs(["mempalace", "search", "query text", "--json"])
        assert captured["args"].json is True
        assert captured["args"].query == "query text"

    def test_mined_accepts_json(self):
        captured = self._parser_attrs(["mempalace", "mined", "--json"])
        assert captured["args"].json is True
        assert captured["name"] == "mined"
