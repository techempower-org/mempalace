"""Joint producer/consumer contract: `curated_first_rank` → the banner.

Written BEFORE the producer shipped, on the lead's instruction, so the field's
shape was fixed by the consumer's need rather than guessed. It settled one
thing immediately: K is the rank in the deep fetch **before** curated-first
reordering. Taken after, it is 1 almost every time a curated hit exists, so
"the curated layer begins around rank K — widen the pool" would carry no
information and the advice would be unactionable.

Producer: the deep-fetch / reservation step (`mempalace/cli.py`, #534).
Consumer: `_print_search_header` (this PR).

The banner is the whole point of #526: *"no curated document matched"* is read
as a statement about the STORE when it is a statement about the DEPTH. So the
three cases are asserted on one result object, and the K a reader sees must be
the K a script parses.
"""

import argparse
import json
from unittest.mock import patch

import pytest

_CURATED = "the curated finding that records the bounds and the correction"
_TRANSCRIPT = "a session transcript discussing the parameter handler at length"


def _hit(i, kind, text):
    return {
        "id": f"d{i}",
        "wing": "2g",
        "room": "problems",
        "snippet": f"{text} number {i}",
        "source_file": ("/p/findings.md" if kind == "file" else f"/p/s{i}.jsonl"),
        "rank": 0.6 - i * 0.01,
    }


def _payload(n=30, curated_at=None):
    rows = []
    for i in range(1, n + 1):
        kind = "file" if (curated_at and i == curated_at) else "transcript"
        rows.append(_hit(i, kind, _CURATED if kind == "file" else _TRANSCRIPT))
    return {"results": rows}


def _args(fmt, limit=3):
    return argparse.Namespace(
        query="cmhs inbound parameter handler binary",
        wing="2g",
        room=None,
        results=limit,
        limit=limit,
        palace=None,
        mode="fast",
        tags=None,
        format=fmt,
        json=False,
        quiet=False,
    )


def _run(fmt, payload, capsys, limit=3):
    from mempalace import cli

    env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
    with (
        patch.dict("os.environ", env, clear=True),
        patch("mempalace.cli._call_daemon_rest", return_value=payload),
    ):
        try:
            cli.cmd_search(_args(fmt, limit))
        except SystemExit:
            pass
    return capsys.readouterr().out


class TestCuratedFirstRankReachesTheBanner:
    def test_curated_inside_the_limit_says_nothing_about_absence(self, capsys):
        """⚠️ REGRESSION GUARD, NOT A DISCRIMINATOR — measured: this also passed on
        the base, because the old banner already stayed silent when a curated hit
        was present. Kept because #534 adds a new way to misfire (a promoted hit
        changes what "present" means); must not be counted as evidence."""
        out = _run("table", _payload(curated_at=2), capsys)
        assert "no curated document" not in out
        assert "promoted from rank" not in out, (
            "it ranked there on its own; claiming a promotion would be false"
        )

    def test_promotion_names_the_rank_and_the_mechanism(self, capsys):
        out = _run("table", _payload(curated_at=14), capsys)
        assert "1 curated hit promoted from rank 14" in out
        assert "the curated layer in this wing begins there" in out
        # The advice must name the MECHANISM: the limit sizes the fusion candidate
        # pool, so at --limit 3 the document was never a candidate. "Look further
        # down the list" would describe the wrong thing.
        assert "widen the pool with --limit 30" in out

    def test_never_claims_the_store_is_empty(self, capsys):
        """The exact sentence #526 was filed about."""
        out = _run("table", _payload(curated_at=14), capsys)
        assert "no curated document matched" not in out

    def test_no_curated_anywhere_states_the_depth_and_omits_the_rank(self, capsys):
        out = _run("table", _payload(curated_at=None), capsys)
        assert "no curated document in the top 3" in out
        assert "promoted from rank" not in out
        assert "first curated hit at rank" not in out, (
            "no K exists; inventing one would be #526's error in a new place"
        )

    @pytest.mark.parametrize("k", [14, 27])
    def test_the_printed_K_equals_the_json_K(self, capsys, k):
        """THE PAIR. A banner whose number disagrees with --json is worse than no
        number: one reader acts on the prose, another parses the field."""
        payload = _payload(curated_at=k)
        emitted = json.loads(_run("json", payload, capsys))
        assert emitted["curated_first_rank"] == k
        out = _run("table", payload, capsys)
        assert f"promoted from rank {emitted['curated_first_rank']}" in out

    def test_json_reports_null_when_there_is_no_curated_hit(self, capsys):
        emitted = json.loads(_run("json", _payload(curated_at=None), capsys))
        assert emitted["curated_first_rank"] is None, (
            "null, not 0 and not absent — a script must tell 'none found' from 'not implemented'"
        )

    def test_quiet_suppresses_the_banner(self, capsys):
        from mempalace import cli

        args = _args("table")
        args.quiet = True
        env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch("mempalace.cli._call_daemon_rest", return_value=_payload(curated_at=14)),
        ):
            try:
                cli.cmd_search(args)
            except SystemExit:
                pass
        out = capsys.readouterr().out
        # --quiet drops the header CHROME, not the hits: the banner sentence
        # goes, the per-hit ⟨curated, promoted from rank K⟩ marker (#534, every
        # renderer) stays — a piped reader must still tell reserved from ranked.
        assert "1 curated hit promoted from rank" not in out
        assert "the curated layer in this wing begins there" not in out
        assert "⟨curated, promoted from rank 14⟩" in out
