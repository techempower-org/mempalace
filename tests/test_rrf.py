"""Unit tests for mempalace.rrf — pure Reciprocal Rank Fusion math.

These are the cheap, fast tests. The end-to-end multi-encoder
retrieval path is exercised by ``test_multi_encoder.py``.
"""

from __future__ import annotations

import math

import pytest

from mempalace.rrf import DEFAULT_K, explain_fusion, rrf_fuse, rrf_scores


def test_single_list_identity():
    """RRF over one list reproduces its rank ordering."""
    items = ["a", "b", "c", "d"]
    scored = rrf_scores([items])
    # Rank 1 > rank 2 > rank 3 > rank 4.
    assert scored["a"] > scored["b"] > scored["c"] > scored["d"]
    # Numerically: 1/(60+1), 1/(60+2), …
    assert math.isclose(scored["a"], 1 / 61)
    assert math.isclose(scored["d"], 1 / 64)


def test_two_list_fusion_promotes_consensus_winner():
    """Item ranked top in both lists fuses higher than item ranked top in one."""
    list_a = ["x", "y", "z"]
    list_b = ["x", "z", "y"]
    scored = rrf_scores([list_a, list_b])
    # x is rank 1 in both; y is rank 2 then rank 3; z is rank 3 then rank 2.
    # x: 1/61 + 1/61 = 2/61
    # y: 1/62 + 1/63
    # z: 1/63 + 1/62
    assert scored["x"] > scored["y"]
    assert scored["x"] > scored["z"]
    # y and z are symmetric.
    assert math.isclose(scored["y"], scored["z"])


def test_only_in_one_list_still_appears():
    """Item present in only one list still gets a score."""
    list_a = ["a", "b"]
    list_b = ["c", "d"]
    scored = rrf_scores([list_a, list_b])
    assert set(scored) == {"a", "b", "c", "d"}
    # No cross-list reinforcement: a and c are both rank 1 in their list,
    # tied at 1/61.
    assert math.isclose(scored["a"], scored["c"])


def test_k_smoothing_changes_top_gap():
    """Larger k makes top-1 and top-2 closer (smaller relative gap)."""
    items = ["a", "b"]
    small_k = rrf_scores([items], k=1)
    large_k = rrf_scores([items], k=1000)
    gap_small = small_k["a"] - small_k["b"]
    gap_large = large_k["a"] - large_k["b"]
    assert gap_small > gap_large


def test_rrf_fuse_returns_sorted_descending():
    list_a = ["x", "y", "z"]
    list_b = ["y", "x", "z"]
    fused = rrf_fuse([list_a, list_b])
    scores = [score for _ident, score, _rep in fused]
    assert scores == sorted(scores, reverse=True)


def test_rrf_fuse_first_occurrence_representative_by_default():
    """Default representative is the first occurrence (best-ranked-list copy)."""
    list_a = [("x", "from-a")]
    list_b = [("x", "from-b")]
    fused = rrf_fuse([list_a, list_b], key=lambda pair: pair[0])
    assert len(fused) == 1
    _ident, _score, rep = fused[0]
    assert rep == ("x", "from-a")


def test_rrf_fuse_custom_representative():
    """Custom representative selector wins over default."""
    list_a = [("x", "short")]
    list_b = [("x", "longer-description")]
    fused = rrf_fuse(
        [list_a, list_b],
        key=lambda pair: pair[0],
        representative=lambda items: max(items, key=lambda p: len(p[1])),
    )
    _ident, _score, rep = fused[0]
    assert rep == ("x", "longer-description")


def test_rrf_fuse_handles_unhashable_items_via_key():
    """Dicts are unhashable; key= must be provided to make them fusable."""
    list_a = [{"id": "x", "v": 1}, {"id": "y", "v": 2}]
    list_b = [{"id": "y", "v": 2}, {"id": "x", "v": 1}]
    fused = rrf_fuse([list_a, list_b], key=lambda d: d["id"])
    idents = [t[0] for t in fused]
    # x and y both appear in both lists, with symmetric ranks (1 vs 2 /
    # 2 vs 1) → identical scores. Sort is stable on tie, so the
    # first-seen identity (x) comes first.
    assert set(idents) == {"x", "y"}


def test_rrf_zero_k_rejected():
    with pytest.raises(ValueError, match="k must be"):
        rrf_scores([["a"]], k=0)


def test_rrf_scores_within_list_dedup():
    """Duplicate identities within a single list use the first-seen rank."""
    items = ["a", "b", "a"]
    scored = rrf_scores([items])
    # 'a' rank 1, 'b' rank 2; the second 'a' at rank 3 is ignored.
    assert math.isclose(scored["a"], 1 / (DEFAULT_K + 1))
    assert math.isclose(scored["b"], 1 / (DEFAULT_K + 2))


def test_explain_fusion_reports_per_list_ranks():
    list_a = ["x", "y", "z"]
    list_b = ["y", "z"]
    explain = explain_fusion([list_a, list_b], list_names=["A", "B"])
    by_ident = {row["identity"]: row for row in explain}
    assert by_ident["x"]["ranks"] == {"A": 1, "B": None}
    assert by_ident["y"]["ranks"] == {"A": 2, "B": 1}
    assert by_ident["z"]["ranks"] == {"A": 3, "B": 2}
    # Output is sorted by RRF score descending.
    scores = [row["rrf_score"] for row in explain]
    assert scores == sorted(scores, reverse=True)


def test_explain_fusion_default_list_names():
    list_a = ["x"]
    list_b = ["y"]
    explain = explain_fusion([list_a, list_b])
    names = list(explain[0]["ranks"].keys())
    assert names == ["list_0", "list_1"]


def test_explain_fusion_rejects_name_count_mismatch():
    with pytest.raises(ValueError, match="list_names length"):
        explain_fusion([["x"], ["y"]], list_names=["only_one"])
