"""A queued `"mode": null` must not crash the drain (#525).

`pending drain` posts whatever the queue holds, and the queue is written by
hooks that pass through whatever they were given.

⚠️ `request.get("mode", "convos")` returns **None** for an explicit
`"mode": null` — a `.get` default only fires on a MISSING key, never on a
present-but-null one. Once `_post_daemon_mine_cli` refuses None (#525), that
difference is a crashed drain versus a drained queue: the ValueError escapes
`pending_queue.replay`'s loop and takes every remaining request with it.

⭐ The seam's strictness is right, and it created this hazard two files away.
That is why #525 audited the seam's CALLERS and not only the sites that build
its payload — a guard added in one file can turn a recoverable failure into an
unrecoverable one somewhere else.
"""

import argparse
import json
from unittest.mock import MagicMock, patch

import pytest

from mempalace import cli
from mempalace import pending_queue as pq


def _drain_args():
    return argparse.Namespace(yes=True, json=False, quiet=False, pending_action="drain")


def _queue(tmp_path, request):
    d = tmp_path / "pending"
    d.mkdir()
    (d / "2026-09-17.jsonl").write_text(json.dumps(request) + "\n")
    return d


class TestAQueuedNullModeStillDrains:
    def test_it_falls_back_instead_of_propagating(self, tmp_path):
        d = _queue(tmp_path, {"dir": "/x/a.jsonl", "wing": "2g", "mode": None})
        poster = MagicMock(return_value=True)
        with (
            patch.object(pq, "PENDING_DIR", d),
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._post_daemon_mine_cli", poster),
            pytest.raises(SystemExit) as exc,
        ):
            cli.cmd_pending_drain(_drain_args())

        assert exc.value.code == 0
        assert poster.call_count == 1
        assert poster.call_args[0][2] == "convos", (
            "a null mode must fall back at the caller; the poster refuses None"
        )

    def test_a_missing_mode_still_falls_back_too(self, tmp_path):
        """The case the `.get` default already handled — it must keep working."""
        d = _queue(tmp_path, {"dir": "/x/b.jsonl", "wing": "2g"})
        poster = MagicMock(return_value=True)
        with (
            patch.object(pq, "PENDING_DIR", d),
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._post_daemon_mine_cli", poster),
            pytest.raises(SystemExit),
        ):
            cli.cmd_pending_drain(_drain_args())
        assert poster.call_args[0][2] == "convos"

    def test_an_explicit_mode_is_not_overridden(self, tmp_path):
        d = _queue(tmp_path, {"dir": "/x/c.jsonl", "wing": "2g", "mode": "projects"})
        poster = MagicMock(return_value=True)
        with (
            patch.object(pq, "PENDING_DIR", d),
            patch("mempalace.cli._daemon_strict", return_value=True),
            patch("mempalace.cli._post_daemon_mine_cli", poster),
            pytest.raises(SystemExit),
        ):
            cli.cmd_pending_drain(_drain_args())
        assert poster.call_args[0][2] == "projects"
