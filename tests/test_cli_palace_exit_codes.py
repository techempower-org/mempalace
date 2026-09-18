"""One exit-code contract for the commands that open the drawers (#485).

`purge`, `prune` and `mined` each grew their own resolve-backend →
gate-the-local-precheck → open sequence, across #418 and #459, one at a
time. The policy was the same every time; only the refusal messages and the
exit codes drifted apart:

    condition                 purge    prune    mined
    backend unresolvable        2        2        2
    no local database           0        0        2
    unreachable backend         1        1        2

The codes are not a matter of taste here — `cli.py` has carried a documented
contract since #44, revised by #523/#514:

    0  success
    1  no results — the operation RAN and selected nothing
    2  the operation could not run: palace unavailable, or a usage error
    64 the daemon answered and REJECTED a well-formed request (a 4xx)

Every refusal in that table is "could not run", so the answer is 2 for all
nine cells, and the two commands exiting 0 were reporting a refusal as a
success — the defect family #418 and #459 exist to remove, surviving in the
exit status after being fixed in the prose.

⚠️ The old wording of this block said `64 bad args (argparse default for parse
errors)`. argparse exits **2**, and nothing in `mempalace/` subclasses
`ArgumentParser` or overrides `error()`, so that row described a behaviour the
CLI never had (#514). `TestTheContractItself` below pins the real one.

This file is a TABLE rather than nine hand-written tests on purpose: the
failure being prevented is divergence, and a table makes adding a command
without adding it here the visible thing to do.
"""

import argparse
import ast
import inspect
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


PALACE_UNAVAILABLE = 2
NO_RESULTS = 1


def _postgres_palace_dir(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "dsn.env").write_text("MEMPALACE_POSTGRES_DSN=postgresql://x/y\n")
    return palace


def _purge(palace, **kw):
    from mempalace.cli import cmd_purge

    args = argparse.Namespace(
        palace=str(palace), wing="w", room=None, source_file=None, yes=True, **kw
    )
    return cmd_purge, args


def _prune(palace, **kw):
    from mempalace.cli import cmd_prune

    args = argparse.Namespace(
        palace=str(palace),
        stale_days=90,
        wing=None,
        room=None,
        confirm=False,
        json=False,
        quiet=False,
        **kw,
    )
    return cmd_prune, args


def _mined(palace, **kw):
    from mempalace.cli import cmd_mined

    args = argparse.Namespace(
        palace=str(palace), wing=None, limit=None, json=False, quiet=False, **kw
    )
    return cmd_mined, args


COMMANDS = [("purge", _purge), ("prune", _prune), ("mined", _mined)]


@pytest.fixture(autouse=True)
def _no_daemon_and_clean_env(monkeypatch):
    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.delenv("PALACE_DAEMON_URL", raising=False)
    with patch("mempalace.cli._daemon_strict", return_value=False):
        yield


@pytest.mark.parametrize("name,build", COMMANDS, ids=[n for n, _ in COMMANDS])
class TestPalaceUnavailableIsAlwaysTwo:
    """Every way of failing to reach the drawers exits 2, for all commands."""

    def test_missing_palace_directory(self, tmp_path, name, build):
        cmd, args = build(tmp_path / "nonexistent")
        with pytest.raises(SystemExit) as exc:
            cmd(args)
        assert exc.value.code == PALACE_UNAVAILABLE

    def test_directory_without_a_database(self, tmp_path, name, build):
        palace = tmp_path / "palace"
        palace.mkdir()
        cmd, args = build(palace)
        with pytest.raises(SystemExit) as exc:
            cmd(args)
        assert exc.value.code == PALACE_UNAVAILABLE

    def test_unreachable_backend(self, tmp_path, monkeypatch, name, build):
        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
        cmd, args = build(palace)
        with (
            patch(
                "mempalace.palace.get_collection",
                side_effect=RuntimeError("connection refused"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cmd(args)
        assert exc.value.code == PALACE_UNAVAILABLE

    def test_unknown_backend(self, tmp_path, monkeypatch, name, build):
        palace = _postgres_palace_dir(tmp_path)
        monkeypatch.setenv("MEMPALACE_BACKEND", "not-a-real-backend")
        cmd, args = build(palace)
        with pytest.raises(SystemExit) as exc:
            cmd(args)
        assert exc.value.code == PALACE_UNAVAILABLE

    def test_backend_mismatch(self, tmp_path, monkeypatch, name, build):
        from mempalace.palace import BackendMismatchError

        palace = _postgres_palace_dir(tmp_path)
        cmd, args = build(palace)
        with (
            patch(
                "mempalace.palace.resolve_backend_name",
                side_effect=BackendMismatchError("chroma artifacts, postgres selected"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            cmd(args)
        assert exc.value.code == PALACE_UNAVAILABLE

    def test_the_refusal_names_the_palace(self, tmp_path, capsys, name, build):
        """One message shape: whatever the command, the path is in it."""
        palace = tmp_path / "palace"
        palace.mkdir()
        cmd, args = build(palace)
        with pytest.raises(SystemExit):
            cmd(args)
        out = capsys.readouterr()
        assert str(palace) in (out.out + out.err)


class TestEachCommandIsStillWiredUp:
    """The table above proves the POLICY; this proves the commands exist.

    Every test in this file calls `cmd_purge` / `cmd_prune` / `cmd_mined`
    directly, so deleting an `add_parser(...)` or a dispatch entry would
    leave all of them green while the CLI lost the command entirely. Review
    caught that gap (#485): an import catches a deleted function, nothing
    here caught a deleted wiring.

    Two halves, because one probe cannot see both: `--help` exercises parser
    registration and exits BEFORE dispatch, so the dispatch table needs its
    own assertion.
    """

    @pytest.mark.parametrize("name", ["purge", "prune", "mined"])
    def test_the_subcommand_is_registered_with_the_parser(self, name, capsys):
        from mempalace import cli

        with patch.object(sys, "argv", ["mempalace", name, "--help"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()

        # argparse exits 0 for --help and 2 for an unknown subcommand, so a
        # deleted add_parser shows up here as the wrong code.
        assert exc.value.code == 0, f"{name} is not a registered subcommand"
        assert name in capsys.readouterr().out

    @pytest.mark.parametrize("name", ["purge", "prune", "mined"])
    def test_the_subcommand_has_a_dispatch_entry(self, name):
        """`--help` cannot see this: it exits before `dispatch[...]` is read."""
        from mempalace import cli

        source = inspect.getsource(cli.main)
        # Match the handler NAME and its trailing comma, not the `cmd_`
        # prefix: `"purge": cmd_prune,` satisfied the looser form, so the
        # probe would have passed a command wired to the wrong handler —
        # which is worse than a missing entry, because the CLI still works
        # and does the wrong thing (review catch on #485).
        assert f'"{name}": cmd_{name},' in source, (
            f"{name} has no dispatch entry bound to cmd_{name} in main()"
        )


class TestNoResultsIsOneNotTwo:
    """`1` keeps meaning "the operation ran and selected nothing"."""

    def _chroma_palace(self, tmp_path):
        palace = tmp_path / "palace"
        palace.mkdir()
        (palace / "chroma.sqlite3").write_bytes(b"SQLite format 3\x00")
        return palace

    def test_purge_matching_nothing_exits_one(self, tmp_path):
        from mempalace.cli import cmd_purge

        palace = self._chroma_palace(tmp_path)
        col = MagicMock()
        col.get.return_value = {"ids": []}
        args = argparse.Namespace(
            palace=str(palace), wing="w", room=None, source_file=None, yes=True
        )
        with (
            patch("mempalace.palace.get_collection", return_value=col),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_purge(args)
        assert exc.value.code == NO_RESULTS, "a reachable palace that matched nothing"

    def test_an_operation_failure_is_unavailable_not_no_results(self, tmp_path):
        """A delete that errored did not 'select nothing' — it failed."""
        from mempalace.cli import cmd_purge

        palace = self._chroma_palace(tmp_path)
        col = MagicMock()
        col.get.return_value = {"ids": ["d1"]}
        col.delete.side_effect = RuntimeError("write refused")
        args = argparse.Namespace(
            palace=str(palace), wing="w", room=None, source_file=None, yes=True
        )
        with (
            patch("mempalace.palace.get_collection", return_value=col),
            pytest.raises(SystemExit) as exc,
        ):
            cmd_purge(args)
        assert exc.value.code == PALACE_UNAVAILABLE


class TestJsonRefusalsShareOneShape:
    """`--json` callers get the same envelope from every command that has it."""

    @pytest.mark.parametrize(
        "name,build", [("prune", _prune), ("mined", _mined)], ids=["prune", "mined"]
    )
    def test_envelope_is_palace_unavailable_with_path_and_backend(
        self, tmp_path, capsys, name, build
    ):
        palace = tmp_path / "palace"
        palace.mkdir()
        cmd, args = build(palace)
        args.json = True
        with pytest.raises(SystemExit) as exc:
            cmd(args)
        assert exc.value.code == PALACE_UNAVAILABLE
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"] == "palace_unavailable"
        assert payload["palace_path"] == os.path.abspath(str(palace))
        assert "backend" in payload


class TestTheContractItself:
    """The four codes, one test each, and the boundary that matters most.

    Added with #523/#514. The previous contract text claimed 64 was argparse's
    parse-error code; it is 2, and no test had ever checked, which is how a
    false sentence sat in the header of the file that defines the contract.
    """

    def test_argparse_parse_errors_exit_2_not_64(self):
        """#514: the header said 64 for years. Nothing ever asserted it."""
        from mempalace import cli

        with patch.object(sys, "argv", ["mempalace", "--no-such-flag"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 2

    def test_no_argumentparser_subclass_overrides_error(self):
        """The reason argparse's 2 reaches the shell unmodified.

        If some future change subclasses ArgumentParser and overrides error(),
        the row above silently stops being true — so assert the absence.
        """
        import mempalace.cli as cli_mod

        tree = ast.parse(inspect.getsource(cli_mod))
        subclasses = [
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
            and any(getattr(b, "attr", getattr(b, "id", None)) == "ArgumentParser" for b in n.bases)
        ]
        assert subclasses == [], f"ArgumentParser subclassed by {subclasses}"

    def test_a_verb_group_without_an_action_exits_2(self):
        """A usage error, not a daemon rejection — 2, and never 64."""
        from mempalace import cli

        args = argparse.Namespace(pending_action=None, json=False)
        with pytest.raises(SystemExit) as exc:
            cli.cmd_pending(args)
        assert exc.value.code == 2

    def test_that_guard_emits_a_json_document_for_json_callers(self, capsys):
        """A hand-rolled `print(..., file=sys.stderr)` gives a --json caller
        prose and no document at all — the wave's recurring shape, a report
        that disagrees with the promised contract. `_fail_client` fixes it."""
        from mempalace import cli

        args = argparse.Namespace(pending_action=None, json=True)
        with pytest.raises(SystemExit) as exc:
            cli.cmd_pending(args)
        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["source"] == "cli"
        assert "drain" in payload["error"]

    def test_that_guard_never_reaches_the_drain(self):
        """The exit code alone would pass even if it had already posted half
        the queue. What must be true is that nothing was drained."""
        from mempalace import cli

        args = argparse.Namespace(pending_action=None, json=False)
        with patch("mempalace.cli.cmd_pending_drain") as drain:
            with pytest.raises(SystemExit):
                cli.cmd_pending(args)
        drain.assert_not_called()

    def test_a_daemon_refusal_is_64_and_nothing_else_is(self):
        """64 means exactly one thing: the daemon answered and refused."""
        from mempalace import cli

        err = cli.DaemonRequestError("room 'diary' is not in the canonical set", status=400)
        with pytest.raises(SystemExit) as exc:
            cli._exit_daemon_request_error(err, want_json=False)
        assert exc.value.code == 64

    def test_an_outage_is_2_not_1(self):
        """The load-bearing line: 1 means it ran and found nothing."""
        from mempalace import cli

        with patch("mempalace.cli._daemon_url", return_value="http://d:8085"):
            with pytest.raises(SystemExit) as exc:
                cli._fail_daemon(cli.DaemonError("daemon unreachable at http://d:8085"), False)
        assert exc.value.code == 2

    def test_the_header_block_documents_exactly_these_codes(self):
        """Docs-vs-code: the contract is stated in a comment, and a comment
        cannot be executed. This is the only thing that keeps it honest."""
        import mempalace.cli as cli_mod

        header = inspect.getsource(cli_mod)[:6000]
        assert "1  no results" in header
        assert "2  the operation could not run" in header
        assert "64 the daemon answered and REJECTED" in header
        assert "argparse default for parse errors" not in header, (
            "the #514 falsehood must not come back"
        )
