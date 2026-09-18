"""
test_cli_move.py — ``mempalace move`` single-drawer relocation (#191).

The ``move`` subcommand wraps the daemon's ``PATCH /memory/{drawer_id}``
route, relocating one drawer to a different wing/room. It deliberately
exposes only ``--wing`` / ``--room`` (no ``--content``): the fork's
verbatim-always principle forbids the human CLI from editing stored
drawer text. Tests mock ``urllib.request.urlopen`` so flag propagation,
output formats, and failure modes are exercised without touching a real
daemon.

Mirrors ``test_cli_stats.py`` / ``test_cli_list.py`` patterns: per-test
``_args`` factory, ``_FakeResp`` for the urllib mock, parser-level tests
under ``TestParserAcceptsMove`` to guard the argparse wiring.
"""

import argparse
import json
import urllib.error
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


def _make_rest_responder(patch_payload: dict, capture: list | None = None):
    """Return a fake urlopen serving the PATCH /memory/{id} route.

    When ``capture`` is supplied, the parsed request body of each PATCH is
    appended to it so tests can assert which keys the CLI sent (and that no
    PATCH fires when it shouldn't).
    """

    def fake_urlopen(req, timeout=None):
        if getattr(req, "method", None) == "PATCH" or getattr(req, "data", None) is not None:
            if capture is not None:
                capture.append(json.loads(req.data.decode()))
            return _FakeResp(json.dumps(patch_payload).encode())
        return _FakeResp(b"{}")

    return fake_urlopen


_SUCCESS = {
    "success": True,
    "drawer_id": "drw-123",
    "wing": "projects",
    "room": "decisions",
    "tags": [],
    "warnings": [],
    "sanitize_flags": [],
}


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085"}


def _args(**overrides):
    defaults = {
        "drawer_id": "drw-123",
        "wing": None,
        "room": None,
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestMoveFlagPropagation:
    """Verify --wing / --room flow into the PATCH body, individually and together."""

    def test_wing_only_sends_only_wing(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_rest_responder(_SUCCESS, capture=captured),
            ):
                cli.cmd_move(_args(wing="projects"))

        assert len(captured) == 1
        assert captured[0] == {"wing": "projects"}

    def test_room_only_sends_only_room(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_rest_responder(_SUCCESS, capture=captured),
            ):
                cli.cmd_move(_args(room="decisions"))

        assert len(captured) == 1
        assert captured[0] == {"room": "decisions"}

    def test_both_flags_send_both_keys(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_rest_responder(_SUCCESS, capture=captured),
            ):
                cli.cmd_move(_args(wing="projects", room="decisions"))

        assert len(captured) == 1
        assert captured[0] == {"wing": "projects", "room": "decisions"}

    def test_patch_targets_drawer_id_path(self):
        """The drawer_id positional lands in the URL path, not the body."""
        from mempalace import cli

        seen_urls: list = []

        def responder(req, timeout=None):
            seen_urls.append(req.full_url)
            return _FakeResp(json.dumps(_SUCCESS).encode())

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=responder):
                cli.cmd_move(_args(drawer_id="drw-xyz", wing="projects"))

        assert seen_urls and seen_urls[0].endswith("/memory/drw-xyz")


class TestMoveOutput:
    """Default table confirmation and --json pass-through."""

    def test_table_shows_new_wing_and_room(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_SUCCESS)):
                cli.cmd_move(_args(wing="projects", room="decisions"))

        out = capsys.readouterr().out
        assert "Moved drawer drw-123" in out
        assert "wing → projects" in out
        assert "room → decisions" in out

    def test_table_marks_unchanged_field(self, capsys):
        """When only --wing is given, room renders as (unchanged)."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_SUCCESS)):
                cli.cmd_move(_args(wing="projects"))

        out = capsys.readouterr().out
        assert "wing → projects" in out
        assert "(unchanged)" in out

    def test_table_surfaces_warnings(self, capsys):
        from mempalace import cli

        payload = dict(_SUCCESS, warnings=["room 'foo' is not canonical"])
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(payload)):
                cli.cmd_move(_args(room="foo"))

        out = capsys.readouterr().out
        assert "warning: room 'foo' is not canonical" in out

    def test_json_passes_through_envelope(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_SUCCESS)):
                cli.cmd_move(_args(wing="projects", format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is True
        assert payload["drawer_id"] == "drw-123"
        assert payload["wing"] == "projects"

    def test_json_shorthand_via_legacy_flag(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(_SUCCESS)):
                cli.cmd_move(_args(wing="projects", json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["wing"] == "projects"


class TestMoveMissingFlags:
    """No --wing and no --room → clean error, and no PATCH is ever sent."""

    def test_missing_both_exits_2_no_patch(self, capsys):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_rest_responder(_SUCCESS, capture=captured),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_move(_args())

        assert ex.value.code == 2
        assert captured == []  # nothing sent to the daemon
        err = capsys.readouterr().err
        assert "at least one of --wing / --room" in err

    def test_missing_both_json_emits_structured_error(self, capsys):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_rest_responder(_SUCCESS, capture=captured),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_move(_args(json=True))

        assert ex.value.code == 2
        assert captured == []
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "no_change"


class TestMoveDaemonDown:
    """Failure modes — match cmd_list/cmd_graph/cmd_cypher/cmd_stats exit codes."""

    def test_no_daemon_url_exits_2(self, capsys):
        from mempalace import cli
        from mempalace.config import MempalaceConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "mempalace.cli.MempalaceConfig",
                lambda: MempalaceConfig(config_dir="/tmp/empty-mempalace-config-move"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_move(_args(wing="projects"))
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
                    cli.cmd_move(_args(wing="projects"))
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "daemon" in err.lower()

    def test_404_exits_2(self, capsys):
        """An older daemon without the PATCH route returns 404 — exit 1."""
        from mempalace import cli

        def four_oh_four(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=four_oh_four):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_move(_args(wing="projects"))
        assert ex.value.code == 2

    def test_401_exits_2(self, capsys):
        from mempalace import cli

        def unauthorized(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=None)

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=unauthorized):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_move(_args(wing="projects"))
        assert ex.value.code == 2

    def test_403_exits_2(self, capsys):
        from mempalace import cli

        def forbidden(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=None)

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=forbidden):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_move(_args(wing="projects"))
        assert ex.value.code == 2

    def test_inner_error_envelope_exits_2(self, capsys):
        """Daemon returns 200 with ``success=False`` — drawer not found or
        inner validation failure. Daemon reachable, move failed → exit 2."""
        from mempalace import cli

        envelope = {"success": False, "error": "Drawer not found: drw-123"}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_rest_responder(envelope)):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_move(_args(wing="projects"))
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "Drawer not found" in err


class TestParserAcceptsMove:
    """Sanity checks for the argparse wiring."""

    def _parse(self, argv):
        from mempalace import cli

        with patch("sys.argv", argv):
            with patch.object(cli, "cmd_move") as mock:
                mock.side_effect = SystemExit(0)
                with pytest.raises(SystemExit):
                    cli.main()
                return mock.call_args.args[0] if mock.call_args else None

    def test_move_subcommand_parses(self):
        ns = self._parse(["mempalace", "move", "drw-123", "--wing", "projects"])
        assert ns is not None
        assert ns.command == "move"
        assert ns.drawer_id == "drw-123"
        assert ns.wing == "projects"

    def test_room_flag_parses(self):
        ns = self._parse(["mempalace", "move", "drw-123", "--room", "decisions"])
        assert ns is not None
        assert ns.room == "decisions"

    def test_drawer_id_is_required(self):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "move", "--wing", "projects"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2

    def test_format_flag_overrides_default(self):
        ns = self._parse(["mempalace", "move", "drw-123", "--wing", "x", "--format", "json"])
        assert ns is not None
        assert ns.format == "json"

    def test_post_subcommand_json_flag(self):
        ns = self._parse(["mempalace", "move", "drw-123", "--wing", "x", "--json"])
        assert ns is not None
        assert ns.json is True

    def test_format_rejects_unknown(self):
        from mempalace import cli

        argv = ["mempalace", "move", "drw-123", "--wing", "x", "--format", "csv"]
        with patch("sys.argv", argv):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2

    def test_no_content_flag_exists(self):
        """Verbatim-always: --content must be rejected by the parser."""
        from mempalace import cli

        argv = ["mempalace", "move", "drw-123", "--content", "rewritten"]
        with patch("sys.argv", argv):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2
