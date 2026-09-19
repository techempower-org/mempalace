"""The recall harness must refuse to score a corpus that is not indexed.

An unindexed corpus makes every partition read recall@3 = 0, which lands on the
falsifier row "baseline low and unchanged" and reads as a refutation of the idea
rather than as "the drawers are not there yet". The refusal is the guard, so the
refusal path is what these tests exercise.
"""

import importlib.util
import pathlib

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "failure_shape_recall.py"


def _load():
    spec = importlib.util.spec_from_file_location("failure_shape_recall", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _load()


def test_control_record_picks_a_slug_and_a_usable_phrase(harness):
    slug, phrase = harness.control_record()
    assert slug, "no record offered a control phrase"
    assert phrase and len(phrase) >= 20


def test_refuses_when_search_returns_nothing(harness):
    """The whole point: a silent corpus must not be scored as a silent index."""
    indexed, reason = harness.corpus_is_indexed("memorypalace", 30, search_fn=lambda *a, **k: [])
    assert indexed is False
    assert "corpus not indexed" in reason
    assert "not retrievable" in reason


def test_refusal_names_the_slug_it_could_not_find(harness):
    slug, _ = harness.control_record()
    _, reason = harness.corpus_is_indexed("memorypalace", 30, search_fn=lambda *a, **k: [])
    assert slug in reason, "the refusal must name the record, so the reader can check it by hand"


def test_accepts_when_the_control_record_comes_back(harness):
    """Positive control on the guard itself: it must be able to pass, not only to fail."""
    slug, _ = harness.control_record()
    hit = [{"drawer_id": "d1", "content": f"... {slug} ..."}]
    indexed, reason = harness.corpus_is_indexed("memorypalace", 30, search_fn=lambda *a, **k: hit)
    assert indexed is True
    assert "ok" in reason


def test_a_hit_for_a_different_record_does_not_satisfy_the_control(harness):
    """Any-hit is not the question; the CONTROL RECORD coming back is."""
    other = [{"drawer_id": "d9", "content": "an unrelated drawer with no slug in it"}]
    indexed, _ = harness.corpus_is_indexed("memorypalace", 30, search_fn=lambda *a, **k: other)
    assert indexed is False


# ── token overlap: reported beside recall, never subtracted ──────────────────
#
# A high overlap does not invalidate a hit; it explains one. The column exists so
# that a partition's recall can be read next to how much of the trigger's vocabulary
# the record already carries. Before the column is trusted it must be seen to
# discriminate in BOTH directions: a metric only ever observed mid-range is not known
# to see anything.


def test_overlap_high_arm_slug_vocabulary_scores_high(harness):
    """A trigger deliberately built from the slug's own words."""
    score = harness.overlap("take the count from the tally line", "tally line")
    assert score is not None and score >= 0.5, score


def test_overlap_low_arm_disjoint_trigger_scores_zero(harness):
    score = harness.overlap("purple elephants dance quietly", "tally line")
    assert score == 0.0


def test_overlap_is_not_a_constant(harness):
    """Non-constancy: the two arms must differ on the same target."""
    high = harness.overlap("take the count from the tally line", "tally line")
    low = harness.overlap("purple elephants dance quietly", "tally line")
    assert high != low


def test_overlap_empty_trigger_is_not_reported_as_disjoint(harness):
    """|T| = 0 must render as n/a, never as 0.0 — a zero reads as 'disjoint'."""
    assert harness.overlap("the of a", "tally line") is None


def test_overlap_columns_against_a_real_record_both_directions(harness):
    """Both columns, driven end to end from the on-disk record `tally-line`."""
    asked_answered = harness.record_asked_answered("tally-line")
    assert asked_answered and "summary" in asked_answered.lower()
    high_slug = harness.overlap("take the count from the tally line", "tally-line")
    high_aa = harness.overlap("what number did the author state in the summary", asked_answered)
    low_slug = harness.overlap("purple elephants dance quietly", "tally-line")
    low_aa = harness.overlap("purple elephants dance quietly", asked_answered)
    assert high_slug >= 0.5 and high_aa >= 0.5, (high_slug, high_aa)
    assert low_slug == 0.0 and low_aa == 0.0


def test_slug_tokens_split_on_hyphen(harness):
    assert harness.tokens("bare-path-receipt") == {"bare", "path", "receipt"}


def test_overlap_only_mode_scores_without_search(harness, tmp_path, capsys, monkeypatch):
    """--overlap-only must never call search or the corpus control."""
    csv_path = tmp_path / "t.csv"
    csv_path.write_text(
        "trigger,expected_slug,partition\n"
        '"take the count from the summary line at the end",tally-line,independent-blind\n'
        "TBD(fresh-lane),bare-path-receipt,independent-fresh\n",
        encoding="utf-8",
    )

    def boom(*a, **k):
        raise AssertionError("search must not run under --overlap-only")

    monkeypatch.setattr(harness, "search", boom)
    rc = harness.main(["--triggers", str(csv_path), "--overlap-only"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "tally-line" in out and "ovl/slug" in out and "ovl/asked+answered" in out
    assert "1 pending" in out


def test_readme_in_records_dir_is_not_a_record(harness, tmp_path, monkeypatch):
    """docs/failure-shapes/README.md has no front matter and must not fail the set check."""
    recs = tmp_path / "failure-shapes"
    recs.mkdir()
    (recs / "README.md").write_text("# not a record\n", encoding="utf-8")
    (recs / "a-shape.md").write_text("---\nslug: a-shape\n---\n# a-shape\n", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("### The runnable table\n\n| `a-shape` | A | x |\n", encoding="utf-8")
    monkeypatch.setattr(harness, "RECORDS", recs)
    monkeypatch.setattr(harness, "SPEC", spec)
    assert harness.check_set(verbose=False) == 0
    slugs, mismatched = harness.record_slugs()
    assert slugs == {"a-shape"} and mismatched == []
