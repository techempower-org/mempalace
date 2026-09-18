"""`pending drain` / `replay` must say what it will do before doing it (#498).

`mempalace replay` took no arguments, printed no description, and re-posted
every request in `~/.mempalace/pending/*.jsonl` to the daemon as mine jobs.
Reproduced 2026-09-17: a 2g session exploring chronological options ran it
expecting a read-only replay of history and queued twelve mines against
production.

Two properties carry the fix, and the second is the one that makes the first
worth anything:

1. **Default is a plan, not an action.** Nothing is posted without `--yes`.
2. **The plan equals the action.** `replay` drops de-duplicated lines,
   unparseable lines and legacy whole-directory requests before posting, so
   a preview that counted raw lines would promise twelve and post nine —
   which is the same "report disagrees with what happened" defect the
   command was filed for, reintroduced by the fix.
"""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest


def _args(**overrides):
    defaults = {"yes": False, "json": False, "quiet": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _queue(tmp_path, requests):
    d = tmp_path / "pending"
    d.mkdir()
    with open(d / "2026-09-17.jsonl", "w", encoding="utf-8") as f:
        for r in requests:
            f.write(json.dumps(r) + "\n")
    return d


_TRANSCRIPT = "/home/jp/.claude/projects/-home-jp-Projects-2g/a.jsonl"
_TRANSCRIPT_B = "/home/jp/.claude/projects/-home-jp-Projects-2g/b.jsonl"


# ── the read-only peek ─────────────────────────────────────────────────


class TestPeekIsReadOnlyAndMatchesReplay:
    def test_peek_does_not_touch_the_queue(self, tmp_path):
        """`replay` claims each file by renaming it; a preview must not."""
        from mempalace import pending_queue as pq

        d = _queue(tmp_path, [{"dir": _TRANSCRIPT, "wing": "2g", "mode": "convos"}])
        before = sorted(p.name for p in d.iterdir())
        before_bytes = (d / "2026-09-17.jsonl").read_bytes()

        pq.peek(directory=d)

        assert sorted(p.name for p in d.iterdir()) == before, "peek renamed or removed a file"
        assert (d / "2026-09-17.jsonl").read_bytes() == before_bytes

    def test_peek_returns_exactly_what_replay_would_post(self, tmp_path):
        """The plan must equal the action, or the fix recreates the bug.

        `replay` de-duplicates, skips unparseable lines and DROPS legacy
        whole-directory requests without posting them. A preview counting
        raw lines would over-promise.
        """
        from mempalace import pending_queue as pq

        requests = [
            {"dir": _TRANSCRIPT, "wing": "2g", "mode": "convos"},
            {"dir": _TRANSCRIPT, "wing": "2g", "mode": "convos"},  # duplicate
            {"dir": "/home/jp/Projects/2g", "wing": "2g", "mode": "convos"},  # legacy dir
            {"dir": _TRANSCRIPT_B, "wing": "2g", "mode": "session"},
        ]
        d = _queue(tmp_path, requests)
        with open(d / "2026-09-17.jsonl", "a", encoding="utf-8") as f:
            f.write("{ not json\n")

        planned = pq.peek(directory=d)

        posted = []
        pq.replay(lambda r: (posted.append(r), True)[1], directory=d)

        assert planned == posted, f"peek promised {len(planned)} and replay posted {len(posted)}"

    def test_peek_on_a_missing_directory_is_empty(self, tmp_path):
        from mempalace import pending_queue as pq

        assert pq.peek(directory=tmp_path / "nope") == []


# ── the guard ──────────────────────────────────────────────────────────


class TestDrainRequiresYes:
    def _run(self, tmp_path, args, post=None):
        from mempalace import cli, pending_queue as pq

        d = _queue(
            tmp_path,
            [
                {"dir": _TRANSCRIPT, "wing": "2g", "mode": "convos"},
                {"dir": _TRANSCRIPT_B, "wing": "2g", "mode": "session"},
            ],
        )
        poster = post or MagicMock(return_value=True)
        with (
            patch.object(pq, "PENDING_DIR", d),
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._post_daemon_mine_cli", poster),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_pending_drain(args)
        return poster, exc.value.code, d

    def test_without_yes_it_posts_nothing(self, tmp_path, capsys):
        """The mutation the whole issue is about: no request leaves the host."""
        poster, code, _d = self._run(tmp_path, _args())

        poster.assert_not_called()
        assert code == 0, "a plan is not a failure"
        out = capsys.readouterr().out
        assert "2" in out, "the plan must state how many requests"
        assert "--yes" in out, "the plan must say how to actually run it"

    def test_without_yes_the_queue_is_untouched(self, tmp_path):
        poster, _code, d = self._run(tmp_path, _args())
        assert [p.name for p in d.iterdir()] == ["2026-09-17.jsonl"]

    def test_the_plan_names_the_targets(self, tmp_path, capsys):
        self._run(tmp_path, _args())
        out = capsys.readouterr().out
        assert "2g" in out, "the plan must name the wing"
        assert "a.jsonl" in out, "the plan must name what it would mine"

    def test_with_yes_it_posts(self, tmp_path):
        poster, code, _d = self._run(tmp_path, _args(yes=True))
        assert poster.call_count == 2
        assert code == 0

    def test_with_yes_a_failure_exits_non_zero(self, tmp_path):
        """`cmd_replay` computed this and returned it; main() discarded the
        return value, so the shell always saw 0. These paths exit."""
        poster, code, _d = self._run(tmp_path, _args(yes=True), post=MagicMock(return_value=False))
        assert poster.call_count == 2
        assert code != 0

    def test_json_plan_is_machine_readable(self, tmp_path, capsys):
        poster, code, _d = self._run(tmp_path, _args(json=True))
        poster.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["dry_run"] is True
        assert payload["pending"] == 2
        assert len(payload["requests"]) == 2

    def test_an_empty_queue_says_so_and_posts_nothing(self, tmp_path, capsys):
        from mempalace import cli, pending_queue as pq

        d = tmp_path / "pending"
        d.mkdir()
        poster = MagicMock()
        with (
            patch.object(pq, "PENDING_DIR", d),
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._post_daemon_mine_cli", poster),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_pending_drain(_args(yes=True))

        poster.assert_not_called()
        assert exc.value.code == 0
        assert "empty" in capsys.readouterr().out.lower()


# ── the alias ──────────────────────────────────────────────────────────


class TestReplayIsAWarningAlias:
    def test_replay_warns_and_names_the_new_verb(self, tmp_path, capsys):
        from mempalace import cli, pending_queue as pq

        d = _queue(tmp_path, [{"dir": _TRANSCRIPT, "wing": "2g", "mode": "convos"}])
        with (
            patch.object(pq, "PENDING_DIR", d),
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._post_daemon_mine_cli") as poster,
            pytest.raises(SystemExit),
        ):
            cli.cmd_replay(_args())

        err = capsys.readouterr().err
        assert "pending drain" in err, "the alias must name its replacement"
        poster.assert_not_called(), None

    def test_replay_still_defaults_to_a_plan(self, tmp_path):
        """The alias inherits the guard; it is not a back door."""
        from mempalace import cli, pending_queue as pq

        d = _queue(tmp_path, [{"dir": _TRANSCRIPT, "wing": "2g", "mode": "convos"}])
        poster = MagicMock(return_value=True)
        with (
            patch.object(pq, "PENDING_DIR", d),
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._post_daemon_mine_cli", poster),
            pytest.raises(SystemExit),
        ):
            cli.cmd_replay(_args())

        poster.assert_not_called()


# ── the description the issue asked for ────────────────────────────────


class TestHelpSaysWhatItDrains:
    @pytest.mark.parametrize("argv", [["pending", "--help"], ["replay", "--help"]])
    def test_help_says_it_drains_the_pending_mine_queue(self, argv, capsys):
        import sys as _sys

        from mempalace import cli

        with patch.object(_sys, "argv", ["mempalace", *argv]):
            with pytest.raises(SystemExit) as exc:
                cli.main()

        assert exc.value.code == 0
        text = capsys.readouterr().out.lower()
        assert "drain" in text
        assert "pending" in text and "mine" in text, (
            "help must say it drains the pending MINE queue, not 'replay history'"
        )

    def test_the_pending_verb_has_a_dispatch_entry(self):
        import inspect

        from mempalace import cli

        assert '"pending": cmd_pending,' in inspect.getsource(cli.main)
