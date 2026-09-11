"""`mempalace tunnels --rebuild --wing W` (#474).

After #478 (`--no-tunnels` + the zero-upsert short-circuit), palace-daemon#262
(unchanged transcripts skipped) and palace-daemon#264 (the daemon skipping
single-file and memory-dir mines), the derived graph refreshes ONLY as a side
effect of a full-directory mine that actually files drawers. That is the
intended trade, but it leaves no way to say "refresh the graph" on purpose.

This is that way. It calls the same three functions the post-mine block calls,
against one named wing, without mining anything.

Deliberately single-wing. An `--all` sweep would pay
`compute_hallways_for_wing`'s full load-and-rewrite of hallways.json per wing:
extrapolated from a measured 396 MB point (load 3.1 s, dump 8.4 s) to the
1.2 GB production file, that is ~44 s per wing, ~37 min across 50 wings —
which would recreate the very problem #474 is about, on the verb meant to fix
it. `--all` waits for the hallway store to stop being a monolithic JSON (#442).

Run with::

    cd <worktree>
    PYTHONPATH=$PWD /home/jp/Projects/memorypalace/.venv/bin/python \
        -m pytest tests/test_cli_tunnels_rebuild.py -q
"""

import argparse
from unittest.mock import patch

import pytest

from mempalace.cli import cmd_tunnels


def _args(**overrides):
    defaults = dict(
        command="tunnels",
        wing=None,
        passive=False,
        format=None,
        rebuild=False,
        palace=None,
        json=False,
        quiet=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def spies(monkeypatch):
    calls = {"topic": [], "hallways": [], "entity": []}
    from mempalace import miner as m

    monkeypatch.setattr(
        m, "_compute_topic_tunnels_for_wing", lambda w, config=None: calls["topic"].append(w) or 2
    )
    monkeypatch.setattr(
        m,
        "compute_hallways_for_wing",
        lambda w, col=None, config=None: calls["hallways"].append(w) or [1, 2, 3],
    )
    monkeypatch.setattr(
        m, "_compute_entity_tunnels_for_wing", lambda w, config=None: calls["entity"].append(w) or 4
    )
    monkeypatch.setattr(m, "get_collection", lambda *_a, **_k: object())
    return calls


def test_rebuild_runs_all_three_steps_for_the_named_wing(spies, capsys):
    cmd_tunnels(_args(rebuild=True, wing="2g"))

    assert spies == {"topic": ["2g"], "hallways": ["2g"], "entity": ["2g"]}
    out = capsys.readouterr().out
    assert "2g" in out


def test_rebuild_reports_what_it_created(spies, capsys):
    cmd_tunnels(_args(rebuild=True, wing="2g"))

    out = capsys.readouterr().out
    assert "2" in out and "3" in out and "4" in out, (
        "the counts the three steps returned must be visible — a rebuild that "
        "prints nothing is indistinguishable from one that did nothing"
    )


def test_rebuild_requires_a_wing(spies, capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_tunnels(_args(rebuild=True))

    assert exc.value.code == 2
    assert "--wing" in capsys.readouterr().err
    assert spies == {"topic": [], "hallways": [], "entity": []}


def test_rebuild_does_not_require_the_daemon(spies, monkeypatch, capsys):
    """The list path needs the daemon; the rebuild is local and must not."""
    monkeypatch.setattr("mempalace.cli._daemon_url", lambda: None)

    cmd_tunnels(_args(rebuild=True, wing="2g"))

    assert spies["topic"] == ["2g"]


def test_listing_is_untouched_by_the_new_flag(monkeypatch):
    """Control: without --rebuild this is still the daemon list command."""
    monkeypatch.setattr("mempalace.cli._daemon_url", lambda: None)

    with pytest.raises(SystemExit) as exc:
        cmd_tunnels(_args())

    assert exc.value.code == 2, "no daemon -> the list path still exits 2"


def test_a_failing_step_does_not_abort_the_others(monkeypatch, capsys):
    """Same fault tolerance as the post-mine block: a derived analytic must
    never take down the whole operation."""
    from mempalace import miner as m

    seen = []
    monkeypatch.setattr(m, "get_collection", lambda *_a, **_k: object())
    monkeypatch.setattr(
        m,
        "_compute_topic_tunnels_for_wing",
        lambda w, config=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        m,
        "compute_hallways_for_wing",
        lambda w, col=None, config=None: seen.append("hallways") or [],
    )
    monkeypatch.setattr(
        m, "_compute_entity_tunnels_for_wing", lambda w, config=None: seen.append("entity") or 0
    )

    cmd_tunnels(_args(rebuild=True, wing="2g"))

    assert seen == ["hallways", "entity"], "the later steps must still run"
    assert "boom" in capsys.readouterr().err


def test_help_documents_why_there_is_no_all_flag():
    import contextlib
    import io

    from mempalace import cli

    buf = io.StringIO()
    with patch("sys.argv", ["mempalace", "tunnels", "--help"]):
        with contextlib.redirect_stdout(buf):
            with pytest.raises(SystemExit):
                cli.main()

    help_text = " ".join(buf.getvalue().split())
    assert "--rebuild" in help_text
    assert "one wing" in help_text
