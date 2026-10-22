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
def test_age_add_triple_with_null_temporal_and_source():
    """add_triple must succeed when valid_from/valid_to/source are all None.

    Regression for #221: the static Cypher template emitted
    ``valid_from: NULL`` literals inside the property map, which AGE
    rejects with ``SyntaxError: a name constant is expected``. The fix
    omits the corresponding keys entirely when the value is None.
    """
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(
            subject="JP",
            relation_type="uses",
            object_="mempalace",
        )
        triples = kg.query_triples(subject="JP")
        assert len(triples) == 1
        t = triples[0]
        assert t["subject"] == "JP"
        assert t["relation_type"] == "uses"
        assert t["object"] == "mempalace"
        assert t["source"] is None
        assert t["valid_from"] is None
        assert t["valid_to"] is None
    finally:
        kg.close()


@pgmark
def test_age_add_triple_with_only_valid_from():
    """A triple with valid_from set but valid_to=None must write cleanly."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(
            subject="JP",
            relation_type="started",
            object_="run",
            valid_from="2026-05-01",
        )
        triples = kg.query_triples(subject="JP")
        assert len(triples) == 1
        assert triples[0]["valid_from"] == "2026-05-01"
        assert triples[0]["valid_to"] is None
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
            assert len(kg.query_triples(subject="X", as_of=date)) == 1, (
                f"unbounded triple should be active as of {date}"
            )
    finally:
        kg.close()


# ── stats() (#96) ─────────────────────────────────────────────────────


@pgmark
def test_age_stats_empty_graph():
    """Fresh graph returns zero counts and an empty relationship_types list."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        s = kg.stats()
        assert s == {
            "entities": 0,
            "triples": 0,
            "current_facts": 0,
            "expired_facts": 0,
            "relationship_types": [],
        }
    finally:
        kg.close()


@pgmark
def test_age_stats_shape_matches_sqlite_kg():
    """Result envelope matches the SQLite KG's stats() shape so palace-daemon's
    /graph KG panel and tool_kg_stats consumers don't have to special-case the
    backend."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        # 3 entities (JP, Alice, Bob), 4 triples — 3 active + 1 expired.
        kg.add_triple("JP", "married_to", "Alice")
        kg.add_triple("JP", "child_of", "Bob")
        kg.add_triple("Alice", "child_of", "Bob")
        kg.add_triple("JP", "loves", "Old_hobby", valid_from="2010-01-01", valid_to="2015-01-01")

        s = kg.stats()
        assert s["entities"] == 4  # JP, Alice, Bob, Old_hobby
        assert s["triples"] == 4
        assert s["current_facts"] == 3
        assert s["expired_facts"] == 1
        # Sorted alphabetically, deduped.
        assert s["relationship_types"] == ["child_of", "loves", "married_to"]
    finally:
        kg.close()


@pgmark
def test_age_stats_handles_all_expired():
    """A graph where every triple has valid_to set reports 0 current facts."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple("A", "was", "B", valid_from="2020-01-01", valid_to="2021-01-01")
        kg.add_triple("A", "was", "C", valid_from="2021-01-01", valid_to="2022-01-01")

        s = kg.stats()
        assert s["triples"] == 2
        assert s["current_facts"] == 0
        assert s["expired_facts"] == 2
    finally:
        kg.close()


# ── stats() fast path + fallback (#266) ───────────────────────────────


@pgmark
def test_age_stats_fast_path_matches_cypher_path():
    """Backing-table fast path returns the same envelope as the Cypher
    fallback. We exercise both paths on the same seeded graph and check
    every field for equality.

    This guards the #266 contract: switching to the fast path must not
    change observed values for any caller. Tests both paths against a
    real AGE-enabled postgres so the agtype ``->>`` cast actually runs.
    """
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple("JP", "married_to", "Alice")
        kg.add_triple("JP", "child_of", "Bob")
        kg.add_triple("Alice", "child_of", "Bob")
        kg.add_triple("JP", "loves", "Old_hobby", valid_from="2010-01-01", valid_to="2015-01-01")

        fast = kg._stats_fast()
        slow = kg._stats_cypher()
        assert fast == slow
        # Also confirm public stats() returns the same (it should pick fast).
        assert kg.stats() == fast
    finally:
        kg.close()


@pgmark
def test_age_stats_falls_back_when_fast_path_raises(monkeypatch):
    """If the backing tables aren't available (fresh palace, label not
    created, exotic AGE version), ``stats()`` must transparently fall
    through to the Cypher path and still return a correct envelope.
    """
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple("A", "knows", "B")
        kg.add_triple("A", "knows", "C", valid_from="2020-01-01", valid_to="2021-01-01")

        def boom(self):
            raise RuntimeError("backing tables not available")

        monkeypatch.setattr(KnowledgeGraphAGE, "_stats_fast", boom)
        s = kg.stats()
        assert s["entities"] == 3
        assert s["triples"] == 2
        assert s["current_facts"] == 1
        assert s["expired_facts"] == 1
        assert s["relationship_types"] == ["knows"]
    finally:
        kg.close()


# ── SPOC context slot + temporal integration (#161) ──────────────────


@pgmark
def test_age_add_triple_persists_context_slot():
    """``context`` is stored on the RELATION edge and surfaces in reads."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(
            subject="JP",
            relation_type="works_on",
            object_="mempalace",
            valid_from="2026-04-21",
            context="drawer:abc123",
        )
        triples = kg.query_triples(subject="JP")
        assert len(triples) == 1
        assert triples[0]["context"] == "drawer:abc123"
    finally:
        kg.close()


@pgmark
def test_age_context_slot_optional_and_omitted():
    """A triple written without ``context`` reads back with ``context=None``
    so callers can distinguish set from unset without a missing-key check."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(subject="JP", relation_type="uses", object_="git")
        triples = kg.query_triples(subject="JP")
        assert len(triples) == 1
        assert triples[0]["context"] is None
    finally:
        kg.close()


@pgmark
def test_age_query_entity_returns_context():
    """``query_entity`` surfaces ``context`` on every result row."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(
            subject="Alice",
            relation_type="knows",
            object_="Bob",
            context="drawer:d1",
        )
        facts = kg.query_entity("Alice")
        assert len(facts) == 1
        assert facts[0]["context"] == "drawer:d1"
    finally:
        kg.close()


@pgmark
def test_age_timeline_with_as_of_filter():
    """``timeline(as_of=...)`` returns only triples whose interval contains
    the date — same as_of semantics as ``query_triples``."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(
            "JP",
            "works_on",
            "old_project",
            valid_from="2024-01-01",
            valid_to="2025-12-31",
        )
        kg.add_triple(
            "JP",
            "works_on",
            "mempalace",
            valid_from="2026-04-21",
            valid_to=None,
        )

        # Full timeline returns both.
        all_facts = kg.timeline("JP")
        assert len(all_facts) == 2

        # As of mid-2025, only old_project was active.
        old = kg.timeline("JP", as_of="2025-06-01")
        assert len(old) == 1
        assert old[0]["object"] == "old_project"

        # As of 2026-05-01, only mempalace is active.
        active = kg.timeline("JP", as_of="2026-05-01")
        assert len(active) == 1
        assert active[0]["object"] == "mempalace"
    finally:
        kg.close()


@pgmark
def test_age_timeline_paginates_with_stable_order_and_total():
    """AGE ``timeline`` honors ``limit``/``offset`` like the SQLite backend (2026-10 upstream sync), with a stable
    order across pages even when ``valid_from`` ties, and ``timeline_total`` counts every match with the same
    entity/``as_of`` filters."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        for i in range(7):
            # Three share a valid_from, so the order relies on the tie-breaker.
            vf = "2026-01-01" if i < 3 else f"2026-02-{i:02d}"
            kg.add_triple("JP", "touched", f"thing_{i}", valid_from=vf, valid_to=None)
        kg.add_triple("JP", "worked_on", "expired", valid_from="2024-01-01", valid_to="2024-12-31")

        pages = [kg.timeline("JP", limit=3, offset=o) for o in (0, 3, 6)]
        assert [len(p) for p in pages] == [3, 3, 2]
        seen = [f["object"] for p in pages for f in p]
        assert len(seen) == len(set(seen)) == 8, "pages must not overlap or skip"
        assert pages == [kg.timeline("JP", limit=3, offset=o) for o in (0, 3, 6)], "page order must be stable"

        assert kg.timeline_total("JP") == 8
        assert kg.timeline_total("JP", as_of="2026-03-01") == 7
        assert kg.timeline_total() == 8
        assert kg.timeline_total(as_of="2024-06-01") == 1
        assert len(kg.timeline(limit=100, offset=7)) == 1
    finally:
        kg.close()


@pgmark
def test_age_timeline_without_entity_filter_respects_as_of():
    """The full-graph branch of ``timeline()`` (no entity_name) honors
    ``as_of`` too. Guards the alternate Cypher template added in #161."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple(
            "A",
            "was",
            "B",
            valid_from="2020-01-01",
            valid_to="2021-01-01",
        )
        kg.add_triple(
            "C",
            "is",
            "D",
            valid_from="2026-01-01",
        )
        # No entity filter, no as_of — both rows return.
        assert len(kg.timeline()) == 2
        # As-of inside the older interval — only A→B.
        old_only = kg.timeline(as_of="2020-06-01")
        assert len(old_only) == 1
        assert old_only[0]["object"] == "B"
        # As-of in the current period — only C→D.
        current = kg.timeline(as_of="2026-05-01")
        assert len(current) == 1
        assert current[0]["object"] == "D"
    finally:
        kg.close()


@pgmark
def test_age_timeline_returns_context_field():
    """Every timeline row carries the ``context`` slot — even when None."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple("X", "is", "Y", context="drawer:dx")
        kg.add_triple("X", "uses", "Z")  # no context
        rows = kg.timeline("X")
        contexts = {(r["object"], r["context"]) for r in rows}
        assert ("Y", "drawer:dx") in contexts
        assert ("Z", None) in contexts
    finally:
        kg.close()


@pgmark
def test_age_stats_per_field_cypher_helpers_match_canonical():
    """The per-field Cypher helpers used by the fast-path savepoint
    fallback agree with the canonical ``_stats_cypher`` walk. Guards the
    contract that if the agtype ``->>`` cast misfires on a future AGE,
    the field-level fallbacks still produce identical values.
    """
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()
        kg.add_triple("X", "likes", "Y")
        kg.add_triple("X", "hates", "Z", valid_from="2020-01-01", valid_to="2021-01-01")

        slow = kg._stats_cypher()
        assert kg._current_facts_cypher() == slow["current_facts"]
        assert kg._relationship_types_cypher() == slow["relationship_types"]
    finally:
        kg.close()


# ── edge-endpoint indexes (mempalace#335 item 1) ─────────────────────


def test_edge_endpoint_index_targets_are_the_four_we_expect():
    """Guard the index name/label/column triples (no postgres required).

    Keeps the set in sync with palace-daemon's scripts/age_graph_indexes.sql
    + the operator-online POST /backfill-age/indexes route.
    """
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    assert set(KnowledgeGraphAGE._EDGE_ENDPOINT_INDEXES) == {
        ("idx_mentions_end_id", "MENTIONS", "end_id"),
        ("idx_mentions_start_id", "MENTIONS", "start_id"),
        ("idx_relation_start_id", "RELATION", "start_id"),
        ("idx_relation_end_id", "RELATION", "end_id"),
    }


@pgmark
def test_ensure_edge_endpoint_indexes_skips_absent_then_installs():
    """_ensure_edge_endpoint_indexes is a no-op while the edge label tables
    don't exist (fresh graph, no edges written), then installs all four once
    a MENTIONS and a RELATION edge have been created. Idempotent on re-run."""
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    graph = KnowledgeGraphAGE.GRAPH_NAME
    wanted = {n for n, _, _ in KnowledgeGraphAGE._EDGE_ENDPOINT_INDEXES}

    def _present(kg) -> set:
        with kg._conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = ANY(%s)",
                (graph, list(wanted)),
            )
            return {r[0] for r in cur.fetchall()}

    kg = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        kg.clear()  # fresh graph: no MENTIONS/RELATION label tables yet

        # No edge tables → no indexes created, no error.
        kg._ensure_edge_endpoint_indexes()
        assert _present(kg) == set(), "should skip while edge tables are absent"

        # Create one of each edge type so both label tables exist. add_triple
        # makes a RELATION edge; a raw MENTIONS edge mirrors backfill_age.
        kg.add_triple("A", "rel", "B", source="d1")
        with kg._conn.cursor() as cur:
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute(
                "SELECT * FROM cypher('mempalace_kg', $$ "
                "  MERGE (d:Drawer {id: 'd1'}) "
                "  MERGE (e:Entity {name: 'A'}) "
                "  CREATE (d)-[:MENTIONS {count: 1}]->(e) "
                "$$) AS (v agtype)"
            )
        kg._conn.commit()

        kg._ensure_edge_endpoint_indexes()
        assert _present(kg) == wanted, "all four indexes should now exist"

        # Idempotent: a second call neither errors nor changes anything.
        kg._ensure_edge_endpoint_indexes()
        assert _present(kg) == wanted
    finally:
        kg.close()
