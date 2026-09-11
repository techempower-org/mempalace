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
