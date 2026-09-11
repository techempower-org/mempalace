"""``mempalace mine <file> --mode projects`` at the CLI boundary (#451).

Three things the command line owns, which the miner itself cannot:

1. A ``.jsonl`` transcript handed to projects mode is a mode mistake, not a
   mine. Projects mode would chunk the raw JSON as prose; the user wants
   ``--mode convos``. Today it is silently a no-op (``scan_project`` walks a
   file and finds nothing), which is the worst of both. Say so, exit 2.
2. In daemon-strict mode the wing is derived locally before the POST. For a
   directory that is the directory's name; for a FILE it must be the
   project's name, or ``/home/jp/Projects/2g/CLAUDE.md`` would be filed into
   a wing called ``claude_md`` instead of ``2g``.
3. The path is passed through to the daemon unchanged, as a path — the
   daemon-side ``/mine`` support is palace-daemon#252.

Run with::

    cd <worktree>
    PYTHONPATH=$PWD /home/jp/Projects/memorypalace/.venv/bin/python \
        -m pytest tests/test_cli_mine_single_file.py -q
"""

import argparse
from unittest.mock import patch

import pytest

from mempalace.cli import cmd_mine


def _mine_args(directory, **overrides):
    defaults = dict(
        command="mine",
        dir=str(directory),
        source=None,
        mode="projects",
        wing=None,
        palace=None,
        dry_run=False,
        agent="mempalace",
        limit=0,
        no_gitignore=False,
        include_ignored=[],
        redetect_origin=False,
        extract="exchange",
        max_chunks_per_file=None,
        workers=1,
        background=False,
        daemon=False,
        json=False,
        quiet=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _project_with_file(tmp_path, name="CLAUDE.md"):
    root = tmp_path / "2g"
    (root / ".git").mkdir(parents=True)
    target = root / name
    target.write_text("# doc\n", encoding="utf-8")
    return root, target


# ---------------------------------------------------------------------------
# 1. .jsonl in projects mode is a mode mistake, said out loud
# ---------------------------------------------------------------------------


def test_jsonl_file_in_projects_mode_is_an_explicit_error(tmp_path, capsys):
    root, target = _project_with_file(tmp_path, "session.jsonl")

    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        pytest.raises(SystemExit) as exc,
    ):
        cmd_mine(_mine_args(target))

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--mode convos" in err, "the error must name the mode that does work"


def test_jsonl_directory_name_is_not_mistaken_for_a_transcript(tmp_path):
    """A DIRECTORY whose name ends in .jsonl is still a directory mine."""
    weird = tmp_path / "exports.jsonl"
    weird.mkdir()
    (weird / "notes.md").write_text("# notes\n", encoding="utf-8")

    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.miner.mine") as mine_mock,
    ):
        cmd_mine(_mine_args(weird))

    assert mine_mock.called


def test_jsonl_file_with_mode_convos_is_untouched(tmp_path):
    _root, target = _project_with_file(tmp_path, "session.jsonl")

    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.convo_miner.mine_convos") as convos_mock,
    ):
        cmd_mine(_mine_args(target, mode="convos"))

    assert convos_mock.called


# ---------------------------------------------------------------------------
# 2. + 3. daemon-strict: path through, wing from the project
# ---------------------------------------------------------------------------


def test_daemon_strict_single_file_posts_the_file_path(tmp_path):
    _root, target = _project_with_file(tmp_path)

    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True) as post,
        pytest.raises(SystemExit) as exc,
    ):
        cmd_mine(_mine_args(target))

    assert exc.value.code == 0
    assert post.call_args.args[0] == str(target.resolve())


def test_daemon_strict_single_file_wing_comes_from_the_project_not_the_filename(tmp_path):
    _root, target = _project_with_file(tmp_path)

    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True) as post,
        pytest.raises(SystemExit),
    ):
        cmd_mine(_mine_args(target))

    assert post.call_args.kwargs["wing"] == "2g", (
        "a file named CLAUDE.md in ~/Projects/2g belongs to wing '2g'; deriving "
        "the wing from the file's own name would file it under 'claude_md'"
    )


def test_daemon_strict_explicit_wing_still_wins(tmp_path):
    _root, target = _project_with_file(tmp_path)

    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True) as post,
        pytest.raises(SystemExit),
    ):
        cmd_mine(_mine_args(target, wing="explicit"))

    assert post.call_args.kwargs["wing"] == "explicit"


def test_daemon_strict_refuses_to_guess_a_wing_for_a_path_it_cannot_see(tmp_path, capsys):
    """The daemon mines ITS host's copy; `is_file()` here answers about ours.

    A path that exists on the daemon host and not on this one makes `is_file()`
    False, and the historic dirname fallback would then derive the wing from
    the FILE's name: measured, `~/Projects/2g/CLAUDE.md` lands in a wing called
    `claude.md` (the dot survives `normalize_wing_name`). A file and a directory
    derive different wings and an absent path is indistinguishable between them,
    so refuse and say so.
    """
    absent = tmp_path / "2g" / "CLAUDE.md"  # never created

    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True) as post,
        pytest.raises(SystemExit) as exc,
    ):
        cmd_mine(_mine_args(absent))

    assert exc.value.code == 2
    assert post.call_args_list == [], "must not POST a mine whose wing we had to guess"
    err = capsys.readouterr().err
    assert "--wing" in err, "the error must name the flag that resolves it"


def test_daemon_strict_unseen_path_is_fine_when_wing_is_explicit(tmp_path):
    """--wing removes the ambiguity, so the mine proceeds: path-mapped and
    synced-only paths (PALACE_DAEMON_PATH_MAP) keep working."""
    absent = tmp_path / "2g" / "CLAUDE.md"

    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True) as post,
        pytest.raises(SystemExit) as exc,
    ):
        cmd_mine(_mine_args(absent, wing="2g"))

    assert exc.value.code == 0
    assert post.call_args.kwargs["wing"] == "2g"
    assert post.call_args.args[0] == str(absent)


def test_daemon_strict_unseen_directory_shaped_path_still_routes():
    """A remote-only DIRECTORY path must keep working without --wing.

    The daemon can mine paths this machine cannot see (synced, or remapped by
    PALACE_DAEMON_PATH_MAP), and `tests/test_cli_daemon.py::TestCmdMineDaemon::
    test_routes_projects_mode_to_daemon` has pinned that contract since before
    this feature existed: `/home/u/proj`, no --wing, exit 0.

    That is why the refusal above is scoped to paths carrying a DOCUMENT
    SUFFIX rather than to every unresolvable path. For a directory the
    historic rule (basename) is correct whether or not we can see it; only a
    FILE derives its wing from somewhere else, and only a file therefore
    produces a junk wing when we guess wrong.
    """
    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True) as post,
        pytest.raises(SystemExit) as exc,
    ):
        cmd_mine(_mine_args("/home/u/proj-that-does-not-exist-here"))

    assert exc.value.code == 0
    assert post.call_args.kwargs["wing"] == "proj_that_does_not_exist_here"


def test_daemon_strict_directory_wing_is_unchanged(tmp_path):
    """Regression guard: directory mines keep deriving the wing from the dir."""
    root, _target = _project_with_file(tmp_path)

    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True) as post,
        pytest.raises(SystemExit),
    ):
        cmd_mine(_mine_args(root))

    assert post.call_args.args[0] == str(root.resolve())
    assert post.call_args.kwargs["wing"] == "2g"


# ---------------------------------------------------------------------------
# local (non-daemon) path: the file reaches the miner as-is
# ---------------------------------------------------------------------------


def test_local_mine_passes_the_file_through_to_the_miner(tmp_path):
    _root, target = _project_with_file(tmp_path)

    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.miner.mine") as mine_mock,
    ):
        cmd_mine(_mine_args(target))

    assert mine_mock.call_args.kwargs["project_dir"] == str(target)


def test_mine_help_mentions_single_file_projects_mode():
    """The positional's help must stop implying a file only works with convos."""
    import contextlib
    import io

    from mempalace import cli

    buf = io.StringIO()
    with patch("sys.argv", ["mempalace", "mine", "--help"]):
        with contextlib.redirect_stdout(buf):
            with pytest.raises(SystemExit):
                cli.main()

    help_text = " ".join(buf.getvalue().split())
    assert "one file" in help_text
    assert "projects mode" in help_text
    # Single-file projects mode needs a daemon carrying palace-daemon#258;
    # an older one answers 400 and the CLI exits 1. Help must not promise
    # more than the deployed daemon can do.
    assert "palace-daemon#258" in help_text
