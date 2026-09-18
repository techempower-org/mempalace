"""test_search_dedup.py — identical-text hits are collapsed at result time.

A curated document that lives at two paths — ``docs/foo.md`` and its
``docs/rescued-from-vartmp-20260905/foo.md`` copy — is mined twice and comes back
twice, so a ``--limit 3`` spends two of its three slots on the same words
(techempower-org/mempalace#526, PR 3). Same words, different paths: the existing
``searcher._dedupe_rendered_hits`` keys on the SAME path and only for closet hits,
and ``prefer_curated`` reorders but never removes, so neither closes this.

The collapse runs BEFORE the curated deep fetch and BEFORE the reserved slot, so
the trigger, ``curated_first_rank`` and the truncation all see one list. It is
marked on both channels: ``duplicates_collapsed`` / ``duplicate_of`` on the kept
hit in ``--json``, ``⟨N identical copies collapsed⟩`` in every prose renderer.
"""

import argparse
import copy
import json
from unittest.mock import patch

import pytest

_T = "a session transcript discussing the parameter handler at length"
_C = "the curated finding that records the bounds and the correction"


def _hit(i, kind, text, path=None):
    if path is None:
        path = "/p/findings.md" if kind == "file" else f"/p/s{i}.jsonl"
    return {
        "id": f"d{i}",
        "wing": "2g",
        "room": "problems",
        "snippet": text,
        "source_file": path,
        "rank": 0.6 - i * 0.01,
    }


def _payload(rows):
    return {"results": rows}


def _transcripts(n, start=1):
    return [_hit(i, "transcript", f"{_T} number {i}") for i in range(start, start + n)]


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


class TestCollapseIdenticalTextHelper:
    def test_keeps_first_and_records_the_other_paths(self):
        from mempalace.result_ordering import collapse_identical_text

        hits = [
            {"text": _C, "source_file": "/p/docs/foo.md", "source_kind": "file"},
            {"text": _T, "source_file": "/p/s.jsonl", "source_kind": "transcript"},
            {"text": _C, "source_file": "/p/docs/rescued/foo.md", "source_kind": "file"},
        ]
        out = collapse_identical_text(hits)
        assert [h["source_file"] for h in out] == ["/p/docs/foo.md", "/p/s.jsonl"]
        assert out[0]["duplicates_collapsed"] == 1
        assert out[0]["duplicate_of"] == ["/p/docs/rescued/foo.md"]
        assert "duplicates_collapsed" not in out[1]

    def test_whitespace_differences_are_the_same_text(self):
        from mempalace.result_ordering import collapse_identical_text

        hits = [
            {"text": "alpha  beta\ngamma ", "source_file": "/a.md"},
            {"text": "alpha beta gamma", "source_file": "/b.md"},
        ]
        assert len(collapse_identical_text(hits)) == 1

    def test_the_curated_copy_takes_the_kept_slot(self):
        """Same words, better provenance: the position is the ranker's, the
        payload is the copy a reader can cite."""
        from mempalace.result_ordering import collapse_identical_text

        hits = [
            {"text": _C, "source_file": "/p/s1.jsonl", "source_kind": "transcript"},
            {"text": _T, "source_file": "/p/s2.jsonl", "source_kind": "transcript"},
            {"text": _C, "source_file": "/p/docs/foo.md", "source_kind": "file"},
        ]
        out = collapse_identical_text(hits)
        assert len(out) == 2
        assert out[0]["source_kind"] == "file"
        assert out[0]["source_file"] == "/p/docs/foo.md"
        assert out[0]["duplicate_of"] == ["/p/s1.jsonl"]

    def test_different_text_same_basename_both_survive(self):
        """⚠️ REGRESSION GUARD, NOT A DISCRIMINATOR — passes on the base too.
        A one-line edit between two copies is two documents, not one."""
        from mempalace.result_ordering import collapse_identical_text

        hits = [
            {"text": _C, "source_file": "/p/docs/foo.md"},
            {"text": _C + " (amended)", "source_file": "/p/docs/rescued/foo.md"},
        ]
        assert len(collapse_identical_text(hits)) == 2

    def test_idempotent_and_tolerant(self):
        from mempalace.result_ordering import collapse_identical_text

        hits = [{"text": _C, "source_file": "/a"}, "not a dict", {"text": _C, "source_file": "/b"}]
        once = collapse_identical_text(list(hits))
        assert collapse_identical_text(list(once)) == once
        assert once[1] == "not a dict"
        assert collapse_identical_text(None) is None
        assert collapse_identical_text([{"text": None}, {"text": None}]) == [
            {"text": None},
            {"text": None},
        ]


class TestCollapseBeforeReservation:
    def test_no_two_returned_hits_share_text_and_the_slot_is_refilled(self, capsys):
        """Shallow 3 holds a duplicate pair; collapsing leaves 2, so ONE deeper
        fetch refills the third slot. Unit: identical-text hits among returned."""
        shallow = [_hit(1, "file", _C, "/p/docs/foo.md"), _hit(2, "transcript", _T + " 2")]
        shallow.append(_hit(3, "file", _C, "/p/docs/rescued/foo.md"))
        # The daemon answers the deeper call with FRESH objects; a fixture that
        # shared dicts between the two payloads would count one collapse twice.
        deep = copy.deepcopy(shallow) + _transcripts(27, start=4)
        out, m = _run(capsys, [_payload(shallow), _payload(deep)])
        got = json.loads(out.out)["results"]
        texts = [" ".join(h["text"].split()) for h in got]
        assert len(texts) == len(set(texts)) == 3
        assert m.call_count == 2
        kept = next(h for h in got if h.get("duplicates_collapsed"))
        assert kept["duplicates_collapsed"] == 1
        assert kept["duplicate_of"] == ["/p/docs/rescued/foo.md"]

    def test_collapse_runs_before_the_curated_deep_fetch_and_reservation(self, capsys):
        """The curated copy at rank 14 says the SAME words as the transcript at
        rank 2. Collapsed first, the curated copy takes rank 2 on its own merit —
        visible at --limit 3 with NO promotion, and curated_first_rank reports 2."""
        deep = _transcripts(30)
        deep[1] = _hit(2, "transcript", _C)
        deep[13] = _hit(14, "file", _C, "/p/docs/foo.md")
        shallow = copy.deepcopy(deep[:3])
        out, m = _run(capsys, [_payload(shallow), _payload(deep)])
        data = json.loads(out.out)
        got = data["results"]
        assert got[1]["source_kind"] == "file"
        assert not any(h.get("promoted") for h in got)
        assert data["curated_first_rank"] == 2

    def test_no_second_call_when_nothing_collapsed_and_curated_is_visible(self, capsys):
        shallow = [_hit(1, "file", _C)] + _transcripts(2, start=2)
        out, m = _run(capsys, [_payload(shallow)])
        assert m.call_count == 1
        assert len(json.loads(out.out)["results"]) == 3


class TestCollapseMarkedInEveryProseRenderer:
    _MARK = "⟨1 identical copy collapsed⟩"

    def _payloads(self):
        shallow = [_hit(1, "file", _C, "/p/docs/foo.md"), _hit(2, "transcript", _T)]
        shallow.append(_hit(3, "file", _C, "/p/docs/rescued/foo.md"))
        deep = copy.deepcopy(shallow) + _transcripts(27, start=4)
        return [_payload(shallow), _payload(deep)]

    @pytest.mark.parametrize("fmt", ["table", "full", "compact"])
    def test_renderer_marks_the_collapse(self, capsys, fmt):
        out, _ = _run(capsys, self._payloads(), fmt=fmt)
        assert self._MARK in out.out

    def test_plural_wording(self):
        from mempalace import cli

        assert cli._collapse_tag({"duplicates_collapsed": 2}) == " ⟨2 identical copies collapsed⟩"
        assert cli._collapse_tag({"duplicates_collapsed": 0}) == ""
        assert cli._collapse_tag({}) == ""

    def test_table_does_not_mark_unique_hits(self, capsys):
        payload = _payload([_hit(1, "file", _C)] + _transcripts(2, start=2))
        out, _ = _run(capsys, [payload], fmt="table")
        assert "identical cop" not in out.out
