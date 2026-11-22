"""Integration smoke tests for the postgres backend.

These tests require a live Postgres with the `vector` extension installed.
Set `TEST_POSTGRES_DSN` to the connection string (e.g.
`postgresql://palace@familiar:5433/mempalace_test` or a local DSN);
without it, the tests are skipped so the default suite stays green on
machines that don't have a Postgres at hand.

The substrate the fork uses for these tests is the docker container
documented in `scratch/postgres-preflight-2026-05-10.md` —
`apache/age:release_PG16_1.6.0` with `postgresql-16-pgvector` apt-installed
on top. The AGE graph extension is loaded but not exercised by these
tests; knowledge-graph coverage lives in its own test module.

Pairs with the postgres backend code in `mempalace/backends/postgres.py`
(upstream PR #665) and the BaseCollection contract in
`mempalace/backends/base.py`.
"""

import os

import pytest

from mempalace.backends import get_backend
from mempalace.backends.base import BaseCollection
from mempalace.backends.postgres import PostgresBackend, PostgresCollection

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    POSTGRES_DSN is None,
    reason="set TEST_POSTGRES_DSN to run postgres backend tests",
)


def _palace_ref(path: str):
    """Build a PalaceRef compatible with the postgres backend's get_collection."""
    from mempalace.backends import PalaceRef

    return PalaceRef(id=path, local_path=path)


def test_postgres_backend_is_registered():
    """`get_backend("postgres")` returns a PostgresBackend singleton."""
    backend = get_backend("postgres")
    assert isinstance(backend, PostgresBackend)
    # Singleton property: repeated calls return the same instance.
    assert get_backend("postgres") is backend


def test_postgres_backend_smoke():
    """End-to-end: get a collection, add a drawer, read it back, clean up by id."""
    backend = get_backend("postgres")
    palace = _palace_ref("smoke_test_palace")
    collection_name = "smoke_test_drawers"

    col = backend.get_collection(
        palace=palace,
        collection_name=collection_name,
        create=True,
        options={"dsn": POSTGRES_DSN},
    )
    assert isinstance(col, (BaseCollection, PostgresCollection))

    # Idempotent setup: delete any leftover row from a prior run, then add.
    try:
        col.delete(ids=["smoke_d1"])
    except Exception:
        pass

    col.add(
        ids=["smoke_d1"],
        documents=["hello world from the postgres smoke test"],
        embeddings=[[0.1] * 384],
        metadatas=[{"wing": "test", "room": "smoke", "filed_at": "2026-05-11T00:00:00Z"}],
    )

    res = col.get(ids=["smoke_d1"])
    assert res["documents"] == ["hello world from the postgres smoke test"]
    assert res["metadatas"][0]["wing"] == "test"

    # Clean up the row we added — leaves the table in place for the next test.
    col.delete(ids=["smoke_d1"])


def test_postgres_vector_distance_query():
    """A vector query returns rows ordered by L2 distance to the query embedding."""
    backend = get_backend("postgres")
    palace = _palace_ref("smoke_test_palace")
    collection_name = "smoke_test_distance"

    col = backend.get_collection(
        palace=palace,
        collection_name=collection_name,
        create=True,
        options={"dsn": POSTGRES_DSN},
    )

    # Idempotent setup
    try:
        col.delete(ids=["near", "far"])
    except Exception:
        pass

    col.add(
        ids=["near", "far"],
        documents=["close to the query", "very different"],
        embeddings=[[0.1] * 384, [0.9] * 384],
        metadatas=[{"wing": "test"}, {"wing": "test"}],
    )

    query_emb = [[0.1] * 384]
    qres = col.query(query_embeddings=query_emb, n_results=2)
    assert qres["ids"][0][0] == "near", "nearest neighbor should be 'near' drawer"
    assert qres["ids"][0][1] == "far"

    col.delete(ids=["near", "far"])


def test_maybe_create_vector_index_recognizes_offname_hnsw():
    """A pre-existing HNSW index under a non-canonical name is recognized.

    Regression test for techempower-org/mempalace#73 priority 1. Before the
    structure-based check, _maybe_create_vector_index queried only by literal
    name (`{table}_vec_idx`). An HNSW index created out-of-band by the
    migration tool under e.g. `{table}_embedding_hnsw` was invisible to it,
    so every threshold crossing fell through to CREATE INDEX and stacked
    duplicate full HNSW builds holding ACCESS EXCLUSIVE.
    """
    import psycopg as psycopg2

    backend = get_backend("postgres")
    palace = _palace_ref("smoke_test_palace")
    collection_name = "smoke_test_offname_idx"

    col = backend.get_collection(
        palace=palace,
        collection_name=collection_name,
        create=True,
        options={"dsn": POSTGRES_DSN},
    )

    # Wipe any prior state on this table (idempotent re-runs).
    conn = psycopg2.connect(POSTGRES_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX IF EXISTS "{col.table_name}_vec_idx"')
            cur.execute(f'DROP INDEX IF EXISTS "{col.table_name}_offname_hnsw"')

        # Need at least one row so HNSW build succeeds.
        try:
            col.delete(ids=["seed"])
        except Exception:
            pass
        col.add(
            ids=["seed"],
            documents=["just to keep the table non-empty"],
            embeddings=[[0.0] * 384],
            metadatas=[{"wing": "test"}],
        )

        # Create the HNSW index under a non-canonical name (mimics migration
        # tool / operator pre-creating it). Same column + amname + ops as
        # _maybe_create_vector_index would have used.
        offname = f"{col.table_name}_offname_hnsw"
        with conn.cursor() as cur:
            cur.execute(
                f'CREATE INDEX "{offname}" '
                f'ON "{col.table_name}" USING hnsw (embedding vector_cosine_ops)'
            )

        # Force the helper past its row-budget gate without needing 5k rows.
        # (The structural check we're exercising runs BEFORE _estimated_count.)
        col._rows_since_index_check = 10_000
        col._vector_index_ready = False
        col._maybe_create_vector_index(inserted_rows=0)

        # After the call: ready flag flips True and no second index appears.
        assert col._vector_index_ready, "structural check should mark index ready"
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM pg_index idx
                JOIN pg_class ix ON ix.oid = idx.indexrelid
                JOIN pg_class t ON t.oid = idx.indrelid
                JOIN pg_am am ON am.oid = ix.relam
                WHERE t.relname = %s AND am.amname = 'hnsw' AND idx.indisvalid
                """,
                (col.table_name,),
            )
            count = cur.fetchone()[0]
        assert count == 1, (
            f"expected exactly 1 HNSW index after structural-recognition path, "
            f"got {count} — duplicate-build regression?"
        )

        # Cleanup
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX IF EXISTS "{offname}"')
        col.delete(ids=["seed"])
    finally:
        conn.close()


def test_postgres_write_guard_wing_never_empty():
    """Writes that omit wing land in FALLBACK_WING, not the '' column default (#381)."""
    from mempalace.backends.postgres import FALLBACK_WING

    backend = get_backend("postgres")
    palace = _palace_ref("smoke_test_palace")
    col = backend.get_collection(
        palace=palace,
        collection_name="smoke_test_drawers",
        create=True,
        options={"dsn": POSTGRES_DSN},
    )
    ids = ["wingless_d1", "hyphen_d1"]
    try:
        col.delete(ids=ids)
    except Exception:
        pass

    col.add(
        ids=ids,
        documents=["no wing at all", "hyphenated dirname wing"],
        embeddings=[[0.1] * 384, [0.2] * 384],
        metadatas=[{"room": "smoke"}, {"wing": "Kiyo-XHCI-Fix", "room": "smoke"}],
    )
    try:
        res = col.get(ids=ids)
        by_id = dict(zip(res["ids"], res["metadatas"]))
        assert by_id["wingless_d1"]["wing"] == FALLBACK_WING
        assert by_id["hyphen_d1"]["wing"] == "kiyo_xhci_fix"
    finally:
        col.delete(ids=ids)


def test_postgres_update_and_rename_guard_wing():
    """update() and rename_wing() apply the same wing coercion as writes (#381)."""
    backend = get_backend("postgres")
    palace = _palace_ref("smoke_test_palace")
    col = backend.get_collection(
        palace=palace,
        collection_name="smoke_test_drawers",
        create=True,
        options={"dsn": POSTGRES_DSN},
    )
    try:
        col.delete(ids=["renorm_d1"])
    except Exception:
        pass

    col.add(
        ids=["renorm_d1"],
        documents=["update path coercion"],
        embeddings=[[0.3] * 384],
        metadatas=[{"wing": "test", "room": "smoke"}],
    )
    try:
        col.update(ids=["renorm_d1"], metadatas=[{"wing": "Renamed-Wing"}])
        res = col.get(ids=["renorm_d1"])
        assert res["metadatas"][0]["wing"] == "renamed_wing"

        # rename_wing coerces the destination so it can't mint a malformed wing.
        stats = col.rename_wing(from_wing="renamed_wing", to_wing="Renamed-Wing-2")
        assert stats["renamed"] == 1
        res = col.get(ids=["renorm_d1"])
        assert res["metadatas"][0]["wing"] == "renamed_wing_2"
    finally:
        col.delete(ids=["renorm_d1"])


def test_postgres_count_where_matches_a_paged_walk(tmp_path):
    """``count_where`` is the scope count the searcher reports as
    ``available_in_scope``. It must equal what a paged ``get`` of the same
    filter returns, for every filter shape the backend translates, because
    both go through ``_where_to_sql``. Before it existed the searcher paged
    the whole scope to count it: 6.9 s for a 57K-drawer wing in production."""
    import uuid

    backend = get_backend("postgres")
    col = backend.get_collection(
        palace=_palace_ref(str(tmp_path)),
        collection_name=f"count_where_{uuid.uuid4().hex[:8]}",
        create=True,
        options={"dsn": POSTGRES_DSN},
    )
    rows = [("a", "r1")] * 7 + [("a", "r2")] * 5 + [("b", "r1")] * 4
    col.add(
        ids=[f"d{i:03d}" for i in range(len(rows))],
        documents=[f"doc {i}" for i in range(len(rows))],
        metadatas=[{"wing": w, "room": r} for w, r in rows],
        embeddings=[[0.01 * (i % 7)] * 384 for i in range(len(rows))],
    )

    def paged(where):
        n, offset = 0, 0
        while True:
            page = col.get(limit=4, offset=offset, include=[], where=where)
            if not page["ids"]:
                return n
            n += len(page["ids"])
            offset += len(page["ids"])

    cases = [
        ({"wing": "a"}, 12),
        ({"wing": "b"}, 4),
        ({"wing": "nope"}, 0),
        ({"$and": [{"wing": "a"}, {"room": "r2"}]}, 5),
        ({"$or": [{"wing": "b"}, {"room": "r2"}]}, 9),
        ({"wing": {"$in": ["a", "b"]}}, 16),
        ({"wing": {"$ne": "a"}}, 4),
    ]
    for where, expected in cases:
        assert col.count_where(where) == expected == paged(where), where
    # No filter: the same as count().
    assert col.count_where(None) == col.count() == 16


def test_new_collection_indexes_source_file_and_reports_it(tmp_path, caplog):
    """A new table gets the ``metadata->>'source_file'`` expression index, and
    ``has_source_file_index`` reads pg_index rather than the backend type: an
    older palace without the index must not take the any-size scoped path,
    where every lookup would be a full scan (#567)."""
    import logging
    import uuid

    import psycopg as psycopg2

    from mempalace.backends import postgres as pg
    from mempalace.palace import _indexed_scope

    backend = get_backend("postgres")
    col = backend.get_collection(
        palace=_palace_ref(str(tmp_path)),
        collection_name=f"sf_idx_{uuid.uuid4().hex[:8]}",
        create=True,
        options={"dsn": POSTGRES_DSN},
    )
    col.add(
        ids=["d1"],
        documents=["doc"],
        metadatas=[{"wing": "w", "source_file": "/s/a.jsonl"}],
        embeddings=[[0.0] * 384],
    )
    index = f"{col.table_name}_source_file_idx"
    conn = psycopg2.connect(POSTGRES_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SET enable_seqscan = off")
            cur.execute(
                f'EXPLAIN SELECT id FROM "{col.table_name}" '
                "WHERE metadata->>'source_file' = '/s/a.jsonl'"
            )
            plan = "\n".join(row[0] for row in cur.fetchall())
        assert index in plan, plan
        assert col.has_source_file_index()
        assert _indexed_scope(col)

        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX "{index}"')
        bare = PostgresCollection(POSTGRES_DSN, table_name=col.table_name)
        pg._WARNED_NO_SOURCE_FILE_INDEX.discard(col.table_name)
        with caplog.at_level(logging.WARNING, logger="mempalace.postgres"):
            assert not bare.has_source_file_index()
            assert not _indexed_scope(bare)
        warnings = [
            r for r in caplog.records if "no index on metadata->>'source_file'" in r.message
        ]
        assert len(warnings) == 1, "named once per table, not on every check"

        # A False is not cached: an operator's later CONCURRENTLY build is seen.
        with conn.cursor() as cur:
            cur.execute(
                f'CREATE INDEX CONCURRENTLY "{index}" '
                f"ON \"{col.table_name}\" ((metadata->>'source_file'))"
            )
        assert bare.has_source_file_index()

        # None of these serves `metadata->>'source_file' = %s`, so none counts: a partial
        # index, the expression as a second key, and keys whose literal differs only in
        # the characters a looser comparison would strip.
        for shape in (
            "((metadata->>'source_file')) WHERE wing = 'w'",
            "(wing, (metadata->>'source_file'))",
            "((metadata->>'source_(file)'))",
            "((metadata->>'source_ file'))",
        ):
            with conn.cursor() as cur:
                cur.execute(f'DROP INDEX IF EXISTS "{index}"')
                cur.execute(f'CREATE INDEX "{index}" ON "{col.table_name}" {shape}')
            assert not PostgresCollection(
                POSTGRES_DSN, table_name=col.table_name
            ).has_source_file_index(), shape
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{col.table_name}"')
        conn.close()


def _lookup_table(tmp_path, rows=20000):
    """A new collection plus ``rows`` drawers inserted in SQL: 1 in 50 with parent_drawer_id, 1 in 50 with
    parent_entry_id, the rest with source_file only (the production mix is ~98.6 % without a parent key)."""
    import uuid

    import psycopg

    col = get_backend("postgres").get_collection(
        palace=_palace_ref(str(tmp_path)),
        collection_name=f"lk_idx_{uuid.uuid4().hex[:8]}",
        create=True,
        options={"dsn": POSTGRES_DSN},
    )
    col.add(ids=["seed"], documents=["d"], metadatas=[{"wing": "w"}], embeddings=[[0.0] * 384])
    conn = psycopg.connect(POSTGRES_DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{col.table_name}" (id, wing, room, document, metadata) '
            "SELECT 'd' || lpad(g::text, 8, '0'), 'w', 'r', 'x', CASE "
            "WHEN g %% 50 = 0 THEN jsonb_build_object('parent_drawer_id', 'p' || g) "
            "WHEN g %% 50 = 1 THEN jsonb_build_object('parent_entry_id', 'e' || g) "
            "ELSE jsonb_build_object('source_file', '/f/' || (g %% 300)) END "
            "FROM generate_series(1, %s) g ORDER BY random()",
            (rows,),
        )
    return col, conn


def test_a_new_collection_gets_every_lookup_index(tmp_path):
    """source_file, parent_drawer_id and parent_entry_id each get their expression index at CREATE TABLE."""
    from mempalace.backends import postgres as pg

    col, conn = _lookup_table(tmp_path, rows=0)
    try:
        report = col.ensure_lookup_indexes(dry_run=True)
        assert [(r["key"], r["state"]) for r in report] == [
            (k, "present") for k in pg.LOOKUP_INDEX_KEYS
        ]
        assert report[1]["index"] == f"{col.table_name}_meta_parent_drawer_id_idx"
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{col.table_name}"')
        conn.close()


def test_an_existing_palace_builds_the_missing_ones_analyzes_and_its_plans_use_them(tmp_path):
    """The 2026-11-21 production gap: a palace created before the parent-id indexes. ensure_lookup_indexes builds
    them CONCURRENTLY, ANALYZEs (an expression index has no statistics until then, and the planner keeps its
    default guess), and the judged-parent lookup's plan uses each index. A rerun builds nothing."""
    col, conn = _lookup_table(tmp_path)
    try:
        with conn.cursor() as cur:
            for key in ("parent_drawer_id", "parent_entry_id"):
                cur.execute(f'DROP INDEX "{col.table_name}_meta_{key}_idx"')
        dry = col.ensure_lookup_indexes(dry_run=True)
        assert [r["state"] for r in dry] == ["present", "missing", "missing"]

        report = col.ensure_lookup_indexes(plan_check_min_pages=0)
        assert [r["state"] for r in report] == ["present", "built", "built"]
        assert all(r["plan_uses_index"] for r in report[1:]), report
        assert all(r["generic_plan_uses_index"] for r in report[1:]), report
        assert report[0]["plan_note"] == "no judged-parent lookup on this key"
        with conn.cursor() as cur:
            for r in report[1:]:
                cur.execute("SELECT count(*) FROM pg_stats WHERE tablename = %s", (r["index"],))
                assert cur.fetchone()[0] > 0, f"{r['index']} has no statistics: ANALYZE did not run"

        again = col.ensure_lookup_indexes(plan_check_min_pages=0)
        assert [r["state"] for r in again] == ["present"] * 3
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{col.table_name}"')
        conn.close()


def test_an_invalid_leftover_is_dropped_and_rebuilt(tmp_path):
    """A failed CREATE INDEX CONCURRENTLY leaves an INVALID index under the lookup name; it is not counted as
    present, and the next run drops and rebuilds it."""
    col, conn = _lookup_table(tmp_path, rows=1000)
    name = f"{col.table_name}_meta_parent_entry_id_idx"
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX "{name}"')
            cur.execute(
                f'CREATE INDEX "{name}" ON "{col.table_name}" ((metadata->>\'parent_entry_id\'))'
            )
            cur.execute(
                "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(%s)", (name,)
            )
        assert [r["state"] for r in col.ensure_lookup_indexes(dry_run=True)][2] == "missing"
        report = col.ensure_lookup_indexes()
        assert report[2]["state"] == "built"
        assert report[2]["plan_note"] == "small table"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)", (name,)
            )
            assert cur.fetchone()[0] is True
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{col.table_name}"')
        conn.close()


def _statement_text(query, conn) -> str:
    """The SQL text psycopg sends for ``query`` (a str, or a psycopg.sql Composed rendered on ``conn``). The driver
    binds parameters server-side, so the text keeps its placeholders and the values travel separately."""
    return query if isinstance(query, str) else query.as_string(conn)


def _record_side_connection(monkeypatch, hook=None):
    """Route ensure_lookup_indexes' side connection through a recorder: every statement it executes is logged as
    (text, params), the text being what psycopg sends. ``hook(real_cursor, query, params, text)`` runs before each
    statement and may raise in its place."""
    import types

    from mempalace.backends import postgres as pg

    psycopg, sql = pg._load_psycopg2()
    log = []

    class Cur:
        def __init__(self, real):
            self._real = real

        def execute(self, query, params=None):
            text = _statement_text(query, self._real.connection)
            log.append((text, None if params is None else list(params)))
            if hook is not None:
                hook(self._real, query, params, text)
            return self._real.execute(query, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._real.close()

    class Conn:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def cursor(self):
            return Cur(self._real.cursor())

    fake = types.SimpleNamespace(connect=lambda dsn: Conn(psycopg.connect(dsn)))
    monkeypatch.setattr(pg, "_load_psycopg2", lambda: (fake, sql))
    return log


def _drop_lookup_table(col, conn):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{col.table_name}"')
    conn.close()


def test_the_plan_check_explains_the_statement_the_judged_parent_lookup_sends(
    tmp_path, monkeypatch
):
    """#655 r1 (1): the probe must be the real lookup, not a look-alike. The searcher calls
    get(where={key: {"$in": ids}}, include=["metadatas"], limit=_JUDGED_PARENT_ROW_LIMIT); the plan check EXPLAINs
    exactly the statement text get() sends for that call, with the same bound parameters (psycopg binds them
    server-side). Since round 2 the lookup key is inlined, so it is no parameter at all."""
    import re

    from mempalace import searcher
    from mempalace.backends import postgres as pg

    assert pg._LOOKUP_PROBE_LIMIT == searcher._JUDGED_PARENT_ROW_LIMIT
    assert pg._JUDGED_PARENT_KEYS == searcher._PARENT_ID_KEYS
    col, conn = _lookup_table(tmp_path, rows=2000)
    try:
        sent = []
        real_execute = pg._RetryingCursor.execute

        def spy(self, q, params=None):
            sent.append((q, params))
            return real_execute(self, q, params)

        monkeypatch.setattr(pg._RetryingCursor, "execute", spy)
        col.get(
            where={"parent_drawer_id": {"$in": list(pg._LOOKUP_PROBE_IDS)}},
            include=["metadatas"],
            limit=searcher._JUDGED_PARENT_ROW_LIMIT,
        )
        monkeypatch.setattr(pg._RetryingCursor, "execute", real_execute)
        get_query, get_params = sent[-1]
        get_text = _statement_text(get_query, conn)
        assert get_text.startswith("SELECT id, NULL::text, wing, room, metadata FROM"), get_text

        log = _record_side_connection(monkeypatch)
        col.ensure_lookup_indexes(plan_check_min_pages=0)
        explains = [(t, p) for t, p in log if t.startswith("EXPLAIN ") and "parent_drawer_id" in t]
        assert explains == [("EXPLAIN " + get_text, list(get_params))], (explains, get_text)
        # The generic-plan check prepares that same text, its placeholders numbered.
        numbers = iter(range(1, len(get_params) + 1))
        numbered = re.sub(r"%s", lambda _m: f"${next(numbers)}", get_text)
        assert ("PREPARE mempalace_lookup_probe AS " + numbered, None) in log
    finally:
        _drop_lookup_table(col, conn)


def test_a_warmed_production_connection_keeps_using_the_index_under_a_generic_plan(tmp_path):
    """#655 r2 (1, 2): the lookup on the backend's own connection, prepared by psycopg (prepare_threshold=0, so from
    the first call) and run repeatedly under plan_cache_mode = force_generic_plan: the plan postgres reuses for every
    value must still use the parent index. A key bound as $1 can't match the expression index; an inlined one can."""
    from mempalace import searcher
    from mempalace.backends import postgres as pg

    col, conn = _lookup_table(tmp_path)
    try:
        col.ensure_lookup_indexes(
            plan_check_min_pages=0
        )  # ANALYZE, so the planner has the index's statistics
        live = col._get_conn()
        live.prepare_threshold = 0
        col._cursor().execute("SET plan_cache_mode = force_generic_plan")
        sent = []
        real_execute = pg._RetryingCursor.execute

        def spy(self, q, params=None):
            sent.append(params)
            return real_execute(self, q, params)

        lookup = {
            "where": {"parent_drawer_id": {"$in": ["p50", "p100"]}},
            "include": ["metadatas"],
            "limit": searcher._JUDGED_PARENT_ROW_LIMIT,
        }
        pg._RetryingCursor.execute = spy
        try:
            for _ in range(6):
                assert len(col.get(**lookup)["ids"]) == 2
        finally:
            pg._RetryingCursor.execute = real_execute
        cur = col._cursor()
        cur.execute(
            # Matched by table and shape, not by key: a bound key never appears in the statement text.
            "SELECT name, generic_plans FROM pg_prepared_statements "
            "WHERE statement LIKE %s AND statement LIKE %s AND statement NOT LIKE %s",
            (f'%"{col.table_name}" WHERE%', "%ORDER BY id LIMIT%", "%pg_prepared_statements%"),
        )
        prepared = cur.fetchall()
        assert len(prepared) == 1 and prepared[0][1] >= 6, prepared
        args = ", ".join("'" + str(v).replace("'", "''") + "'" for v in sent[-1])
        cur.execute(f'EXPLAIN EXECUTE "{prepared[0][0]}"({args})')
        plan = "\n".join(r[0] for r in cur.fetchall())
        assert pg._plan_names_index(plan, f"{col.table_name}_meta_parent_drawer_id_idx"), plan
    finally:
        _drop_lookup_table(col, conn)


def test_a_second_run_waits_its_turn_and_changes_nothing(tmp_path):
    """#655 r1 (2): two runs must never drop or build under each other. A run takes the table's advisory lock before
    it reads any state; while another session holds it, the run raises LookupIndexesBusy and touches nothing."""
    from mempalace.backends.postgres import LookupIndexesBusy

    col, conn = _lookup_table(tmp_path, rows=1000)
    name = f"{col.table_name}_meta_parent_entry_id_idx"
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX "{name}"')
            cur.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (f"mempalace.lookup_indexes:{col.table_name}",),
            )
        with pytest.raises(LookupIndexesBusy):
            col.ensure_lookup_indexes()
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (name,))
            assert cur.fetchone()[0] is None, "a run that did not hold the lock built an index"
            cur.execute("SELECT pg_advisory_unlock_all()")
        assert col.ensure_lookup_indexes()[2]["state"] == "built"
    finally:
        _drop_lookup_table(col, conn)


def test_a_failed_build_is_reported_and_its_leftover_dropped_in_the_same_run(
    tmp_path, monkeypatch, capsys
):
    """#655 r1 (3): a CONCURRENTLY build that fails part-way leaves an INVALID index. The run must say so (state
    "failed", the error, the index named; the CLI exits 1) and drop the leftover itself, not leave it for the next
    run to trip over."""
    import types

    import psycopg

    col, conn = _lookup_table(tmp_path, rows=1000)
    name = f"{col.table_name}_meta_parent_entry_id_idx"

    def fail_the_parent_entry_build(real_cur, query, params, text):
        if text.startswith("CREATE INDEX CONCURRENTLY") and "parent_entry_id" in text:
            real_cur.execute(query, params)  # the build gets as far as a catalog entry ...
            with conn.cursor() as cur:  # ... and is then cancelled, which leaves it INVALID
                cur.execute(
                    "UPDATE pg_index SET indisvalid = false WHERE indexrelid = to_regclass(%s)",
                    (name,),
                )
            raise psycopg.errors.QueryCanceled("injected: canceling statement due to lock timeout")

    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX "{name}"')
        _record_side_connection(monkeypatch, hook=fail_the_parent_entry_build)
        report = col.ensure_lookup_indexes()
        row = report[2]
        assert row["state"] == "failed" and row["index"] == name
        assert "injected" in row["error"]
        assert row["leftover"] == "dropped"
        assert [r["state"] for r in report[:2]] == ["present", "present"]
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (name,))
            assert cur.fetchone()[0] is None, "the INVALID leftover is still there"

        from mempalace import cli  # the cmd_*.py fragments share this one namespace

        monkeypatch.setattr(cli, "_selected_backend_for_palace", lambda _p: "postgres")
        monkeypatch.setattr("mempalace.palace.get_collection", lambda *_a, **_k: col)
        with pytest.raises(SystemExit) as exit_info:
            cli.cmd_lookup_indexes(types.SimpleNamespace(palace=str(tmp_path), dry_run=False))
        assert exit_info.value.code == 1
        assert f"{name}  BUILD FAILED" in capsys.readouterr().out
    finally:
        _drop_lookup_table(col, conn)


def test_a_rerun_analyzes_a_palace_whose_indexes_exist_without_statistics(tmp_path):
    """#655 r1 (4): the 2026-11-21 production case. The indexes exist and are valid, but nothing has ANALYZEd them,
    so the planner keeps its default guess and walks the primary key. A rerun that builds nothing must still
    ANALYZE, and the plans then use the indexes."""
    col, conn = _lookup_table(tmp_path)
    try:
        with conn.cursor() as cur:
            cur.execute(f'ALTER TABLE "{col.table_name}" SET (autovacuum_enabled = false)')
            cur.execute(
                "SELECT count(*) FROM pg_stats WHERE tablename LIKE %s",
                (f"{col.table_name}_meta_%",),
            )
            assert cur.fetchone()[0] == 0, (
                "precondition: the expression indexes have no statistics yet"
            )
        report = col.ensure_lookup_indexes(plan_check_min_pages=0)
        assert [r["state"] for r in report] == ["present"] * 3
        with conn.cursor() as cur:
            for r in report[1:]:
                cur.execute("SELECT count(*) FROM pg_stats WHERE tablename = %s", (r["index"],))
                assert cur.fetchone()[0] > 0, f"{r['index']} still has no statistics"
        assert all(r["plan_uses_index"] for r in report[1:]), report
    finally:
        _drop_lookup_table(col, conn)


def test_discovery_and_the_plan_check_accept_the_same_indexes(tmp_path):
    """#655 r1 (5): discovery counts any valid btree on the expression, whatever its name, so the plan check must
    accept that same index. A hand-built index under another name is reported as the index, and its plan passes."""
    col, conn = _lookup_table(tmp_path)
    custom = f"{col.table_name}_hand_built_pd"
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP INDEX "{col.table_name}_meta_parent_drawer_id_idx"')
            cur.execute(
                f'CREATE INDEX "{custom}" ON "{col.table_name}" ((metadata->>\'parent_drawer_id\'))'
            )
        report = col.ensure_lookup_indexes(plan_check_min_pages=0)
        row = report[1]
        assert (row["state"], row["index"]) == ("present", custom)
        assert row["plan_uses_index"] is True, row
    finally:
        _drop_lookup_table(col, conn)


def test_plan_names_index_matches_whole_names_only():
    from mempalace.backends.postgres import _plan_names_index

    plan = "Limit\n  ->  Index Scan using t_meta_parent_drawer_id_idx on t\n"
    assert _plan_names_index(plan, "t_meta_parent_drawer_id_idx")
    assert not _plan_names_index(plan, "t_meta_parent_drawer_id")
    assert _plan_names_index("  ->  Bitmap Index Scan on t_x_idx", "t_x_idx")
    assert not _plan_names_index("Index Scan using t_pkey on t", "t_x_idx")
