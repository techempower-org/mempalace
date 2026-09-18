"""`mempalace window` and `mempalace source` (#500, #502).

Both are daemon-strict read verbs over palace-daemon ≥ 1.10.0's
``/window`` and ``/source``. The exit codes come from `cli.py`'s standing
contract (0 success, 1 no results, 2 palace unavailable, 64 bad args),
resolved for these verbs as:

=========================================  ====
daemon lacks the route (404)               2
bad ISO bound / unknown room / bad cursor  64   (the daemon's own message)
auth rejected (401/403)                    2    NOT the "deploy" message
transport failure / timeout / 5xx          2
window matched no drawers                  1
=========================================  ====

The 401/403 row is the one worth spelling out. The existing
``_get_daemon_rest`` collapses 404, 401 and 403 all into ``None``, so a
key mismatch would be reported as "deploy a newer daemon" — a refusal
naming the wrong reason, which `cli.py`'s own helper docstring calls out
as a defect family. These verbs therefore read the status code.

Wiring is checked at all three layers, because a ``--help`` smoke exits
before the dispatch table is read and cannot see a deleted dispatch entry
(Oracle PART 22 on #485). Each probe is verified to fail alone.
"""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from mempalace import cli


# ---------------------------------------------------------------------------
# Layer 1+2: the verb is wired — parser, function, dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["window", "source"])
def test_help_smoke_exits_zero(verb, capsys):
    """Layer 1+2: the subparser exists and its help renders.

    ``cli.main()`` reads ``sys.argv`` rather than taking argv, so the smoke
    patches it — matching the established pattern in
    test_cli_mine_single_file.py and test_cli_tunnels_rebuild.py.
    """
    with patch("sys.argv", ["mempalace", verb, "--help"]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert verb in out


@pytest.mark.parametrize("verb", ["window", "source"])
def test_the_command_function_exists(verb):
    assert callable(getattr(cli, f"cmd_{verb.replace('-', '_')}"))


@pytest.mark.parametrize("verb", ["window", "source"])
def test_the_dispatch_entry_exists(verb):
    """Layer 3, which the --help smoke structurally cannot reach.

    Read out of the source rather than by running main(), because running
    it would need a daemon. A deleted dispatch line fails here while the
    help smoke above still passes — the two probes fail independently.
    """
    import inspect

    src = inspect.getsource(cli.main)
    assert f'"{verb}": cmd_{verb}' in src, f"{verb} is not in the dispatch table"


def test_the_top_level_help_lists_both_verbs(capsys):
    """`--help` is the discovery surface; an unlisted verb is undiscoverable."""
    with patch("sys.argv", ["mempalace", "--help"]):
        with pytest.raises(SystemExit):
            cli.main()
    out = capsys.readouterr().out
    assert "window" in out
    assert "source" in out


@pytest.mark.parametrize(
    "verb,needle",
    [("window", "unranked"), ("source", "chunk")],
)
def test_each_help_says_what_the_verb_is_for(verb, needle, capsys):
    """ "window" alone means nothing; the help has to say why it exists."""
    with patch("sys.argv", ["mempalace", verb, "--help"]):
        with pytest.raises(SystemExit):
            cli.main()
    assert needle in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def _args(**kw):
    import argparse

    base = dict(
        wing="w",
        room=None,
        source_file=None,
        since=None,
        before=None,
        limit=100,
        cursor=None,
        json=False,
        file=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture()
def daemon(monkeypatch):
    """Point the CLI at a daemon and script one HTTP answer."""
    monkeypatch.setenv("PALACE_DAEMON_URL", "http://127.0.0.1:9")

    def _install(code, payload=None, detail=None, raise_transport=False):
        def fake(path, params=None):
            if raise_transport:
                raise cli.DaemonError("daemon unreachable at http://127.0.0.1:9: refused")
            return cli._DaemonHTTPResult(code=code, payload=payload, detail=detail or "")

        monkeypatch.setattr(cli, "_window_daemon_get", fake)

    return _install


_ONE = {
    "drawers": [
        {
            "drawer_id": "d1",
            "wing": "w",
            "room": "diary",
            "filed_at": "2026-09-02T10:00:00.000000",
            "content_preview": "hello",
            "metadata": {"wing": "w"},
        }
    ],
    "count": 1,
    "next_cursor": None,
    "ordering": "wallclock",
    "excluded_no_filed_at": 0,
}


def _run(fn, args):
    with pytest.raises(SystemExit) as exc:
        fn(args)
    return exc.value.code


def test_success_exits_zero(daemon, capsys):
    daemon(200, _ONE)
    assert _run(cli.cmd_window, _args()) == 0
    assert "d1" in capsys.readouterr().out


def test_an_empty_window_exits_1_not_0(daemon, capsys):
    """Contract: 1 means "ran and selected nothing". A window with no
    drawers is a real answer, not a failure and not a success."""
    daemon(200, {"drawers": [], "count": 0, "excluded_no_filed_at": 0})
    assert _run(cli.cmd_window, _args(since="2026-09-01")) == 1


def test_a_missing_route_exits_2_and_names_the_version(daemon, capsys):
    daemon(404)
    assert _run(cli.cmd_window, _args()) == 2
    err = capsys.readouterr().err
    assert "/window" in err
    assert "1.10.0" in err, f"the message must name the version that ships it: {err!r}"
    assert "deploy" in err.lower()


def test_a_bad_bound_exits_64_with_the_daemons_message(daemon, capsys):
    """64 is "bad args", and the daemon is the thing that knows why."""
    daemon(400, detail="since must be an ISO date string, got 'yesterday'")
    assert _run(cli.cmd_window, _args(since="yesterday")) == 64
    assert "ISO date string" in capsys.readouterr().err


def test_a_422_also_exits_64(daemon):
    daemon(422, detail="room 'nope' is not a canonical room")
    assert _run(cli.cmd_window, _args(room="nope")) == 64


def test_a_bad_cursor_exits_64(daemon, capsys):
    daemon(400, detail="cursor is not a valid token: 'garbage'")
    assert _run(cli.cmd_window, _args(cursor="garbage")) == 64
    assert "cursor" in capsys.readouterr().err


def test_an_auth_failure_is_2_but_NOT_the_deploy_message(daemon, capsys):
    """The distinction the shared helper cannot express.

    ``_get_daemon_rest`` returns None for 404, 401 AND 403 alike, so an
    auth mismatch would be reported as "deploy a newer daemon" — sending
    the reader after entirely the wrong problem.
    """
    # The scripted detail is deliberately neutral. An earlier version used
    # "invalid api key", and the assertion below passed on that word coming
    # straight from the mock — so the test survived a mutation that routed
    # 401 into the generic branch entirely. The mock was asserting the field
    # that happened to be right.
    daemon(401, detail="nope")
    assert _run(cli.cmd_window, _args()) == 2
    err = capsys.readouterr().err
    assert "1.10.0" not in err, f"an auth failure must not blame the version: {err!r}"
    assert "PALACE_API_KEY" in err, (
        f"the 401 branch must name the credential env var itself, not rely on "
        f"the daemon's detail text: {err!r}"
    )


def test_a_403_is_reported_the_same_way_as_a_401(daemon, capsys):
    daemon(403, detail="nope")
    assert _run(cli.cmd_window, _args()) == 2
    err = capsys.readouterr().err
    assert "PALACE_API_KEY" in err
    assert "1.10.0" not in err


def test_a_generic_5xx_does_NOT_claim_an_auth_problem(daemon, capsys):
    """The mirror of the test above: the branches must be distinguishable
    in both directions, or one of them is untested."""
    daemon(503, detail="nope")
    assert _run(cli.cmd_window, _args()) == 2
    err = capsys.readouterr().err
    assert "PALACE_API_KEY" not in err
    assert "1.10.0" not in err


def test_a_server_error_exits_2(daemon):
    daemon(500, detail="psycopg OperationalError")
    assert _run(cli.cmd_window, _args()) == 2


def test_a_transport_failure_exits_2(daemon):
    daemon(0, raise_transport=True)
    assert _run(cli.cmd_window, _args()) == 2


def test_without_a_daemon_url_it_refuses_with_2(monkeypatch, capsys):
    """Daemon-strict: these verbs have no local path at all."""
    monkeypatch.delenv("PALACE_DAEMON_URL", raising=False)
    assert _run(cli.cmd_window, _args()) == 2
    assert "PALACE_DAEMON_URL" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def test_json_format_emits_the_daemon_envelope(daemon, capsys):
    daemon(200, _ONE)
    assert _run(cli.cmd_window, _args(json=True)) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["drawers"][0]["drawer_id"] == "d1"
    assert body["ordering"] == "wallclock"


def test_the_excluded_count_is_surfaced_to_the_user(daemon, capsys):
    """The daemon reports it; a CLI that swallowed it would re-hide the
    2,613 drawers a bound cannot reach."""
    daemon(200, dict(_ONE, excluded_no_filed_at=2613))
    _run(cli.cmd_window, _args(since="2026-09-01"))
    assert "2613" in capsys.readouterr().out.replace(",", "")


def test_the_ordering_is_surfaced_so_the_answer_is_self_describing(daemon, capsys):
    daemon(200, _ONE)
    _run(cli.cmd_window, _args())
    assert "wallclock" in capsys.readouterr().out


def test_a_next_cursor_is_shown_so_the_walk_can_continue(daemon, capsys):
    daemon(200, dict(_ONE, next_cursor="abc123"))
    _run(cli.cmd_window, _args())
    assert "abc123" in capsys.readouterr().out


def test_no_next_cursor_means_no_confusing_continuation_hint(daemon, capsys):
    daemon(200, _ONE)
    _run(cli.cmd_window, _args())
    assert "--cursor" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Parameters actually reach the daemon
# ---------------------------------------------------------------------------


def test_the_flags_are_passed_through_as_query_params(monkeypatch):
    monkeypatch.setenv("PALACE_DAEMON_URL", "http://127.0.0.1:9")
    seen = {}

    def fake(path, params=None):
        seen["path"] = path
        seen["params"] = dict(params or {})
        return cli._DaemonHTTPResult(code=200, payload=_ONE, detail="")

    monkeypatch.setattr(cli, "_window_daemon_get", fake)
    _run(
        cli.cmd_window,
        _args(since="2026-09-01", before="2026-09-05", room="diary", limit=50, cursor="tok"),
    )

    assert seen["path"] == "/window"
    assert seen["params"]["since"] == "2026-09-01"
    assert seen["params"]["before"] == "2026-09-05"
    assert seen["params"]["room"] == "diary"
    assert seen["params"]["limit"] == 50
    assert seen["params"]["cursor"] == "tok"


def test_absent_flags_are_not_sent_as_empty_strings(monkeypatch):
    """An empty `room=` is a filter for the empty room, not "no filter"."""
    monkeypatch.setenv("PALACE_DAEMON_URL", "http://127.0.0.1:9")
    seen = {}

    def fake(path, params=None):
        seen.update(params or {})
        return cli._DaemonHTTPResult(code=200, payload=_ONE, detail="")

    monkeypatch.setattr(cli, "_window_daemon_get", fake)
    _run(cli.cmd_window, _args())

    assert "room" not in seen
    assert "since" not in seen
    assert "cursor" not in seen


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------


_SRC = {
    "drawers": [
        {
            "drawer_id": "c0",
            "wing": "w",
            "room": "sessions",
            "filed_at": "2026-09-02T10:00:00",
            "content_preview": "chunk zero",
            "metadata": {"chunk_index": "0"},
        },
    ],
    "count": 1,
    "source_file": "t.jsonl",
    "wing": "w",
}


def test_source_success(daemon, capsys):
    daemon(200, _SRC)
    assert _run(cli.cmd_source, _args(file="t.jsonl")) == 0
    assert "c0" in capsys.readouterr().out


def test_source_with_no_drawers_exits_1(daemon):
    daemon(200, {"drawers": [], "count": 0, "source_file": "t.jsonl"})
    assert _run(cli.cmd_source, _args(file="t.jsonl")) == 1


def test_source_missing_route_names_the_version(daemon, capsys):
    daemon(404)
    assert _run(cli.cmd_source, _args(file="t.jsonl")) == 2
    err = capsys.readouterr().err
    assert "/source" in err and "1.10.0" in err


def test_source_requires_a_file(monkeypatch, capsys):
    monkeypatch.setenv("PALACE_DAEMON_URL", "http://127.0.0.1:9")
    assert _run(cli.cmd_source, _args(file=None)) == 64


def test_source_passes_the_basename_through_unchanged(monkeypatch):
    """The daemon matches basename OR full path; the CLI must not 'help'."""
    monkeypatch.setenv("PALACE_DAEMON_URL", "http://127.0.0.1:9")
    seen = {}

    def fake(path, params=None):
        seen.update(params or {})
        return cli._DaemonHTTPResult(code=200, payload=_SRC, detail="")

    monkeypatch.setattr(cli, "_window_daemon_get", fake)
    _run(cli.cmd_source, _args(file="/abs/path/t.jsonl"))
    assert seen["source_file"] == "/abs/path/t.jsonl"


# ---------------------------------------------------------------------------
# The real binary, against a daemon that is not there
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expect_in_err",
    [
        (["window", "--wing", "w"], "unreachable"),
        (["source", "--file", "t.jsonl"], "unreachable"),
    ],
)
def test_the_real_cli_refuses_against_a_closed_port(argv, expect_in_err, tmp_path):
    """One probe of the actual binary, per COMMON-DRAIN's CLI-probe rule.

    Mocks assert the field that happens to be right; a real invocation
    catches a refusal that names the wrong reason. Port 9 (discard) is
    closed, so this exercises the transport path end to end.
    """
    env = dict(os.environ)
    env["PALACE_DAEMON_URL"] = "http://127.0.0.1:9"
    env["PALACE_MCP_TIMEOUT"] = "5"
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    proc = subprocess.run(
        [sys.executable, "-m", "mempalace.cli", *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 2, f"rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}"
    assert expect_in_err in (proc.stdout + proc.stderr).lower()
