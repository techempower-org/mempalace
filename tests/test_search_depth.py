"""test_search_depth.py — conditional deep fetch and curated slot reservation.

In a wing with ~941K transcript drawers the first curated document ranks 14-27, so
``--limit 3`` returns three transcripts and the reader concludes the corpus has
nothing (techempower-org/mempalace#526).

Two separate causes, both measured on production before this was written:

* **The limit sizes the hybrid fusion candidate pool.** ``limit=3`` and ``limit=30``
  return *different* top-3 drawers for the same query, while the BM25 route is stable
  across limits. At ``--limit 3`` the curated document is not ranked below the cut —
  it is never a candidate. #477's widen already fired here but computed
  ``min(n*2, 40)`` = **6** at limit 3, and the curated layer begins at 14: it landed
  short.
* **#477's ordering cannot lift it.** That ordering is bounded to near-duplicates by
  design (two review rounds hardened the bound). Measured similarity between the first
  curated hit and the top-3 transcripts: ``0.0, 0.0, 0.0`` on one query and
  ``0.008`` on another, against a 0.35 threshold. #477 fixes *a transcript that quotes
  the card*; this is *the same topic with no shared 3-grams*. Different defect.

So the fix is a reserved slot, not a re-rank — and it is marked, because a silent
promotion would be #526's own error in reverse: a reader could not tell "ranked here"
from "reserved here".
"""

import argparse
import json
from unittest.mock import patch


_TRANSCRIPT = "a session transcript discussing the parameter handler at length"
_CURATED = "the curated finding that records the bounds and the correction"


def _hit(i, kind, text):
    return {
        "id": f"d{i}",
        "wing": "2g",
        "room": "problems",
        "snippet": f"{text} number {i}",
        "source_file": ("/p/findings.md" if kind == "file" else f"/p/s{i}.jsonl"),
        "rank": 0.6 - i * 0.01,
    }


def _payload(n, curated_at=None):
    rows = []
    for i in range(1, n + 1):
        kind = "file" if (curated_at and i == curated_at) else "transcript"
        rows.append(_hit(i, kind, _CURATED if kind == "file" else _TRANSCRIPT))
    return {"results": rows}


def _args(fmt="json", limit=3):
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


def _run(capsys, payloads, fmt="json", limit=3):
    from mempalace import cli

    env = {"PALACE_DAEMON_URL": "http://daemon.example:8085"}
    with (
        patch.dict("os.environ", env, clear=True),
        patch("mempalace.cli._call_daemon_rest", side_effect=payloads) as m,
    ):
        try:
            cli.cmd_search(_args(fmt, limit))
        except SystemExit:
            pass
    return capsys.readouterr(), m


class TestConditionalDeepFetchDepth:
    def test_deep_fetch_asks_for_thirty_not_double_the_limit(self, capsys):
        """#477's widen asked for min(3*2,40)=6 and the curated layer starts at 14."""
        shallow = _payload(3)
        deep = _payload(30, curated_at=14)
        _, m = _run(capsys, [shallow, deep])
        assert m.call_count == 2, "no curated hit in the shallow result must widen"
        assert m.call_args_list[1].args[1]["limit"] == 30, (
            f"deep fetch must reach the curated layer, asked for "
            f"{m.call_args_list[1].args[1]['limit']}"
        )

    def test_no_deep_fetch_when_the_shallow_result_already_has_curated(self, capsys):
        """The common case must cost exactly one call — a 19-22 s floor under every
        interactive search on a large wing is not acceptable (measured, issue #533)."""
        _, m = _run(capsys, [_payload(3, curated_at=2)])
        assert m.call_count == 1

    def test_the_floor_never_narrows_a_larger_limit(self):
        """max(limit, 30) — the 30 is a FLOOR, not a ceiling. Driven through the
        helper directly: a route-level test cannot see this, because when the
        shallow fetch already returned `limit` rows there is nothing deeper to
        ask for and the second call is correctly skipped."""
        from mempalace import cli

        asked = []

        def fetch(limit):
            asked.append(limit)
            return [dict(h, source_kind="transcript") for h in _payload(limit)["results"]]

        shallow = [dict(h, source_kind="transcript") for h in _payload(10)["results"]]
        cli._deep_fetch_when_nothing_curated(shallow, 50, fetch)
        assert asked == [50], f"floor must not narrow a larger limit, asked {asked}"

        asked.clear()
        cli._deep_fetch_when_nothing_curated(shallow[:3], 3, fetch)
        assert asked == [30], f"a shallow limit is raised to the floor, asked {asked}"

    def test_no_second_call_when_the_shallow_result_is_already_that_deep(self):
        """Nothing deeper to fetch — and the call costs 19-22 s on a large wing."""
        from mempalace import cli

        asked = []
        shallow = [dict(h, source_kind="transcript") for h in _payload(30)["results"]]
        cli._deep_fetch_when_nothing_curated(shallow, 30, lambda n: asked.append(n))
        assert asked == []


class TestCuratedFirstRank:
    def test_is_the_pre_reorder_rank_in_the_deep_fetch(self, capsys):
        out, _ = _run(capsys, [_payload(3), _payload(30, curated_at=14)])
        assert json.loads(out.out)["curated_first_rank"] == 14

    def test_is_null_when_the_deep_fetch_has_no_curated_hit(self, capsys):
        out, _ = _run(capsys, [_payload(3), _payload(30)])
        assert json.loads(out.out)["curated_first_rank"] is None, (
            "null, not 0 and not absent — a script must tell 'none found' from 'not implemented'"
        )


class TestCuratedSlotReservation:
    def test_reserves_the_last_slot_for_the_top_curated_hit(self, capsys):
        out, _ = _run(capsys, [_payload(3), _payload(30, curated_at=14)])
        got = json.loads(out.out)["results"]
        assert len(got) == 3
        assert got[-1]["source_kind"] == "file", "the LAST slot is the reserved one"
        assert [h["id"] for h in got[:2]] == ["d1", "d2"], (
            "the first N-1 stay exactly as the ranker ordered them"
        )

    def test_marks_the_promotion_on_the_json_channel(self, capsys):
        out, _ = _run(capsys, [_payload(3), _payload(30, curated_at=14)])
        doc = json.loads(out.out)
        reserved = doc["results"][-1]
        assert reserved["promoted"] is True
        assert reserved["promoted_from_rank"] == doc["curated_first_rank"] == 14, (
            "the per-hit rank and the top-level rank must be the same number"
        )

    def test_unpromoted_hits_carry_no_promoted_flag(self, capsys):
        out, _ = _run(capsys, [_payload(3), _payload(30, curated_at=14)])
        assert all("promoted" not in h for h in json.loads(out.out)["results"][:2])

    def test_no_reservation_when_curated_is_already_visible(self, capsys):
        out, _ = _run(capsys, [_payload(3, curated_at=2)])
        got = json.loads(out.out)["results"]
        assert all("promoted" not in h for h in got), (
            "it ranked there; claiming a promotion would be a false statement"
        )

    def test_no_reservation_when_the_deep_fetch_has_none(self, capsys):
        out, _ = _run(capsys, [_payload(3), _payload(30)])
        got = json.loads(out.out)["results"]
        assert len(got) == 3
        assert all(h["source_kind"] != "file" for h in got)

    def test_limit_of_one_still_yields_the_curated_hit(self, capsys):
        out, _ = _run(capsys, [_payload(1), _payload(30, curated_at=14)], limit=1)
        got = json.loads(out.out)["results"]
        assert len(got) == 1 and got[0]["source_kind"] == "file"

    def test_prose_marks_the_promotion_too(self, capsys):
        """Both channels or neither: a reader of the table must be able to tell
        'ranked here' from 'reserved here' exactly as a script can."""
        out, _ = _run(capsys, [_payload(3), _payload(30, curated_at=14)], fmt="compact")
        assert "⟨curated, promoted from rank 14⟩" in out.out
