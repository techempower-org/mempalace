"""Tests for the ChromaDB → Postgres migration tool.

Most tests gate on TEST_POSTGRES_DSN since they touch a real Postgres.
The CLI-help test runs unconditionally — it doesn't need the database.
"""

import os
import subprocess
import sys
from unittest.mock import patch

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
    assert res.returncode == 0, (
        f"--help exited {res.returncode}: {res.stderr or res.stdout}"
    )
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

    with patch.dict(os.environ, {"PALACE_DAEMON_URL": "http://disks:8085"}), \
         patch("urllib.request.urlopen", return_value=FakeResponse()):
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


# ── Phase 1 — schema creation (real postgres required) ───────────────


@pgmark
def test_phase_1_creates_extensions_and_checkpoint_table(capsys):
    """phase_1_schema installs vector + age and creates the meta table."""
    import psycopg2
    from mempalace.migrate_to_postgres import (
        CHECKPOINT_TABLE,
        phase_1_schema,
        _get_checkpoint,
    )

    phase_1_schema(POSTGRES_DSN)
    out = capsys.readouterr().out
    assert "phase 1" in out.lower()

    with psycopg2.connect(POSTGRES_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'age')"
        )
        ext = {r[0] for r in cur.fetchall()}
        assert "vector" in ext, "pgvector should be installed after phase 1"
        # AGE may or may not install depending on infrastructure; required at
        # preflight, but a non-AGE setup may have only vector.
        cur.execute(
            "SELECT to_regclass(%s)",
            (CHECKPOINT_TABLE,),
        )
        assert cur.fetchone()[0] is not None, (
            f"{CHECKPOINT_TABLE} should exist after phase 1"
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
