"""
test_cli_bulk_move.py — ``mempalace bulk-move`` multi-drawer relocation (#191).

The ``bulk-move`` subcommand is the multi-drawer complement to ``move``. It
selects drawers by source wing/room via the daemon's ``GET /list`` route
(offset-paginated) and PATCHes each match to a target wing/room. Like
``move``, it relocates metadata only — there is no ``--content`` flag, per
the fork's verbatim-always principle.

The safety model is the point of most of these tests:
  * a source filter (--wing/--room) is required → exit 2 if absent;
  * a target (--to-wing/--to-room) is required → exit 2 if absent;
  * dry-run is the DEFAULT — no PATCH is sent without --apply;
  * --apply refuses to run unattended (non-TTY / json) without --yes;
  * one PATCH failing does not abort the batch; exit 2 if any failed.

We mock ``_call_daemon_rest`` (selection) and ``_patch_daemon_rest``
(mutation) directly — the same style as ``test_cli_list.py`` /
``test_cli_move.py`` — so nothing touches a real daemon.
"""

import argparse
import json
from unittest.mock import patch

import pytest


def _args(**overrides):
    """Build a Namespace with the defaults the parser would produce."""
    base = {
        "wing": None,
        "room": None,
        "to_wing": None,
        "to_room": None,
        "apply": False,
        "yes": False,
        "format": None,
        "json": False,
        "quiet": False,
        "palace": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085"}


def _drawer(i: int, wing: str = "old", room: str = "inbox") -> dict:
    return {
        "drawer_id": f"drw-{i:03d}",
        "wing": wing,
        "room": room,
        "tags": [],
        "content_preview": f"verbatim content {i}",
    }


def _list_responder(drawers, total=None, page=1000):
    """Return a fake ``_call_daemon_rest`` that paginates ``drawers``.

    Honours the ``offset``/``limit`` params so a multi-page total exercises
    the pagination loop in ``_gather_bulk_move_matches``.
    """
    total = len(drawers) if total is None else total
    calls: list = []

    def fake_rest(path, params=None):
        params = params or {}
        calls.append((path, dict(params)))
        offset = int(params.get("offset", 0) or 0)
        limit = int(params.get("limit", page) or page)
        window = drawers[offset : offset + limit]
        return {
            "drawers": window,
            "total": total,
            "count": len(window),
            "offset": offset,
            "limit": limit,
        }

    fake_rest.calls = calls
    return fake_rest


def _patch_responder(fail_ids=None, capture=None):
    """Return a fake ``_patch_daemon_rest``.

    Any id in ``fail_ids`` returns ``success=False``; everything else
    succeeds. ``capture`` (if given) collects ``(drawer_id, body)`` tuples.
    """
    fail_ids = set(fail_ids or [])

    def fake_patch(path, body):
        did = path.rsplit("/", 1)[-1]
        if capture is not None:
            capture.append((did, dict(body)))
        if did in fail_ids:
            return {"success": False, "error": f"could not move {did}"}
        return {"success": True, "drawer_id": did, **body}

    return fake_patch


# ── selection / target validation (no daemon hit) ─────────────────────


class TestBulkMoveValidation:
    def test_missing_source_filter_exits_2_no_patch(self, capsys):
        from mempalace import cli

        patch_mock = patch("mempalace.cli._patch_daemon_rest")
        with patch.dict("os.environ", _env(), clear=True):
            with patch_mock as pm:
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_bulk_move(_args(to_wing="new"))
        assert ex.value.code == 2
        pm.assert_not_called()
        err = capsys.readouterr().err
        assert "source filter" in err

    def test_missing_target_exits_2_no_patch(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._patch_daemon_rest") as pm:
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_bulk_move(_args(wing="old"))
        assert ex.value.code == 2
        pm.assert_not_called()
        err = capsys.readouterr().err
        assert "nothing to change" in err

    def test_no_daemon_url_exits_2(self, capsys):
        from mempalace import cli
        from mempalace.config import MempalaceConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "mempalace.cli.MempalaceConfig",
                lambda: MempalaceConfig(config_dir="/tmp/empty-mempalace-config-bulk"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_bulk_move(_args(wing="old", to_wing="new"))
        assert ex.value.code == 2
        err = capsys.readouterr().err
        assert "PALACE_DAEMON_URL" in err


# ── dry-run is the default ─────────────────────────────────────────────


class TestBulkMoveDryRun:
    def test_dry_run_default_makes_no_patch_calls(self, capsys):
        from mempalace import cli

        drawers = [_drawer(i) for i in range(3)]
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=_list_responder(drawers)):
                with patch("mempalace.cli._patch_daemon_rest") as pm:
                    cli.cmd_bulk_move(_args(wing="old", to_wing="new"))
        pm.assert_not_called()
        out = capsys.readouterr().out
        assert "Matched 3 drawer(s)" in out
        assert "DRY RUN" in out
        assert "--apply" in out

    def test_dry_run_preview_shows_current_to_target(self, capsys):
        from mempalace import cli

        drawers = [_drawer(0, wing="old", room="inbox")]
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=_list_responder(drawers)):
                with patch("mempalace.cli._patch_daemon_rest"):
                    cli.cmd_bulk_move(_args(wing="old", to_wing="new", to_room="archive"))
        out = capsys.readouterr().out
        assert "old/inbox → new/archive" in out


# ── --apply happy path ─────────────────────────────────────────────────


class TestBulkMoveApply:
    def test_apply_yes_moves_all_one_patch_each(self, capsys):
        from mempalace import cli

        drawers = [_drawer(i) for i in range(3)]
        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=_list_responder(drawers)):
                with patch(
                    "mempalace.cli._patch_daemon_rest",
                    side_effect=_patch_responder(capture=captured),
                ):
                    cli.cmd_bulk_move(_args(wing="old", to_wing="new", apply=True, yes=True))
        # one PATCH per drawer, body carries only the supplied target key
        assert [did for did, _ in captured] == ["drw-000", "drw-001", "drw-002"]
        assert all(body == {"wing": "new"} for _, body in captured)
        out = capsys.readouterr().out
        assert "moved 3, failed 0" in out

    def test_apply_only_sends_supplied_target_keys(self):
        from mempalace import cli

        drawers = [_drawer(0)]
        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=_list_responder(drawers)):
                with patch(
                    "mempalace.cli._patch_daemon_rest",
                    side_effect=_patch_responder(capture=captured),
                ):
                    cli.cmd_bulk_move(_args(wing="old", to_room="archive", apply=True, yes=True))
        assert captured == [("drw-000", {"room": "archive"})]

    def test_apply_tty_prompt_accept(self, capsys):
        from mempalace import cli

        drawers = [_drawer(0)]
        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch("sys.stdin.isatty", return_value=True):
                with patch("builtins.input", return_value="y"):
                    with patch(
                        "mempalace.cli._call_daemon_rest",
                        side_effect=_list_responder(drawers),
                    ):
                        with patch(
                            "mempalace.cli._patch_daemon_rest",
                            side_effect=_patch_responder(capture=captured),
                        ):
                            cli.cmd_bulk_move(_args(wing="old", to_wing="new", apply=True))
        assert [did for did, _ in captured] == ["drw-000"]
        assert "moved 1, failed 0" in capsys.readouterr().out

    def test_apply_tty_prompt_decline_no_patch(self, capsys):
        from mempalace import cli

        drawers = [_drawer(0)]
        with patch.dict("os.environ", _env(), clear=True):
            with patch("sys.stdin.isatty", return_value=True):
                with patch("builtins.input", return_value="n"):
                    with patch(
                        "mempalace.cli._call_daemon_rest",
                        side_effect=_list_responder(drawers),
                    ):
                        with patch("mempalace.cli._patch_daemon_rest") as pm:
                            cli.cmd_bulk_move(_args(wing="old", to_wing="new", apply=True))
        pm.assert_not_called()
        assert "aborted" in capsys.readouterr().out

    def test_apply_non_tty_without_yes_refuses_exit_2(self, capsys):
        from mempalace import cli

        drawers = [_drawer(0)]
        with patch.dict("os.environ", _env(), clear=True):
            with patch("sys.stdin.isatty", return_value=False):
                with patch(
                    "mempalace.cli._call_daemon_rest",
                    side_effect=_list_responder(drawers),
                ):
                    with patch("mempalace.cli._patch_daemon_rest") as pm:
                        with pytest.raises(SystemExit) as ex:
                            cli.cmd_bulk_move(_args(wing="old", to_wing="new", apply=True))
        assert ex.value.code == 2
        pm.assert_not_called()
        err = capsys.readouterr().err
        assert "refusing to bulk-move" in err
        assert "--yes" in err


# ── partial failure ────────────────────────────────────────────────────


class TestBulkMovePartialFailure:
    def test_one_failure_exits_2_others_still_moved(self, capsys):
        from mempalace import cli

        drawers = [_drawer(i) for i in range(3)]
        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=_list_responder(drawers)):
                with patch(
                    "mempalace.cli._patch_daemon_rest",
                    side_effect=_patch_responder(fail_ids={"drw-001"}, capture=captured),
                ):
                    with pytest.raises(SystemExit) as ex:
                        cli.cmd_bulk_move(_args(wing="old", to_wing="new", apply=True, yes=True))
        assert ex.value.code == 2
        # all three were attempted — batch did not abort on the failure
        assert [did for did, _ in captured] == ["drw-000", "drw-001", "drw-002"]
        out = capsys.readouterr().out
        assert "moved 2, failed 1" in out
        assert "drw-001" in out

    def test_daemon_error_during_patch_counts_as_failure(self, capsys):
        from mempalace import cli

        drawers = [_drawer(0), _drawer(1)]

        def boom_patch(path, body):
            if path.endswith("drw-000"):
                raise cli.DaemonError("connection reset")
            return {"success": True}

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=_list_responder(drawers)):
                with patch("mempalace.cli._patch_daemon_rest", side_effect=boom_patch):
                    with pytest.raises(SystemExit) as ex:
                        cli.cmd_bulk_move(_args(wing="old", to_wing="new", apply=True, yes=True))
        assert ex.value.code == 2
        out = capsys.readouterr().out
        assert "moved 1, failed 1" in out


# ── pagination ─────────────────────────────────────────────────────────


class TestBulkMovePagination:
    def test_total_exceeds_one_page_gathers_all(self, capsys):
        from mempalace import cli

        # 5 drawers but a page size of 2 → 3 /list calls, all ids gathered.
        drawers = [_drawer(i) for i in range(5)]
        responder = _list_responder(drawers, total=5, page=2)
        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._LIST_LIMIT_MAX", 2):
                with patch("mempalace.cli._call_daemon_rest", side_effect=responder):
                    with patch(
                        "mempalace.cli._patch_daemon_rest",
                        side_effect=_patch_responder(capture=captured),
                    ):
                        cli.cmd_bulk_move(_args(wing="old", to_wing="new", apply=True, yes=True))
        # paginated across 3 calls, every id moved exactly once
        assert len(responder.calls) == 3
        assert [c[1]["offset"] for c in responder.calls] == [0, 2, 4]
        assert [did for did, _ in captured] == [f"drw-{i:03d}" for i in range(5)]
        assert "moved 5, failed 0" in capsys.readouterr().out


# ── daemon unreachable during listing ──────────────────────────────────


class TestBulkMoveListFailure:
    def test_daemon_error_during_list_exits_2(self, capsys):
        from mempalace import cli

        def boom(path, params=None):
            raise cli.DaemonError("daemon down")

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=boom):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_bulk_move(_args(wing="old", to_wing="new"))
        assert ex.value.code == 2
        assert "daemon" in capsys.readouterr().err.lower()

    def test_list_none_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", return_value=None):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_bulk_move(_args(wing="old", to_wing="new"))
        assert ex.value.code == 2

    def test_inner_error_envelope_during_list_exits_2(self, capsys):
        from mempalace import cli

        envelope = {"error": "palace unreachable from daemon"}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", return_value=envelope):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_bulk_move(_args(wing="old", to_wing="new"))
        assert ex.value.code == 2


# ── json output shape ──────────────────────────────────────────────────


class TestBulkMoveJson:
    def test_json_dry_run_shape(self, capsys):
        from mempalace import cli

        drawers = [_drawer(i) for i in range(2)]
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=_list_responder(drawers)):
                with patch("mempalace.cli._patch_daemon_rest") as pm:
                    cli.cmd_bulk_move(_args(wing="old", to_wing="new", format="json"))
        pm.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["matched"] == 2
        assert payload["dry_run"] is True
        assert payload["moved"] == []
        assert payload["failed"] == []
        assert payload["target"] == {"wing": "new"}

    def test_json_apply_shape_with_failure(self, capsys):
        from mempalace import cli

        drawers = [_drawer(0), _drawer(1)]
        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", side_effect=_list_responder(drawers)):
                with patch(
                    "mempalace.cli._patch_daemon_rest",
                    side_effect=_patch_responder(fail_ids={"drw-001"}),
                ):
                    with pytest.raises(SystemExit) as ex:
                        cli.cmd_bulk_move(
                            _args(wing="old", to_wing="new", apply=True, yes=True, json=True)
                        )
        assert ex.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["moved"] == ["drw-000"]
        assert payload["failed"][0]["id"] == "drw-001"
        assert payload["dry_run"] is False

    def test_json_apply_without_yes_non_tty_refuses(self, capsys):
        from mempalace import cli

        drawers = [_drawer(0)]
        with patch.dict("os.environ", _env(), clear=True):
            # json mode is treated as non-interactive regardless of TTY
            with patch("sys.stdin.isatty", return_value=True):
                with patch(
                    "mempalace.cli._call_daemon_rest",
                    side_effect=_list_responder(drawers),
                ):
                    with patch("mempalace.cli._patch_daemon_rest") as pm:
                        with pytest.raises(SystemExit) as ex:
                            cli.cmd_bulk_move(
                                _args(wing="old", to_wing="new", apply=True, json=True)
                            )
        assert ex.value.code == 2
        pm.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "confirmation_required"


# ── argparse wiring ────────────────────────────────────────────────────


class TestParserAcceptsBulkMove:
    def _parse(self, argv):
        from mempalace import cli

        with patch("sys.argv", argv):
            with patch.object(cli, "cmd_bulk_move") as mock:
                mock.side_effect = SystemExit(0)
                with pytest.raises(SystemExit):
                    cli.main()
                return mock.call_args.args[0] if mock.call_args else None

    def test_bulk_move_subcommand_parses(self):
        ns = self._parse(["mempalace", "bulk-move", "--wing", "old", "--to-wing", "new"])
        assert ns is not None
        assert ns.command == "bulk-move"
        assert ns.wing == "old"
        assert ns.to_wing == "new"
        assert ns.apply is False

    def test_apply_and_yes_flags_parse(self):
        ns = self._parse(
            ["mempalace", "bulk-move", "--room", "inbox", "--to-room", "archive", "--apply", "-y"]
        )
        assert ns is not None
        assert ns.apply is True
        assert ns.yes is True
        assert ns.to_room == "archive"

    def test_no_content_flag_exists(self):
        """Verbatim-always: --content must be rejected by the parser."""
        from mempalace import cli

        argv = ["mempalace", "bulk-move", "--wing", "old", "--content", "rewritten"]
        with patch("sys.argv", argv):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2

    def test_format_rejects_unknown(self):
        from mempalace import cli

        argv = ["mempalace", "bulk-move", "--wing", "old", "--to-wing", "n", "--format", "csv"]
        with patch("sys.argv", argv):
            with pytest.raises(SystemExit) as ex:
                cli.main()
        assert ex.value.code == 2
