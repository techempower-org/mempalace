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
