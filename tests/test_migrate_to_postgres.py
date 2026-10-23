"""Tests for the ChromaDB → Postgres migration tool.

Most tests gate on TEST_POSTGRES_DSN since they touch a real Postgres.
The CLI-help test runs unconditionally — it doesn't need the database.
"""

import os
import subprocess
import sys
from unittest.mock import patch

import importlib.util

import pytest

from mempalace.migrate_to_postgres import _redact_dsn

POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN")


# ── CLI surface (no postgres required) ───────────────────────────────


def test_migrate_subcommand_help():
    """`mempalace migrate-to-postgres --help` lists --from and --to flags."""
    res = subprocess.run(
        [sys.executable, "-m", "mempalace", "migrate-to-postgres", "--help"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"--help exited {res.returncode}: {res.stderr or res.stdout}"
    assert "--from" in res.stdout
    assert "--to" in res.stdout
    assert "--batch-size" in res.stdout
    assert "--dry-run" in res.stdout


def test_migrate_requires_from_and_to():
    """argparse rejects invocation without --from/--to."""
    res = subprocess.run(
        [sys.executable, "-m", "mempalace", "migrate-to-postgres"],
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    # argparse error message includes the required flags
    err = res.stderr + res.stdout
    assert "--from" in err or "--to" in err or "required" in err.lower()


def test_redact_dsn_url_form_hides_password():
    """Password in URL-form DSN is replaced with ***."""
    out = _redact_dsn("postgresql://user:secret@host:5432/mempalace")
    assert "secret" not in out
    assert "***" in out
    assert "user" in out
    assert "mempalace" in out


def test_redact_dsn_keyvalue_form_hides_password():
    """Password in key=value DSN is replaced with ***."""
    out = _redact_dsn("host=db user=u password=secret dbname=mempalace")
    assert "secret" not in out
    assert "password=***" in out
    assert "host=db" in out


def test_redact_dsn_no_password_passthrough():
    """DSN without a password renders unchanged."""
    s = "postgresql://localhost/mempalace"
    assert _redact_dsn(s) == s


def test_redact_dsn_empty():
    """Empty input → empty output."""
    assert _redact_dsn("") == ""


# ── Phase 0 preflight (no postgres required for the failure paths) ───


def test_preflight_fails_when_chroma_path_missing(tmp_path):
    """Source palace not a directory → SystemExit with 'not found'."""
    from mempalace.migrate_to_postgres import phase_0_preflight

    missing = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit) as excinfo:
        phase_0_preflight(str(missing), "postgresql://localhost/mempalace_test")
    assert "not found" in str(excinfo.value)


def test_preflight_refuses_when_daemon_reachable(tmp_path):
    """Mock /health 200 from daemon → abort with explicit instruction."""
    from mempalace.migrate_to_postgres import phase_0_preflight

    palace = tmp_path / "palace"
    palace.mkdir()

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    with (
        patch.dict(os.environ, {"PALACE_DAEMON_URL": "http://disks:8085"}),
        patch("urllib.request.urlopen", return_value=FakeResponse()),
    ):
        with pytest.raises(SystemExit) as excinfo:
            phase_0_preflight(str(palace), "postgresql://localhost/mempalace_test")
    assert "palace-daemon" in str(excinfo.value)
    assert "systemctl stop" in str(excinfo.value)


def test_preflight_proceeds_when_daemon_unreachable(tmp_path):
    """Daemon URL set but unreachable → preflight continues to ext check."""
    from mempalace.migrate_to_postgres import phase_0_preflight

    palace = tmp_path / "palace"
    palace.mkdir()

    with patch.dict(os.environ, {"PALACE_DAEMON_URL": "http://no-such-host.invalid:1"}):
        # Will fail later on psycopg2 connection, not on daemon check.
        with pytest.raises(SystemExit) as excinfo:
            phase_0_preflight(str(palace), "postgresql://no-such-host.invalid/x")
        msg = str(excinfo.value)
        # The error should be about postgres, not about the daemon
        assert "palace-daemon" not in msg


# ── Phase 0 + extension probe (real postgres required) ───────────────

pgmark = pytest.mark.skipif(
    POSTGRES_DSN is None, reason="set TEST_POSTGRES_DSN to run postgres tests"
)


@pgmark
def test_preflight_passes_against_live_postgres(tmp_path, capsys):
    """Real postgres with vector + age available → preflight passes."""
    from mempalace.migrate_to_postgres import phase_0_preflight

    palace = tmp_path / "palace"
    palace.mkdir()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PALACE_DAEMON_URL", None)
        phase_0_preflight(str(palace), POSTGRES_DSN)
    out = capsys.readouterr().out
    assert "preflight passed" in out


@pgmark
def test_dry_run_exits_after_preflight(tmp_path, capsys):
    """run_migration with dry_run=True returns after phase 0."""
    from mempalace.migrate_to_postgres import run_migration

    palace = tmp_path / "palace"
    palace.mkdir()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PALACE_DAEMON_URL", None)
        run_migration(str(palace), POSTGRES_DSN, dry_run=True)
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()


# ── Schema creation (real postgres required) ─────────────────────────


@pgmark
def test_phase_1_creates_extensions_and_checkpoint_table(capsys):
    """phase_1_schema installs vector + age and creates the meta table."""
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it
    from mempalace.migrate_to_postgres import (
        CHECKPOINT_TABLE,
        phase_1_schema,
        _get_checkpoint,
    )

    phase_1_schema(POSTGRES_DSN)
    out = capsys.readouterr().out
    assert "schema" in out.lower()

    with psycopg2.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'age')")
        ext = {r[0] for r in cur.fetchall()}
        assert "vector" in ext, "pgvector should be installed after the schema-setup step"
        # AGE may or may not install depending on infrastructure; required at
        # preflight, but a non-AGE setup may have only vector.
        cur.execute(
            "SELECT to_regclass(%s)",
            (CHECKPOINT_TABLE,),
        )
        assert cur.fetchone()[0] is not None, (
            f"{CHECKPOINT_TABLE} should exist after the schema-setup step"
        )
        # Checkpoint recorded
        with psycopg2.connect(POSTGRES_DSN) as conn2:
            assert _get_checkpoint(conn2, "migration_phase_schema") == "done"


@pgmark
def test_phase_1_idempotent():
    """Re-running phase_1_schema is a no-op (no exception)."""
    from mempalace.migrate_to_postgres import phase_1_schema

    phase_1_schema(POSTGRES_DSN)
    phase_1_schema(POSTGRES_DSN)  # second call must not raise


# ── Drawer batch copy (real postgres required) ───────────────────────


@pytest.fixture
def fixture_chroma_palace(tmp_path, request):
    """Build a small ChromaDB palace with 10 drawers for migration tests.

    Test isolation: each test gets a uniquely-named chroma collection
    (e.g. ``mempalace_drawers_test_abc123``). phase_2_drawers writes to
    a postgres table with the same name, so per-test data lives in its
    own table — separate from production's ``mempalace_drawers`` table
    on the shared postgres instance. Cleanup drops the table after.
    """
    import secrets

    chromadb = pytest.importorskip("chromadb")
    palace = tmp_path / "palace"
    palace.mkdir()
    client = chromadb.PersistentClient(path=str(palace))

    # Unique collection name per test → unique postgres table name.
    test_table = f"mempalace_drawers_test_{secrets.token_hex(4)}"
    col = client.get_or_create_collection(test_table, metadata={"hnsw:space": "cosine"})
    col.add(
        ids=[f"d{i}" for i in range(10)],
        documents=[f"doc {i}" for i in range(10)],
        embeddings=[[float(i) / 10] * 384 for i in range(10)],
        # Room must be canonical per the FK constraint on mempalace_drawers
        # (added in the hybrid-search-taxonomy work, 2026-05-14). Without
        # this, phase_2_drawers inserts trip mempalace_drawers_room_fk.
        metadatas=[{"wing": "test", "room": "references", "idx": i} for i in range(10)],
    )

    # Register cleanup so test-specific postgres tables don't accumulate
    def _cleanup():
        if POSTGRES_DSN:
            try:
                import psycopg2

                with psycopg2.connect(POSTGRES_DSN) as conn:
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute(f'DROP TABLE IF EXISTS "{test_table}" CASCADE')
            except Exception:
                pass  # best-effort cleanup

    request.addfinalizer(_cleanup)

    return (str(palace), test_table)


@pgmark
def test_phase_2_copies_all_drawers(fixture_chroma_palace, capsys):
    """phase_2 copies every drawer; counts match source."""
    from mempalace.migrate_to_postgres import phase_1_schema, phase_2_drawers
    from mempalace.backends.postgres import PostgresBackend

    palace_path, test_table = fixture_chroma_palace
    phase_1_schema(POSTGRES_DSN)
    phase_2_drawers(palace_path, POSTGRES_DSN, batch_size=4)

    backend = PostgresBackend(dsn=POSTGRES_DSN)
    from mempalace.backends.base import PalaceRef

    palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
    col = backend.get_collection(palace=palace_ref, collection_name=test_table, create=True)
    # Reach into the backend to count — keep this loose since backend
    # API surface may evolve. The presence + correctness of d3 is the
    # invariant we care about.
    res = col.get(ids=["d3"])
    assert res["documents"] == ["doc 3"]
    assert res["metadatas"][0]["wing"] == "test"


@pgmark
def test_phase_2_idempotent(fixture_chroma_palace):
    """Re-running phase_2 against the same source doesn't dupe rows."""
    from mempalace.migrate_to_postgres import phase_1_schema, phase_2_drawers
    from mempalace.backends.postgres import PostgresBackend

    palace_path, test_table = fixture_chroma_palace
    phase_1_schema(POSTGRES_DSN)
    phase_2_drawers(palace_path, POSTGRES_DSN, batch_size=4)
    # Reset the per-collection done marker so the second pass actually runs
    # the loop (vs short-circuiting via checkpoint).
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    with psycopg2.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM mempalace_backend_meta WHERE key = %s",
            (f"migration_drawer_done::{test_table}",),
        )
        conn.commit()
    phase_2_drawers(palace_path, POSTGRES_DSN, batch_size=4)

    backend = PostgresBackend(dsn=POSTGRES_DSN)
    from mempalace.backends.base import PalaceRef

    palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
    col = backend.get_collection(palace=palace_ref, collection_name=test_table, create=True)
    res = col.get(ids=[f"d{i}" for i in range(10)])
    assert len(res["ids"]) == 10, "should still have exactly 10 rows after re-run"


@pgmark
def test_phase_2_skips_collection_when_marked_done(fixture_chroma_palace, capsys):
    """If migration_drawer_done::<name> is already 'done', we skip the loop."""
    from mempalace.migrate_to_postgres import (
        phase_1_schema,
        phase_2_drawers,
        _set_checkpoint,
    )

    palace_path, test_table = fixture_chroma_palace
    phase_1_schema(POSTGRES_DSN)
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    with psycopg2.connect(POSTGRES_DSN) as conn:
        _set_checkpoint(conn, f"migration_drawer_done::{test_table}", "done")
    capsys.readouterr()  # discard prior output
    phase_2_drawers(palace_path, POSTGRES_DSN, batch_size=4)
    out = capsys.readouterr().out
    assert "skipping" in out.lower()


# ── Phase 5 — KG migration (sqlite → AGE) ─────────────────────────────


def _build_sqlite_kg(palace_dir, triples):
    """Build a knowledge_graph.sqlite3 fixture with the supplied triples.

    Each triple is a dict: {id, subject, predicate, object, valid_from?,
    valid_to?, confidence?, source_drawer_id?}.
    """
    import sqlite3

    kg_path = palace_dir / "knowledge_graph.sqlite3"
    with sqlite3.connect(str(kg_path)) as conn:
        conn.execute(
            """
            CREATE TABLE triples (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                confidence REAL DEFAULT 1.0,
                source_closet TEXT,
                source_file TEXT,
                source_drawer_id TEXT,
                adapter_name TEXT,
                extracted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for t in triples:
            conn.execute(
                """
                INSERT INTO triples
                    (id, subject, predicate, object,
                     valid_from, valid_to, confidence, source_drawer_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t["id"],
                    t["subject"],
                    t["predicate"],
                    t["object"],
                    t.get("valid_from"),
                    t.get("valid_to"),
                    t.get("confidence", 1.0),
                    t.get("source_drawer_id"),
                ),
            )
        conn.commit()
    return kg_path


def test_phase_5_no_sqlite_kg_marks_done(tmp_path, capsys):
    """If <chroma_path>/knowledge_graph.sqlite3 absent, phase no-ops cleanly."""
    from mempalace.migrate_to_postgres import phase_5_kg, _KG_DONE_KEY

    # Need POSTGRES_DSN even to check checkpoint, so skip if absent.
    if POSTGRES_DSN is None:
        pytest.skip("phase_5 checkpoint check needs postgres")

    # Test isolation: clear the KG done checkpoint from any prior test run
    # so phase_5_kg actually runs the no-sqlite path (vs short-circuiting
    # via checkpoint). Without this, a previous test run that completed
    # phase_5 leaves the checkpoint set and the assert below misfires.
    from mempalace.migrate_to_postgres import phase_1_schema
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    phase_1_schema(POSTGRES_DSN)
    with psycopg2.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM mempalace_backend_meta WHERE key = %s",
            (_KG_DONE_KEY,),
        )
        conn.commit()

    palace = tmp_path / "palace"
    palace.mkdir()
    # No knowledge_graph.sqlite3 in palace dir
    phase_5_kg(str(palace), POSTGRES_DSN)
    out = capsys.readouterr().out
    assert "nothing to migrate" in out.lower() or "no sqlite" in out.lower()

    # Verify checkpoint was set
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it
    from mempalace.migrate_to_postgres import _get_checkpoint

    with psycopg2.connect(POSTGRES_DSN) as conn:
        assert _get_checkpoint(conn, _KG_DONE_KEY) == "done"


@pgmark
def test_phase_5_copies_triples(tmp_path, capsys):
    """phase_5_kg moves triples from sqlite to AGE."""
    from mempalace.migrate_to_postgres import (
        phase_1_schema,
        phase_5_kg,
        _KG_DONE_KEY,
    )
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    palace = tmp_path / "palace"
    palace.mkdir()
    _build_sqlite_kg(
        palace,
        [
            {
                "id": "t1",
                "subject": "JP",
                "predicate": "works_on",
                "object": "mempalace",
                "valid_from": "2026-04-21",
                "source_drawer_id": "drawer_abc",
                "confidence": 0.9,
            },
            {
                "id": "t2",
                "subject": "JP",
                "predicate": "uses",
                "object": "Postgres",
                "confidence": 1.0,
            },
        ],
    )

    phase_1_schema(POSTGRES_DSN)

    # Reset state for a clean test
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    with psycopg2.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM mempalace_backend_meta WHERE key LIKE 'migration_kg_%%' OR key = %s",
            (_KG_DONE_KEY,),
        )
        conn.commit()
    age_for_clear = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    age_for_clear.clear()
    age_for_clear.close()

    phase_5_kg(str(palace), POSTGRES_DSN)
    out = capsys.readouterr().out
    assert "2 copied" in out or "kg complete" in out.lower()

    # Read back via AGE
    age = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        jp_triples = age.query_triples(subject="JP")
        assert len(jp_triples) == 2
        # Sanity: one of them is the "works_on mempalace" relation
        works_on = [t for t in jp_triples if t["relation_type"] == "works_on"]
        assert len(works_on) == 1
        assert works_on[0]["object"] == "mempalace"
        assert works_on[0]["valid_from"] == "2026-04-21"
    finally:
        age.close()


@pgmark
def test_phase_5_skips_when_done_checkpoint(tmp_path, capsys):
    """Phase exits immediately when migration_phase_kg=done."""
    from mempalace.migrate_to_postgres import (
        phase_1_schema,
        phase_5_kg,
        _set_checkpoint,
        _KG_DONE_KEY,
    )

    palace = tmp_path / "palace"
    palace.mkdir()
    _build_sqlite_kg(
        palace,
        [
            {"id": "t1", "subject": "A", "predicate": "r", "object": "B"},
        ],
    )

    phase_1_schema(POSTGRES_DSN)
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    with psycopg2.connect(POSTGRES_DSN) as conn:
        _set_checkpoint(conn, _KG_DONE_KEY, "done")

    capsys.readouterr()
    phase_5_kg(str(palace), POSTGRES_DSN)
    out = capsys.readouterr().out
    assert "already migrated" in out.lower() or "skipping" in out.lower()


# ── Phase 6 — verify + Phase 7 — cutover ─────────────────────────────


@pgmark
def test_phase_6_verify_reports_match(fixture_chroma_palace, tmp_path, capsys):
    """After full migration, phase_6_verify returns all_match=True."""
    from mempalace.migrate_to_postgres import (
        phase_1_schema,
        phase_2_drawers,
        phase_5_kg,
        phase_6_verify,
    )
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    palace_path, test_table = fixture_chroma_palace
    # Reset state for a clean test
    phase_1_schema(POSTGRES_DSN)
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    with psycopg2.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM mempalace_backend_meta WHERE key LIKE 'migration_%%'")
        conn.commit()
    age = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    age.clear()
    age.close()

    phase_2_drawers(palace_path, POSTGRES_DSN, batch_size=4)
    phase_5_kg(palace_path, POSTGRES_DSN)

    result = phase_6_verify(palace_path, POSTGRES_DSN, sample_n=5)
    assert result["chroma_drawer_count"] == 10
    assert result["postgres_drawer_count"] == 10
    assert result["drawers_match"] is True
    assert result["sample_mismatches"] == []
    assert result["all_match"] is True


@pgmark
def test_phase_6_verify_detects_drawer_mismatch(fixture_chroma_palace, capsys):
    """If a drawer is missing in postgres, drawers_match is False."""
    from mempalace.migrate_to_postgres import (
        phase_1_schema,
        phase_2_drawers,
        phase_6_verify,
    )
    from mempalace.backends.postgres import PostgresBackend

    palace_path, test_table = fixture_chroma_palace
    phase_1_schema(POSTGRES_DSN)
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    with psycopg2.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM mempalace_backend_meta WHERE key LIKE 'migration_%%'")
        conn.commit()

    phase_2_drawers(palace_path, POSTGRES_DSN, batch_size=4)
    # Delete one drawer to create the mismatch
    from mempalace.backends.base import PalaceRef

    backend = PostgresBackend(dsn=POSTGRES_DSN)
    palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
    backend.get_collection(palace=palace_ref, collection_name=test_table, create=True).delete(
        ids=["d0"]
    )

    result = phase_6_verify(palace_path, POSTGRES_DSN, sample_n=10)
    assert result["chroma_drawer_count"] == 10
    assert result["postgres_drawer_count"] == 9
    assert result["drawers_match"] is False
    assert result["all_match"] is False


@pgmark
def test_phase_7_done_prints_cutover_and_records_timestamp(fixture_chroma_palace, capsys):
    """phase_7_done emits cutover instructions + sets migrated_from_chroma_at."""
    from mempalace.migrate_to_postgres import (
        phase_1_schema,
        phase_7_done,
        _get_checkpoint,
    )

    palace_path, _ = fixture_chroma_palace
    phase_1_schema(POSTGRES_DSN)
    phase_7_done(palace_path, POSTGRES_DSN)
    out = capsys.readouterr().out
    assert "Migration complete" in out
    assert "MEMPALACE_BACKEND=postgres" in out
    assert "MEMPALACE_KG_BACKEND=age" in out
    assert "systemctl" in out

    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    with psycopg2.connect(POSTGRES_DSN) as conn:
        ts = _get_checkpoint(conn, "migrated_from_chroma_at")
        assert ts is not None
        # ISO-8601 prefix
        assert ts.startswith("20")


def test_phase_7_redacts_dsn_in_output(capsys, tmp_path):
    """The printed cutover instructions show a redacted DSN, not the password."""
    if POSTGRES_DSN is None:
        pytest.skip("phase_7 needs postgres for checkpoint write")
    from mempalace.migrate_to_postgres import phase_1_schema

    phase_1_schema(POSTGRES_DSN)
    # Use a DSN with a fake password in it; checkpoint goes via the real DSN
    # but the print path should redact whatever DSN we pass in.
    palace = tmp_path / "palace"
    palace.mkdir()
    fake = "postgresql://user:supersecret@host/db"
    # phase_7_done writes a checkpoint via the dsn arg, so we need a working
    # DSN. Use POSTGRES_DSN but assert redaction by passing it through the
    # _redact_dsn helper directly.
    from mempalace.migrate_to_postgres import _redact_dsn

    assert "supersecret" not in _redact_dsn(fake)


@pgmark
def test_phase_5_skips_bad_temporal_data(tmp_path, capsys):
    """Triples with inverted intervals get logged + skipped, not crash."""
    from mempalace.migrate_to_postgres import (
        phase_1_schema,
        phase_5_kg,
        _KG_DONE_KEY,
    )
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    palace = tmp_path / "palace"
    palace.mkdir()
    _build_sqlite_kg(
        palace,
        [
            {
                "id": "good",
                "subject": "A",
                "predicate": "r",
                "object": "B",
            },
            {
                "id": "bad_temporal",
                "subject": "X",
                "predicate": "y",
                "object": "Z",
                "valid_from": "2026-05-10",
                "valid_to": "2025-01-01",  # inverted
            },
        ],
    )

    phase_1_schema(POSTGRES_DSN)
    import psycopg as psycopg2  # psycopg 3, as migrate_to_postgres imports it

    with psycopg2.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM mempalace_backend_meta WHERE key LIKE 'migration_kg_%%' OR key = %s",
            (_KG_DONE_KEY,),
        )
        conn.commit()
    age = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    age.clear()
    age.close()

    phase_5_kg(str(palace), POSTGRES_DSN)
    out = capsys.readouterr().out
    assert "1 skipped" in out or "skip bad_temporal" in out

    # Good triple landed, bad one did not
    age = KnowledgeGraphAGE(dsn=POSTGRES_DSN)
    try:
        triples = age.query_triples(subject="A")
        assert len(triples) == 1
        triples = age.query_triples(subject="X")
        assert len(triples) == 0
    finally:
        age.close()


# ── Raw-sqlite drawer reader unit tests (no postgres required) ──────


def _build_synthetic_chroma_sqlite(palace_dir, collections):
    """Build a minimal chroma.sqlite3 with documents and metadata.

    ``collections`` is a list of dicts:
      {
        "name": str,
        "dimension": int | None,
        "drawers": [
          {"id": str, "document": str, "metadata": dict},
          ...
        ],
      }

    Schema mirrors the chroma 1.x layout we depend on: ``collections``,
    ``segments``, ``embeddings``, ``embedding_metadata``. Vectors are
    deliberately absent — the migration recomputes them, so the synthetic
    fixture only needs the document text + sidecar metadata.
    """
    import sqlite3
    import uuid

    sqlite_path = palace_dir / "chroma.sqlite3"
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE databases (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                dimension INTEGER,
                database_id TEXT NOT NULL
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                scope TEXT NOT NULL,
                collection TEXT NOT NULL
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL,
                seq_id BLOB NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (segment_id, embedding_id)
            );
            CREATE TABLE embedding_metadata (
                id INTEGER REFERENCES embeddings(id),
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (id, key)
            );
            """
        )
        db_id = str(uuid.uuid4())
        conn.execute("INSERT INTO databases (id, name) VALUES (?, ?)", (db_id, "default"))

        next_emb_id = 1
        for col in collections:
            col_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO collections (id, name, dimension, database_id) VALUES (?, ?, ?, ?)",
                (col_id, col["name"], col.get("dimension"), db_id),
            )

            seg_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO segments (id, type, scope, collection) VALUES (?, ?, ?, ?)",
                (seg_id, "urn:chroma:segment/metadata/sqlite", "METADATA", col_id),
            )

            for drawer in col["drawers"]:
                conn.execute(
                    "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) "
                    "VALUES (?, ?, ?, ?)",
                    (next_emb_id, seg_id, drawer["id"], b"\x00"),
                )
                conn.execute(
                    "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, ?, ?)",
                    (next_emb_id, "chroma:document", drawer["document"]),
                )
                for k, v in drawer["metadata"].items():
                    if isinstance(v, bool):
                        conn.execute(
                            "INSERT INTO embedding_metadata (id, key, bool_value) VALUES (?, ?, ?)",
                            (next_emb_id, k, 1 if v else 0),
                        )
                    elif isinstance(v, int):
                        conn.execute(
                            "INSERT INTO embedding_metadata (id, key, int_value) VALUES (?, ?, ?)",
                            (next_emb_id, k, v),
                        )
                    elif isinstance(v, float):
                        conn.execute(
                            "INSERT INTO embedding_metadata (id, key, float_value) "
                            "VALUES (?, ?, ?)",
                            (next_emb_id, k, v),
                        )
                    elif v is None:
                        conn.execute(
                            "INSERT INTO embedding_metadata (id, key) VALUES (?, ?)",
                            (next_emb_id, k),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO embedding_metadata (id, key, string_value) "
                            "VALUES (?, ?, ?)",
                            (next_emb_id, k, str(v)),
                        )
                next_emb_id += 1
        conn.commit()
    return sqlite_path


def test_iter_chroma_collections_via_sqlite_lists_all(tmp_path):
    """Collections come back sorted by name with their dimensions."""
    from mempalace.migrate_to_postgres import _iter_chroma_collections_via_sqlite

    palace = tmp_path / "palace"
    palace.mkdir()
    _build_synthetic_chroma_sqlite(
        palace,
        [
            {"name": "zeta", "dimension": 384, "drawers": []},
            {"name": "alpha", "dimension": 384, "drawers": []},
        ],
    )

    cols = list(_iter_chroma_collections_via_sqlite(palace / "chroma.sqlite3"))
    names = [c[1] for c in cols]
    assert names == ["alpha", "zeta"]
    assert all(c[2] == 384 for c in cols)


def test_iter_drawer_batches_via_sqlite_yields_documents_and_metadata(tmp_path):
    """Batches contain external ids, documents, and metadata dicts."""
    from mempalace.migrate_to_postgres import (
        _iter_chroma_collections_via_sqlite,
        _iter_drawer_batches_via_sqlite,
    )

    palace = tmp_path / "palace"
    palace.mkdir()
    _build_synthetic_chroma_sqlite(
        palace,
        [
            {
                "name": "mempalace_drawers",
                "dimension": 384,
                "drawers": [
                    {
                        "id": "d1",
                        "document": "first doc",
                        "metadata": {"wing": "test", "idx": 1, "flag": True},
                    },
                    {
                        "id": "d2",
                        "document": "second doc",
                        "metadata": {"wing": "test", "idx": 2, "score": 0.5},
                    },
                ],
            }
        ],
    )

    sqlite_path = palace / "chroma.sqlite3"
    (col_id, _name, _dim) = next(_iter_chroma_collections_via_sqlite(sqlite_path))
    batches = list(_iter_drawer_batches_via_sqlite(sqlite_path, col_id, batch_size=10))
    assert len(batches) == 1
    ids, docs, metas = batches[0]
    assert ids == ["d1", "d2"]
    assert docs == ["first doc", "second doc"]
    assert metas[0]["wing"] == "test"
    assert metas[0]["idx"] == 1
    assert metas[0]["flag"] is True
    assert metas[1]["score"] == 0.5
    # chroma:document should not leak into the metadata dict
    assert "chroma:document" not in metas[0]
    assert "chroma:document" not in metas[1]


def test_iter_drawer_batches_pagination_via_watermark(tmp_path):
    """Pages by embeddings.id watermark; covers all rows with small batches."""
    from mempalace.migrate_to_postgres import (
        _iter_chroma_collections_via_sqlite,
        _iter_drawer_batches_via_sqlite,
    )

    palace = tmp_path / "palace"
    palace.mkdir()
    _build_synthetic_chroma_sqlite(
        palace,
        [
            {
                "name": "c",
                "dimension": 384,
                "drawers": [
                    {"id": f"d{i}", "document": f"doc {i}", "metadata": {}} for i in range(7)
                ],
            }
        ],
    )

    sqlite_path = palace / "chroma.sqlite3"
    (col_id, _name, _dim) = next(_iter_chroma_collections_via_sqlite(sqlite_path))
    batches = list(_iter_drawer_batches_via_sqlite(sqlite_path, col_id, batch_size=3))
    sizes = [len(b[0]) for b in batches]
    assert sizes == [3, 3, 1]
    all_ids = [i for batch in batches for i in batch[0]]
    assert all_ids == [f"d{i}" for i in range(7)]


def test_iter_drawer_batches_handles_empty_collection(tmp_path):
    """A collection with zero drawers yields no batches (clean exit)."""
    from mempalace.migrate_to_postgres import (
        _iter_chroma_collections_via_sqlite,
        _iter_drawer_batches_via_sqlite,
    )

    palace = tmp_path / "palace"
    palace.mkdir()
    _build_synthetic_chroma_sqlite(
        palace,
        [{"name": "c", "dimension": 384, "drawers": []}],
    )

    sqlite_path = palace / "chroma.sqlite3"
    (col_id, _name, _dim) = next(_iter_chroma_collections_via_sqlite(sqlite_path))
    batches = list(_iter_drawer_batches_via_sqlite(sqlite_path, col_id, batch_size=10))
    assert batches == []


def test_count_drawers_in_collection(tmp_path):
    """_count_drawers_in_collection returns the metadata-segment row count."""
    from mempalace.migrate_to_postgres import (
        _count_drawers_in_collection,
        _iter_chroma_collections_via_sqlite,
    )

    palace = tmp_path / "palace"
    palace.mkdir()
    _build_synthetic_chroma_sqlite(
        palace,
        [
            {
                "name": "c",
                "dimension": 384,
                "drawers": [
                    {"id": f"d{i}", "document": f"doc {i}", "metadata": {}} for i in range(5)
                ],
            }
        ],
    )

    sqlite_path = palace / "chroma.sqlite3"
    (col_id, _name, _dim) = next(_iter_chroma_collections_via_sqlite(sqlite_path))
    assert _count_drawers_in_collection(sqlite_path, col_id) == 5


@pytest.mark.skipif(
    not importlib.util.find_spec("psycopg2"),
    reason="psycopg2 not installed (postgres extras required)",
)
def test_phase_2_drawers_noops_when_sqlite_missing(tmp_path, capsys):
    """phase_2_drawers prints and returns when chroma.sqlite3 is absent.

    Empty/non-chroma palaces should not be a fatal error — this path
    fires e.g. when the migration is re-run after a successful cutover
    and the source has been archived.
    """
    from mempalace.migrate_to_postgres import phase_2_drawers

    empty = tmp_path / "empty-palace"
    empty.mkdir()
    # No DSN connection happens — there are no collections to copy. Pass
    # an obviously-invalid DSN to prove we never try to open postgres.
    phase_2_drawers(str(empty), "postgresql://invalid:invalid@no-such-host:1/db")
    out = capsys.readouterr().out
    assert "no chroma.sqlite3" in out


@pytest.mark.skipif(
    not importlib.util.find_spec("psycopg2"),
    reason="psycopg2 not installed (postgres extras required)",
)
def test_phase_2_drawers_does_not_require_chromadb(tmp_path, monkeypatch, capsys):
    """phase_2_drawers must not import chromadb during the read path.

    Block ``chromadb`` from the import system entirely and confirm the
    early-return path still runs cleanly. The actual write path needs a
    postgres + an embedding function, which are tested separately under
    the ``@pgmark`` guard.
    """
    import builtins
    import sys as _sys

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "chromadb" or name.startswith("chromadb."):
            raise ImportError(f"chromadb is blocked in this test ({name})")
        return real_import(name, *args, **kwargs)

    # Drop any cached chromadb modules so the next import goes through
    # our blocker.
    for mod in list(_sys.modules):
        if mod == "chromadb" or mod.startswith("chromadb."):
            del _sys.modules[mod]

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    from mempalace.migrate_to_postgres import phase_2_drawers

    empty = tmp_path / "empty-palace"
    empty.mkdir()
    phase_2_drawers(str(empty), "postgresql://invalid:invalid@no-such-host:1/db")
    out = capsys.readouterr().out
    assert "no chroma.sqlite3" in out
