"""``mempalace mine --no-tunnels`` at the CLI boundary (#474).

The flag skips the post-mine derived-analytics block. Its name says "tunnels"
because that is what the operator is thinking about, but it necessarily covers
the within-wing hallway rebuild too -- entity tunnels are derived from the
hallways, so there is no meaningful "skip tunnels but rebuild hallways" state,
and the hallway step is where the big file load + rewrite lives. The help text
has to say so, or the flag under-promises what it turns off.

Run with::

    cd <worktree>
    PYTHONPATH=$PWD /home/jp/Projects/memorypalace/.venv/bin/python \
        -m pytest tests/test_cli_mine_no_tunnels.py -q
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
        no_tunnels=False,
        json=False,
        quiet=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.md").write_text("# doc\n", encoding="utf-8")
    return root


def test_no_tunnels_flag_skips_the_derived_block(project):
    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.miner.mine") as mine_mock,
    ):
        cmd_mine(_mine_args(project, no_tunnels=True))

    assert mine_mock.call_args.kwargs["compute_derived"] is False


def test_default_still_computes_the_derived_block(project):
    """Control: the default must not change -- a silent flip here would stop
    maintaining the cross-wing graph for every existing caller."""
    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.miner.mine") as mine_mock,
    ):
        cmd_mine(_mine_args(project))

    assert mine_mock.call_args.kwargs["compute_derived"] is True


def test_help_says_hallways_are_skipped_too():
    """The flag is named for tunnels but also skips the hallway rebuild."""
    import contextlib
    import io

    from mempalace import cli

    buf = io.StringIO()
    with patch("sys.argv", ["mempalace", "mine", "--help"]):
        with contextlib.redirect_stdout(buf):
            with pytest.raises(SystemExit):
                cli.main()

    help_text = " ".join(buf.getvalue().split())
    assert "--no-tunnels" in help_text
    assert "hallway" in help_text.lower(), (
        "the flag skips the hallway rebuild as well as the two tunnel steps; "
        "help that says only 'tunnels' under-promises what it turns off"
    )


def test_daemon_strict_warns_that_no_tunnels_is_not_forwarded_yet(tmp_path, capsys):
    """Until the daemon carries the `tunnels` body field (palace-daemon#262),
    a daemon-routed mine cannot honour --no-tunnels. Say so rather than
    accepting the flag and quietly doing the expensive thing anyway."""
    root = tmp_path / "proj"
    root.mkdir()

    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True),
        pytest.raises(SystemExit),
    ):
        cmd_mine(_mine_args(root, no_tunnels=True))

    err = capsys.readouterr().err
    assert "--no-tunnels" in err
    assert "daemon-strict" in err


def test_daemon_strict_is_quiet_when_the_flag_is_absent(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()

    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._post_daemon_mine_cli", return_value=True),
        pytest.raises(SystemExit),
    ):
        cmd_mine(_mine_args(root))

    assert "--no-tunnels" not in capsys.readouterr().err


def test_daemon_job_queue_warns_that_no_tunnels_is_not_forwarded(tmp_path, capsys):
    """`--daemon` submits to the local job-queue daemon, whose payload has no
    tunnels field — and that branch returns BEFORE the daemon-strict warning
    below it, so the flag was accepted and silently ignored."""
    root = tmp_path / "proj"
    root.mkdir()

    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.cli._submit_daemon_cli_job") as submit,
    ):
        cmd_mine(_mine_args(root, daemon=True, no_tunnels=True))

    assert submit.called, "control: the job must still be submitted"
    err = capsys.readouterr().err
    assert "--no-tunnels" in err
    assert "daemon" in err.lower()


def test_daemon_job_queue_is_quiet_without_the_flag(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()

    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.cli._submit_daemon_cli_job") as submit,
    ):
        cmd_mine(_mine_args(root, daemon=True))

    assert submit.called
    assert "--no-tunnels" not in capsys.readouterr().err
