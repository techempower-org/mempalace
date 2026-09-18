"""`main()` must propagate a handler's exit code (#508).

`main()` discarded every handler's return value, so a command that `return`ed
an exit code always exited 0. #509 worked around it in the drain path by
switching to `sys.exit`; the dispatcher itself still dropped the value, so the
trap was live for the next handler that returned one.

The guard is `isinstance(rc, int) and not isinstance(rc, bool)`, and both
halves were measured rather than assumed:

* `cmd_sync` returns a ``SyncReport``. ``sys.exit(<non-int>)`` exits **1**
  after printing ``repr()`` to stderr, so a bare ``sys.exit(dispatch[...](args))``
  would have turned a SUCCESSFUL ``mempalace sync`` into a failure.
* ``True`` is an ``int`` in Python, so a handler returning a truthy flag would
  exit 1. None does today — AST over the dispatch table finds exactly one
  handler returning non-None, and it is ``cmd_sync`` — and the bool guard keeps
  that from becoming a status by accident.
"""

import ast
import pathlib

import pytest

CLI = pathlib.Path(__file__).resolve().parents[1] / "mempalace" / "cli.py"


class TestPropagation:
    @pytest.mark.parametrize("rc,expected", [(0, 0), (1, 1), (64, 64), (2, 2)])
    def test_an_int_return_becomes_the_exit_status(self, monkeypatch, rc, expected):
        import sys

        from mempalace import cli

        monkeypatch.setattr(sys, "argv", ["mempalace", "status"])
        with monkeypatch.context() as m:
            m.setattr(cli, "cmd_status", lambda args: rc)
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == expected

    def test_none_return_does_not_exit(self, monkeypatch):
        """The overwhelmingly common case must be untouched."""
        import sys

        from mempalace import cli

        monkeypatch.setattr(sys, "argv", ["mempalace", "status"])
        with monkeypatch.context() as m:
            m.setattr(cli, "cmd_status", lambda args: None)
            cli.main()  # must simply return

    def test_a_non_int_return_does_not_become_a_failure(self, monkeypatch):
        """cmd_sync returns a SyncReport. `sys.exit(<non-int>)` exits 1 and
        prints repr() to stderr, so the naive one-liner would fail a
        successful sync. Measured, which is why the guard is isinstance."""
        import sys

        from mempalace import cli

        class SyncReport:
            def __repr__(self):
                return "SyncReport(added=3)"

        monkeypatch.setattr(sys, "argv", ["mempalace", "status"])
        with monkeypatch.context() as m:
            m.setattr(cli, "cmd_status", lambda args: SyncReport())
            cli.main()  # must not raise SystemExit

    def test_a_bool_return_does_not_become_an_exit_status(self, monkeypatch):
        """True is an int. A handler returning a flag must not exit 1."""
        import sys

        from mempalace import cli

        monkeypatch.setattr(sys, "argv", ["mempalace", "status"])
        with monkeypatch.context() as m:
            m.setattr(cli, "cmd_status", lambda args: True)
            cli.main()  # must not raise SystemExit


class TestTheScopeClaimStaysTrue:
    def test_cmd_sync_is_still_the_only_non_none_returner(self):
        """The guard's justification is that exactly one dispatch handler
        returns non-None. If a second appears, this test says so rather than
        letting the claim rot in a comment.

        Matches on "returns anything that is not None", not on literals — the
        issue's own caveat: its first sweep looked for `ast.Constant` and
        missed `return 0 if ... else 1`, which is an `IfExp`.
        """
        tree = ast.parse(CLI.read_text())
        dispatch = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "dispatch" for t in node.targets
            ):
                if isinstance(node.value, ast.Dict):
                    dispatch = {v.id for v in node.value.values if isinstance(v, ast.Name)}
        assert dispatch, "dispatch table not found"

        returners = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in dispatch:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and sub.value is not None:
                        if isinstance(sub.value, ast.Constant) and sub.value.value is None:
                            continue
                        returners.add(node.name)
        assert returners == {"cmd_sync"}, (
            f"the #508 guard assumes cmd_sync is the only non-None returner; now {returners}"
        )


class TestSyncSuccessStaysZero:
    """The specific regression the naive one-liner would have caused.

    `cmd_sync` is the only dispatch handler returning non-None, and it returns
    a `SyncReport`. `sys.exit(<non-int>)` exits **1** after printing repr() to
    stderr, so `sys.exit(dispatch[...](args))` would report a SUCCESSFUL sync
    as a failure. Asserted against `sync` by name rather than a stand-in, so
    the guard cannot be weakened without this going red.
    """

    def test_sync_returning_a_report_still_exits_0(self, monkeypatch, capsys):
        import sys

        from mempalace import cli

        class SyncReport:
            added = 3
            removed = 0

            def __repr__(self):
                return "SyncReport(added=3, removed=0)"

        seen = {}

        def fake_sync(args):
            seen["called"] = True
            return SyncReport()

        monkeypatch.setattr(sys, "argv", ["mempalace", "sync"])
        monkeypatch.setattr(cli, "cmd_sync", fake_sync)
        cli.main()  # must NOT raise SystemExit
        assert seen.get("called"), "the test did not reach cmd_sync"
        assert "SyncReport" not in capsys.readouterr().err, (
            "the report leaked to stderr — that is what sys.exit(<non-int>) does"
        )
