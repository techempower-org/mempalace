"""One daemon-unreachable message, and no call site reports less than it did (#476).

The string ``palace daemon unreachable at`` was written out by hand at 22 places.
They were never 22 copies of one block — there were four distinct messages:

    A  14x  "… — see mempalace status for diagnostics ({e})"
    B   5x  "… — see mempalace status for diagnostics"      no exception interpolated
    C   2x  "… — /cypher returned {status} (see mempalace status for diagnostics)"
    F   1x  cmd_wings, via _read_family_fail: a leading blank line, an "  ERROR: "
            prefix, and no "see mempalace status" clause at all

⭐ **B is not a degraded copy of A.** It is the ``data is None`` case —
``_call_daemon_rest`` returns None on 404/401/403 and *nothing is raised*, so there is
no exception to interpolate. Those five sites instead name the failing route in their
JSON payload (``{"error": "daemon /list unavailable"}``), which is the only identifying
information a 404 leaves behind.

That correspondence — the five B prose sites ARE the five route-naming JSON sites — is
why the consolidated helper takes ``route=`` and ``**extra``. A helper that could not
express what the call sites already said would make the CLI report **less** than it did,
which is the defect this wave exists to remove, wearing the costume of a cleanup.

These tests pin the machine-facing half specifically, because a prose diff cannot see a
JSON field go missing: a script reading ``.status`` would simply start getting null.
"""

import argparse
import io
import json
import contextlib
from unittest.mock import patch

import pytest

from mempalace import cli


def _run_json(fn, args):
    """Call a cmd_* with --json, returning (exit_code, parsed_payload)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
        fn(args)
    return exc.value.code, json.loads(out.getvalue())


def _run_prose(fn, args):
    err = io.StringIO()
    with contextlib.redirect_stderr(err), pytest.raises(SystemExit) as exc:
        fn(args)
    return exc.value.code, err.getvalue()


# ── the helper reproduces all three shapes ─────────────────────────────


class TestHelperSpeaksEveryShapeItReplaced:
    def test_an_exception_site_is_unchanged(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
            cli._fail_daemon(RuntimeError("boom"), True)
        assert json.loads(out.getvalue()) == {"error": "boom", "source": "daemon"}

    def test_a_404_site_names_the_route_and_omits_the_parenthetical(self):
        """err=None is the 404 case: there is no exception to interpolate."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
            cli._fail_daemon(None, True, route="/list")
        assert json.loads(out.getvalue())["error"] == "daemon /list unavailable"

        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            cli._fail_daemon(None, False, route="/list")
        assert "(None)" not in err.getvalue(), "must not print a bogus (None)"
        assert err.getvalue().rstrip().endswith("see mempalace status for diagnostics")

    def test_extra_fields_survive(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
            cli._fail_daemon("daemon /cypher returned 502", True, status=502)
        payload = json.loads(out.getvalue())
        assert payload["status"] == 502, "a structured field must not be dropped"
        assert payload["error"] == "daemon /cypher returned 502"

    def test_a_server_side_error_still_says_rejected_not_unreachable(self):
        """The branch every routed site newly gains (#499 class)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
            cli._fail_daemon("daemon error -32001: timeout", False)
        assert "rejected the call" in err.getvalue()
        assert "unreachable" not in err.getvalue()


# ── the machine interface, per route ───────────────────────────────────


class TestEveryRouteStillNamesItselfInJson:
    """A prose diff cannot see these; only a JSON assertion can."""

    @pytest.mark.parametrize(
        "fn_name,args,expected",
        [
            ("cmd_list", {"wing": "2g", "room": None, "limit": 1}, "daemon /list unavailable"),
            ("cmd_graph", {"limit": 1}, "daemon /graph unavailable"),
            ("cmd_stats", {}, "daemon /stats unavailable"),
        ],
    )
    def test_a_404_fallthrough_names_its_route(self, fn_name, args, expected):
        defaults = {
            "json": True,
            "quiet": False,
            "palace": None,
            "format": "json",
            "wing": None,
            "room": None,
            "limit": 20,
            "tags": None,
            "since": None,
            "before": None,
            "cursor": None,
        }
        defaults.update(args)
        ns = argparse.Namespace(**defaults)
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            # conftest strips PALACE_DAEMON_URL and gives each run a temp HOME, so
            # without this `cmd_stats` exits 2 at its "stats requires the
            # palace-daemon" guard and never reaches the 404 branch under test.
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
            patch("mempalace.cli._call_daemon_rest", return_value=None),
        ):
            code, payload = _run_json(getattr(cli, fn_name), ns)
        assert code == 1
        assert payload["error"] == expected, (
            "the route name is the only identifying information a 404 leaves; "
            "losing it makes the JSON say less than it did"
        )
        assert payload["source"] == "daemon"

    def test_cmd_move_names_its_route(self):
        """Not a parametrize row: cmd_move needs a drawer_id, and its route
        string is `PATCH /memory` rather than a bare path."""
        ns = argparse.Namespace(
            drawer_id="drawer_abc123",
            wing="smol",
            room=None,
            json=True,
            quiet=False,
            palace=None,
            format="json",
        )
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
            # cmd_move goes through _patch_daemon_rest, not _call_daemon_rest —
            # patching the wrong seam let a real request through and the
            # assertion caught it.
            patch("mempalace.cli._patch_daemon_rest", return_value=None),
        ):
            code, payload = _run_json(cli.cmd_move, ns)
        assert code == 1
        assert payload["error"] == "daemon PATCH /memory unavailable"

    def test_gather_bulk_move_matches_names_its_route(self):
        """Not a cmd_* at all — it takes (wing, room, want_json) positionally,
        which is why it was the site a parametrised table could not reach."""
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
            patch("mempalace.cli._call_daemon_rest", return_value=None),
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exc:
                cli._gather_bulk_move_matches("2g", None, True)
        assert exc.value.code == 1
        assert json.loads(out.getvalue())["error"] == "daemon /list unavailable"


# ── the consolidation itself ───────────────────────────────────────────


class TestTheMessageIsWrittenOnce:
    def test_only_the_two_helpers_still_carry_the_literal(self):
        """22 hand-written occurrences → 2.

        Three writers remain, each for a stated reason:

        * `_fail_daemon` — the one that stays.
        * `_daemon_tool_or_fail` — keeps its own deliberately: it exits 1 for a
          transport failure and 2 for a JSON-RPC error from a daemon that
          answered, a distinction `_fail_daemon` does not yet make. Folding it in
          would have destroyed that; it converges in the exit-code PR, in the
          other direction.
        * `_fail_daemon_unavailable` (#512) — exists only because `_fail_daemon`
          exits 1 while /window and /source are specified at 2. Its own docstring
          says "if `_fail_daemon` ever does move to 2, delete this helper", which
          is the exit-code PR's job, not this one's.

        So 22 hand-written occurrences become 3 named ones, and the count is
        asserted rather than grepped so a new copy has to argue for itself here.
        """
        import inspect

        src = inspect.getsource(cli)
        assert src.count("palace daemon unreachable at") == 3

    def test_cmd_wings_no_longer_has_its_own_shape(self):
        """The one declared prose change (#476).

        Before: a leading blank line, "  ERROR: palace daemon unreachable at <url>
        (<e>)", and no diagnostics clause. After: the same line as the other 19.
        """
        ns = argparse.Namespace(
            json=False, quiet=False, palace=None, format="table", sort=None, limit=20
        )
        with (
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch(
                "mempalace.cli._call_daemon_rest",
                side_effect=cli.DaemonError("daemon unreachable at http://d:8085"),
            ),
            patch("mempalace.cli._daemon_url", return_value="http://d:8085"),
        ):
            code, err = _run_prose(cli.cmd_wings, ns)
        assert code == 1
        assert "ERROR:" not in err, "the ERROR: prefix was the drift"
        assert "see mempalace status for diagnostics" in err
