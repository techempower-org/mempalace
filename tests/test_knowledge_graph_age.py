"""Integration tests for the AGE-backed KnowledgeGraph implementation.

Requires a live Postgres with Apache AGE installed. Set TEST_POSTGRES_DSN
to point at one (e.g. the homelab `mempalace-db` container documented in
`scratch/postgres-preflight-2026-05-10.md`); skipped by default so the
suite stays green on machines without a postgres at hand.

Pairs with `mempalace/knowledge_graph_age.py`. The classic SQLite-backed
`KnowledgeGraph` in `mempalace/knowledge_graph.py` stays the default;
the AGE backend is opt-in via `MEMPALACE_KG_BACKEND=age` once the
config-routing layer is wired.
"""

import os

import pytest

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")


# ── _cypher_literal (no postgres required) ───────────────────────────


def test_cypher_literal_none():
    from mempalace.knowledge_graph_age import _cypher_literal

    assert _cypher_literal(None) == "NULL"


def test_cypher_literal_int():
    from mempalace.knowledge_graph_age import _cypher_literal

    assert _cypher_literal(42) == "42"
    assert _cypher_literal(0) == "0"
    assert _cypher_literal(-7) == "-7"


def test_cypher_literal_float():
    from mempalace.knowledge_graph_age import _cypher_literal

    assert _cypher_literal(3.14) == "3.14"
    assert _cypher_literal(1.0) == "1.0"


def test_cypher_literal_bool():
    """bool is rendered as Cypher true/false. Must be checked BEFORE int
    in the implementation because bool is a subclass of int in Python."""
    from mempalace.knowledge_graph_age import _cypher_literal

    assert _cypher_literal(True) == "true"
    assert _cypher_literal(False) == "false"


def test_cypher_literal_simple_string():
    from mempalace.knowledge_graph_age import _cypher_literal

    assert _cypher_literal("hello") == "'hello'"


def test_cypher_literal_escapes_single_quote():
    """Single quotes get backslash-escaped so the closing quote isn't ambiguous."""
    from mempalace.knowledge_graph_age import _cypher_literal

    assert _cypher_literal("it's") == "'it\\'s'"


def test_cypher_literal_escapes_backslash():
    """Backslashes double up so AGE's parser doesn't consume them."""
    from mempalace.knowledge_graph_age import _cypher_literal

    assert _cypher_literal("a\\b") == "'a\\\\b'"


# ── AGE-backed tests (gate on real postgres) ─────────────────────────


pgmark = pytest.mark.skipif(
    POSTGRES_DSN is None,
    reason="set TEST_POSTGRES_DSN to run AGE knowledge-graph tests",
)


@pgmark
def test_age_kg_instantiates():
    """KnowledgeGraphAGE opens a connection and exits cleanly."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    assert kg is not None
    kg.close()


@pgmark
def test_age_graph_created():
    """`mempalace_kg` graph is registered in AGE's catalog after init."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        with kg._conn.cursor() as cur:
            cur.execute(
                "SELECT graphid FROM ag_catalog.ag_graph WHERE name = %s",
                (KnowledgeGraphAGE.GRAPH_NAME,),
            )
            row = cur.fetchone()
            assert row is not None, "mempalace_kg graph should exist after init"
            assert row[0] is not None, "graph should have a non-null graphid"
    finally:
        kg.close()


@pgmark
def test_age_context_manager():
    """`with KnowledgeGraphAGE(...) as kg:` closes the conn on exit."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    with KnowledgeGraphAGE(dsn=POSTGRES_DSN) as kg:
        assert kg._conn is not None
        assert not kg._conn.closed
    # After the with block, the connection should be closed.
    assert kg._conn.closed


@pgmark
def test_age_add_triple_basic():
    """add_triple persists a triple that query_triples can read back."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(
            subject="JP",
            relation_type="works_on",
            object_="mempalace",
            source="drawer_abc",
            valid_from="2026-05-01",
            valid_to=None,
            confidence=0.9,
        )
        triples = kg.query_triples(subject="JP")
        assert len(triples) == 1
        t = triples[0]
        assert t["subject"] == "JP"
        assert t["relation_type"] == "works_on"
        assert t["object"] == "mempalace"
        assert t["source"] == "drawer_abc"
        assert t["valid_from"] == "2026-05-01"
        assert t["confidence"] == 0.9
    finally:
        kg.close()


@pgmark
def test_age_rejects_inverted_temporal_interval():
    """add_triple rejects valid_to < valid_from at write time."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        with pytest.raises(ValueError, match="valid_to.*valid_from"):
            kg.add_triple(
                subject="X",
                relation_type="r",
                object_="Y",
                valid_from="2026-05-10",
                valid_to="2026-05-01",  # inverted
            )
    finally:
        kg.close()


@pgmark
def test_age_query_triples_returns_empty_on_no_match():
    """query_triples returns [] when nothing matches the filter."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(subject="Alice", relation_type="knows", object_="Bob")
        triples = kg.query_triples(subject="NonExistent")
        assert triples == []
    finally:
        kg.close()


@pgmark
def test_age_clear_drops_and_recreates_graph():
    """clear() removes existing triples and restores empty graph."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(subject="A", relation_type="r", object_="B")
        assert len(kg.query_triples(subject="A")) == 1
        kg.clear()
        assert kg.query_triples(subject="A") == []
    finally:
        kg.close()


@pgmark
def test_age_as_of_filter():
    """as_of filter returns only triples whose interval contains the date."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        # Closed interval, ended last year
        kg.add_triple(
            "JP",
            "works_on",
            "old_project",
            valid_from="2024-01-01",
            valid_to="2025-12-31",
        )
        # Open-ended interval, still active
        kg.add_triple(
            "JP",
            "works_on",
            "mempalace",
            valid_from="2026-04-21",
            valid_to=None,
        )

        # As of 2026-05-01, only mempalace is active
        active = kg.query_triples(subject="JP", as_of="2026-05-01")
        assert len(active) == 1
        assert active[0]["object"] == "mempalace"

        # As of 2025-06-01, only old_project was active
        old = kg.query_triples(subject="JP", as_of="2025-06-01")
        assert len(old) == 1
        assert old[0]["object"] == "old_project"

        # As of 2023-01-01, neither was active yet — empty
        before = kg.query_triples(subject="JP", as_of="2023-01-01")
        assert before == []

        # Without as_of, both come back
        all_triples = kg.query_triples(subject="JP")
        assert len(all_triples) == 2
    finally:
        kg.close()


@pgmark
def test_age_as_of_with_no_valid_from():
    """A triple with valid_from=None is active forever in the past."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        # No temporal bounds at all — always active
        kg.add_triple("X", "is", "Y")
        for date in ("1900-01-01", "2026-05-13", "2099-12-31"):
            assert (
                len(kg.query_triples(subject="X", as_of=date)) == 1
            ), f"unbounded triple should be active as of {date}"
    finally:
        kg.close()
