"""A Python list reaches Cypher as a Cypher list, so the delete-through deletes.

Until 2026-10-24 ``_cypher_literal`` sent every non-scalar through ``json.dumps`` and quoted it, so
``delete_drawers``' ``UNWIND $ids`` unwound one string and matched no drawer. A probe on a real AGE 1.6 graph (the
production-size scratch restore) confirmed it: both drawers survived ``delete_drawers``. ``delete_drawers`` also ran
without ``fetch=True``, so its count was always 0 and hid the miss.
"""

import pytest

from mempalace.knowledge_graph_age import (
    _AGE_DQ_TAG,
    KnowledgeGraphAGE,
    _cypher_literal,
    _inline_cypher_params,
)


def test_a_list_renders_as_a_cypher_list():
    assert _cypher_literal(["a", "b"]) == "['a', 'b']"
    assert _cypher_literal(("a",)) == "['a']"
    assert _cypher_literal([]) == "[]"


def test_list_elements_render_like_scalars():
    assert _cypher_literal([1, 2.5, True, None, "x"]) == "[1, 2.5, true, NULL, 'x']"
    assert _cypher_literal(["O'Brien"]) == "['O\\'Brien']"
    assert _cypher_literal([["a"], ["b", "c"]]) == "[['a'], ['b', 'c']]"


def test_a_list_element_carrying_the_dollar_quote_tag_is_still_refused():
    with pytest.raises(ValueError):
        _cypher_literal(["ok", f"bad {_AGE_DQ_TAG} value"])


def test_dicts_keep_their_json_string_form():
    assert _cypher_literal({"k": 1}) == "'{\"k\": 1}'"


def test_unwind_of_a_list_param_inlines_a_list():
    assert _inline_cypher_params("UNWIND $ids AS did", {"ids": ["d1", "d2"]}) == (
        "UNWIND ['d1', 'd2'] AS did"
    )


def _capturing_kg(rows):
    kg = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)
    kg.sent = []

    def _run_cypher(cypher, params, *, commit=True, fetch=False):
        kg.sent.append((_inline_cypher_params(" ".join(cypher.split()), params), commit, fetch))
        return rows if fetch else []

    kg._run_cypher = _run_cypher
    return kg


def test_delete_drawers_unwinds_a_real_list_and_counts_what_it_removed():
    kg = _capturing_kg(rows=[(1,), (1,)])
    assert kg.delete_drawers(["d1", "d2"]) == 2
    ((sql, commit, fetch),) = kg.sent
    assert sql.startswith("UNWIND ['d1', 'd2'] AS did MATCH (d:Drawer {id: did}) DETACH DELETE d")
    assert commit is True and fetch is True


def test_delete_drawers_with_nothing_to_delete_sends_nothing():
    kg = _capturing_kg(rows=[])
    assert kg.delete_drawers([]) == 0
    assert kg.sent == []
