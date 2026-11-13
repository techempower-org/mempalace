"""Deferred AGE delete-through (#558, #610).

Deleting a drawer must also remove its AGE ``Drawer`` vertex, or the graph fills
with orphans. Doing that inline is too slow on a large graph: AGE 1.6's
``DETACH DELETE`` paid ~16 s per statement plus ~1 s per vertex on the
production graph (2026-11-13: 17.0 s for one vertex, 21.1 s for four), and the
miners delete stale drawers while holding the palace mine lock.

So the delete hook only records the ids in a small table (one upsert), and
:func:`sweep` removes the queued vertices later in batches: one ``DETACH
DELETE`` statement per batch, so the fixed cost is paid once per batch rather
than once per delete. The nightly dream run calls :func:`sweep_from_env`.

Correctness rules the sweep keeps:

* An id whose drawer row exists again (re-mined under the same deterministic
  id, or a relational delete that rolled back after the hook ran) is
  acknowledged WITHOUT touching its vertex: it is live.
* Each enqueue bumps the row's ``version``; the sweep acknowledges only the
  (id, version) pairs it selected, so a delete of the same id that lands while
  a batch is in flight stays queued for the next sweep.
* A batch leaves the queue only after its graph delete returned. Residual: a
  drawer re-added between the liveness check and the DETACH DELETE loses its
  vertex; with write-through off a re-added drawer has none anyway.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

QUEUE_TABLE = "mempalace_kg_delete_queue"
DEFAULT_DRAWER_TABLE = "mempalace_drawers"
BATCH = 500
MAX_BATCHES = 20
_TABLE_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")

_DDL = f"""
CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
    drawer_id     TEXT PRIMARY KEY,
    source_table  TEXT NOT NULL DEFAULT '{DEFAULT_DRAWER_TABLE}',
    version       INTEGER NOT NULL DEFAULT 1,
    queued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

_UPSERT = (
    f"INSERT INTO {QUEUE_TABLE} AS q (drawer_id, source_table)"
    " SELECT unnest(%s::text[]), %s"
    " ON CONFLICT (drawer_id) DO UPDATE"
    " SET version = q.version + 1, queued_at = NOW(), source_table = EXCLUDED.source_table"
)


def _connect(dsn: str):
    from .knowledge_graph_age import _load_psycopg2

    conn = _load_psycopg2().connect(dsn, connect_timeout=5, options="-c statement_timeout=10000")
    conn.autocommit = True
    return conn


def _table(name: Optional[str]) -> str:
    name = (name or DEFAULT_DRAWER_TABLE).strip()
    if not _TABLE_RE.fullmatch(name):
        raise ValueError("unsafe drawer table name")
    return name


def make_queued_deletethrough(
    dsn: str,
    table: Optional[str] = None,
    connect: Optional[Callable[[str], Any]] = None,
):
    """Delete hook that enqueues the deleted drawer ids. Never raises: the
    caller's relational delete stands either way, and a failed enqueue only
    leaves an orphan vertex behind (logged), exactly what an absent hook would."""
    connect = connect or _connect
    source = _table(table)
    state: Dict[str, Any] = {"conn": None, "ready": False}
    lock = threading.Lock()

    def hook(*, drawer_ids) -> None:
        n = "?"
        try:
            ids = sorted({str(d) for d in drawer_ids if d})
            n = len(ids)
            if not ids:
                return
            with lock:
                try:
                    conn = state["conn"]
                    if conn is None or getattr(conn, "closed", 0):
                        conn = state["conn"] = connect(dsn)
                        state["ready"] = False
                    with conn.cursor() as cur:
                        if not state["ready"]:
                            cur.execute(_DDL)
                            state["ready"] = True
                        cur.execute(_UPSERT, (ids, source))
                except Exception:
                    try:
                        if state["conn"] is not None:
                            state["conn"].close()
                    except Exception:  # noqa: BLE001
                        pass
                    state["conn"] = None
                    raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "KG delete-through: could not queue %s id(s) for the graph sweep: %s",
                n,
                type(e).__name__,
            )

    return hook


def _live_ids(cur, table: str, ids: List[str]) -> set:
    """Ids that have a drawer row again. A drawer table that does not exist
    (SQLSTATE 42P01) has no live rows; any other error propagates."""
    try:
        cur.execute(f"SELECT id FROM {_table(table)} WHERE id = ANY(%s)", (ids,))
    except Exception as e:  # noqa: BLE001
        if (getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)) == "42P01":
            return set()
        raise
    return {r[0] for r in cur.fetchall()}


def sweep(
    dsn: str,
    kg: Any = None,
    *,
    batch: int = BATCH,
    max_batches: int = MAX_BATCHES,
    connect: Optional[Callable[[str], Any]] = None,
) -> dict:
    """Remove queued Drawer vertices, ``batch`` ids per DETACH DELETE statement,
    oldest first, at most ``max_batches`` statements. Each batch's wall time is
    logged and returned. An error stops the sweep; the failing batch and
    everything after it stay queued."""
    if kg is None:
        from .knowledge_graph_age import KnowledgeGraphAGE

        kg = KnowledgeGraphAGE(dsn=dsn)
    conn = (connect or _connect)(dsn)
    out: Dict[str, Any] = {
        "batches": 0,
        "ids": 0,
        "removed": 0,
        "live_skipped": 0,
        "seconds": [],
        "left": None,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        for _ in range(max_batches):
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT drawer_id, source_table, version FROM {QUEUE_TABLE}"
                    " ORDER BY queued_at, drawer_id LIMIT %s",
                    (batch,),
                )
                rows = cur.fetchall()
            if not rows:
                break
            ids = [r[0] for r in rows]
            live: set = set()
            with conn.cursor() as cur:
                by_table: Dict[str, List[str]] = {}
                for did, table, _v in rows:
                    by_table.setdefault(table, []).append(did)
                for table, tids in by_table.items():
                    live |= _live_ids(cur, table, tids)
            dead = [i for i in ids if i not in live]
            t0 = time.monotonic()
            removed = kg.delete_drawers(dead) if dead else 0
            secs = round(time.monotonic() - t0, 1)
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {QUEUE_TABLE} q USING"
                    " (SELECT unnest(%s::text[]) AS drawer_id, unnest(%s::int[]) AS version) a"
                    " WHERE q.drawer_id = a.drawer_id AND q.version = a.version",
                    ([r[0] for r in rows], [int(r[2]) for r in rows]),
                )
            out["batches"] += 1
            out["ids"] += len(ids)
            out["removed"] += int(removed or 0)
            out["live_skipped"] += len(live)
            out["seconds"].append(secs)
            logger.info(
                "KG delete sweep: batch of %d id(s) (%d live again, skipped), "
                "%d vertex(es) removed, %.1f s",
                len(ids),
                len(live),
                removed or 0,
                secs,
            )
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {QUEUE_TABLE}")
            out["left"] = cur.fetchone()[0]
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def sweep_from_env(env: Optional[dict] = None, **kw) -> dict:
    """The nightly step. Skips (never fails the night) when the switch is off,
    there is no Postgres DSN, or the graph does not exist."""
    from .kg_writethrough import age_graph_exists, deletethrough_enabled

    env = os.environ if env is None else env
    if not deletethrough_enabled():
        return {"skipped": "MEMPALACE_KG_DELETETHROUGH is off"}
    dsn = env.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        return {"skipped": "no MEMPALACE_POSTGRES_DSN"}
    found = age_graph_exists(dsn)
    if found is not True:
        return {"skipped": "no AGE graph" if found is False else "graph probe failed"}
    try:
        return sweep(dsn, **kw)
    except Exception as e:  # noqa: BLE001
        logger.warning("KG delete sweep failed: %s", type(e).__name__)
        return {"error": type(e).__name__}
