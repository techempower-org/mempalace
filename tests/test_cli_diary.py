"""
test_cli_diary.py — ``mempalace diary write|read`` (slice of #191, issue #354).

``diary write`` wraps ``mempalace_diary_write``; ``diary read`` wraps
``mempalace_diary_read``. Both route to the daemon when daemon-strict is on
and ``--palace`` was not given, else to the local ``mempalace.mcp_server``
tool function — the ``cmd_wakeup`` / ``cmd_mined`` pattern.

The tool contract requires ``agent_name``, so the CLI adds ``--agent`` (with
a ``MEMPALACE_AGENT_NAME`` fallback) on top of issue #354's sketch. ``read``'s
``--topic`` / ``--since`` filters are applied client-side because the tool
takes only ``(agent_name, last_n, wing)``.
"""

import argparse
import io
import json
from unittest.mock import MagicMock, patch

import pytest


def _write_args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "diary_action": "write",
        "entry": "SESSION:2026-08-20|cli.diary.landed|★★★",
        "agent": "morpheus",
        "topic": None,
        "wing": None,
        "session_id": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _read_args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "diary_action": "read",
        "agent": "morpheus",
        "limit": 10,
        "wing": None,
        "topic": None,
        "since": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


_WRITE_OK = {
    "success": True,
    "entry_id": "diary_20260820_120000_000001",
    "agent": "morpheus",
    "topic": "cli-wave",
    "timestamp": "2026-08-20T12:00:00",
    "warnings": [],
    "chunks": 1,
}

_ENTRIES = [
    {
        "drawer_id": "d3",
        "date": "2026-08-20",
        "timestamp": "2026-08-20T12:00:00",
        "topic": "cli-wave",
        "content": "landed the walk command",
    },
    {
        "drawer_id": "d2",
        "date": "2026-08-19",
        "timestamp": "2026-08-19T09:30:00",
        "topic": "sync",
        "content": "upstream v3.8.0 merge",
    },
    {
        "drawer_id": "d1",
        "date": "2026-08-18",
        "timestamp": "2026-08-18T08:00:00",
        "topic": "cli-wave",
        "content": "read the brief",
    },
]

_READ_OK = {"agent": "morpheus", "entries": _ENTRIES, "total": 3, "showing": 3}


# `_fail_daemon`'s exit code, in one place so the rebase that changes it is a
# one-line edit rather than a hunt. Contract #44 wants 2 for "palace
# unavailable"; the diary family (and cmd_why / cmd_tags / cmd_graph) have
# always used 1. VERIFIED 2026-09-17: #509 does NOT move it — its two exit(64)s
# are a daemon *4xx refusal* helper and cmd_pending's unknown-action guard, and
# `_fail_daemon` is untouched by that PR. So this stays 1 until someone owns
# that cross-cutting change.
_DAEMON_FAIL_EXIT = 1


def _daemon(payload):
    """Patch the daemon path on and return the MagicMock standing in for it."""
    fake = MagicMock(return_value=payload)
    return (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._call_daemon_tool", fake),
        fake,
    )


def _agents_args(**overrides):
    defaults = {
        "json": False,
        "quiet": False,
        "format": None,
        "palace": None,
        "diary_action": "agents",
        "wing": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# The production shape (#501): hook entries keyed on the harness name the
# reader cannot guess, alongside a named agent.
_MIXED_ENTRIES = [
    {
        "drawer_id": "h2",
        "date": "2026-09-17",
        "timestamp": "2026-09-17T19:34:27",
        "topic": "hook",
        "agent": "claude-code",
        "wing": "2g",
        "content": "AUTO-SAVE:0972a270|1713.msgs",
    },
    {
        "drawer_id": "t1",
        "date": "2026-09-17",
        "timestamp": "2026-09-17T11:02:00",
        "topic": "planning",
        "agent": "team-lead",
        "wing": "2g",
        "content": "wave 3 landing order",
    },
]

_READ_NO_AGENT_OK = {
    "agent": None,
    "wing": "2g",
    "entries": _MIXED_ENTRIES,
    "total": 2,
    "showing": 2,
    "truncated": False,
}

_AGENTS_OK = {
    "wing": "2g",
    "agents": [
        {"agent": "claude-code", "entries": 1713, "latest": "2026-09-17T19:34:27"},
        {"agent": "team-lead", "entries": 4, "latest": "2026-09-17T11:02:00"},
    ],
    "scanned": 1717,
    "truncated": False,
}


class TestDiaryWrite:
    def test_daemon_payload_carries_every_flag(self):
        from mempalace import cli

        strict, call, fake = _daemon(_WRITE_OK)
        with strict, call:
            cli.cmd_diary(_write_args(topic="cli-wave", wing="memorypalace", session_id="sess-7"))

        name, payload = fake.call_args[0]
        assert name == "mempalace_diary_write"
        assert payload == {
            "agent_name": "morpheus",
            "entry": "SESSION:2026-08-20|cli.diary.landed|★★★",
            "topic": "cli-wave",
            "wing": "memorypalace",
            "session_id": "sess-7",
        }

    def test_table_output_reports_entry_id(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_WRITE_OK)
        with strict, call:
            cli.cmd_diary(_write_args())

        out = capsys.readouterr().out
        assert "Diary entry filed" in out
        assert "diary_20260820_120000_000001" in out
        assert "morpheus" in out

    def test_json_passthrough(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_WRITE_OK)
        with strict, call:
            cli.cmd_diary(_write_args(format="json"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["entry_id"] == "diary_20260820_120000_000001"
        assert payload["success"] is True

    def test_chunked_write_reports_chunk_count(self, capsys):
        from mempalace import cli

        chunked = dict(_WRITE_OK, chunks=4, chunk_ids=["a", "b", "c", "d"])
        strict, call, _ = _daemon(chunked)
        with strict, call:
            cli.cmd_diary(_write_args())

        assert "4 chunks" in capsys.readouterr().out

    def test_stdin_entry_when_dash(self):
        from mempalace import cli

        strict, call, fake = _daemon(_WRITE_OK)
        with strict, call, patch("sys.stdin", io.StringIO("piped entry text")):
            cli.cmd_diary(_write_args(entry="-"))

        assert fake.call_args[0][1]["entry"] == "piped entry text"

    def test_missing_entry_exits_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_WRITE_OK)
        with strict, call, patch("sys.stdin", io.StringIO("   ")):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_write_args(entry=None))

        assert exc.value.code == 2
        assert fake.call_count == 0
        assert "entry text" in capsys.readouterr().err

    def test_missing_agent_exits_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_WRITE_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_write_args(agent=None))

        assert exc.value.code == 2
        assert fake.call_count == 0
        assert "MEMPALACE_AGENT_NAME" in capsys.readouterr().err

    def test_agent_name_from_environment(self):
        from mempalace import cli

        strict, call, fake = _daemon(_WRITE_OK)
        env = {"MEMPALACE_AGENT_NAME": "lucid"}
        with strict, call, patch.dict("os.environ", env, clear=True):
            cli.cmd_diary(_write_args(agent=None))

        assert fake.call_args[0][1]["agent_name"] == "lucid"

    def test_tool_failure_envelope_exits_2(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"success": False, "error": "another mine is in progress"})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_write_args())

        assert exc.value.code == 2
        assert "another mine is in progress" in capsys.readouterr().err

    def test_daemon_unreachable_exits_1(self, capsys):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", side_effect=cli.DaemonError("boom")),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_write_args())

        assert exc.value.code == 1
        assert "daemon unreachable" in capsys.readouterr().err

    def test_local_path_calls_tool_function(self):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.mcp_server.tool_diary_write", return_value=_WRITE_OK) as local,
        ):
            cli.cmd_diary(_write_args(topic="cli-wave"))

        local.assert_called_once_with(
            agent_name="morpheus",
            entry="SESSION:2026-08-20|cli.diary.landed|★★★",
            topic="cli-wave",
        )


class TestDiaryRead:
    def test_limit_becomes_last_n(self):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_OK)
        with strict, call:
            cli.cmd_diary(_read_args(limit=3, wing="memorypalace"))

        name, payload = fake.call_args[0]
        assert name == "mempalace_diary_read"
        assert payload == {"agent_name": "morpheus", "last_n": 3, "wing": "memorypalace"}

    def test_limit_clamped_to_tool_maximum(self):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_OK)
        with strict, call:
            cli.cmd_diary(_read_args(limit=5000))

        assert fake.call_args[0][1]["last_n"] == 100

    def test_table_output_lists_entries_newest_first(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_READ_OK)
        with strict, call:
            cli.cmd_diary(_read_args())

        out = capsys.readouterr().out
        assert "DIARY — morpheus" in out
        assert "landed the walk command" in out
        assert out.index("2026-08-20T12:00:00") < out.index("2026-08-18T08:00:00")

    def test_empty_diary_message_and_exit_1(self, capsys):
        """Was exit 0. Contract #44 reserves 1 for "ran, selected nothing",
        and an empty read is exactly that."""
        from mempalace import cli

        strict, call, _ = _daemon({"agent": "morpheus", "entries": []})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args())

        assert exc.value.code == 1
        assert "No diary entries" in capsys.readouterr().out

    def test_topic_filter_is_client_side_over_a_full_page(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_OK)
        with strict, call:
            cli.cmd_diary(_read_args(topic="sync", limit=2))

        # A client-side filter must not shrink the fetch window, or matching
        # entries just outside --limit would be invisible.
        assert fake.call_args[0][1]["last_n"] == 100
        out = capsys.readouterr().out
        assert "upstream v3.8.0 merge" in out
        assert "landed the walk command" not in out

    def test_since_filter_drops_older_entries(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_READ_OK)
        with strict, call:
            cli.cmd_diary(_read_args(since="2026-08-19"))

        out = capsys.readouterr().out
        assert "upstream v3.8.0 merge" in out
        assert "read the brief" not in out

    def test_limit_truncates_after_filtering(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_READ_OK)
        with strict, call:
            cli.cmd_diary(_read_args(topic="cli-wave", limit=1))

        out = capsys.readouterr().out
        assert "landed the walk command" in out
        assert "read the brief" not in out

    def test_json_reports_filters_and_showing(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_READ_OK)
        with strict, call:
            cli.cmd_diary(_read_args(json=True, topic="cli-wave"))

        payload = json.loads(capsys.readouterr().out)
        assert payload["showing"] == 2
        assert payload["topic_filter"] == "cli-wave"
        assert payload["total"] == 3
        assert [e["drawer_id"] for e in payload["entries"]] == ["d3", "d1"]

    def test_error_envelope_exits_2(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"error": "Failed to read diary entries"})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args())

        assert exc.value.code == 2
        assert "Failed to read diary entries" in capsys.readouterr().err

    def test_local_path_calls_tool_function(self):
        from mempalace import cli

        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.mcp_server.tool_diary_read", return_value=_READ_OK) as local,
        ):
            cli.cmd_diary(_read_args(limit=4))

        local.assert_called_once_with(agent_name="morpheus", last_n=4)

    def test_json_survives_the_mcp_server_stdout_hijack(self, capsys):
        """The local path must not lose stdout to mcp_server's stdio guard.

        ``mempalace.mcp_server`` installs MCP-stdio protection at module
        scope (#225) — ``os.dup2(2, 1)`` and ``sys.stdout = sys.stderr`` —
        and only its own ``main()`` undoes it. Dropping the module from
        ``sys.modules`` makes the local path's import genuinely re-run that
        hijack, so this asserts the routing helper restores stdout before
        the command prints: ``diary read --json`` is exactly what a caller
        pipes, and on stderr it is invisible to them.
        """
        import sys

        from mempalace import cli

        saved_module = sys.modules.pop("mempalace.mcp_server", None)
        saved_stdout = sys.stdout
        try:
            # Importing here is what hijacks stdout for the rest of this test.
            import mempalace.mcp_server  # noqa: F401

            with (
                patch("mempalace.cli._daemon_strict", return_value=False),
                patch("mempalace.mcp_server.tool_diary_read", return_value=_READ_OK),
            ):
                cli.cmd_diary(_read_args(json=True))
        finally:
            sys.stdout = saved_stdout
            if saved_module is not None:
                # Both handles matter: ``patch("mempalace.mcp_server.X")``
                # resolves through sys.modules while ``from . import
                # mcp_server`` reads the package attribute. Leave only one
                # of them pointing at the freshly-imported copy and later
                # tests patch a module the CLI never sees.
                sys.modules["mempalace.mcp_server"] = saved_module
                import mempalace

                mempalace.mcp_server = saved_module

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["agent"] == "morpheus"
        assert "morpheus" not in captured.err

    def test_import_helper_restores_a_hijacked_stdout(self):
        from mempalace import cli

        real = __import__("sys").stdout
        sys_mod = __import__("sys")
        sys_mod.stdout = sys_mod.stderr
        try:
            module = cli._import_mcp_server()
            assert sys_mod.stdout is not sys_mod.stderr
            assert hasattr(module, "tool_diary_read")
            # Idempotent: a second call must not raise on the already-closed
            # duplicate file descriptor.
            cli._import_mcp_server()
            assert sys_mod.stdout is not sys_mod.stderr
        finally:
            sys_mod.stdout = real

    def test_palace_flag_forces_local_path(self, tmp_path):
        from mempalace import cli

        with (
            # --palace seeds MEMPALACE_PALACE_PATH; keep that out of the
            # rest of the session's environment.
            patch.dict("os.environ", {}),
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool") as daemon_call,
            patch("mempalace.mcp_server.tool_diary_read", return_value=_READ_OK) as local,
        ):
            cli.cmd_diary(_read_args(palace=str(tmp_path)))

        assert daemon_call.call_count == 0
        assert local.call_count == 1


class TestDiaryReadWithoutIdentity:
    """#501(a): ``diary read --wing W`` must work with no identity at all.

    Measured 2026-09-17 against the production daemon: entries written by
    the Stop hook are keyed ``agent=claude-code`` (hook.py's ``--harness``
    flag), so ``--agent team-lead`` and ``--agent 2g-c6`` both answered
    "No diary entries" while ``list --wing 2g`` showed the rows.
    """

    def test_no_agent_no_env_does_not_refuse(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_NO_AGENT_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            cli.cmd_diary(_read_args(agent=None, wing="2g"))

        out = capsys.readouterr().out
        assert "requires an agent name" not in out
        assert "AUTO-SAVE:0972a270|1713.msgs" in out

    def test_no_agent_omits_agent_name_from_the_payload(self):
        """It must not send a blank agent_name — the deployed tool answers
        'agent_name must be a non-empty string' to that, which reports a
        problem the caller did not create."""
        from mempalace import cli

        strict, call, fake = _daemon(_READ_NO_AGENT_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            cli.cmd_diary(_read_args(agent=None, wing="2g"))

        _tool, payload = fake.call_args[0]
        assert "agent_name" not in payload
        assert payload["wing"] == "2g"

    def test_listing_shows_which_agent_wrote_each_entry(self, capsys):
        """Without this the reader still cannot learn the name to ask for."""
        from mempalace import cli

        strict, call, _ = _daemon(_READ_NO_AGENT_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            cli.cmd_diary(_read_args(agent=None, wing="2g"))

        out = capsys.readouterr().out
        assert "claude-code" in out
        assert "team-lead" in out

    def test_no_agent_and_no_wing_is_allowed(self):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_NO_AGENT_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            cli.cmd_diary(_read_args(agent=None, wing=None))

        _tool, payload = fake.call_args[0]
        assert "agent_name" not in payload
        assert "wing" not in payload

    def test_limit_is_honoured_without_an_agent(self):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_NO_AGENT_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            cli.cmd_diary(_read_args(agent=None, wing="2g", limit=2))

        assert fake.call_args[0][1]["last_n"] == 2

    def test_since_filter_is_honoured_without_an_agent(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_READ_NO_AGENT_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            cli.cmd_diary(_read_args(agent=None, wing="2g", since="2026-09-17T12:00:00"))

        out = capsys.readouterr().out
        assert "AUTO-SAVE" in out
        assert "wave 3 landing order" not in out

    def test_write_still_requires_an_agent(self, capsys):
        """The gate moved to write-only; it must not have been deleted."""
        from mempalace import cli

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_write_args(agent=None))

        assert exc.value.code == 2
        assert "requires an agent name" in capsys.readouterr().err

    def test_explicit_agent_still_scopes_the_read(self):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_OK)
        with strict, call:
            cli.cmd_diary(_read_args(agent="morpheus"))

        assert fake.call_args[0][1]["agent_name"] == "morpheus"

    def test_old_daemon_schema_rejection_is_explained(self, capsys):
        """The path the real daemon actually takes, measured 2026-09-17.

        A pre-#501 daemon rejects an OMITTED agent_name at schema
        validation, before dispatch, so it arrives as a DaemonError and not
        as a tool envelope:

            -32602: Missing required parameter 'agent_name'
                    for tool mempalace_diary_read

        Exit stays 1 (the diary family's daemon-failure code); only the
        wording changes, because the bare daemon message sends the reader
        after a flag they never passed.
        """
        from mempalace import cli

        fake = MagicMock(
            side_effect=cli.DaemonError(
                "daemon error -32602: Missing required parameter 'agent_name' "
                "for tool mempalace_diary_read"
            )
        )
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", fake),
            patch.dict("os.environ", {}, clear=True),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(agent=None, wing="2g"))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "predates" in err
        assert "--agent" in err

    def test_old_daemon_schema_rejection_with_an_agent_is_passed_through(self, capsys):
        """Only rewrite the message when WE omitted the agent — otherwise a
        genuine agent_name complaint must reach the reader intact."""
        from mempalace import cli

        fake = MagicMock(side_effect=cli.DaemonError("daemon error -32602: bad agent_name"))
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", fake),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(agent="morpheus"))

        assert exc.value.code == _DAEMON_FAIL_EXIT
        assert "predates" not in capsys.readouterr().err

    def test_old_daemon_envelope_rejection_is_explained(self, capsys):
        """A daemon whose mempalace predates #501 answers the agent_name
        error. Surfacing that verbatim sends the reader after a flag they
        did not pass, so name the real cause."""
        from mempalace import cli

        strict, call, _ = _daemon({"error": "agent_name must be a non-empty string"})
        with strict, call, patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(agent=None, wing="2g"))

        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "--agent" in err
        assert "older" in err.lower() or "predates" in err.lower()


class TestDiaryAgents:
    """#501(b): enumerate the agent names present so a reader can pick one."""

    def test_lists_names_with_counts(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_AGENTS_OK)
        with strict, call:
            cli.cmd_diary(_agents_args(wing="2g"))

        out = capsys.readouterr().out
        assert "claude-code" in out
        assert "1713" in out
        assert "team-lead" in out
        assert fake.call_args[0][0] == "mempalace_diary_agents"

    def test_wing_is_forwarded(self):
        from mempalace import cli

        strict, call, fake = _daemon(_AGENTS_OK)
        with strict, call:
            cli.cmd_diary(_agents_args(wing="2g"))

        assert fake.call_args[0][1] == {"wing": "2g"}

    def test_no_wing_sends_an_empty_payload(self):
        from mempalace import cli

        strict, call, fake = _daemon(_AGENTS_OK)
        with strict, call:
            cli.cmd_diary(_agents_args(wing=None))

        assert fake.call_args[0][1] == {}

    def test_empty_result_says_so_and_exits_1(self, capsys):
        """Contract #44: the operation ran and selected nothing -> 1.
        The message still prints first; the code is for scripts."""
        from mempalace import cli

        strict, call, _ = _daemon({"wing": None, "agents": [], "scanned": 0, "truncated": False})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_agents_args())

        assert exc.value.code == 1
        assert "No diary" in capsys.readouterr().out

    def test_truncated_scan_is_reported_as_a_lower_bound(self, capsys):
        """A capped count is a lower bound. Printing it bare would be the
        report-disagrees-with-reality defect this wave keeps finding."""
        from mempalace import cli

        payload = dict(_AGENTS_OK, truncated=True, scanned=10000)
        strict, call, _ = _daemon(payload)
        with strict, call:
            cli.cmd_diary(_agents_args())

        out = capsys.readouterr().out
        assert "least" in out.lower() or "lower bound" in out.lower()

    def test_untruncated_scan_prints_no_caveat(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_AGENTS_OK)
        with strict, call:
            cli.cmd_diary(_agents_args())

        out = capsys.readouterr().out
        assert "lower bound" not in out.lower()

    def test_json_passthrough(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(_AGENTS_OK)
        with strict, call:
            cli.cmd_diary(_agents_args(json=True))

        assert json.loads(capsys.readouterr().out)["agents"][0]["agent"] == "claude-code"

    def test_tool_error_envelope_exits_2(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon({"error": "palace unavailable"})
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_agents_args())

        assert exc.value.code == 2

    def test_daemon_unreachable_exits_1(self, capsys):
        from mempalace import cli

        fake = MagicMock(side_effect=cli.DaemonError("daemon error: connection refused"))
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._call_daemon_tool", fake),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_agents_args())

        assert exc.value.code == _DAEMON_FAIL_EXIT

    def test_local_path_calls_the_tool_function(self, tmp_path):
        from mempalace import cli

        fake_mod = MagicMock()
        fake_mod.tool_diary_agents.return_value = _AGENTS_OK
        with (
            patch("mempalace.cli._daemon_strict", return_value=False),
            patch("mempalace.cli._local_mcp_server") as ctx,
        ):
            ctx.return_value.__enter__.return_value = fake_mod
            cli.cmd_diary(_agents_args(palace=str(tmp_path)))

        fake_mod.tool_diary_agents.assert_called_once_with(**{})


class TestDiaryAgentsWiring:
    """Two independent probes, because they see different layers.

    ``--help`` exits inside argparse, so it proves the SUBPARSER exists but
    is blind to a missing branch in ``cmd_diary``. The dispatch probe runs
    the verb for real and sees that branch. Deletion matrix recorded in the
    PR body: removing the subparser fails both; removing the cmd_diary
    branch fails only the dispatch probe.
    """

    def test_help_smoke_exits_zero(self):
        import os

        from mempalace import cli

        with patch.dict("os.environ", dict(os.environ)):
            with patch("sys.argv", ["mempalace", "diary", "agents", "--help"]):
                with pytest.raises(SystemExit) as exc:
                    cli.main()

        assert exc.value.code == 0

    def test_dispatch_probe_reaches_the_tool(self, capsys):
        import os

        from mempalace import cli

        strict, call, fake = _daemon(_AGENTS_OK)
        with patch.dict("os.environ", dict(os.environ)), strict, call:
            with patch("sys.argv", ["mempalace", "diary", "agents", "--wing", "2g"]):
                cli.main()

        assert fake.call_args[0][0] == "mempalace_diary_agents"
        assert "claude-code" in capsys.readouterr().out


class TestDiaryActionDispatch:
    """`cmd_diary` had NO sub-dispatch: `write` returned and EVERYTHING else
    fell through to read. A verb registered in argparse but unhandled would
    therefore run a read, print a plausible listing and exit 0 — passing any
    check that only looks for a crash. Measured before the fix: action
    "bogus-verb" called mempalace_diary_read and exited 0.
    """

    def test_unknown_action_exits_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_NO_AGENT_OK)
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(diary_action="bogus-verb", agent=None))

        assert exc.value.code == 2
        assert "choose an action" in capsys.readouterr().err

    def test_unknown_action_does_not_silently_read(self):
        """The exit code alone is not the point — it must not have READ."""
        from mempalace import cli

        strict, call, fake = _daemon(_READ_NO_AGENT_OK)
        with strict, call:
            with pytest.raises(SystemExit):
                cli.cmd_diary(_read_args(diary_action="bogus-verb", agent=None))

        fake.assert_not_called()

    def test_missing_action_exits_2(self, capsys):
        from mempalace import cli

        strict, call, fake = _daemon(_READ_NO_AGENT_OK)
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(diary_action=None, agent=None))

        assert exc.value.code == 2
        fake.assert_not_called()

    def test_unknown_action_under_json_emits_a_json_error(self, capsys):
        """`_fail_client` rather than a bare print: a --json caller must get a
        document, not prose on stderr."""
        from mempalace import cli

        strict, call, fake = _daemon(_READ_NO_AGENT_OK)
        with strict, call:
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(diary_action="bogus-verb", agent=None, json=True))

        assert exc.value.code == 2
        assert json.loads(capsys.readouterr().out)["source"] == "cli"
        fake.assert_not_called()

    def test_each_known_action_still_dispatches(self):
        """The guard must not have swallowed the real verbs."""
        from mempalace import cli

        for args, payload, tool in (
            (_read_args(agent="morpheus"), _READ_OK, "mempalace_diary_read"),
            (_agents_args(), _AGENTS_OK, "mempalace_diary_agents"),
            (_write_args(), _WRITE_OK, "mempalace_diary_write"),
        ):
            strict, call, fake = _daemon(payload)
            with strict, call:
                cli.cmd_diary(args)
            assert fake.call_args[0][0] == tool, args.diary_action


class TestDiaryReadNoResultsExitCode:
    def test_read_with_no_entries_exits_1(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(dict(_READ_NO_AGENT_OK, entries=[], total=0, showing=0))
        with strict, call, patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(agent=None, wing="2g"))

        assert exc.value.code == 1
        assert "No diary entries" in capsys.readouterr().out

    def test_read_filtered_to_nothing_exits_1(self, capsys):
        """`--since` that excludes everything is still "ran, selected nothing"."""
        from mempalace import cli

        strict, call, _ = _daemon(_READ_NO_AGENT_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(agent=None, wing="2g", since="2099-01-01"))

        assert exc.value.code == 1

    def test_read_with_entries_exits_0(self):
        """The mirror — a non-empty read must NOT exit non-zero."""
        from mempalace import cli

        strict, call, _ = _daemon(_READ_NO_AGENT_OK)
        with strict, call, patch.dict("os.environ", {}, clear=True):
            cli.cmd_diary(_read_args(agent=None, wing="2g"))

    def test_json_empty_read_still_emits_then_exits_1(self, capsys):
        from mempalace import cli

        strict, call, _ = _daemon(dict(_READ_NO_AGENT_OK, entries=[], total=0, showing=0))
        with strict, call, patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.cmd_diary(_read_args(agent=None, wing="2g", json=True))

        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["entries"] == []
