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
pytestmark = pytest.mark.skipif(
    POSTGRES_DSN is None,
    reason="set TEST_POSTGRES_DSN to run AGE knowledge-graph tests",
)


def test_age_kg_instantiates():
    """KnowledgeGraphAGE opens a connection and exits cleanly."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    assert kg is not None
    kg.close()


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


def test_age_context_manager():
    """`with KnowledgeGraphAGE(...) as kg:` closes the conn on exit."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    with KnowledgeGraphAGE(dsn=POSTGRES_DSN) as kg:
        assert kg._conn is not None
        assert not kg._conn.closed
    # After the with block, the connection should be closed.
    assert kg._conn.closed
