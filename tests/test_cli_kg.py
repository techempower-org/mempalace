"""
test_cli_kg.py — ``mempalace kg add|invalidate|timeline`` (slice of #191, issue #357).

Three MCP tools behind one verb: ``mempalace_kg_add``,
``mempalace_kg_invalidate``, ``mempalace_kg_timeline``. Routing follows
``cmd_wakeup`` / ``cmd_mined`` (daemon-strict and no ``--palace`` → daemon,
else the local ``mempalace.mcp_server`` function).

Two deliberate deviations from the issue's sketch, both driven by the tool
schemas and asserted here:

* ``kg invalidate`` addresses a fact by subject/predicate/object — the tool
  has no triple ids and no reason field — and is gated behind ``--confirm``
  because retracting a fact rewrites graph history.
* ``kg timeline --limit`` truncates client-side; ``mempalace_kg_timeline``
  accepts only ``(entity, as_of)``.

There is no ``kg stats``: ``mempalace stats --section kg`` already renders
the same block (and ``mempalace graph`` embeds ``kg_stats``). The absence is
asserted below so a future slice doesn't re-add the redundant surface without
noticing.
"""

import argparse
import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _add_args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "kg_action": "add",
        "subject": "JP",
        "predicate": "maintains",
        "object": "mempalace",
        "valid_from": None,
        "valid_to": None,
        "source_closet": None,
        "source_file": None,
        "source_drawer_id": None,
        "context": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _inv_args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "kg_action": "invalidate",
        "subject": "JP",
        "predicate": "maintains",
        "object": "old-project",
        "ended": None,
        "confirm": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _tl_args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "kg_action": "timeline",
        "entity": "JP",
        "as_of": None,
        "limit": 20,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


_ADD_OK = {
    "success": True,
    "fact": "JP → maintains → mempalace",
    "valid_from": "2026-05-01",
}

_INV_OK = {
    "success": True,
    "fact": "JP → maintains → old-project",
    "ended": "2026-08-20",
}

_TIMELINE_OK = {
    "entity": "JP",
    "as_of": None,
    "timeline": [
        {
            "subject": "JP",
            "predicate": "maintains",
            "object": "mempalace",
            "valid_from": "2026-05-01",
            "valid_to": None,
            "context": "drawer:abc123",
        },
        {
            "subject": "JP",
            "predicate": "maintains",
            "object": "old-project",
            "valid_from": "2024-01-01",
            "valid_to": "2026-08-20",
            "context": None,
        },
    ],
    "count": 2,
}


def _daemon(payload):
    fake = MagicMock(return_value=payload)
    return (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._call_daemon_tool", fake),
        fake,
    )


class TestKgAdd:
    def test_required_triple_only_sends_three_fields(self):
        from mempalace import cli

        strict, call, fake = _daemon(_ADD_OK)
        with strict, call:
            cli.cmd_kg(_add_args())

        name, payload = fake.call_args[0]
        assert name == "mempalace_kg_add"
        assert payload == {"subject": "JP", "predicate": "maintains", "object": "mempalace"}

    def test_temporal_and_provenance_flags_propagate(self):
        from mempalace import cli

        strict, call, fake = _daemon(_ADD_OK)
        with strict, call:
            cli.cmd_kg(
                _add_args(
                    valid_from="2026-05-01",
                    valid_to="2026-08-01",
                    source_closet="closet-1",
                    source_file="/tmp/notes.md",
                    source_drawer_id="d99",
                    context="drawer:abc123",
                )
            )

        payload = fake.call_args[0][1]
        assert payload["valid_from"] == "2026-05-01"
        assert payload["valid_to"] == "2026-08-01"
        assert payload["source_closet"] == "closet-1"
        assert payload["source_file"] == "/tmp/notes.md"
        assert payload["source_drawer_id"] == "d99"
        assert payload["context"] == "drawer:abc123"

    def test_table_output_confirms_the_fact(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_ADD_OK)
        with strict, call:
            cli.cmd_kg(_add_args())

        out = capsys.readouterr().out
        assert "Added" in out
        assert "JP → maintains → mempalace" in out
        assert "2026-05-01" in out

    def test_json_passthrough(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_ADD_OK)
        with strict, call:
            cli.cmd_kg(_add_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["fact"] == "JP → maintains → mempalace"

    def test_validation_failure_exits_2(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"success": False, "error": "subject must not be empty"})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_kg(_add_args())

        assert exc.value.code == 2
        assert "subject must not be empty" in capsys.readouterr().err

    def test_daemon_unreachable_exits_2(self, capsys):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", side_effect=cli.DaemonError("no route")),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_kg(_add_args())

        assert exc.value.code == 2
        assert "daemon unreachable" in capsys.readouterr().err

    def test_local_path_calls_tool_function(self):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.mcp_server.tool_kg_add", return_value=_ADD_OK) as local,
        ):
            cli.cmd_kg(_add_args(context="drawer:abc123"))

        local.assert_called_once_with(
            subject="JP", predicate="maintains", object="mempalace", context="drawer:abc123"
        )


class TestKgInvalidateConfirmGate:
    def test_refuses_without_confirm_when_not_interactive(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_INV_OK)
        with strict, call, patch("sys.stdin.isatty", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_kg(_inv_args())

        assert exc.value.code == 2
        assert fake.call_count == 0
        assert "--confirm" in capsys.readouterr().err

    def test_refuses_in_json_mode_without_confirm(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_INV_OK)
        with strict, call, patch("sys.stdin.isatty", return_value=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_kg(_inv_args(json=True))

        assert exc.value.code == 2
        assert fake.call_count == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "confirmation_required"
        assert payload["fact"] == "JP → maintains → old-project"

    def test_confirm_flag_proceeds(self):
        from mempalace import cli

        strict, call, fake = _daemon(_INV_OK)
        with strict, call:
            cli.cmd_kg(_inv_args(confirm=True, ended="2026-08-20"))

        name, payload = fake.call_args[0]
        assert name == "mempalace_kg_invalidate"
        assert payload == {
            "subject": "JP",
            "predicate": "maintains",
            "object": "old-project",
            "ended": "2026-08-20",
        }

    def test_interactive_yes_proceeds(self):
        from mempalace import cli

        strict, call, fake = _daemon(_INV_OK)
        with (
            strict,
            call,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="y"),
        ):
            cli.cmd_kg(_inv_args())

        assert fake.call_count == 1

    def test_interactive_decline_aborts_without_calling(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_INV_OK)
        with (
            strict,
            call,
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value=""),
        ):
            cli.cmd_kg(_inv_args())

        assert fake.call_count == 0
        assert "Aborted" in capsys.readouterr().out

    def test_table_output_reports_resolved_end_date(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_INV_OK)
        with strict, call:
            cli.cmd_kg(_inv_args(confirm=True))

        out = capsys.readouterr().out
        assert "Invalidated" in out
        assert "2026-08-20" in out

    def test_local_path_calls_tool_function(self):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.mcp_server.tool_kg_invalidate", return_value=_INV_OK) as local,
        ):
            cli.cmd_kg(_inv_args(confirm=True))

        local.assert_called_once_with(subject="JP", predicate="maintains", object="old-project")


class TestKgTimeline:
    def test_entity_and_as_of_propagate(self):
        from mempalace import cli

        strict, call, fake = _daemon(_TIMELINE_OK)
        with strict, call:
            cli.cmd_kg(_tl_args(as_of="2026-06-01"))

        name, payload = fake.call_args[0]
        assert name == "mempalace_kg_timeline"
        assert payload == {"entity": "JP", "as_of": "2026-06-01"}

    def test_full_timeline_when_no_entity(self):
        from mempalace import cli

        strict, call, fake = _daemon(dict(_TIMELINE_OK, entity="all"))
        with strict, call:
            cli.cmd_kg(_tl_args(entity=None))

        assert fake.call_args[0][1] == {}

    def test_table_output_renders_windows(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_TIMELINE_OK)
        with strict, call:
            cli.cmd_kg(_tl_args())

        out = capsys.readouterr().out
        assert "TIMELINE — JP" in out
        assert "JP → maintains → mempalace" in out
        assert "2024-01-01 → 2026-08-20" in out
        assert "[drawer:abc123]" in out

    def test_limit_truncates_client_side(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_TIMELINE_OK)
        with strict, call:
            cli.cmd_kg(_tl_args(limit=1))

        # The tool takes no limit — the truncation is ours, and the request
        # must stay clean of a parameter the schema doesn't have.
        assert "limit" not in fake.call_args[0][1]
        out = capsys.readouterr().out
        assert "mempalace" in out
        assert "old-project" not in out

    def test_limit_above_the_backend_cap_reports_what_arrived(self, capsys):
        """A --limit past the backend's own ceiling can't widen the window.

        The graph backends' ``timeline()`` stops at 100 rows and the MCP tool
        doesn't expose the parameter, so asking for 200 must not imply 200
        were searched. Confirmed live: ``kg timeline JP --limit 200`` returns
        ``count: 100``.
        """
        from mempalace import cli

        capped = {
            "entity": "JP",
            "as_of": None,
            "timeline": [dict(_TIMELINE_OK["timeline"][0], object=f"o{i}") for i in range(100)],
            "count": 100,
        }
        strict, call, fake = _daemon(capped)
        with strict, call:
            cli.cmd_kg(_tl_args(limit=200, json=True))

        assert "limit" not in fake.call_args[0][1]
        payload = json.loads(capsys.readouterr().out)
        assert payload["showing"] == cli._KG_TIMELINE_TOOL_CAP == 100
        assert payload["count"] == 100

    def test_json_reports_showing_count(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_TIMELINE_OK)
        with strict, call:
            cli.cmd_kg(_tl_args(json=True, limit=1))

        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 2
        assert payload["showing"] == 1
        assert len(payload["timeline"]) == 1

    def test_empty_timeline_message(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"entity": "nobody", "timeline": [], "count": 0})
        with strict, call:
            cli.cmd_kg(_tl_args(entity="nobody"))

        assert "No timeline facts" in capsys.readouterr().out

    def test_error_envelope_exits_2(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"error": "as_of must be YYYY-MM-DD"})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_kg(_tl_args(as_of="yesterday"))

        assert exc.value.code == 2
        assert "as_of must be" in capsys.readouterr().err

    def test_local_path_calls_tool_function(self):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.mcp_server.tool_kg_timeline", return_value=_TIMELINE_OK) as local,
        ):
            cli.cmd_kg(_tl_args())

        local.assert_called_once_with(entity="JP")

    def test_server_side_tool_error_is_not_reported_as_unreachable(self, capsys):
        """A JSON-RPC error is the daemon answering, not the daemon being gone.

        Observed live on the production daemon 2026-08-20: kg_timeline
        returns ``-32000 / OperationalError: the connection is closed``
        while kg_stats on the same handle succeeds. Calling that
        "unreachable" points the reader at the network instead of the
        query.
        """
        from mempalace import cli

        boom = cli.DaemonError("daemon error -32000: Internal tool error")
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", side_effect=boom),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_kg(_tl_args())

        err = capsys.readouterr().err
        assert exc.value.code == 2
        assert "rejected the call" in err
        assert "unreachable" not in err


class TestKgStatsStaysOut:
    """``kg stats`` is intentionally not a subcommand (issue #357's ask)."""

    def test_kg_stats_is_rejected_by_the_parser(self):
        from mempalace import cli

        with patch.dict("os.environ", dict(os.environ)):
            with patch("sys.argv", ["mempalace", "kg", "stats"]):
                with pytest.raises(SystemExit) as exc:
                    cli.main()

        # argparse's invalid-choice exit, not a dispatch into a real command.
        assert exc.value.code == 2

    def test_stats_section_kg_still_exists(self):
        from mempalace import cli

        assert "kg" in cli._STATS_VALID_SECTIONS
