"""
test_cli_list.py — coverage for ``mempalace list`` (issue #191).

The subcommand is a thin wrapper around the daemon's ``GET /list`` REST
route (which itself wraps ``mempalace_list_drawers``). We mock
``_call_daemon_rest`` to avoid spinning up a real daemon — these tests
exercise the CLI surface: flag parsing, output formatting, JSON shape,
and the daemon-down fallback (returns None or raises DaemonError →
stderr message + exit 1).

Recall-preserving by design: ``mempalace list`` never drops a drawer.
Filtering is metadata-only and pagination is offset-based, so every
matching drawer is reachable.
"""

import argparse
import json
from unittest.mock import patch

import pytest


def _make_args(**overrides):
    """Build a Namespace with the defaults the parser would produce."""
    base = {
        "wing": None,
        "room": None,
        "limit": 20,
        "offset": 0,
        "format": None,
        "json": False,
        "quiet": False,
        "palace": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _sample_response(n: int = 2, total: int = None) -> dict:
    """Build a daemon-shaped /list response."""
    drawers = [
        {
            "drawer_id": f"abcdef0123{i:02x}-1111-2222-3333-444455556666",
            "wing": f"wing{i}",
            "room": f"room{i}",
            "tags": [f"tag{i}"],
            "content_preview": f"verbatim content for drawer {i}" * (1 + i),
        }
        for i in range(n)
    ]
    return {
        "drawers": drawers,
        "total": total if total is not None else n,
        "count": n,
        "offset": 0,
        "limit": 20,
    }


# ── flag propagation to _call_daemon_rest ─────────────────────────────


class TestListFlagPropagation:
    def test_default_args_send_limit_20_offset_0(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["path"] = path
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_list(_make_args())

        assert captured["path"] == "/list"
        assert captured["params"]["limit"] == 20
        assert captured["params"]["offset"] == 0
        # No wing/room → keys omitted, not sent as None.
        assert "wing" not in captured["params"]
        assert "room" not in captured["params"]

    def test_wing_only_passed_through(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_list(_make_args(wing="projects"))

        assert captured["params"]["wing"] == "projects"
        assert "room" not in captured["params"]

    def test_room_only_passed_through(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_list(_make_args(room="reflect"))

        assert captured["params"]["room"] == "reflect"
        assert "wing" not in captured["params"]

    def test_wing_and_room_both_pass(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_list(_make_args(wing="memorypalace", room="reflect"))

        assert captured["params"]["wing"] == "memorypalace"
        assert captured["params"]["room"] == "reflect"

    def test_limit_offset_propagate(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_list(_make_args(limit=50, offset=100))

        assert captured["params"]["limit"] == 50
        assert captured["params"]["offset"] == 100

    def test_limit_clamped_to_sanity_max(self):
        """--limit beyond the sanity cap is clamped CLI-side before the
        daemon ever sees a 10-million-row request."""
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_list(_make_args(limit=99999))

        # cli._LIST_LIMIT_MAX is the sanity ceiling.
        assert captured["params"]["limit"] == cli._LIST_LIMIT_MAX

    def test_negative_offset_clamped_to_zero(self):
        from mempalace import cli

        captured = {}

        def fake_rest(path, params=None):
            captured["params"] = params
            return _sample_response()

        with patch("mempalace.cli._call_daemon_rest", side_effect=fake_rest):
            cli.cmd_list(_make_args(offset=-5))

        assert captured["params"]["offset"] == 0


# ── format rendering ──────────────────────────────────────────────────


class TestListFormats:
    def test_table_format_is_default(self, capsys):
        from mempalace import cli

        with patch("mempalace.cli._call_daemon_rest", return_value=_sample_response(n=2)):
            cli.cmd_list(_make_args())

        out = capsys.readouterr().out
        assert "Drawers 1–2 of 2" in out
        # Truncated drawer_id (first 12 chars) appears.
        assert "abcdef012300" in out
        assert "wing0/room0" in out
        # Preview prose shows up.
        assert "verbatim content for drawer 0" in out

    def test_compact_format_one_line_per_drawer(self, capsys):
        from mempalace import cli

        with patch("mempalace.cli._call_daemon_rest", return_value=_sample_response(n=3)):
            cli.cmd_list(_make_args(format="compact"))

        out = capsys.readouterr().out
        # Three drawers → three non-empty lines.
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 3
        # Each line follows "<id12> <wing>/<room>: <preview>".
        for i, line in enumerate(lines):
            assert line.startswith(f"abcdef0123{i:02x}")
            assert f"wing{i}/room{i}:" in line

    def test_full_format_has_labelled_sections(self, capsys):
        from mempalace import cli

        with patch("mempalace.cli._call_daemon_rest", return_value=_sample_response(n=1)):
            cli.cmd_list(_make_args(format="full"))

        out = capsys.readouterr().out
        # Full layout labels every field — no truncation.
        assert "drawer_id:" in out
        assert "wing:" in out
        assert "room:" in out
        assert "tags:" in out
        assert "content:" in out
        # The full preview prose is present without trailing ellipsis.
        assert "verbatim content for drawer 0" in out

    def test_json_format_emits_stable_top_level_shape(self, capsys):
        from mempalace import cli

        resp = _sample_response(n=2, total=42)
        resp["offset"] = 10
        resp["limit"] = 20
        with patch("mempalace.cli._call_daemon_rest", return_value=resp):
            cli.cmd_list(_make_args(format="json"))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert set(payload.keys()) == {"drawers", "total", "count", "offset", "limit"}
        assert payload["total"] == 42
        assert payload["count"] == 2
        assert payload["offset"] == 10
        assert payload["limit"] == 20
        assert len(payload["drawers"]) == 2

    def test_top_level_json_flag_matches_format_json(self, capsys):
        """--json (legacy top-level interop flag) should produce the same
        machine-readable output as --format=json."""
        from mempalace import cli

        with patch("mempalace.cli._call_daemon_rest", return_value=_sample_response(n=1)):
            cli.cmd_list(_make_args(json=True))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "drawers" in payload
        assert "total" in payload


# ── empty results ─────────────────────────────────────────────────────


class TestListEmpty:
    def test_empty_drawers_human_message(self, capsys):
        from mempalace import cli

        empty = {"drawers": [], "total": 0, "count": 0, "offset": 0, "limit": 20}
        with patch("mempalace.cli._call_daemon_rest", return_value=empty):
            cli.cmd_list(_make_args())

        out = capsys.readouterr().out
        assert "No drawers found" in out

    def test_empty_drawers_json_still_has_shape(self, capsys):
        from mempalace import cli

        empty = {"drawers": [], "total": 0, "count": 0, "offset": 0, "limit": 20}
        with patch("mempalace.cli._call_daemon_rest", return_value=empty):
            cli.cmd_list(_make_args(format="json"))

        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["drawers"] == []
        assert payload["total"] == 0


# ── daemon-down fallback ──────────────────────────────────────────────


class TestListDaemonDown:
    def test_none_response_exits_2_with_stderr(self, capsys):
        """``_call_daemon_rest`` returns None on 404/401/403 — endpoint
        missing or auth mismatch. ``mempalace list`` should not silently
        fall back to "no results"; it should treat this as daemon-down
        per the team-lead's spec."""
        from mempalace import cli

        with patch("mempalace.cli._call_daemon_rest", return_value=None):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_list(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "palace daemon unreachable" in err
        assert "mempalace status" in err

    def test_daemon_error_exits_2_with_stderr(self, capsys):
        """Network failures raise DaemonError; same stderr/exit-code contract."""
        from mempalace import cli

        with patch(
            "mempalace.cli._call_daemon_rest",
            side_effect=cli.DaemonError("connection refused"),
        ):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_list(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "palace daemon unreachable" in err
        assert "connection refused" in err

    def test_daemon_error_json_emits_structured_error(self, capsys):
        """With --format=json, the failure surfaces as a JSON document on
        stdout (not a bare stderr line) — machine callers need a parseable
        shape."""
        from mempalace import cli

        with patch(
            "mempalace.cli._call_daemon_rest",
            side_effect=cli.DaemonError("timeout"),
        ):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_list(_make_args(format="json"))

        assert ex.value.code == 2
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "error" in payload
        assert "timeout" in payload["error"]

    def test_inner_error_payload_exits_2(self, capsys):
        """When the daemon returns a JSON ``error`` payload (e.g. palace
        unreachable from inside the daemon), surface it as exit 2 — same
        contract as cmd_status."""
        from mempalace import cli

        err_payload = {"error": "palace_unavailable", "drawers": []}
        with patch("mempalace.cli._call_daemon_rest", return_value=err_payload):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_list(_make_args())

        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "palace_unavailable" in err
