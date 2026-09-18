"""
test_cli_rate.py — ``mempalace rate`` drawer feedback (slice of #191, issue #361).

Wraps ``mempalace_rate_memory``, which records a boolean and has no field for
a score scale or a free-text reason — so issue #361's ``--score N --reason``
sketch becomes ``--useful`` / ``--not-useful``. The rating is metadata only;
verbatim content is never touched (asserted at the tool layer in
tests/test_ratings.py — here we assert the CLI contract).
"""

import argparse
import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "drawer_id": "drawer-abc123",
        "useful": True,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


_RATE_OK = {
    "success": True,
    "drawer_id": "drawer-abc123",
    "useful": True,
    "rating_useful": 3,
    "rating_not_useful": 1,
    "net_rating": 2,
}


def _daemon(payload):
    fake = MagicMock(return_value=payload)
    return (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._call_daemon_tool", fake),
        fake,
    )


class TestRate:
    def test_useful_sends_true(self):
        from mempalace import cli

        strict, call, fake = _daemon(_RATE_OK)
        with strict, call:
            cli.cmd_rate(_args())

        name, payload = fake.call_args[0]
        assert name == "mempalace_rate_memory"
        assert payload == {"drawer_id": "drawer-abc123", "useful": True}

    def test_not_useful_sends_false(self):
        from mempalace import cli

        strict, call, fake = _daemon(dict(_RATE_OK, useful=False))
        with strict, call:
            cli.cmd_rate(_args(useful=False))

        assert fake.call_args[0][1]["useful"] is False

    def test_drawer_id_is_stripped(self):
        from mempalace import cli

        strict, call, fake = _daemon(_RATE_OK)
        with strict, call:
            cli.cmd_rate(_args(drawer_id="  drawer-abc123  "))

        assert fake.call_args[0][1]["drawer_id"] == "drawer-abc123"

    def test_table_output_reports_counters(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_RATE_OK)
        with strict, call:
            cli.cmd_rate(_args())

        out = capsys.readouterr().out
        assert "Rated drawer-abc123 useful" in out
        assert "useful=3" in out
        assert "not_useful=1" in out
        assert "net=2" in out

    def test_not_useful_verdict_in_table(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(dict(_RATE_OK, useful=False))
        with strict, call:
            cli.cmd_rate(_args(useful=False))

        assert "not useful" in capsys.readouterr().out

    def test_json_passthrough(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_RATE_OK)
        with strict, call:
            cli.cmd_rate(_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["net_rating"] == 2
        assert payload["drawer_id"] == "drawer-abc123"

    def test_missing_drawer_id_exits_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_RATE_OK)
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_rate(_args(drawer_id="   "))

        assert exc.value.code == 2
        assert fake.call_count == 0
        assert "drawer ID" in capsys.readouterr().err

    def test_missing_verdict_exits_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_RATE_OK)
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_rate(_args(useful=None))

        assert exc.value.code == 2
        assert fake.call_count == 0
        assert "--useful or --not-useful" in capsys.readouterr().err

    def test_unknown_drawer_exits_2(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"success": False, "error": "Drawer not found: drawer-abc123"})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_rate(_args())

        assert exc.value.code == 2
        assert "Drawer not found" in capsys.readouterr().err

    def test_daemon_unreachable_exits_2(self, capsys):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", side_effect=cli.DaemonError("offline")),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_rate(_args())

        assert exc.value.code == 2
        assert "daemon unreachable" in capsys.readouterr().err

    def test_local_path_calls_tool_function(self):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.mcp_server.tool_rate_memory", return_value=_RATE_OK) as local,
        ):
            cli.cmd_rate(_args())

        local.assert_called_once_with(drawer_id="drawer-abc123", useful=True)


class TestRateParser:
    """The verdict flags are mutually exclusive and one is required.

    ``cli.main()`` pops ``PYTHONPATH`` by design (#1423), so both cases
    snapshot the environment rather than leaking that into the rest of the
    session.
    """

    def test_both_flags_rejected(self):
        from mempalace import cli

        with (
            patch.dict("os.environ", dict(os.environ)),
            patch("sys.argv", ["mempalace", "rate", "d1", "--useful", "--not-useful"]),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.main()

        assert exc.value.code == 2

    def test_no_flag_rejected(self):
        from mempalace import cli

        with (
            patch.dict("os.environ", dict(os.environ)),
            patch("sys.argv", ["mempalace", "rate", "d1"]),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.main()

        assert exc.value.code == 2
