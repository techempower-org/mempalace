"""Default search leaves out drawers flagged ``judged_class`` chatter or stale; ``all_classes`` brings them back.

docs/designs/judged-drawer-classes.md, condition 2: the search exclusion ships, with an include flag, before
any flag is written, and a flagged drawer must never become unreachable. These tests run against a real
temporary Chroma palace (the ``seeded_collection`` fixture), not a mock of the search pipeline.
"""

import argparse
from unittest.mock import patch

import pytest

from mempalace.searcher import JUDGED_SEARCH_EXCLUDED, _judged_hidden_ids, search_memories

QUERY = "authentication JWT tokens session database migrations React frontend sprint planning"
ALL_IDS = {
    "drawer_proj_backend_aaa",
    "drawer_proj_backend_bbb",
    "drawer_proj_frontend_ccc",
    "drawer_notes_planning_ddd",
}


def _flag(col, flags):
    got = col.get(ids=list(flags), include=["metadatas"])
    metas = []
    for drawer_id, meta in zip(got["ids"], got["metadatas"]):
        meta = dict(meta or {})
        meta["judged_class"] = flags[drawer_id]
        metas.append(meta)
    col.update(ids=got["ids"], metadatas=metas)


def _ids(result):
    return {h["drawer_id"] for h in result.get("results") or []}


@pytest.fixture
def flagged(seeded_collection, palace_path):
    _flag(
        seeded_collection,
        {
            "drawer_proj_backend_aaa": "chatter",
            "drawer_proj_backend_bbb": "stale",
            "drawer_proj_frontend_ccc": "secret",
        },
    )
    return palace_path


def test_excluded_classes_are_chatter_and_stale_only():
    assert JUDGED_SEARCH_EXCLUDED == frozenset({"chatter", "stale"})


def test_default_search_leaves_out_chatter_and_stale(flagged):
    result = search_memories(QUERY, flagged, n_results=4)
    ids = _ids(result)
    assert "drawer_proj_backend_aaa" not in ids
    assert "drawer_proj_backend_bbb" not in ids
    # secret is not a search exclusion; an unflagged drawer is untouched
    assert {"drawer_proj_frontend_ccc", "drawer_notes_planning_ddd"} <= ids
    assert result["judged_excluded"] == 2
    assert any("--all-classes" in w for w in result.get("warnings") or [])


def test_all_classes_returns_every_drawer(flagged):
    result = search_memories(QUERY, flagged, n_results=4, all_classes=True)
    assert _ids(result) == ALL_IDS
    assert "judged_excluded" not in result


def test_unflagged_palace_is_unchanged(seeded_collection, palace_path):
    plain = search_memories(QUERY, palace_path, n_results=4)
    every = search_memories(QUERY, palace_path, n_results=4, all_classes=True)
    assert _ids(plain) == _ids(every) == ALL_IDS
    assert plain["judged_excluded"] == 0


def test_page_still_fills_when_flagged_drawers_rank_first(flagged):
    """Two flagged drawers in the top two: the pool widens so two unflagged drawers come back."""
    result = search_memories(
        "JWT tokens authentication PostgreSQL Alembic migrations", flagged, n_results=2
    )
    ids = _ids(result)
    assert len(ids) == 2
    assert not ids & {"drawer_proj_backend_aaa", "drawer_proj_backend_bbb"}


def test_lookup_fails_open_for_unknown_ids(flagged):
    assert _judged_hidden_ids(flagged, None, ["no_such_drawer", ""]) == set()
    assert _judged_hidden_ids(
        flagged, None, ["drawer_proj_backend_aaa", "drawer_proj_frontend_ccc"]
    ) == {"drawer_proj_backend_aaa"}


def test_mcp_tool_passes_all_classes_and_rejects_non_bool(monkeypatch):
    from mempalace import mcp_server

    captured = []

    def fake_search(*args, **kwargs):
        captured.append(kwargs)
        return {"results": []}

    monkeypatch.setattr(mcp_server, "search_memories", fake_search)
    mcp_server.tool_search(query="anything", all_classes=True)
    assert captured[-1]["all_classes"] is True
    mcp_server.tool_search(query="anything")
    assert captured[-1]["all_classes"] is False
    assert "error" in mcp_server.tool_search(query="anything", all_classes="yes")


def test_cli_flag_reaches_search_memories(tmp_path):
    from mempalace import cli

    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is not None:
        args = parser.parse_args(["search", "q", "--all-classes"])
        assert args.all_classes is True
    args = argparse.Namespace(
        query="q",
        palace=str(tmp_path),
        wing=None,
        room=None,
        tags=None,
        results=5,
        format="compact",
        json=False,
        quiet=True,
        mode=None,
        all_classes=True,
    )
    with (
        patch("mempalace.cli._daemon_strict", return_value=False),
        patch("mempalace.cli._forward_search_to_hub", return_value=False),
        patch("mempalace.searcher.search_memories", return_value={"results": []}) as sm,
        patch("mempalace.cli._print_daemon_search"),
        pytest.raises(SystemExit),
    ):
        cli.cmd_search(args)
    assert sm.call_args.kwargs["all_classes"] is True


def test_hub_never_forwards_an_all_classes_search():
    from mempalace.cli import _search_args_forwardable

    args = argparse.Namespace(
        query="q", results=5, include_media=False, query_task="search", all_classes=True
    )
    assert _search_args_forwardable(args) is False


def test_daemon_strict_puts_the_flag_on_the_tool_call(tmp_path):
    from mempalace import cli

    args = argparse.Namespace(
        query="q",
        palace=None,
        wing=None,
        room=None,
        tags=None,
        results=5,
        format="compact",
        json=False,
        quiet=True,
        mode="hybrid",
        all_classes=True,
    )
    with (
        patch("mempalace.cli._daemon_strict", return_value=True),
        patch("mempalace.cli._daemon_search_hybrid") as hybrid,
        patch("mempalace.cli._call_daemon_tool", return_value={"results": []}) as tool,
        patch("mempalace.cli._print_daemon_search"),
    ):
        cli.cmd_search(args)
    hybrid.assert_not_called()
    assert tool.call_args.args[1]["all_classes"] is True


# ── xreview round 1 (2026-11-15): folded copies, logical parent ids, media, fail-open, bounded widening ──


def _add_chunks(col, parent, classes):
    ids = [f"{parent}_chunk{i}" for i in range(len(classes))]
    metas = []
    for i, cls in enumerate(classes):
        meta = {
            "wing": "notes",
            "room": "planning",
            "source_file": f"{parent}.md",
            "chunk_index": i,
            "parent_drawer_id": parent,
            "filed_at": "2026-01-05T00:00:00",
        }
        if cls:
            meta["judged_class"] = cls
        metas.append(meta)
    col.add(
        ids=ids, documents=[f"chunk {i} of {parent}" for i in range(len(classes))], metadatas=metas
    )


def test_logical_parent_id_hidden_only_when_every_chunk_is_flagged(seeded_collection, palace_path):
    _add_chunks(seeded_collection, "logical_all", ["chatter", "stale"])
    _add_chunks(seeded_collection, "logical_mixed", ["chatter", None])
    hidden = _judged_hidden_ids(
        palace_path, None, ["logical_all", "logical_mixed", "logical_unknown"]
    )
    assert hidden == {"logical_all"}


def test_folded_hit_stays_while_any_copy_is_unflagged():
    from mempalace.searcher import _drop_judged_hits

    hit = {"drawer_id": "a", "also_in": [{"drawer_id": "b"}]}
    assert _drop_judged_hits([hit], {"a"}) == [hit]
    assert _drop_judged_hits([hit], {"a", "b"}) == []
    no_id = {"text": "media asset"}
    assert _drop_judged_hits([no_id], {"a"}) == [no_id]


def test_lookup_failure_keeps_everything(flagged, monkeypatch):
    import mempalace.searcher as S

    def boom(*a, **k):
        raise RuntimeError("backend down")

    monkeypatch.setitem(S.__dict__, "_open_search_collection", boom)
    assert _judged_hidden_ids(flagged, None, ["drawer_proj_backend_aaa"]) == set()


def _fake_impl(monkeypatch, pages):
    """Replace the inner search with one returning ``pages(n)`` and count the calls."""
    import mempalace.searcher as S

    calls = []

    def impl(query, palace_path, **kw):
        calls.append(kw["n_results"])
        return {"results": pages(kw["n_results"]), "warnings": []}

    monkeypatch.setitem(S.__dict__, "_search_memories_impl", impl)
    return calls


def test_media_search_still_filters_text_drawers(flagged, monkeypatch):
    _fake_impl(
        monkeypatch,
        lambda n: [
            {"drawer_id": "drawer_proj_backend_aaa", "text": "x"},
            {"path": "/media/diagram.png"},
            {"drawer_id": "drawer_notes_planning_ddd", "text": "y"},
        ],
    )
    out = search_memories("q", flagged, n_results=5, include_media=True)
    assert [h.get("drawer_id") for h in out["results"]] == [None, "drawer_notes_planning_ddd"]
    assert out["judged_excluded"] == 1


def test_widening_is_bounded_when_everything_is_flagged(flagged, monkeypatch):
    calls = _fake_impl(monkeypatch, lambda n: [{"drawer_id": "drawer_proj_backend_aaa"}] * n)
    out = search_memories("q", flagged, n_results=4)
    assert out["results"] == []
    assert len(calls) <= 3
    assert max(calls) <= 12


def test_slow_first_search_is_not_repeated(flagged, monkeypatch):
    import mempalace.searcher as S

    calls = _fake_impl(monkeypatch, lambda n: [{"drawer_id": "drawer_proj_backend_aaa"}] * n)
    monkeypatch.setitem(S.__dict__, "_JUDGED_WIDEN_SLOW_S", -1.0)
    search_memories("q", flagged, n_results=4)
    assert calls == [4]


def test_vector_disabled_path_filters_too(flagged):
    out = search_memories("JWT tokens authentication", flagged, n_results=4, vector_disabled=True)
    assert "drawer_proj_backend_aaa" not in _ids(out)


# ── xreview round 2: bounded parent resolution ──


def test_parent_rows_past_the_limit_keep_the_parent_visible(
    seeded_collection, palace_path, monkeypatch
):
    import mempalace.searcher as S

    _add_chunks(seeded_collection, "logical_big", ["chatter"] * 5)
    monkeypatch.setitem(S.__dict__, "_JUDGED_PARENT_ROW_LIMIT", 3)
    assert _judged_hidden_ids(palace_path, None, ["logical_big"]) == set()
    monkeypatch.setitem(S.__dict__, "_JUDGED_PARENT_ROW_LIMIT", 256)
    assert _judged_hidden_ids(palace_path, None, ["logical_big"]) == {"logical_big"}


def test_spent_lookup_budget_skips_parent_resolution(seeded_collection, palace_path, monkeypatch):
    import mempalace.searcher as S

    _add_chunks(seeded_collection, "logical_slow", ["chatter", "stale"])
    monkeypatch.setitem(S.__dict__, "_JUDGED_LOOKUP_BUDGET_S", -1.0)
    assert _judged_hidden_ids(palace_path, None, ["logical_slow"]) == set()


def test_a_full_page_on_one_parent_key_stops_the_other_key_hiding_it(
    seeded_collection, palace_path, monkeypatch
):
    """xreview r3: parent_drawer_id=P overflows (an unflagged chunk unread); parent_entry_id=P has one flagged chunk."""
    import mempalace.searcher as S

    _add_chunks(
        seeded_collection, "P", ["chatter", "chatter", None]
    )  # parent_drawer_id rows, one unflagged
    seeded_collection.add(
        ids=["P_entry0"],
        documents=["entry chunk"],
        metadatas=[
            {"wing": "notes", "room": "planning", "parent_entry_id": "P", "judged_class": "chatter"}
        ],
    )
    monkeypatch.setitem(S.__dict__, "_JUDGED_PARENT_ROW_LIMIT", 2)
    assert _judged_hidden_ids(palace_path, None, ["P"]) == set()
