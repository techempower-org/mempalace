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


# ---------------------------------------------------------------------------
# review fixes (#486)
# ---------------------------------------------------------------------------


def test_daemon_strict_without_palace_refuses_instead_of_rebuilding_locally(spies, capsys):
    """F1: the worst outcome is a confident wrong answer.

    Under daemon-strict the palace lives on another host. Rebuilding the LOCAL
    palace prints +0/+0/+0 and exits 0 — so an operator who ran this BECAUSE
    the graph was stale is now told it is fresh. Refuse and name the host.
    """
    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._daemon_url", return_value="http://familiar:8085"),
        pytest.raises(SystemExit) as exc,
    ):
        cmd_tunnels(_args(rebuild=True, wing="2g"))

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--palace" in err
    assert "palace host" in err
    assert spies == {"topic": [], "hallways": [], "entity": []}, "must not touch a local palace"


def test_daemon_strict_with_explicit_palace_still_rebuilds(spies, tmp_path):
    """--palace says "I mean this local one", which is unambiguous."""
    with patch("mempalace.cli._daemon_strict", return_value=True):
        cmd_tunnels(_args(rebuild=True, wing="2g", palace=str(tmp_path)))

    assert spies["topic"] == ["2g"]


def test_rebuild_takes_the_palace_write_lock(monkeypatch, spies, capsys):
    """F2: hallways is a whole-file load/modify/save that carries other wings
    forward from its own load, so a concurrent drain mine and a rebuild
    silently drop one side's records — last os.replace wins."""
    from mempalace import palace as palace_mod

    taken = []

    import contextlib

    @contextlib.contextmanager
    def spy_lock(palace_path):
        taken.append(palace_path)
        yield

    monkeypatch.setattr(palace_mod, "mine_palace_lock", spy_lock)
    cmd_tunnels(_args(rebuild=True, wing="2g"))

    assert len(taken) == 1, "the rebuild must serialise against mines"


def test_rebuild_surfaces_lock_contention_instead_of_corrupting(monkeypatch, capsys):
    from mempalace import palace as palace_mod

    def busy(_palace_path):
        raise palace_mod.MineAlreadyRunning("held by PID 1234")

    monkeypatch.setattr(palace_mod, "mine_palace_lock", busy)

    with pytest.raises(SystemExit) as exc:
        cmd_tunnels(_args(rebuild=True, wing="2g"))

    assert exc.value.code == 1
    assert "1234" in capsys.readouterr().err


def test_rebuild_does_not_materialise_a_palace_at_a_bad_path(monkeypatch, capsys, tmp_path):
    """F3: get_collection defaults create=True, so a typo'd --palace created
    an empty palace and reported +0/+0/+0 success against it."""
    from mempalace import miner as m

    seen = {}

    def fake_get_collection(palace_path, *a, **kw):
        seen.update(kw)
        raise FileNotFoundError("no palace here")

    monkeypatch.setattr(m, "get_collection", fake_get_collection)
    monkeypatch.setattr(m, "_compute_topic_tunnels_for_wing", lambda w, config=None: 0)
    monkeypatch.setattr(m, "compute_hallways_for_wing", lambda w, col=None, config=None: [])
    monkeypatch.setattr(m, "_compute_entity_tunnels_for_wing", lambda w, config=None: 0)

    cmd_tunnels(_args(rebuild=True, wing="2g", palace=str(tmp_path / "nope")))

    assert seen.get("create") is False, "a rebuild must never create a palace"


def test_wing_is_normalised_like_the_miner_does(spies):
    """F4: the miner stores My-Project as my_project, so a raw wing here
    matches nothing and reports +0 exit 0."""
    cmd_tunnels(_args(rebuild=True, wing="My-Project"))

    assert spies["topic"] == ["my_project"]
    assert spies["hallways"] == ["my_project"]
    assert spies["entity"] == ["my_project"]


def test_all_flag_is_accepted_and_refused_with_a_reason(spies, capsys):
    """F5: a bare argparse error teaches nothing. Name the cost and the issue."""
    with pytest.raises(SystemExit) as exc:
        cmd_tunnels(_args(rebuild=True, all=True))

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "442" in err
    assert "wing" in err
    assert spies == {"topic": [], "hallways": [], "entity": []}


def test_the_three_steps_have_exactly_one_definition(spies):
    """F6: the verb and the post-mine block must not drift into two lists.

    Both call the same miner entry point, so adding a fourth derived step
    cannot land in one and miss the other.
    """
    from mempalace import miner as m

    assert hasattr(m, "recompute_derived_graph")

    result = m.recompute_derived_graph("2g", collection=object(), config=None)

    assert spies == {"topic": ["2g"], "hallways": ["2g"], "entity": ["2g"]}
    assert result["topic_tunnels"] == 2
    assert result["hallways"] == 3
    assert result["entity_tunnels"] == 4


def test_all_is_a_real_parser_flag_not_just_a_namespace_field():
    """F5 asked for the flag to be ACCEPTED, so argparse must know it.

    The refusal test above builds a Namespace directly and would pass even if
    the parser still rejected `--all` with a bare usage error.
    """
    import contextlib
    import io

    from mempalace import cli

    buf = io.StringIO()
    with patch("sys.argv", ["mempalace", "tunnels", "--help"]):
        with contextlib.redirect_stdout(buf):
            with pytest.raises(SystemExit):
                cli.main()

    assert "--all" in buf.getvalue()
