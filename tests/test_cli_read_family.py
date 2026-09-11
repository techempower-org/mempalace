"""
test_cli_read_family.py — ``wings`` / ``hallway`` / ``taxonomy`` / ``aaak``
/ ``checkpoint`` (slices of #191: issues #356, #358, #360, #362).

Each command has two routing paths and both are exercised:

- **daemon** — ``_daemon_strict()`` on and no ``--palace``. Mocked at
  ``urllib.request.urlopen`` so the JSON-RPC envelope shape is part of
  the test (same style as ``tests/test_cli_tunnels.py``), except where a
  test needs to assert on a JSON-RPC *error* envelope.
- **local** — patched ``mempalace.mcp_server`` tool functions, reached by
  passing ``--palace`` (which overrides daemon routing by design).

``checkpoint`` writes, so it is *only* ever tested against mocks — never
a live palace.
"""

import argparse
import json
import os
from unittest.mock import patch

import pytest


# ── harness ───────────────────────────────────────────────────────────


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


def _rpc_error(message: str, code: int = -32001) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}
    ).encode()


def _make_responder(payload, captured: list | None = None, raw: bytes | None = None):
    """urlopen stand-in. Records the POSTed JSON-RPC body when asked."""

    def fake_urlopen(req, timeout=None):
        if captured is not None and getattr(req, "data", None) is not None:
            captured.append(json.loads(req.data.decode()))
        return _FakeResp(raw if raw is not None else _mcp_envelope(payload))

    return fake_urlopen


def _env():
    return {"PALACE_DAEMON_URL": "http://daemon.example:8085"}


def _args(**overrides):
    """Namespace with every attribute the read family reads."""
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "wing": None,
        "room": None,
        "limit": 0,
        "sort": "name",
        "confirm": False,
        "hallway_id": None,
        "hallway_action": None,
        "aaak_action": None,
        "items_file": None,
        "content": None,
        "content_file": None,
        "diary_agent": None,
        "diary_entry": None,
        "diary_entry_file": None,
        "diary_topic": None,
        "diary_wing": None,
        "dedup_threshold": 0.9,
        "added_by": None,
        "dry_run": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


_WINGS = {"memorypalace": 300, "candela": 700, "ha": 0}
_TAXONOMY = {
    "memorypalace": {"decisions": 120, "references": 180},
    "candela": {"sessions": 700},
}
_HALLWAYS = [
    {
        "id": "hw-a",
        "wing": "memorypalace",
        "entity_a": "pgvector",
        "entity_b": "AGE",
        "co_occurrence_count": 12,
        "rooms": ["decisions"],
    },
    {
        "id": "hw-b",
        "wing": "candela",
        "entity_a": "wax",
        "entity_b": "wick",
        "co_occurrence_count": 40,
        "rooms": ["sessions"],
    },
]


# ── #356 mempalace wings ──────────────────────────────────────────────


class TestWingsDaemonPath:
    def test_status_fast_serves_the_counts(self, capsys):
        """The daemon path prefers GET /status/fast — mempalace_list_wings
        does not return in usable time on a large palace."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.cli._call_daemon_rest",
                return_value={"total_drawers": 1000, "wings": _WINGS, "rooms": {}},
            ) as rest:
                with patch("mempalace.cli._call_daemon_tool") as tool:
                    cli.cmd_wings(_args())

        rest.assert_called_once_with("/status/fast")
        tool.assert_not_called()
        out = capsys.readouterr().out
        assert "WINGS — 3" in out
        assert "memorypalace" in out

    def test_falls_back_to_the_mcp_tool_when_rest_is_absent(self, capsys):
        """_call_daemon_rest returns None on 404/401/403 — older daemons."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", return_value=None):
                with patch(
                    "urllib.request.urlopen", side_effect=_make_responder({"wings": _WINGS})
                ):
                    cli.cmd_wings(_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload == {"wings": _WINGS}

    def test_json_shape_is_the_tool_envelope_on_both_paths(self, capsys):
        """--json consumers must see {"wings": {...}} whichever path served."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.cli._call_daemon_rest",
                return_value={"total_drawers": 1000, "wings": _WINGS, "rooms": {"x": 1}},
            ):
                cli.cmd_wings(_args(json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload == {"wings": _WINGS}
        assert "rooms" not in payload
        assert "total_drawers" not in payload

    def test_unreachable_daemon_exits_1(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.cli._call_daemon_rest",
                side_effect=cli.DaemonError("daemon unreachable at http://x: boom"),
            ):
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_wings(_args(json=True))
        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["source"] == "daemon"


class TestWingsLocalPath:
    def test_palace_flag_routes_local(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_list_wings", return_value={"wings": _WINGS}
            ) as fn:
                with patch("mempalace.cli._call_daemon_rest") as rest:
                    cli.cmd_wings(_args(palace=str(tmp_path), format="json"))

        fn.assert_called_once_with()
        rest.assert_not_called()
        assert json.loads(capsys.readouterr().out) == {"wings": _WINGS}

    def test_palace_flag_redirects_the_tool_at_that_palace(self, tmp_path):
        """--palace must point the tool handler at THAT palace.

        Asserts through ``_config.palace_path``, which is a property
        re-reading ``MEMPALACE_PALACE_PATH`` per access — so this passes
        regardless of when ``mcp_server`` was imported or which config
        singleton it happens to hold.
        """
        from mempalace import cli
        from mempalace import mcp_server

        seen = {}

        def _capture():
            seen["path"] = mcp_server._config.palace_path
            return {"wings": {}}

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_list_wings", side_effect=_capture):
                cli.cmd_wings(_args(palace=str(tmp_path), format="json"))

        assert seen["path"] == str(tmp_path)

    def test_palace_override_does_not_leak_to_a_later_command(self, tmp_path):
        """``os.environ`` is process-global and the handlers read it
        lazily, so an un-restored override would silently redirect the
        next command that passed no --palace at all."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_list_wings", return_value={"wings": {}}):
                cli.cmd_wings(_args(palace=str(tmp_path), format="json"))
            assert "MEMPALACE_PALACE_PATH" not in os.environ

    def test_a_preexisting_palace_env_value_is_put_back(self, tmp_path):
        from mempalace import cli

        env = dict(_env())
        env["MEMPALACE_PALACE_PATH"] = "/pre/existing"
        with patch.dict("os.environ", env, clear=True):
            with patch("mempalace.mcp_server.tool_list_wings", return_value={"wings": {}}):
                cli.cmd_wings(_args(palace=str(tmp_path), format="json"))
            assert os.environ["MEMPALACE_PALACE_PATH"] == "/pre/existing"

    def test_override_is_restored_even_when_the_tool_raises(self, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_list_wings", side_effect=RuntimeError("boom")):
                with pytest.raises(RuntimeError):
                    cli.cmd_wings(_args(palace=str(tmp_path)))
            assert "MEMPALACE_PALACE_PATH" not in os.environ

    def test_snapshot_predates_the_import_so_mcp_servers_own_write_is_unwound(self, tmp_path):
        """Importing ``mcp_server`` is itself an env mutation: it runs
        ``parse_known_args()`` at module scope against the importing
        process's ``sys.argv`` and sets ``MEMPALACE_PALACE_PATH`` from any
        ``--palace`` it finds. A snapshot taken *after* the import would
        capture that write and restore it, leaving the override behind —
        which is exactly what a live `mempalace --palace X wings --json`
        run did before the snapshot moved above the import.
        """
        from mempalace import cli

        def _import_that_also_writes_env():
            os.environ["MEMPALACE_PALACE_PATH"] = "/written/by/the/import"
            import mempalace.mcp_server as m

            return m

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.cli._import_mcp_server", side_effect=_import_that_also_writes_env
            ):
                with cli._local_mcp_server(str(tmp_path)):
                    assert os.environ["MEMPALACE_PALACE_PATH"] == str(tmp_path)
            assert "MEMPALACE_PALACE_PATH" not in os.environ


class TestWingsSortAndLimit:
    def test_sort_count_is_descending(self):
        from mempalace.cli import _sorted_wing_rows

        assert _sorted_wing_rows(_WINGS, "count") == [
            ("candela", 700),
            ("memorypalace", 300),
            ("ha", 0),
        ]

    def test_sort_name_is_alphabetical(self):
        from mempalace.cli import _sorted_wing_rows

        assert [n for n, _ in _sorted_wing_rows(_WINGS, "name")] == [
            "candela",
            "ha",
            "memorypalace",
        ]

    def test_count_ties_break_by_name_for_stable_output(self):
        from mempalace.cli import _sorted_wing_rows

        tied = {"zebra": 5, "alpha": 5, "mid": 9}
        assert _sorted_wing_rows(tied, "count") == [("mid", 9), ("alpha", 5), ("zebra", 5)]

    def test_limit_truncates_and_says_so(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.cli._call_daemon_rest", return_value={"wings": _WINGS, "rooms": {}}
            ):
                cli.cmd_wings(_args(sort="count", limit=1))

        out = capsys.readouterr().out
        assert "candela" in out
        assert "2 more" in out

    def test_empty_palace_renders_clean_message(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_rest", return_value={"wings": {}, "rooms": {}}):
                cli.cmd_wings(_args())

        assert "(no wings)" in capsys.readouterr().out


# ── #362 mempalace taxonomy ───────────────────────────────────────────


class TestTaxonomy:
    def test_daemon_path_renders_the_tree(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"taxonomy": _TAXONOMY}),
            ):
                cli.cmd_taxonomy(_args())

        out = capsys.readouterr().out
        assert "TAXONOMY — 2 wing(s)" in out
        assert "decisions" in out
        assert "sessions" in out

    def test_calls_the_taxonomy_tool_with_no_arguments(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"taxonomy": _TAXONOMY}, captured=captured),
            ):
                cli.cmd_taxonomy(_args(format="json"))

        assert captured[0]["params"]["name"] == "mempalace_get_taxonomy"
        assert captured[0]["params"]["arguments"] == {}

    def test_wing_filter_is_applied_client_side(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"taxonomy": _TAXONOMY}),
            ):
                cli.cmd_taxonomy(_args(wing="candela", format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert list(payload["taxonomy"]) == ["candela"]
        assert payload["wing_filter"] == "candela"

    def test_unknown_wing_yields_an_empty_tree_not_a_crash(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"taxonomy": _TAXONOMY}),
            ):
                cli.cmd_taxonomy(_args(wing="nope"))

        assert "(no taxonomy for wing=nope)" in capsys.readouterr().out

    def test_daemon_tool_timeout_exits_2_and_says_the_daemon_answered(self, capsys):
        """A JSON-RPC error is not "unreachable" — the sibling commands'
        single message is wrong for it, so this family splits the two."""
        from mempalace import cli

        raw = _rpc_error("MCP tool 'mempalace_get_taxonomy' exceeded PALACE_MCP_TOOL_TIMEOUT")
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(None, raw=raw)):
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_taxonomy(_args(json=True))

        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["reachable"] is True
        assert payload["tool"] == "mempalace_get_taxonomy"

    def test_local_path_uses_the_mcp_tool(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_get_taxonomy", return_value={"taxonomy": _TAXONOMY}
            ) as fn:
                cli.cmd_taxonomy(_args(palace=str(tmp_path), format="json"))

        fn.assert_called_once_with()
        assert json.loads(capsys.readouterr().out)["taxonomy"] == _TAXONOMY

    def test_error_envelope_exits_2(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"error": "no palace"}),
            ):
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_taxonomy(_args(json=True))
        assert exc.value.code == 2
        assert json.loads(capsys.readouterr().out)["error"] == "no palace"

    def test_partial_result_with_an_error_key_still_prints(self, capsys):
        """tool_get_taxonomy sets error+partial when a facet read fails
        midway; the tree it did collect is still worth showing."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(
                    {"taxonomy": _TAXONOMY, "error": "facet read failed", "partial": True}
                ),
            ):
                cli.cmd_taxonomy(_args())

        assert "TAXONOMY — 2 wing(s)" in capsys.readouterr().out


# ── #362 mempalace aaak spec ──────────────────────────────────────────


class TestAaakSpec:
    def test_daemon_path_prints_the_raw_spec(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"aaak_spec": "AAAK is a dialect."}),
            ):
                cli.cmd_aaak(_args(aaak_action="spec"))

        assert capsys.readouterr().out == "AAAK is a dialect.\n"

    def test_json_passthrough(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"aaak_spec": "SPEC"}),
            ):
                cli.cmd_aaak(_args(aaak_action="spec", format="json"))

        assert json.loads(capsys.readouterr().out) == {"aaak_spec": "SPEC"}

    def test_local_path_serves_the_installed_spec(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_get_aaak_spec", return_value={"aaak_spec": "LOCAL"}
            ) as fn:
                cli.cmd_aaak(_args(aaak_action="spec", palace=str(tmp_path)))

        fn.assert_called_once_with()
        assert capsys.readouterr().out == "LOCAL\n"

    def test_missing_spec_exits_2(self):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder({})):
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_aaak(_args(aaak_action="spec"))
        assert exc.value.code == 2

    def test_trailing_newline_is_not_doubled(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"aaak_spec": "SPEC\n"}),
            ):
                cli.cmd_aaak(_args(aaak_action="spec"))

        assert capsys.readouterr().out == "SPEC\n"


# ── #358 mempalace hallway list ───────────────────────────────────────


class TestHallwayList:
    def test_daemon_path_renders_rows_strongest_first(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                cli.cmd_hallway_list(_args(hallway_action="list", limit=50))

        out = capsys.readouterr().out
        assert "HALLWAYS — 2" in out
        # hw-b has the higher co-occurrence count, so it sorts first.
        assert out.index("hw-b") < out.index("hw-a")

    def test_wing_filter_reaches_the_tool(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_HALLWAYS, captured=captured),
            ):
                cli.cmd_hallway_list(_args(hallway_action="list", wing="candela", format="json"))

        assert captured[0]["params"]["name"] == "mempalace_list_hallways"
        assert captured[0]["params"]["arguments"] == {"wing": "candela"}

    def test_no_wing_sends_no_arguments(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_HALLWAYS, captured=captured),
            ):
                cli.cmd_hallway_list(_args(hallway_action="list", format="json"))

        assert captured[0]["params"]["arguments"] == {}

    def test_json_envelope_wraps_the_bare_list(self, capsys):
        """The tool returns a bare list; --json adds the count/filter
        context a scripted caller needs without losing a record."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                cli.cmd_hallway_list(_args(hallway_action="list", wing="candela", json=True))

        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 2
        assert payload["wing_filter"] == "candela"
        assert [h["id"] for h in payload["hallways"]] == ["hw-b", "hw-a"]

    def test_dict_wrapped_response_is_tolerated(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"hallways": _HALLWAYS}),
            ):
                cli.cmd_hallway_list(_args(hallway_action="list", json=True))

        assert json.loads(capsys.readouterr().out)["total"] == 2

    def test_empty_response_explains_where_hallways_come_from(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder([])):
                cli.cmd_hallway_list(_args(hallway_action="list"))

        out = capsys.readouterr().out
        assert "(no hallways)" in out
        assert "when you mine" in out

    def test_limit_truncates(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                cli.cmd_hallway_list(_args(hallway_action="list", limit=1))

        assert "1 more" in capsys.readouterr().out

    def test_local_path_uses_the_mcp_tool_so_the_wing_is_sanitized(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_list_hallways", return_value=_HALLWAYS) as fn:
                cli.cmd_hallway_list(
                    _args(hallway_action="list", palace=str(tmp_path), wing="candela", json=True)
                )

        fn.assert_called_once_with("candela")
        assert json.loads(capsys.readouterr().out)["total"] == 2

    def test_local_sanitization_rejection_exits_2(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_list_hallways",
                return_value={"error": "wing contains illegal characters"},
            ):
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_hallway_list(
                        _args(hallway_action="list", palace=str(tmp_path), wing="../etc", json=True)
                    )

        assert exc.value.code == 2
        assert "illegal" in json.loads(capsys.readouterr().out)["error"]


# ── #407 legacy `hallways` alias ──────────────────────────────────────


class TestLegacyHallwaysAlias:
    """``hallways`` (plural, pre-daemon) delegates to ``hallway list``.

    #438 gave the legacy verb its deprecation notice and taught it to
    emit ``--json``, but left it on its own local-only fetch path — so
    the other four rows of #407's comparison table (daemon routing, wing
    sanitization, the stable id tiebreak, the richer output) were still
    open. Delegating closes them as one code path instead of two.
    """

    def test_legacy_verb_routes_to_the_daemon(self):
        """Was: always local, whatever ``PALACE_DAEMON_URL`` said."""
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(_HALLWAYS, captured=captured),
            ):
                cli.cmd_hallways(_args(wing="candela", json=True))

        assert captured[0]["params"]["name"] == "mempalace_list_hallways"
        assert captured[0]["params"]["arguments"] == {"wing": "candela"}

    def test_legacy_verb_local_path_goes_through_the_sanitizing_tool(self, capsys, tmp_path):
        """Was: ``args.wing`` passed raw to ``list_hallways``."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_list_hallways", return_value=_HALLWAYS) as fn:
                cli.cmd_hallways(_args(palace=str(tmp_path), wing="candela", json=True))

        fn.assert_called_once_with("candela")
        assert json.loads(capsys.readouterr().out)["total"] == 2

    def test_legacy_verb_sanitization_rejection_exits_2(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_list_hallways",
                return_value={"error": "wing contains illegal characters"},
            ):
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_hallways(_args(palace=str(tmp_path), wing="../etc", json=True))

        assert exc.value.code == 2
        assert "illegal" in json.loads(capsys.readouterr().out)["error"]

    def test_equal_counts_get_a_stable_id_tiebreak(self, capsys):
        """Was: co-occurrence only, so equal-count rows came back in
        whatever order the store happened to yield."""
        from mempalace import cli

        tied = [
            {"id": "hw-z", "wing": "w", "entity_a": "a", "entity_b": "b", "co_occurrence_count": 5},
            {"id": "hw-a", "wing": "w", "entity_a": "c", "entity_b": "d", "co_occurrence_count": 5},
        ]
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(tied)):
                cli.cmd_hallways(_args(json=True))

        ids = [h["id"] for h in json.loads(capsys.readouterr().out)["hallways"]]
        assert ids == ["hw-a", "hw-z"]

    def test_legacy_verb_prints_the_richer_table(self, capsys):
        """Was: the label string only — no id, wing or count column."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                cli.cmd_hallways(_args(limit=50))

        out = capsys.readouterr().out
        assert "HALLWAYS — 2" in out
        assert "hw-b" in out and "wax ↔ wick" in out

    def test_notice_goes_to_stderr_not_stdout(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                cli.cmd_hallways(_args(limit=50))

        captured = capsys.readouterr()
        assert "deprecated" in captured.err and "hallway list" in captured.err
        assert "deprecated" not in captured.out

    def test_json_surface_stays_free_of_the_notice(self, capsys):
        """#438's decision, kept: a ``--json`` caller gets the marker in
        the payload instead, so nothing has to parse stderr."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                cli.cmd_hallways(_args(json=True, limit=1))

        captured = capsys.readouterr()
        assert captured.err == ""
        payload = json.loads(captured.out)
        assert payload["deprecated"] == "use `mempalace hallway list`"
        assert payload["total"] == 2, "total still reports the unsliced count"
        assert len(payload["hallways"]) == 1

    def test_new_verb_json_carries_no_deprecated_marker(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                cli.cmd_hallway_list(_args(hallway_action="list", json=True))

        assert "deprecated" not in json.loads(capsys.readouterr().out)

    def test_json_honours_limit_on_the_new_verb(self, capsys):
        """``--limit`` was accepted and silently ignored on ``hallway
        list``'s ``--json`` surface — the same defect class #407 was
        filed about, one verb over."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                cli.cmd_hallway_list(_args(hallway_action="list", json=True, limit=1))

        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 2
        assert len(payload["hallways"]) == 1

    @pytest.mark.parametrize("verb", ["cmd_hallways", "cmd_hallway_list"])
    def test_negative_limit_shows_nothing_not_everything(self, capsys, verb):
        """``max(0, limit)`` collapsed a negative limit into the
        documented ``0 = all`` sentinel, so ``--limit -1`` dumped the
        whole graph instead of showing nothing."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                getattr(cli, verb)(_args(hallway_action="list", json=True, limit=-1))

        assert json.loads(capsys.readouterr().out)["hallways"] == []

    @pytest.mark.parametrize("verb", ["cmd_hallways", "cmd_hallway_list"])
    def test_zero_limit_still_means_all(self, capsys, verb):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(_HALLWAYS)):
                getattr(cli, verb)(_args(hallway_action="list", json=True, limit=0))

        assert len(json.loads(capsys.readouterr().out)["hallways"]) == 2


# ── #358 mempalace hallway delete ─────────────────────────────────────


class TestHallwayDeleteGating:
    def test_refuses_without_confirm_when_there_is_no_tty(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._stdin_is_tty", return_value=False):
                with patch("mempalace.cli._call_daemon_tool") as tool:
                    with pytest.raises(SystemExit) as exc:
                        cli.cmd_hallway_delete(_args(hallway_action="delete", hallway_id="hw-a"))

        assert exc.value.code == 2
        tool.assert_not_called()
        assert "--confirm" in capsys.readouterr().err

    def test_json_mode_never_prompts(self, capsys):
        """A machine-output caller has no one to answer the prompt."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._stdin_is_tty", return_value=True):
                with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                    with patch("mempalace.cli._call_daemon_tool") as tool:
                        with pytest.raises(SystemExit) as exc:
                            cli.cmd_hallway_delete(
                                _args(hallway_action="delete", hallway_id="hw-a", json=True)
                            )

        assert exc.value.code == 2
        tool.assert_not_called()

    def test_tty_prompt_declined_deletes_nothing(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._stdin_is_tty", return_value=True):
                with patch("builtins.input", return_value="n"):
                    with patch("mempalace.cli._call_daemon_tool") as tool:
                        cli.cmd_hallway_delete(_args(hallway_action="delete", hallway_id="hw-a"))

        tool.assert_not_called()
        assert "Aborted" in capsys.readouterr().out

    def test_tty_prompt_accepted_deletes(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._stdin_is_tty", return_value=True):
                with patch("builtins.input", return_value="y"):
                    with patch(
                        "urllib.request.urlopen",
                        side_effect=_make_responder({"deleted": True}),
                    ):
                        cli.cmd_hallway_delete(_args(hallway_action="delete", hallway_id="hw-a"))

        assert "Deleted hallway hw-a" in capsys.readouterr().out

    def test_blank_id_exits_2(self):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_hallway_delete(_args(hallway_action="delete", hallway_id="   "))
        assert exc.value.code == 2


class TestHallwayDeleteExecution:
    def test_confirm_sends_the_id_to_the_daemon(self, capsys):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"deleted": True}, captured=captured),
            ):
                cli.cmd_hallway_delete(
                    _args(hallway_action="delete", hallway_id="hw-a", confirm=True)
                )

        assert captured[0]["params"]["name"] == "mempalace_delete_hallway"
        assert captured[0]["params"]["arguments"] == {"hallway_id": "hw-a"}
        assert "Deleted hallway hw-a" in capsys.readouterr().out

    def test_id_is_stripped_before_it_is_sent(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"deleted": True}, captured=captured),
            ):
                cli.cmd_hallway_delete(
                    _args(hallway_action="delete", hallway_id="  hw-a\n", confirm=True)
                )

        assert captured[0]["params"]["arguments"] == {"hallway_id": "hw-a"}

    def test_miss_is_reported_but_is_not_an_error(self, capsys):
        """delete is idempotent — a no-op must not look like a failure."""
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"deleted": False}),
            ):
                cli.cmd_hallway_delete(
                    _args(hallway_action="delete", hallway_id="ghost", confirm=True)
                )

        assert "nothing deleted" in capsys.readouterr().out

    def test_json_passthrough(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder({"deleted": True}),
            ):
                cli.cmd_hallway_delete(
                    _args(hallway_action="delete", hallway_id="hw-a", confirm=True, json=True)
                )

        assert json.loads(capsys.readouterr().out) == {"deleted": True}

    def test_local_path_deletes_through_the_mcp_tool(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "mempalace.mcp_server.tool_delete_hallway", return_value={"deleted": True}
            ) as fn:
                cli.cmd_hallway_delete(
                    _args(
                        hallway_action="delete",
                        hallway_id="hw-a",
                        confirm=True,
                        palace=str(tmp_path),
                    )
                )

        fn.assert_called_once_with("hw-a")
        assert "Deleted hallway hw-a" in capsys.readouterr().out


class TestHallwayDispatch:
    def test_list_action_routes_to_list(self):
        from mempalace import cli

        with patch("mempalace.cli.cmd_hallway_list") as listed:
            with patch("mempalace.cli.cmd_hallway_delete") as deleted:
                cli.cmd_hallway(_args(hallway_action="list"))
        listed.assert_called_once()
        deleted.assert_not_called()

    def test_delete_action_routes_to_delete(self):
        from mempalace import cli

        with patch("mempalace.cli.cmd_hallway_list") as listed:
            with patch("mempalace.cli.cmd_hallway_delete") as deleted:
                cli.cmd_hallway(_args(hallway_action="delete", hallway_id="x"))
        deleted.assert_called_once()
        listed.assert_not_called()


# ── #360 mempalace checkpoint ─────────────────────────────────────────
#
# checkpoint WRITES. Every test here is mock-only — no live palace, no
# daemon, ever.


class TestCheckpointItemBuilding:
    def test_single_item_flags(self):
        from mempalace.cli import _checkpoint_items

        items = _checkpoint_items(_args(wing="w", room="r", content="verbatim"))
        assert items == [{"wing": "w", "room": "r", "content": "verbatim"}]

    def test_items_file_json_array(self, tmp_path):
        from mempalace.cli import _checkpoint_items

        path = tmp_path / "items.json"
        path.write_text(json.dumps([{"wing": "w", "room": "r", "content": "c"}]))
        assert _checkpoint_items(_args(items_file=str(path))) == [
            {"wing": "w", "room": "r", "content": "c"}
        ]

    def test_items_file_accepts_the_tool_envelope_too(self, tmp_path):
        from mempalace.cli import _checkpoint_items

        path = tmp_path / "items.json"
        path.write_text(json.dumps({"items": [{"wing": "w", "room": "r", "content": "c"}]}))
        assert len(_checkpoint_items(_args(items_file=str(path)))) == 1

    def test_mixing_items_file_and_single_flags_is_rejected(self, tmp_path):
        from mempalace.cli import _checkpoint_items

        path = tmp_path / "items.json"
        path.write_text("[]")
        with pytest.raises(ValueError, match="or the single-item flags"):
            _checkpoint_items(_args(items_file=str(path), wing="w"))

    def test_no_items_at_all_is_rejected(self):
        from mempalace.cli import _checkpoint_items

        with pytest.raises(ValueError, match="checkpoint needs items"):
            _checkpoint_items(_args())

    def test_malformed_json_is_rejected_with_a_readable_message(self, tmp_path):
        from mempalace.cli import _checkpoint_items

        path = tmp_path / "items.json"
        path.write_text("{nope")
        with pytest.raises(ValueError, match="not valid JSON"):
            _checkpoint_items(_args(items_file=str(path)))

    def test_non_array_json_is_rejected(self, tmp_path):
        from mempalace.cli import _checkpoint_items

        path = tmp_path / "items.json"
        path.write_text('"just a string"')
        with pytest.raises(ValueError, match="JSON array"):
            _checkpoint_items(_args(items_file=str(path)))

    def test_empty_array_is_rejected(self, tmp_path):
        from mempalace.cli import _checkpoint_items

        path = tmp_path / "items.json"
        path.write_text("[]")
        with pytest.raises(ValueError, match="no items"):
            _checkpoint_items(_args(items_file=str(path)))

    @pytest.mark.parametrize("field", ["wing", "room", "content"])
    def test_blank_required_field_is_rejected_before_anything_is_filed(self, tmp_path, field):
        from mempalace.cli import _checkpoint_items

        item = {"wing": "w", "room": "r", "content": "c"}
        item[field] = "   "
        path = tmp_path / "items.json"
        path.write_text(json.dumps([item]))
        with pytest.raises(ValueError, match=f"item 0: {field}"):
            _checkpoint_items(_args(items_file=str(path)))

    def test_non_object_item_is_rejected_with_its_index(self, tmp_path):
        from mempalace.cli import _checkpoint_items

        path = tmp_path / "items.json"
        path.write_text(json.dumps([{"wing": "w", "room": "r", "content": "c"}, "nope"]))
        with pytest.raises(ValueError, match="item 1 is not an object"):
            _checkpoint_items(_args(items_file=str(path)))

    def test_content_file_is_read_verbatim(self, tmp_path):
        """CRLF must survive — the palace stores exact bytes."""
        from mempalace.cli import _checkpoint_items

        path = tmp_path / "content.txt"
        path.write_bytes(b"line one\r\nline two\r\n")
        items = _checkpoint_items(_args(wing="w", room="r", content_file=str(path)))
        assert items[0]["content"] == "line one\r\nline two\r\n"


class TestCheckpointDiaryBuilding:
    def test_absent_diary_is_none(self):
        from mempalace.cli import _checkpoint_diary

        assert _checkpoint_diary(_args()) is None

    def test_entry_plus_metadata(self):
        from mempalace.cli import _checkpoint_diary

        diary = _checkpoint_diary(
            _args(diary_entry="AAAK line", diary_agent="nebula", diary_topic="cli", diary_wing="mp")
        )
        assert diary == {
            "entry": "AAAK line",
            "agent_name": "nebula",
            "topic": "cli",
            "wing": "mp",
        }

    def test_diary_metadata_without_an_entry_is_rejected(self):
        from mempalace.cli import _checkpoint_diary

        with pytest.raises(ValueError, match="need --diary-entry"):
            _checkpoint_diary(_args(diary_agent="nebula"))

    def test_blank_entry_is_rejected(self):
        from mempalace.cli import _checkpoint_diary

        with pytest.raises(ValueError, match="must not be blank"):
            _checkpoint_diary(_args(diary_entry="   "))


class TestCheckpointExecution:
    _OK = {"added": [{"wing": "w", "room": "r", "id": "d1"}], "duplicates": [], "errors": []}

    def test_daemon_payload_shape_matches_the_tool_schema(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._OK, captured=captured),
            ):
                cli.cmd_checkpoint(
                    _args(
                        wing="w",
                        room="r",
                        content="c",
                        diary_entry="e",
                        diary_agent="nebula",
                        added_by="nebula",
                        json=True,
                    )
                )

        params = captured[0]["params"]
        assert params["name"] == "mempalace_checkpoint"
        assert params["arguments"] == {
            "items": [{"wing": "w", "room": "r", "content": "c"}],
            "dedup_threshold": 0.9,
            "diary": {"entry": "e", "agent_name": "nebula"},
            "added_by": "nebula",
        }

    def test_added_by_is_omitted_when_unset_so_the_tool_can_default_it(self):
        from mempalace import cli

        captured: list = []
        with patch.dict("os.environ", _env(), clear=True):
            with patch(
                "urllib.request.urlopen",
                side_effect=_make_responder(self._OK, captured=captured),
            ):
                cli.cmd_checkpoint(_args(wing="w", room="r", content="c", json=True))

        assert "added_by" not in captured[0]["params"]["arguments"]
        assert "diary" not in captured[0]["params"]["arguments"]

    def test_dry_run_sends_nothing(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_tool") as tool:
                cli.cmd_checkpoint(_args(wing="w", room="r", content="hello", dry_run=True))

        tool.assert_not_called()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "w/r" in out

    def test_dry_run_json_shows_the_exact_payload(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_tool") as tool:
                cli.cmd_checkpoint(
                    _args(wing="w", room="r", content="hello", dry_run=True, json=True)
                )

        tool.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True
        assert payload["would_send"]["items"] == [{"wing": "w", "room": "r", "content": "hello"}]

    def test_report_lists_added_duplicates_and_errors(self, capsys):
        from mempalace import cli

        response = {
            "added": [{"wing": "w", "room": "r", "id": "d1"}],
            "duplicates": [{"room": "r", "matches": []}],
            "errors": [{"error": "bad item"}],
            "diary": {"id": "diary-1"},
        }
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(response)):
                cli.cmd_checkpoint(_args(wing="w", room="r", content="c"))

        out = capsys.readouterr().out
        assert "1 filed, 1 duplicate, 1 error" in out
        assert "diary-1" in out

    def test_all_items_failing_exits_2(self, capsys):
        """Nothing landed and the batch reported errors — a scripted
        caller must not read that as success."""
        from mempalace import cli

        response = {"added": [], "duplicates": [], "errors": [{"error": "boom"}]}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(response)):
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_checkpoint(_args(wing="w", room="r", content="c"))
        assert exc.value.code == 2

    def test_all_duplicates_is_success_not_failure(self):
        from mempalace import cli

        response = {"added": [], "duplicates": [{"room": "r", "matches": []}], "errors": []}
        with patch.dict("os.environ", _env(), clear=True):
            with patch("urllib.request.urlopen", side_effect=_make_responder(response)):
                cli.cmd_checkpoint(_args(wing="w", room="r", content="c"))

    @pytest.mark.parametrize("bad", [-0.1, 1.5])
    def test_out_of_range_threshold_exits_2(self, bad):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_tool") as tool:
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_checkpoint(_args(wing="w", room="r", content="c", dedup_threshold=bad))
        assert exc.value.code == 2
        tool.assert_not_called()

    def test_non_numeric_threshold_exits_2(self):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_checkpoint(_args(wing="w", room="r", content="c", dedup_threshold="high"))
        assert exc.value.code == 2

    def test_validation_failure_exits_2_before_any_call(self, capsys):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.cli._call_daemon_tool") as tool:
                with pytest.raises(SystemExit) as exc:
                    cli.cmd_checkpoint(_args(json=True))

        assert exc.value.code == 2
        tool.assert_not_called()
        assert "checkpoint needs items" in json.loads(capsys.readouterr().out)["error"]

    def test_local_path_calls_the_tool_with_keyword_arguments(self, capsys, tmp_path):
        from mempalace import cli

        with patch.dict("os.environ", _env(), clear=True):
            with patch("mempalace.mcp_server.tool_checkpoint", return_value=self._OK) as fn:
                cli.cmd_checkpoint(
                    _args(wing="w", room="r", content="c", palace=str(tmp_path), json=True)
                )

        fn.assert_called_once_with(
            items=[{"wing": "w", "room": "r", "content": "c"}], dedup_threshold=0.9
        )
        assert json.loads(capsys.readouterr().out) == self._OK


# ── shared routing behaviour ──────────────────────────────────────────


class TestReadFamilyRouting:
    def test_no_daemon_configured_falls_to_local(self, capsys):
        """Unlike `tunnels`, the read family has a working local path, so
        an unconfigured daemon is not an error.

        ``_daemon_strict`` is patched rather than cleared from the env:
        ``MempalaceConfig`` also reads ``daemon_url`` out of
        ``~/.mempalace/config.json``, so clearing ``os.environ`` alone
        leaves a developer machine routing at its real daemon — the test
        would pass locally and reach the network doing it.
        """
        from mempalace import cli

        with patch("mempalace.cli._daemon_strict", return_value=False):
            with patch(
                "mempalace.mcp_server.tool_list_wings", return_value={"wings": {"w": 1}}
            ) as fn:
                with patch("mempalace.cli._call_daemon_rest") as rest:
                    cli.cmd_wings(_args(format="json"))

        fn.assert_called_once()
        rest.assert_not_called()
        assert json.loads(capsys.readouterr().out) == {"wings": {"w": 1}}

    def test_importing_mcp_server_does_not_steal_stdout(self, tmp_path):
        """``mempalace.mcp_server`` hijacks stdio at IMPORT time (#225):
        ``sys.stdout = sys.stderr`` plus an fd-level ``os.dup2(2, 1)``, so
        a stray print cannot corrupt its JSON-RPC stream. A CLI process
        that imports it inherits that for the rest of its life — every
        ``--json`` document would land on stderr and every pipe would read
        empty, with nothing raised anywhere.

        The import is forced by dropping the module first. Without that
        this test passes **vacuously**: the module is already in
        ``sys.modules`` by the time it runs, so ``dup2`` never re-fires and
        there is nothing to restore. Verified by disabling the fix — the
        unforced version stayed green in a whole-file run and only failed
        when it happened to be the first test to import the module.

        Dropping it takes **both** handles. ``from . import mcp_server``
        resolves through ``getattr(mempalace, "mcp_server")`` before it
        consults ``sys.modules``, so popping ``sys.modules`` alone hands
        back the cached module and the import never re-runs. Both are
        restored afterwards for the same reason in reverse: a stale
        package attribute would make every later
        ``mock.patch("mempalace.mcp_server.tool_x")`` in the suite patch a
        ghost module.

        Targets ``_import_mcp_server`` — the canonical helper from #355 —
        through ``_local_mcp_server``, which delegates to it. Deliberately
        a second, independently-written test of the same guarantee: this
        one forces the import, the drawer family's asserts on the repaired
        state, and a regression that survives both is genuinely fixed.
        """
        import sys as _sys

        import mempalace
        from mempalace import cli

        popped = _sys.modules.pop("mempalace.mcp_server", None)
        package_attr = getattr(mempalace, "mcp_server", None)
        if package_attr is not None:
            delattr(mempalace, "mcp_server")
        try:
            before = _sys.stdout
            with cli._local_mcp_server(str(tmp_path)) as reimported:
                # The import really did re-run, so the hijack really did
                # re-fire and really was undone.
                assert reimported is not popped
                assert _sys.stdout is before
                assert _sys.stdout is not _sys.stderr
            assert _sys.stdout is before
        finally:
            if popped is not None:
                _sys.modules["mempalace.mcp_server"] = popped
            if package_attr is not None:
                # Restoring sys.modules alone is not enough:
                # ``mock.patch("mempalace.mcp_server.tool_x")`` resolves
                # through the PACKAGE ATTRIBUTE, so leaving the re-imported
                # module bound there would make every later patch in the
                # suite target a ghost module.
                mempalace.mcp_server = package_attr

    def test_local_path_json_document_arrives_on_stdout(self, capsys, tmp_path):
        """The end-to-end consequence of the hijack: `--json | jq` must
        actually receive the document, and stderr must stay clean."""
        from mempalace import cli

        with patch("mempalace.mcp_server.tool_list_wings", return_value={"wings": {"w": 1}}):
            cli.cmd_wings(_args(palace=str(tmp_path), format="json"))

        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"wings": {"w": 1}}
        assert captured.err == ""

    def test_strict_off_forces_local_even_with_a_daemon_url(self, capsys):
        from mempalace import cli

        env = dict(_env())
        env["PALACE_DAEMON_STRICT"] = "0"
        with patch.dict("os.environ", env, clear=True):
            with patch(
                "mempalace.mcp_server.tool_list_wings", return_value={"wings": {"w": 1}}
            ) as fn:
                with patch("mempalace.cli._call_daemon_rest") as rest:
                    cli.cmd_wings(_args(format="json"))

        fn.assert_called_once()
        rest.assert_not_called()

    @pytest.mark.parametrize(
        "args_kwargs,expected",
        [
            ({}, "table"),
            ({"json": True}, "json"),
            ({"format": "json"}, "json"),
            ({"format": "table", "json": True}, "table"),
        ],
    )
    def test_format_resolution(self, args_kwargs, expected):
        from mempalace.cli import _resolve_read_format

        assert _resolve_read_format(_args(**args_kwargs)) == expected
