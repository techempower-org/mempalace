"""ChromaDB → Postgres (pgvector + AGE) migration tool.

A restartable, idempotent, checkpointed migration. Seven phases:

    0. preflight  — env probes, daemon not running, extensions available
    1. schema     — CREATE EXTENSION + bootstrap mempalace.* tables
    2. drawers    — batch-copy drawers from chroma to pgvector
    3. closets    — batch-copy closets collection
    4. indexes    — HNSW + supporting indexes on copied data
    5. kg         — SQLite knowledge_graph.sqlite3 → AGE graph
    6. verify     — counts match, sample queries match

Per-phase checkpoint in ``mempalace.backend_meta`` so a re-run resumes
from the last completed phase. Designed to be safe to invoke multiple
times against the same source/target pair.

Tracks Phase 3 of `docs/superpowers/plans/2026-05-10-pgvector-age-migration-impl.md`.
3.1 (this commit) ships the CLI scaffold + phase 0; phases 1–6 land in
subsequent commits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def run_migration(
    chroma_path: str,
    postgres_dsn: str,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> None:
    """Orchestrate the 7-phase migration.

    Phase 0 always runs (it's the gate). When ``dry_run`` is true,
    we stop after phase 0 — no writes happen.
    """
    redacted_dsn = _redact_dsn(postgres_dsn)
    print(f"[mempalace migrate-to-postgres] from={chroma_path} to={redacted_dsn}")
    phase_0_preflight(chroma_path, postgres_dsn)
    if dry_run:
        print("[dry-run] preflight only; exiting before any writes")
        return
    phase_1_schema(postgres_dsn)
    raise NotImplementedError(
        "phases 2–6 land in subsequent tasks of the pgvector-age migration plan"
    )


# ── Phase 0 — Preflight ───────────────────────────────────────────────


def phase_0_preflight(chroma_path: str, postgres_dsn: str) -> None:
    """Verify the migration is safe to run; exit non-zero on any failure.

    Checks (any failure aborts):
      1. Source chroma palace directory exists.
      2. palace-daemon is NOT responding at PALACE_DAEMON_URL (a running
         daemon would race the migration's writes against live MCP traffic).
      3. Postgres ``vector`` and ``age`` extensions are available in the
         target database (not necessarily installed — just present in
         ``pg_available_extensions``).
    """
    if not Path(chroma_path).is_dir():
        sys.exit(f"FATAL: source palace not found: {chroma_path}")

    _check_daemon_not_running()
    _check_postgres_extensions(postgres_dsn)
    print("[phase 0] preflight passed")


def _check_daemon_not_running() -> None:
    """Refuse to migrate while palace-daemon is responsive.

    Reads ``PALACE_DAEMON_URL`` from the environment; if it's reachable
    we abort with an explicit ``systemctl stop`` instruction. If the
    env var is unset OR the daemon is unreachable, we proceed.
    """
    daemon_url = os.environ.get("PALACE_DAEMON_URL", "").strip().rstrip("/")
    if not daemon_url:
        return
    try:
        from urllib.request import urlopen

        with urlopen(f"{daemon_url}/health", timeout=2) as r:
            if r.status == 200:
                sys.exit(
                    f"FATAL: palace-daemon is responsive at {daemon_url}; "
                    "stop the daemon before migrating "
                    "(`sudo systemctl stop palace-daemon` on the daemon host)."
                )
    except Exception:
        pass  # daemon not reachable — proceed


def _check_postgres_extensions(postgres_dsn: str) -> None:
    """Verify pgvector + AGE are available (not necessarily installed)."""
    try:
        import psycopg2
    except ImportError:
        sys.exit(
            "FATAL: psycopg2 not installed. Install with: "
            "`pip install -e '.[postgres]'`"
        )

    try:
        with psycopg2.connect(postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT extname FROM pg_available_extensions "
                "WHERE extname IN ('vector', 'age')"
            )
            avail = {row[0] for row in cur.fetchall()}
    except psycopg2.OperationalError as e:
        sys.exit(f"FATAL: cannot connect to target Postgres — {e}")

    for required in ("vector", "age"):
        if required not in avail:
            sys.exit(
                f"FATAL: Postgres extension '{required}' not available on "
                "target database. For pgvector see "
                "https://github.com/pgvector/pgvector; for AGE see "
                "https://age.apache.org/age-manual/master/intro/setup.html"
            )


# ── Phase 1 — Schema (extensions + checkpoint table) ─────────────────


CHECKPOINT_TABLE = "mempalace_backend_meta"


def phase_1_schema(postgres_dsn: str) -> None:
    """Install pgvector + AGE extensions and create the checkpoint table.

    Idempotent: ``CREATE EXTENSION IF NOT EXISTS`` and
    ``CREATE TABLE IF NOT EXISTS`` mean a re-run is a no-op. The drawer
    and closet tables themselves are NOT created here — ``PostgresBackend``
    bootstraps those lazily during phase 2 when the first write lands.
    Keeping schema-creation responsibilities split (extensions+meta here,
    data tables in the backend) avoids two sources of truth for the
    drawer schema.

    Records ``migration_phase_schema=done`` in the checkpoint table so a
    re-run of the whole migration can skip this phase next time.
    """
    import psycopg2

    with psycopg2.connect(postgres_dsn) as conn:
        # autocommit needed for CREATE EXTENSION on some Postgres builds
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("CREATE EXTENSION IF NOT EXISTS age")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CHECKPOINT_TABLE} (
                    key text PRIMARY KEY,
                    value text NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        conn.autocommit = False
        _set_checkpoint(conn, "migration_phase_schema", "done")
    print("[phase 1] schema created")


# ── Helpers ───────────────────────────────────────────────────────────


def _redact_dsn(dsn: str) -> str:
    """Hide the password portion of a Postgres DSN for log lines.

    Accepts both URL form (``postgresql://user:pass@host/db``) and
    key=value form. Returns the DSN with the password replaced by ``***``
    or the original string if no password is detected.
    """
    if not dsn:
        return ""
    # URL form
    if "://" in dsn:
        try:
            from urllib.parse import urlparse, urlunparse

            parsed = urlparse(dsn)
            if parsed.password:
                netloc = parsed.netloc.replace(parsed.password, "***", 1)
                return urlunparse(parsed._replace(netloc=netloc))
        except Exception:
            pass
    # key=value form — naive scrub
    parts = []
    for token in dsn.split():
        if token.lower().startswith("password="):
            parts.append("password=***")
        else:
            parts.append(token)
    return " ".join(parts)


# ── Checkpoint helpers (used from phase 1+) ───────────────────────────


def _set_checkpoint(conn, key: str, value: str) -> None:
    """Idempotent upsert into the checkpoint table.

    Used by phases 1–6 to record completion so a re-run can skip work.
    Caller controls the transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {CHECKPOINT_TABLE} (key, value) VALUES (%s, %s) "
            f"ON CONFLICT (key) DO UPDATE SET "
            f"  value = EXCLUDED.value, updated_at = now()",
            (key, value),
        )
    conn.commit()


def _get_checkpoint(conn, key: str) -> Optional[str]:
    """Read a checkpoint value; None if absent."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT value FROM {CHECKPOINT_TABLE} WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()
    return row[0] if row else None
