"""
test_cli_tunnels.py — ``mempalace tunnels`` cross-wing tunnel inventory (slice of #191).

The ``tunnels`` subcommand wraps the daemon's ``mempalace_list_tunnels``
MCP tool. The daemon returns a bare list of tunnel records, each tagged
with ``kind: 'explicit'|'passive'`` (issue #75). Default is explicit
only; ``--passive`` opts in to the inferred-overlap tunnels.

Mirrors ``test_cli_tags.py`` patterns.
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


def _mcp_envelope(payload) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
        }
    ).encode()


def _make_responder(payload, captured: list | None = None):
    def fake_urlopen(req, timeout=None):
        if captured is not None and getattr(req, "data", None) is not None:
            captured.append(json.loads(req.data.decode()))
        return _FakeResp(_mcp_envelope(payload))

    return fake_urlopen


_EXPLICIT_TUNNELS = [
    {
        "id": "t1",
        "source_wing": "memorypalace",
        "source_room": "decisions",
        "target_wing": "palace-daemon",
        "target_room": "decisions",
        "label": "shared decision history",
        "kind": "explicit",
    },
    {
        "id": "t2",
        "source_wing": "projects",
        "source_room": "sessions",
        "target_wing": "storyvox",
        "target_room": "sessions",
        "label": "shared session history",
        "kind": "explicit",
    },
]

_MIXED_TUNNELS = _EXPLICIT_TUNNELS + [
    {
        "source_wing": "memorypalace",
        "source_room": "references",
        "target_wing": "palace-daemon",
        "target_room": "references",
        "kind": "passive",
    },
]


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085"}


def _args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "wing": None,
        "passive": False,
        "format": None,
        "palace": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestTunnelsTableOutput:
    """Default table mode renders header + per-tunnel rows."""

    def test_renders_explicit_tunnels(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_EXPLICIT_TUNNELS),
            ):
                cli.cmd_tunnels(_args())

        out = capsys.readouterr().out
        assert "TUNNELS — 2" in out
        assert "memorypalace" in out
        assert "palace-daemon" in out
        assert "explicit" in out
        # No passive rows in the explicit-only default.
        assert "passive" not in out

    def test_empty_response_renders_clean_message(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder([])):
                cli.cmd_tunnels(_args())

        out = capsys.readouterr().out
        assert "(no tunnels)" in out

    def test_wing_scope_label_in_header(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_EXPLICIT_TUNNELS[:1]),
            ):
                cli.cmd_tunnels(_args(wing="memorypalace"))

        out = capsys.readouterr().out
        assert "wing=memorypalace" in out

    def test_passive_kind_renders(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_MIXED_TUNNELS)):
                cli.cmd_tunnels(_args(passive=True))

        out = capsys.readouterr().out
        assert "passive" in out
        assert "explicit" in out
        assert "TUNNELS — 3" in out


class TestTunnelsJsonOutput:
    """JSON mode is a pass-through of the daemon envelope."""

    def test_json_envelope_shape(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_EXPLICIT_TUNNELS),
            ):
                cli.cmd_tunnels(_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list)
        assert len(payload) == 2
        assert payload[0]["source_wing"] == "memorypalace"

    def test_json_shorthand_via_legacy_flag(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_EXPLICIT_TUNNELS),
            ):
                cli.cmd_tunnels(_args(json=True))

        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 2


class TestTunnelsArguments:
    """--wing / --passive flow into the daemon arguments."""

    def test_wing_filter_propagates(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_EXPLICIT_TUNNELS, captured=captured),
            ):
                cli.cmd_tunnels(_args(wing="projects"))

        args_sent = captured[0]["params"]["arguments"]
        assert args_sent["wing"] == "projects"
        assert args_sent["include_passive"] is False

    def test_passive_opt_in_propagates(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_MIXED_TUNNELS, captured=captured),
            ):
                cli.cmd_tunnels(_args(passive=True))

        args_sent = captured[0]["params"]["arguments"]
        assert args_sent["include_passive"] is True
        # No wing key when not scoped — keep the request slim.
        assert "wing" not in args_sent


class TestTunnelsFailureModes:
    """Failure shape matches sibling fast-path commands."""

    def test_no_daemon_url_exits_2(self, capsys):
        from mempalace import cli
        from mempalace.config import MempalaceConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "mempalace.cli.MempalaceConfig",
                lambda: MempalaceConfig(config_dir="/tmp/empty-mempalace-config-tunnels"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_tunnels(_args())
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
                    cli.cmd_tunnels(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "daemon" in err.lower()

    def test_inner_error_envelope_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"error": "tunnels file unreadable"}),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_tunnels(_args())
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "tunnels file unreadable" in err


class TestParserAcceptsTunnels:
    """Argparse wiring — flags propagate onto the Namespace."""

    def _parse(self, argv):
        from mempalace import cli

        with patch("sys.argv", argv):
            with patch.object(cli, "cmd_tunnels") as mock:
                mock.side_effect = SystemExit(0)
                with pytest.raises(SystemExit):
                    cli.main()
                return mock.call_args.args[0] if mock.call_args else None

    def test_no_args(self):
        ns = self._parse(["mempalace", "tunnels"])
        assert ns is not None
        assert ns.command == "tunnels"
        assert ns.wing is None
        assert ns.passive is False

    def test_wing_filter(self):
        ns = self._parse(["mempalace", "tunnels", "--wing", "projects"])
        assert ns is not None
        assert ns.wing == "projects"

    def test_passive_flag(self):
        ns = self._parse(["mempalace", "tunnels", "--passive"])
        assert ns is not None
        assert ns.passive is True

    def test_format_json(self):
        ns = self._parse(["mempalace", "tunnels", "--format", "json"])
        assert ns is not None
        assert ns.format == "json"

    def test_json_shorthand(self):
        ns = self._parse(["mempalace", "tunnels", "--json"])
        assert ns is not None
        assert ns.json is True
