"""``mempalace mine --wait-lock SECONDS``: a scheduled mine waits for the palace lock.

Without it, a mine that finds another mine holding the per-palace lock exits 1 at
once ("palace … is held by PID …"). A scheduled mine that shares the palace with
hook-spawned mines then loses every race and only retries on its unit's restart
timer. ``--wait-lock`` retries in-process for a bounded budget; 0 (the default)
keeps the fail-fast behaviour.

The contention here is real: a child process holds ``mine_palace_lock`` (an OS
flock) for the same palace, and the mine under test takes the same lock.
"""

import argparse
import os
import subprocess
import sys
import textwrap
import time
from unittest.mock import patch

import pytest

from mempalace.cli import cmd_mine

_REAL_SLEEP = time.sleep


def _args(directory, palace, **overrides):
    defaults = dict(
        command="mine",
        dir=str(directory),
        source=None,
        mode="projects",
        wing=None,
        palace=str(palace),
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
        wait_lock=0,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Lock files live under ~/.mempalace/locks: isolate HOME for parent and child."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.md").write_text("# doc\n", encoding="utf-8")
    return root


def _hold_lock(palace, home, seconds):
    """Start a child that holds the palace lock for ``seconds``; return once it holds it."""
    code = textwrap.dedent(
        f"""
        import sys, time
        from mempalace.palace import mine_palace_lock
        with mine_palace_lock({str(palace)!r}):
            print("held", flush=True)
            time.sleep({seconds})
        """
    )
    env = dict(os.environ, HOME=str(home))
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, env=env, text=True
    )
    assert child.stdout.readline().strip() == "held"
    return child


def _mine_taking_the_lock(**kwargs):
    """Stand-in for miner.mine that takes the real palace lock, as mine() does."""
    from mempalace.palace import mine_palace_lock

    with mine_palace_lock(kwargs["palace_path"]):
        return None


def _fast_sleep(seconds):
    _REAL_SLEEP(min(seconds, 0.2))


def _run(args):
    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.cli._forward_mine_to_hub", return_value=False),
        patch("mempalace.miner.mine", side_effect=_mine_taking_the_lock) as mine_mock,
        patch("time.sleep", side_effect=_fast_sleep),
    ):
        cmd_mine(args)
    return mine_mock


def test_default_fails_fast_while_another_mine_holds_the_lock(home, project, tmp_path, capsys):
    child = _hold_lock(tmp_path / "palace", home, 30)
    try:
        with pytest.raises(SystemExit) as exc:
            _run(_args(project, tmp_path / "palace"))
        assert exc.value.code == 1
        assert "is held by" in capsys.readouterr().err
    finally:
        child.kill()
        child.wait()


def test_wait_lock_succeeds_once_the_holder_releases(home, project, tmp_path, capsys):
    child = _hold_lock(tmp_path / "palace", home, 1)
    try:
        mine_mock = _run(_args(project, tmp_path / "palace", wait_lock=60))
    finally:
        child.kill()
        child.wait()
    assert mine_mock.call_count >= 2, "the first attempt must have met the held lock"
    err = capsys.readouterr().err
    assert "--wait-lock: retrying" in err


def test_wait_lock_budget_spent_exits_1_with_the_same_message(home, project, tmp_path, capsys):
    child = _hold_lock(tmp_path / "palace", home, 30)
    try:
        t0 = time.monotonic()
        with pytest.raises(SystemExit) as exc:
            _run(_args(project, tmp_path / "palace", wait_lock=1))
        assert time.monotonic() - t0 < 10
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert err.rstrip().splitlines()[-1].startswith("mempalace: palace ")
        assert "is held by" in err.rstrip().splitlines()[-1]
    finally:
        child.kill()
        child.wait()


def test_help_names_the_flag():
    import contextlib
    import io

    from mempalace import cli

    buf = io.StringIO()
    with patch("sys.argv", ["mempalace", "mine", "--help"]):
        with contextlib.redirect_stdout(buf):
            with pytest.raises(SystemExit):
                cli.main()
    assert "--wait-lock" in buf.getvalue()
