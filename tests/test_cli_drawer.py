"""
test_cli_drawer.py — ``mempalace drawer`` single-drawer CRUD (slice of #191, #355).

The ``drawer`` subcommand binds four MCP tools (``mempalace_get_drawer`` /
``add_drawer`` / ``delete_drawer`` / ``update_drawer``) to the shell. Both
routes are covered: daemon-strict goes through the ``/mcp`` JSON-RPC
endpoint, and the local path calls the handler on ``mempalace.mcp_server``
directly.

Two invariants get their own tests because they encode fork principles
rather than tool behaviour:

  * ``drawer update`` cannot mutate content — there is no ``--content``
    flag, same as ``move`` (verbatim-always).
  * ``drawer delete`` refuses to run unattended without ``--confirm``.

Mirrors ``test_cli_tunnels.py`` / ``test_cli_move.py`` patterns.
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


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085", "PALACE_DAEMON_STRICT": "1"}


def _args(action, **overrides):
    defaults = {
        "drawer_action": action,
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "drawer_id": "drawer_test_decisions_abc123",
        "wing": None,
        "room": None,
        "content": None,
        "content_file": None,
        "source": None,
        "added_by": "cli",
        "tag": None,
        "no_tags": False,
        "clear_tags": False,
        "confirm": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


_DRAWER = {
    "drawer_id": "drawer_test_decisions_abc123",
    "content": "We chose pgvector over ChromaDB on 2026-05-14.",
    "wing": "memorypalace",
    "room": "decisions",
    "tags": ["pgvector", "cutover"],
    "metadata": {
        "wing": "memorypalace",
        "room": "decisions",
        "filed_at": "2026-05-14T09:12:00",
        "added_by": "cli",
        "source_file": "notes.md",
    },
}


class TestDrawerGet:
    """``drawer get`` renders metadata + verbatim body, or the body alone."""

    def test_table_output_shows_metadata_and_content(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_DRAWER)):
                cli.cmd_drawer(_args("get"))

        out = capsys.readouterr().out
        assert "DRAWER drawer_test_decisions_abc123" in out
        assert "memorypalace" in out
        assert "decisions" in out
        assert "pgvector, cutover" in out
        assert "2026-05-14T09:12:00" in out
        assert "We chose pgvector over ChromaDB on 2026-05-14." in out

    def test_content_format_emits_body_only(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_DRAWER)):
                cli.cmd_drawer(_args("get", format="content"))

        out = capsys.readouterr().out
        assert out == "We chose pgvector over ChromaDB on 2026-05-14."

    def test_json_is_tool_passthrough(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_DRAWER)):
                cli.cmd_drawer(_args("get", format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload == _DRAWER

    def test_chunked_drawer_reports_chunk_count(self, capsys):
        from mempalace import cli

        chunked = dict(_DRAWER, chunks=4)
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(chunked)):
                cli.cmd_drawer(_args("get"))

        assert "chunks  4" in capsys.readouterr().out

    def test_not_found_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"error": "Drawer not found: nope"}),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_drawer(_args("get", drawer_id="nope"))

        assert ex.value.code == 2
        assert "Drawer not found: nope" in capsys.readouterr().err

    def test_drawer_id_propagates_to_daemon(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_DRAWER, captured=captured),
            ):
                cli.cmd_drawer(_args("get", drawer_id="drawer_x"))

        assert captured[0]["params"]["name"] == "mempalace_get_drawer"
        assert captured[0]["params"]["arguments"] == {"drawer_id": "drawer_x"}


class TestDrawerAdd:
    """``drawer add`` files verbatim content; tags default to auto-extraction."""

    _ADDED = {
        "success": True,
        "drawer_id": "drawer_memorypalace_decisions_deadbeef",
        "wing": "memorypalace",
        "room": "decisions",
        "tags": ["pgvector"],
        "warnings": [],
        "sanitize_flags": [],
        "chunks": 1,
    }

    def test_files_content_and_reports_id(self, capsys):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._ADDED, captured=captured),
            ):
                cli.cmd_drawer(
                    _args(
                        "add",
                        wing="memorypalace",
                        room="decisions",
                        content="pgvector cutover notes",
                    )
                )

        sent = captured[0]["params"]["arguments"]
        assert captured[0]["params"]["name"] == "mempalace_add_drawer"
        assert sent["wing"] == "memorypalace"
        assert sent["room"] == "decisions"
        assert sent["content"] == "pgvector cutover notes"
        assert sent["added_by"] == "cli"
        # No tags key: the palace auto-extracts when the CLI stays silent.
        assert "tags" not in sent
        out = capsys.readouterr().out
        assert "Filed drawer drawer_memorypalace_decisions_deadbeef" in out
        assert "pgvector" in out

    def test_content_file_is_read_byte_exact(self, capsys, tmp_path):
        from mempalace import cli

        body = "line one\r\nline two\n"
        source = tmp_path / "verbatim.txt"
        source.write_bytes(body.encode())

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._ADDED, captured=captured),
            ):
                cli.cmd_drawer(
                    _args(
                        "add",
                        wing="memorypalace",
                        room="decisions",
                        content_file=str(source),
                    )
                )

        assert captured[0]["params"]["arguments"]["content"] == body
        capsys.readouterr()

    def test_tags_propagate(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._ADDED, captured=captured),
            ):
                cli.cmd_drawer(
                    _args(
                        "add",
                        wing="w",
                        room="decisions",
                        content="x",
                        tag=["shipped", "cli"],
                    )
                )

        assert captured[0]["params"]["arguments"]["tags"] == ["shipped", "cli"]

    def test_no_tags_sends_empty_list(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._ADDED, captured=captured),
            ):
                cli.cmd_drawer(_args("add", wing="w", room="decisions", content="x", no_tags=True))

        assert captured[0]["params"]["arguments"]["tags"] == []

    def test_tag_and_no_tags_conflict_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_drawer(
                    _args(
                        "add",
                        wing="w",
                        room="decisions",
                        content="x",
                        tag=["a"],
                        no_tags=True,
                    )
                )

        assert ex.value.code == 2
        assert "--no-tags" in capsys.readouterr().err

    def test_missing_content_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_drawer(_args("add", wing="w", room="decisions"))

        assert ex.value.code == 2
        assert "--content" in capsys.readouterr().err

    def test_content_and_content_file_conflict_exits_2(self, capsys, tmp_path):
        from mempalace import cli

        source = tmp_path / "x.txt"
        source.write_text("x")
        with patch.dict("os.environ", _env(), clear=True):
            with pytest.raises(SystemExit) as ex:
                cli.cmd_drawer(
                    _args(
                        "add",
                        wing="w",
                        room="decisions",
                        content="inline",
                        content_file=str(source),
                    )
                )

        assert ex.value.code == 2
        assert "not both" in capsys.readouterr().err

    def test_already_exists_is_reported_not_an_error(self, capsys):
        from mempalace import cli

        payload = {
            "success": True,
            "reason": "already_exists",
            "drawer_id": "drawer_w_decisions_cafe",
            "tags": [],
            "warnings": [],
            "sanitize_flags": [],
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(payload)):
                cli.cmd_drawer(_args("add", wing="w", room="decisions", content="x"))

        out = capsys.readouterr().out
        assert "already filed" in out
        assert "nothing was written" in out

    def test_tool_failure_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"success": False, "error": "wing is empty"}),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_drawer(_args("add", wing="", room="decisions", content="x"))

        assert ex.value.code == 2
        assert "wing is empty" in capsys.readouterr().err

    def test_warnings_and_sanitize_flags_surface(self, capsys):
        from mempalace import cli

        payload = dict(
            self._ADDED,
            warnings=["room 'notes' is not canonical"],
            sanitize_flags=["control_chars_stripped"],
        )
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(payload)):
                cli.cmd_drawer(_args("add", wing="w", room="notes", content="x"))

        out = capsys.readouterr().out
        assert "warning: room 'notes' is not canonical" in out
        assert "sanitized: control_chars_stripped" in out


class TestDrawerDelete:
    """``drawer delete`` is gated — irreversible, so never unattended."""

    _DELETED = {
        "success": True,
        "drawer_id": "drawer_test_decisions_abc123",
        "deleted_ids": ["drawer_test_decisions_abc123"],
        "chunks_deleted": 1,
    }

    def test_confirm_flag_deletes(self, capsys):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._DELETED, captured=captured),
            ):
                cli.cmd_drawer(_args("delete", confirm=True))

        assert captured[0]["params"]["name"] == "mempalace_delete_drawer"
        out = capsys.readouterr().out
        assert "Deleted drawer drawer_test_decisions_abc123" in out
        assert "rows    1" in out

    def test_non_interactive_without_confirm_refuses(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("sys.stdin.isatty", return_value=False):
                with patch("urllib.request.urlopen") as urlopen:
                    with pytest.raises(SystemExit) as ex:
                        cli.cmd_drawer(_args("delete"))

        assert ex.value.code == 2
        urlopen.assert_not_called()
        assert "without --confirm" in capsys.readouterr().err

    def test_json_without_confirm_refuses_with_structured_error(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen") as urlopen:
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_drawer(_args("delete", format="json"))

        assert ex.value.code == 2
        urlopen.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "confirmation_required"

    def test_interactive_yes_proceeds(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("sys.stdin.isatty", return_value=True):
                with patch("builtins.input", return_value="y"):
                    with patch(
                        "urllib.request.urlopen",
                        side_effect=_make_responder(self._DELETED),
                    ):
                        cli.cmd_drawer(_args("delete"))

        assert "Deleted drawer" in capsys.readouterr().out

    def test_interactive_decline_aborts_without_calling_daemon(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("sys.stdin.isatty", return_value=True):
                with patch("builtins.input", return_value="n"):
                    with patch("urllib.request.urlopen") as urlopen:
                        cli.cmd_drawer(_args("delete"))

        urlopen.assert_not_called()
        assert "Aborted" in capsys.readouterr().out

    def test_missing_drawer_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"success": False, "error": "Drawer not found: x"}),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_drawer(_args("delete", drawer_id="x", confirm=True))

        assert ex.value.code == 2
        assert "Drawer not found: x" in capsys.readouterr().err


class TestDrawerUpdate:
    """``drawer update`` moves metadata only — content is never rewritten."""

    _UPDATED = {
        "success": True,
        "drawer_id": "drawer_test_decisions_abc123",
        "wing": "memorypalace",
        "room": "architecture",
        "tags": ["shipped"],
        "warnings": [],
        "sanitize_flags": [],
    }

    def test_tag_only_update(self, capsys):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._UPDATED, captured=captured),
            ):
                cli.cmd_drawer(_args("update", tag=["shipped"]))

        sent = captured[0]["params"]["arguments"]
        assert captured[0]["params"]["name"] == "mempalace_update_drawer"
        assert sent == {"drawer_id": "drawer_test_decisions_abc123", "tags": ["shipped"]}
        out = capsys.readouterr().out
        assert "Updated drawer" in out
        assert "tags    → shipped" in out
        # wing/room were not requested, so they must read as unchanged.
        assert "(unchanged)" in out

    def test_wing_and_room_update(self, capsys):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._UPDATED, captured=captured),
            ):
                cli.cmd_drawer(_args("update", wing="memorypalace", room="architecture"))

        sent = captured[0]["params"]["arguments"]
        assert sent["wing"] == "memorypalace"
        assert sent["room"] == "architecture"
        out = capsys.readouterr().out
        assert "wing    → memorypalace" in out
        assert "room    → architecture" in out

    def test_clear_tags_sends_empty_list(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(dict(self._UPDATED, tags=[]), captured=captured),
            ):
                cli.cmd_drawer(_args("update", clear_tags=True))

        assert captured[0]["params"]["arguments"]["tags"] == []

    def test_no_fields_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen") as urlopen:
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_drawer(_args("update"))

        assert ex.value.code == 2
        urlopen.assert_not_called()
        assert "at least one of --wing / --room / --tag" in capsys.readouterr().err

    def test_noop_response_reported(self, capsys):
        from mempalace import cli

        payload = {"success": True, "drawer_id": "d1", "noop": True}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(payload)):
                cli.cmd_drawer(_args("update", tag=["x"]))

        assert "unchanged — nothing to update" in capsys.readouterr().out

    def test_update_parser_has_no_content_flag(self):
        """Verbatim-always: the human CLI must never rewrite drawer text.

        Guards the same invariant ``cmd_move``'s docstring states — if a
        future refactor adds ``--content`` to ``drawer update``, this fails.
        """
        import contextlib
        import io

        from mempalace import cli

        buf = io.StringIO()
        with patch("sys.argv", ["mempalace", "drawer", "update", "--help"]):
            with contextlib.redirect_stdout(buf):
                with pytest.raises(SystemExit):
                    cli.main()

        help_text = buf.getvalue()
        assert "--wing" in help_text
        assert "--tag" in help_text
        assert "--content" not in help_text


class TestDrawerParserWiring:
    """main() registration: nested dispatch + post-subcommand --json."""

    def test_bare_drawer_prints_help(self, capsys):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "drawer"]):
            cli.main()

        out = capsys.readouterr().out
        assert "{get,add,delete,update}" in out

    def test_bare_duplicate_prints_help(self, capsys):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "duplicate"]):
            cli.main()

        assert "check" in capsys.readouterr().out

    def test_post_subcommand_json_flag_parses(self, capsys):
        """Nested leaves are skipped by main()'s propagation loop, so each
        one registers ``--json`` / ``--quiet`` itself (cf. ``logstream``).
        """
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_DRAWER)):
                with patch("sys.argv", ["mempalace", "drawer", "get", "d1", "--json"]):
                    cli.main()

        assert json.loads(capsys.readouterr().out) == _DRAWER

    def test_duplicate_rejects_percentage_threshold(self, capsys):
        from mempalace import cli

        with patch("sys.argv", ["mempalace", "duplicate", "check", "--threshold", "90"]):
            with pytest.raises(SystemExit) as ex:
                cli.main()

        assert ex.value.code == 2  # argparse's own usage-error code
        assert "between 0 and 1" in capsys.readouterr().err


class TestDrawerRouting:
    """Daemon-strict routes to /mcp; --palace runs the handler locally."""

    def test_palace_flag_uses_local_handler(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_get_drawer", return_value=_DRAWER) as handler:
                with patch("urllib.request.urlopen") as urlopen:
                    cli.cmd_drawer(_args("get", palace=str(tmp_path)))

        urlopen.assert_not_called()
        handler.assert_called_once_with(drawer_id="drawer_test_decisions_abc123")
        assert "DRAWER drawer_test_decisions_abc123" in capsys.readouterr().out

    def test_palace_flag_sets_palace_path_env(self, tmp_path):
        import os

        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_get_drawer", return_value=_DRAWER):
                cli.cmd_drawer(_args("get", palace=str(tmp_path)))
                assert os.environ["MEMPALACE_PALACE_PATH"] == str(tmp_path)

    def test_local_handler_used_when_daemon_strict_off(self, tmp_path):
        from mempalace import cli
        from mempalace.config import MempalaceConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "mempalace.cli.MempalaceConfig",
                lambda: MempalaceConfig(config_dir=str(tmp_path / "cfg")),
            ):
                with patch("mempalace.mcp_server.tool_get_drawer", return_value=_DRAWER) as handler:
                    with patch("urllib.request.urlopen") as urlopen:
                        cli.cmd_drawer(_args("get", format="json"))

        urlopen.assert_not_called()
        handler.assert_called_once()

    def test_local_add_forwards_every_argument(self, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_add_drawer",
                return_value={"success": True, "drawer_id": "d", "tags": []},
            ) as handler:
                cli.cmd_drawer(
                    _args(
                        "add",
                        palace=str(tmp_path),
                        wing="w",
                        room="decisions",
                        content="body",
                        source="notes.md",
                        added_by="luna",
                        tag=["a"],
                        format="json",
                    )
                )

        handler.assert_called_once_with(
            wing="w",
            room="decisions",
            content="body",
            added_by="luna",
            source_file="notes.md",
            tags=["a"],
        )

    def test_import_mcp_server_restores_a_hijacked_stdout(self):
        """``mcp_server`` steals stdout at import; the CLI must take it back.

        ``mempalace/mcp_server.py`` runs ``sys.stdout = sys.stderr`` (plus
        an ``os.dup2(2, 1)``) at module scope so chromadb banners cannot
        corrupt the MCP stdio protocol (#225), and only undoes it inside
        its own ``main()`` — which a CLI process never reaches. Without
        ``_import_mcp_server``'s repair, every local-path command would
        print to stderr and a ``--json`` pipeline would read an empty
        document.
        """
        import sys as _sys

        from mempalace import cli

        real_stdout = _sys.stdout
        try:
            _sys.stdout = _sys.stderr
            cli._import_mcp_server()
            assert _sys.stdout is not _sys.stderr
        finally:
            _sys.stdout = real_stdout

    def test_real_stdout_snapshot_matches_the_importers_stream(self):
        """Pin the coincidence `_import_mcp_server`'s docstring relies on.

        The helper prefers the caller's pre-import stream over whatever
        ``_restore_stdout()`` installs, because ``_REAL_STDOUT`` is a
        snapshot of unbounded age. Today the two are the same object,
        since ``_REAL_STDOUT = sys.stdout`` is the first statement in
        mcp_server's module body — above its own imports and above the
        redirect. That equality is a property of statement order in
        *another* module, so if an upstream sync moves the snapshot down
        or adds an import above it, this test fails and the docstring's
        claim gets re-examined rather than quietly going stale.

        Runs in a subprocess because it needs a genuine first import.
        """
        import subprocess
        import sys as _sys

        probe = (
            "import sys\n"
            "saved = sys.stdout\n"
            "from mempalace import mcp_server\n"
            "print(sys.stdout is sys.stderr, mcp_server._REAL_STDOUT is saved, file=sys.stderr)\n"
        )
        result = subprocess.run(
            [_sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        # The import hijacks stdout, and the snapshot is the importer's stream.
        assert "True True" in result.stderr

    def test_local_path_emits_json_on_stdout(self, capsys, tmp_path):
        """End-to-end guard: the local route's JSON must land on stdout."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_get_drawer", return_value=_DRAWER):
                cli.cmd_drawer(_args("get", palace=str(tmp_path), format="json"))

        captured = capsys.readouterr()
        assert json.loads(captured.out) == _DRAWER
        assert captured.err == ""

    def test_local_delete_and_update_reach_their_handlers(self, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_delete_drawer",
                return_value={"success": True, "drawer_id": "d1", "chunks_deleted": 2},
            ) as delete_handler:
                cli.cmd_drawer(_args("delete", palace=str(tmp_path), drawer_id="d1", confirm=True))
            with patch(
                "mempalace.mcp_server.tool_update_drawer",
                return_value={"success": True, "drawer_id": "d1", "wing": "w", "room": "r"},
            ) as update_handler:
                cli.cmd_drawer(_args("update", palace=str(tmp_path), drawer_id="d1", room="r"))

        delete_handler.assert_called_once_with(drawer_id="d1")
        update_handler.assert_called_once_with(drawer_id="d1", room="r")


class TestDrawerFailureModes:
    """Daemon unreachable → exit 1, matching cmd_list / cmd_move / cmd_tunnels."""

    def test_daemon_unreachable_exits_2(self, capsys):
        import urllib.error

        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("connection refused"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_drawer(_args("get"))

        assert ex.value.code == 2
        assert "palace daemon unreachable" in capsys.readouterr().err

    def test_daemon_unreachable_json_shape(self, capsys):
        import urllib.error

        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("connection refused"),
            ):
                with pytest.raises(SystemExit) as ex:
                    cli.cmd_drawer(_args("get", format="json"))

        assert ex.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"] == "daemon"
        assert "error" in payload
