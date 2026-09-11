"""test_result_ordering.py — curated-over-transcript ordering for near-duplicates.

A paraphrased question ranks session transcripts above the curated card that
answers it, so a reader at the default ``--limit 10`` never reaches the
correction (techempower-org/mempalace#451 item F, "FAIL-D: right file, right
chunk, right markers, ranked below the cut").

The fix is deliberately BOUNDED: a curated hit is promoted only over a hit it
is a near-duplicate of — i.e. one that quotes substantially the same text.
Genuinely transcript-only recall, where no curated document says the same
thing, is left exactly as the ranker returned it.
"""

import pytest

from mempalace.result_ordering import (
    NEAR_DUP_THRESHOLD,
    is_near_duplicate,
    prefer_curated,
    similarity,
)

# A claim that appears verbatim in both the curated card and the transcript
# that quotes it — the real shape from the 2g corpus.
CLAIM = (
    "the five handsets refused one cell and the fault was never inside "
    "their modems because a documented refuser camped and carried a call"
)
CARD = "REFUTED 2026-09-06 " + CLAIM + " this correction supersedes the headline"
TRANSCRIPT = "and then I told the team " + CLAIM + " which is what the log shows"
UNRELATED = (
    "the postgres backend scrubs lone surrogates and nul bytes at every bind "
    "site so a mined corpus cannot abort the whole batch on one stray byte"
)


def _hit(kind, text, **extra):
    h = {"source_kind": kind, "text": text}
    h.update(extra)
    return h


# ── similarity ──────────────────────────────────────────────────────────


class TestSimilarity:
    def test_identical_text_is_one(self):
        assert similarity(CARD, CARD) == pytest.approx(1.0)

    def test_disjoint_text_is_zero(self):
        assert similarity(CARD, UNRELATED) == pytest.approx(0.0)

    def test_containment_scores_high_despite_length_difference(self):
        """The measure is overlap-coefficient, not Jaccard: a short card chunk
        wholly quoted inside a long transcript must score ~1, where Jaccard
        would be dragged down by the transcript's extra words. Measured on
        production, Jaccard peaked at 0.16 for true quoting pairs — too low to
        threshold safely."""
        long_transcript = (UNRELATED + " ") * 3 + CLAIM
        assert similarity(CLAIM, long_transcript) == pytest.approx(1.0)

    def test_is_symmetric(self):
        assert similarity(CARD, TRANSCRIPT) == pytest.approx(similarity(TRANSCRIPT, CARD))

    def test_empty_text_is_zero(self):
        assert similarity("", CARD) == 0.0
        assert similarity(None, CARD) == 0.0


class TestIsNearDuplicate:
    def test_quoting_pair_is_a_near_duplicate(self):
        assert is_near_duplicate(CARD, TRANSCRIPT) is True

    def test_unrelated_pair_is_not(self):
        assert is_near_duplicate(CARD, UNRELATED) is False

    def test_threshold_is_honoured(self):
        sim = similarity(CARD, TRANSCRIPT)
        assert is_near_duplicate(CARD, TRANSCRIPT, threshold=sim + 0.01) is False
        assert is_near_duplicate(CARD, TRANSCRIPT, threshold=sim - 0.01) is True

    def test_short_text_cannot_be_decided(self):
        """Too few shingles to be evidence — must not fire on a stub."""
        assert is_near_duplicate("five handsets", "five handsets") is False

    def test_calibrated_threshold_sits_in_the_measured_gap(self):
        """Production calibration (wing 2g, limit 20, both routes): genuine
        buried-card pairs scored 0.373 / 0.473 / 0.491; same-topic non-quoting
        pairs scored <= 0.323. The default must separate them."""
        assert 0.323 < NEAR_DUP_THRESHOLD <= 0.373


# ── prefer_curated ──────────────────────────────────────────────────────


class TestPreferCurated:
    def test_promotes_a_curated_hit_above_the_transcript_that_quotes_it(self):
        hits = [_hit("transcript", TRANSCRIPT, id="t"), _hit("file", CARD, id="f")]
        prefer_curated(hits)
        assert [h["id"] for h in hits] == ["f", "t"]

    def test_leaves_a_curated_hit_alone_when_nothing_above_is_a_near_duplicate(self):
        """THE BOUND. A curated hit ranked below unrelated transcripts stays put
        — this feature reorders near-duplicates, it does not float every
        curated document to the top."""
        hits = [
            _hit("transcript", UNRELATED, id="t1"),
            _hit("transcript", UNRELATED + " tail", id="t2"),
            _hit("file", CARD, id="f"),
        ]
        prefer_curated(hits)
        assert [h["id"] for h in hits] == ["t1", "t2", "f"]

    def test_transcript_only_results_are_untouched(self):
        hits = [_hit("transcript", CLAIM, id="a"), _hit("transcript", CLAIM, id="b")]
        prefer_curated(hits)
        assert [h["id"] for h in hits] == ["a", "b"]

    def test_two_curated_near_duplicates_do_not_reorder_each_other(self):
        hits = [_hit("file", CARD, id="f1"), _hit("memory", CARD, id="f2")]
        prefer_curated(hits)
        assert [h["id"] for h in hits] == ["f1", "f2"]

    def test_diary_orders_below_a_curated_near_duplicate(self):
        hits = [_hit("diary", TRANSCRIPT, id="d"), _hit("file", CARD, id="f")]
        prefer_curated(hits)
        assert [h["id"] for h in hits] == ["f", "d"]

    def test_diary_is_untouched_when_no_curated_near_duplicate_exists(self):
        hits = [_hit("diary", CLAIM, id="d"), _hit("file", UNRELATED, id="f")]
        prefer_curated(hits)
        assert [h["id"] for h in hits] == ["d", "f"]

    def test_unrelated_hits_keep_their_relative_order(self):
        hits = [
            _hit("transcript", UNRELATED, id="x1"),
            _hit("transcript", TRANSCRIPT, id="t"),
            _hit("transcript", UNRELATED + " more", id="x2"),
            _hit("file", CARD, id="f"),
        ]
        prefer_curated(hits)
        ids = [h["id"] for h in hits]
        assert ids.index("f") < ids.index("t"), "curated must clear its near-duplicate"
        assert ids.index("x1") < ids.index("x2"), "unrelated hits keep relative order"

    def test_promotes_to_the_position_of_the_highest_ranked_near_duplicate(self):
        hits = [
            _hit("transcript", TRANSCRIPT, id="t_top"),
            _hit("transcript", UNRELATED, id="x"),
            _hit("file", CARD, id="f"),
        ]
        prefer_curated(hits)
        assert [h["id"] for h in hits] == ["f", "t_top", "x"]

    def test_is_idempotent(self):
        hits = [_hit("transcript", TRANSCRIPT, id="t"), _hit("file", CARD, id="f")]
        once = [h["id"] for h in prefer_curated(hits)]
        twice = [h["id"] for h in prefer_curated(hits)]
        assert once == twice == ["f", "t"]

    def test_returns_the_same_list_object(self):
        hits = [_hit("file", CARD, id="f")]
        assert prefer_curated(hits) is hits

    def test_derives_kind_when_not_annotated(self):
        hits = [
            {"source_file": "a.jsonl", "text": TRANSCRIPT, "id": "t"},
            {"source_file": "/p/CLAUDE.md", "text": CARD, "id": "f"},
        ]
        prefer_curated(hits)
        assert [h["id"] for h in hits] == ["f", "t"]

    def test_tolerates_non_dict_items_and_non_list_input(self):
        hits = [None, "junk", _hit("file", CARD, id="f")]
        prefer_curated(hits)
        assert hits[0] is None and hits[1] == "junk"
        assert prefer_curated(None) is None
        assert prefer_curated({"results": []}) == {"results": []}

    def test_skips_pairwise_work_on_an_implausibly_large_result_set(self):
        """The comparison is O(n^2); a caller asking for thousands of hits gets
        the ranker's order rather than a stall."""
        hits = [_hit("transcript", TRANSCRIPT, id=f"t{i}") for i in range(400)]
        hits.append(_hit("file", CARD, id="f"))
        prefer_curated(hits)
        assert hits[-1]["id"] == "f", "unchanged when the set is too large to compare"


# ── the bound is DIRECT, not transitive ────────────────────────────────

# Three chunks where near-duplicate chains but does not connect the ends:
#   A is wholly contained in B      -> sim(A, B) = 1.0
#   B and C share their second half -> sim(B, C) ~ 0.49
#   A and C share nothing at all    -> sim(A, C) = 0.0
_SEG1 = (
    "the five handsets refused one cell and the fault was never inside their "
    "modems because a documented refuser camped and carried an answered call"
)
_SEG2 = (
    "the postgres backend scrubs lone surrogates and nul bytes at every bind "
    "site so a mined corpus cannot abort the whole batch on one stray byte"
)
_SEG3 = (
    "the hallways file grew to nine hundred megabytes of json which the miner "
    "loads in full on every pass and that is what made the sweep take an hour"
)
CHUNK_A = _SEG1
CHUNK_B = _SEG1 + " " + _SEG2
CHUNK_C = _SEG2 + " " + _SEG3


class TestNearDuplicateIsDirectNotTransitive:
    """A curated hit may only clear hits it *itself* duplicates.

    Grouping by connected component makes near-duplicate transitive: A~B and
    B~C would put A, B and C in one group and let A jump over C even though
    they share no 3-gram at all. That silently widens the documented bound —
    the whole point of which is that a curated document never outranks
    something it does not duplicate.
    """

    def test_the_chain_has_the_shape_the_bug_needs(self):
        assert similarity(CHUNK_A, CHUNK_B) == pytest.approx(1.0)
        assert similarity(CHUNK_A, CHUNK_C) == pytest.approx(0.0)
        assert is_near_duplicate(CHUNK_B, CHUNK_C) is True
        assert is_near_duplicate(CHUNK_A, CHUNK_C) is False

    def test_curated_does_not_jump_a_hit_it_shares_no_text_with(self):
        hits = [
            _hit("transcript", CHUNK_C, id="C"),
            _hit("transcript", CHUNK_B, id="B"),
            _hit("file", CHUNK_A, id="A"),
        ]
        prefer_curated(hits)
        ids = [h["id"] for h in hits]
        assert ids.index("A") > ids.index("C"), (
            "A shares no 3-gram with C and must not be promoted over it"
        )
        assert ids.index("A") < ids.index("B"), (
            "A IS a direct near-duplicate of B and must clear it"
        )
        assert ids == ["C", "A", "B"]

    def test_a_non_duplicate_keeps_its_place_relative_to_non_duplicates(self):
        """B is a true near-duplicate of A, but that must not drag B below C."""
        hits = [
            _hit("transcript", CHUNK_C, id="C"),
            _hit("transcript", CHUNK_B, id="B"),
            _hit("file", CHUNK_A, id="A"),
        ]
        prefer_curated(hits)
        ids = [h["id"] for h in hits]
        assert ids.index("C") < ids.index("B"), "B keeps its place behind C"


# ── a hit must never pass something it does not outrank ────────────────

# One transcript that duplicates BOTH a diary (which it outranks) and a card
# (which it does not). The shared halves are disjoint, so the diary and the
# card do not duplicate each other.
_SEGa = (
    "a documented refuser cold booted onto the access point registered and "
    "carried an answered call on the same sim and the same network code"
)
_SEGb = (
    "the five handsets refused one cell and the fault was never inside their "
    "modems which is what the corrected headline now records in full"
)
_SEGc = (
    "the checkpoint hook filed its periodic summary at the end of the session "
    "with no source file behind it that a reader could open and verify"
)
_SEGd = (
    "this correction supersedes the earlier headline and is dated the sixth of "
    "september after the measurement that overturned the original claim"
)
TXT_TRANSCRIPT = _SEGa + " " + _SEGb
TXT_DIARY = _SEGa + " " + _SEGc
TXT_CARD = _SEGb + " " + _SEGd


class TestNeverPassesAHitItDoesNotOutrank:
    """The rank check has to guard every hit PASSED, not just the hit earned from.

    Regression executed by review: a transcript that duplicates a diary below
    it in kind AND a card above it in kind earned the diary's index, and in
    landing there it sailed past the card — demoting the curated hit below a
    transcript that quotes it, which is the exact inverse of the feature.
    """

    def test_the_repro_has_the_similarities_it_needs(self):
        assert is_near_duplicate(TXT_TRANSCRIPT, TXT_DIARY) is True
        assert is_near_duplicate(TXT_TRANSCRIPT, TXT_CARD) is True
        assert is_near_duplicate(TXT_CARD, TXT_DIARY) is False

    def test_transcript_does_not_overtake_the_card_it_duplicates(self):
        hits = [
            _hit("diary", TXT_DIARY, id="DIARY"),
            _hit("file", TXT_CARD, id="CARD"),
            _hit("transcript", TXT_TRANSCRIPT, id="TRANSCRIPT"),
        ]
        prefer_curated(hits)
        ids = [h["id"] for h in hits]
        assert ids.index("CARD") < ids.index("TRANSCRIPT"), (
            "the curated card must never end up below a transcript quoting it"
        )
        assert ids == ["DIARY", "CARD", "TRANSCRIPT"], (
            "the card already outranks the transcript — there is nothing to do"
        )


class TestOrderingProperties:
    """Randomised three-kind inputs. The suite missed the regression because
    nothing covered one hit duplicating two others of DIFFERENT kinds."""

    KINDS = ("file", "memory", "transcript", "diary", "unknown")
    SEGMENTS = (_SEGa, _SEGb, _SEGc, _SEGd, _SEG1, _SEG2, _SEG3)

    def _random_hits(self, rnd):
        n = rnd.randint(3, 6)
        out = []
        for i in range(n):
            parts = rnd.sample(self.SEGMENTS, rnd.randint(1, 2))
            out.append(_hit(rnd.choice(self.KINDS), " ".join(parts), id=f"h{i}"))
        return out

    def test_is_idempotent_over_randomised_inputs(self):
        import random

        rnd = random.Random(451)
        for case in range(4000):
            hits = self._random_hits(rnd)
            once = [h["id"] for h in prefer_curated(list(hits))]
            twice = [h["id"] for h in prefer_curated(prefer_curated(list(hits)))]
            assert once == twice, f"case {case}: not a fixed point, {once} -> {twice}"

    def test_never_passes_a_hit_it_does_not_outrank(self):
        """THE BOUND as an invariant over every pair, not just the promoted one."""
        import random

        from mempalace.result_ordering import _kind_rank

        rnd = random.Random(4510)
        for case in range(4000):
            hits = self._random_hits(rnd)
            before = {h["id"]: (i, _kind_rank(h)) for i, h in enumerate(hits)}
            after = [h["id"] for h in prefer_curated(list(hits))]
            pos = {hid: i for i, hid in enumerate(after)}
            for a, (ia, ra) in before.items():
                for b, (ib, rb) in before.items():
                    if ia < ib and ra <= rb:
                        assert pos[a] < pos[b], (
                            f"case {case}: {b} (rank {rb}) overtook {a} (rank {ra}) "
                            f"without outranking it"
                        )


class TestStabilisationCap:
    """The loop promises a fixed point. If it ever runs out of passes it must
    SAY so — a silently under-settled order contradicts the docstring and, at
    the old cap of 8, happened routinely on dense inputs at n=40 (the widen
    cap). Raised to 32, which measured zero non-fixed-points at that width."""

    def test_cap_is_high_enough_for_the_widest_window_we_fetch(self):
        from mempalace.result_ordering import _MAX_STABILISE_PASSES
        from mempalace.cli import _WIDEN_CAP

        # 32 still left 2/600 non-fixed-points on a dense generator at n=40;
        # 48 measured zero, at identical cost (the loop exits as soon as a
        # pass moves nothing). Pinned so a future trim re-measures first.
        assert _MAX_STABILISE_PASSES >= 48, (
            f"cap must clear the densest window the widen can produce (_WIDEN_CAP={_WIDEN_CAP})"
        )

    def test_warns_when_the_cap_is_exhausted(self, caplog, monkeypatch):
        import logging

        import mempalace.result_ordering as ro

        # One pass is provably not enough for this input, so the cap is hit.
        monkeypatch.setattr(ro, "_MAX_STABILISE_PASSES", 1)
        hits = [
            _hit("transcript", CHUNK_C, id="C"),
            _hit("transcript", CHUNK_B, id="B"),
            _hit("file", CHUNK_A, id="A"),
        ]
        with caplog.at_level(logging.WARNING, logger="mempalace.result_ordering"):
            ro.prefer_curated(hits)
        assert any("did not settle" in r.message for r in caplog.records), (
            f"cap exhaustion must not be silent; got {[r.message for r in caplog.records]}"
        )

    def test_silent_when_the_order_settles(self, caplog):
        import logging

        import mempalace.result_ordering as ro

        hits = [_hit("transcript", TRANSCRIPT, id="t"), _hit("file", CARD, id="f")]
        with caplog.at_level(logging.WARNING, logger="mempalace.result_ordering"):
            ro.prefer_curated(hits)
        assert not caplog.records, "a settled order must log nothing"


# ── wiring: every interactive route applies the preference ──────────────


class TestSearcherAppliesPreference:
    def test_envelope_orders_curated_above_its_near_duplicate(self):
        from mempalace.searcher import _search_result_envelope

        out = _search_result_envelope(
            query="q",
            wing=None,
            room=None,
            source_file=None,
            since=None,
            before=None,
            hits=[
                {"source_file": "a.jsonl", "text": TRANSCRIPT, "id": "t"},
                {"source_file": "/p/CLAUDE.md", "text": CARD, "id": "f"},
            ],
            candidates_fetched=2,
            pool_size=20,
            date_window_active=False,
        )
        assert [h["id"] for h in out["results"]] == ["f", "t"]

    def test_envelope_leaves_unrelated_hits_in_ranker_order(self):
        from mempalace.searcher import _search_result_envelope

        out = _search_result_envelope(
            query="q",
            wing=None,
            room=None,
            source_file=None,
            since=None,
            before=None,
            hits=[
                {"source_file": "a.jsonl", "text": UNRELATED, "id": "t"},
                {"source_file": "/p/CLAUDE.md", "text": CARD, "id": "f"},
            ],
            candidates_fetched=2,
            pool_size=20,
            date_window_active=False,
        )
        assert [h["id"] for h in out["results"]] == ["t", "f"]
