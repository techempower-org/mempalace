"""
test_cli_tags.py — ``mempalace tags`` fast-path daemon command (slice of #191).

The ``tags`` subcommand wraps the daemon's ``mempalace_list_tags`` MCP
tool. Tests mock ``urllib.request.urlopen`` so the dispatcher, filter
propagation, output formats, and failure modes are exercised without
touching a real daemon.

Mirrors ``test_cli_stats.py`` / ``test_cli_cypher.py`` patterns: per-class
``_args`` factory and ``_FakeResp`` wrapper for the urllib mock.
"""

import argparse
import json
from unittest.mock import patch

import pytest


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _mcp_envelope(payload: dict) -> bytes:
    """Wrap a tool payload in the JSON-RPC content envelope the daemon emits."""
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }
    ).encode()


def _make_tags_responder(payload: dict, captured: list | None = None):
    """Return a fake urlopen that serves the /mcp tool call for tags.

    Optionally records the POST body so the test can assert which
    arguments the CLI forwarded (filter propagation).
    """

    def fake_urlopen(req, timeout=None):
        if getattr(req, "data", None) is not None and captured is not None:
            captured.append(json.loads(req.data.decode()))
        return _FakeResp(_mcp_envelope(payload))

    return fake_urlopen


_FULL_TAGS = {
    "tags": [
        {"tag": "rust", "count": 12},
        {"tag": "python", "count": 9},
        {"tag": "ops", "count": 4},
    ],
    "total_unique_tags": 3,
    "filters": {"wing": None, "room": None, "min_count": 1},
}


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085"}


def _args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "wing": None,
        "room": None,
        "min_count": 1,
        "top": 20,
        "format": None,
        "palace": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestTagsTableOutput:
    """Default table mode renders the count gauge + filter scope label."""

    def test_renders_aligned_table(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_tags_responder(_FULL_TAGS)):
                cli.cmd_tags(_args())

        out = capsys.readouterr().out
        assert "TAGS — 3 unique" in out
        # Each tag and its count appear.
        for tag in ("rust", "python", "ops"):
            assert tag in out
        # The gauge column renders something for the largest tag.
        assert "12" in out

    def test_scope_label_shows_wing_filter(self, capsys):
        from mempalace import cli

        scoped = dict(_FULL_TAGS)
        scoped["filters"] = {"wing": "projects", "room": None, "min_count": 1}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_tags_responder(scoped)):
                cli.cmd_tags(_args(wing="projects"))

        out = capsys.readouterr().out
        assert "wing=projects" in out

    def test_top_truncates_and_emits_tail(self, capsys):
        from mempalace import cli

        payload = {
            "tags": [{"tag": f"t{i:02}", "count": 100 - i} for i in range(20)],
            "total_unique_tags": 20,
            "filters": {"wing": None, "room": None, "min_count": 1},
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_tags_responder(payload)):
                cli.cmd_tags(_args(top=3))

        out = capsys.readouterr().out
        assert "more tags" in out
        # Only the top 3 names render (t00..t02).
        assert "t00" in out and "t02" in out
        # The 4th-and-later names are suppressed.
        assert "t03" not in out

    def test_empty_payload_renders_clean_message(self, capsys):
        from mempalace import cli

        empty = {
            "tags": [],
            "total_unique_tags": 0,
            "filters": {"wing": "empty-wing", "room": None, "min_count": 5},
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_tags_responder(empty)):
                cli.cmd_tags(_args(wing="empty-wing", min_count=5))

        out = capsys.readouterr().out
        assert "no tags match" in out


class TestTagsJsonOutput:
    """JSON mode passes the daemon envelope through unchanged."""

    def test_json_passes_through_full_envelope(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_tags_responder(_FULL_TAGS)):
                cli.cmd_tags(_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["total_unique_tags"] == 3
        assert payload["tags"][0]["tag"] == "rust"
        # Filters round-trip even when None.
        assert "filters" in payload

    def test_json_shorthand_via_legacy_flag(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_tags_responder(_FULL_TAGS)):
                cli.cmd_tags(_args(json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["tags"][1]["tag"] == "python"


class TestTagsFilterPropagation:
    """The CLI must forward --wing/--room/--min-count onto the MCP arguments."""

    def test_wing_and_room_forwarded(self):
        from mempalace import cli

        captured: list[dict] = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_tags_responder(_FULL_TAGS, captured=captured),
            ):
                cli.cmd_tags(_args(wing="projects", room="discoveries", min_count=3))

        assert len(captured) == 1
        arguments = captured[0]["params"]["arguments"]
        assert arguments["wing"] == "projects"
        assert arguments["room"] == "discoveries"
        assert arguments["min_count"] == 3

    def test_unset_filters_omitted_from_arguments(self):
        from mempalace import cli

        captured: list[dict] = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_tags_responder(_FULL_TAGS, captured=captured),
            ):
                cli.cmd_tags(_args())

        arguments = captured[0]["params"]["arguments"]
        # min_count always rides along (default 1); wing/room must not
        # leak as ``None`` to the daemon.
        assert "wing" not in arguments
        assert "room" not in arguments
        assert arguments["min_count"] == 1


class TestTagsFailureModes:
    """Failure shape matches the sibling fast-path commands."""

    def test_no_daemon_url_exits_2(self, capsys):
        from mempalace import cli
        from mempalace.config import MempalaceConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "mempalace.cli.MempalaceConfig",
                lambda: MempalaceConfig(config_dir="/tmp/empty-mempalace-config-tags"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_tags(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "PALACE_DAEMON_URL" in err

    def test_unreachable_exits_2(self, capsys):
        from mempalace import cli

        def boom(req, timeout=None):
            raise ConnectionError("daemon down")

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=boom):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_tags(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "daemon" in err.lower()

    def test_inner_error_envelope_exits_2(self, capsys):
        from mempalace import cli

        bad = {"error": "palace unavailable"}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_tags_responder(bad)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_tags(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "palace unavailable" in err


class TestParserAcceptsTags:
    """Argparse wiring — flags propagate onto the Namespace."""

    def _parse(self, argv):
        from mempalace import cli

        with patch("sys.argv", argv):
            with patch.object(cli, "cmd_tags") as mock:
                mock.side_effect = SystemExit(0)
                with pytest.raises(SystemExit):
                    cli.main()
                return mock.call_args.args[0] if mock.call_args else None

    def test_defaults(self):
        ns = self._parse(["mempalace", "tags"])
        assert ns is not None
        assert ns.command == "tags"
        assert ns.wing is None
        assert ns.room is None
        assert ns.min_count == 1
        assert ns.top == 20

    def test_wing_and_room(self):
        ns = self._parse(["mempalace", "tags", "--wing", "projects", "--room", "discoveries"])
        assert ns is not None
        assert ns.wing == "projects"
        assert ns.room == "discoveries"

    def test_min_count_and_top(self):
        ns = self._parse(["mempalace", "tags", "--min-count", "5", "--top", "3"])
        assert ns is not None
        assert ns.min_count == 5
        assert ns.top == 3

    def test_min_count_rejects_negative(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "tags", "--min-count", "-1"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2

    def test_format_json(self):
        ns = self._parse(["mempalace", "tags", "--format", "json"])
        assert ns is not None
        assert ns.format == "json"

    def test_json_shorthand(self):
        ns = self._parse(["mempalace", "tags", "--json"])
        assert ns is not None
        assert ns.json is True
