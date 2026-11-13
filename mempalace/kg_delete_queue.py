"""Deferred AGE delete-through (#558, #610).

Deleting a drawer must also remove its AGE ``Drawer`` vertex, or the graph fills
with orphans. Doing that inline is too slow on a large graph: AGE 1.6's
``DETACH DELETE`` paid ~16 s per statement plus ~1 s per vertex on the
production graph (2026-11-13: 17.0 s for one vertex, 21.1 s for four), and the
miners delete stale drawers while holding the palace mine lock.

So the delete hook only records the ids in a small table (one INSERT), and
:func:`sweep` removes the queued vertices later in batches: one ``DETACH
DELETE`` statement per batch, so the fixed cost is paid once per batch rather
than once per delete. The nightly dream run calls :func:`sweep_from_env`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

QUEUE_TABLE = "mempalace_kg_delete_queue"
BATCH = 500
MAX_BATCHES = 20

_DDL = f"""
CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
    drawer_id  TEXT PRIMARY KEY,
    queued_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _connect(dsn: str):
    from .knowledge_graph_age import _load_psycopg2

    conn = _load_psycopg2().connect(dsn, connect_timeout=5, options="-c statement_timeout=10000")
    conn.autocommit = True
    return conn


def make_queued_deletethrough(dsn: str, connect: Optional[Callable[[str], Any]] = None):
    """Delete hook that enqueues the deleted drawer ids. Never raises: the
    relational delete has already committed, and a failed enqueue only leaves
    an orphan vertex behind (logged), exactly what an absent hook would."""
    connect = connect or _connect
    state: Dict[str, Any] = {"conn": None, "ready": False}
    lock = threading.Lock()

    def hook(*, drawer_ids) -> None:
        ids = sorted({str(d) for d in drawer_ids if d})
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
                    cur.execute(
                        f"INSERT INTO {QUEUE_TABLE} (drawer_id)"
                        " SELECT unnest(%s::text[]) ON CONFLICT (drawer_id) DO NOTHING",
                        (ids,),
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "KG delete-through: could not queue %d id(s) for the graph sweep: %s",
                    len(ids),
                    type(e).__name__,
                )
                try:
                    if state["conn"] is not None:
                        state["conn"].close()
                except Exception:  # noqa: BLE001
                    pass
                state["conn"] = None

    return hook


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
    logged and returned. A batch's ids leave the queue only after its delete
    returned; an error stops the sweep and keeps the rest queued."""
    if kg is None:
        from .knowledge_graph_age import KnowledgeGraphAGE

        kg = KnowledgeGraphAGE(dsn=dsn)
    conn = (connect or _connect)(dsn)
    out: Dict[str, Any] = {"batches": 0, "ids": 0, "removed": 0, "seconds": [], "left": None}
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        for _ in range(max_batches):
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT drawer_id FROM {QUEUE_TABLE} ORDER BY queued_at, drawer_id LIMIT %s",
                    (batch,),
                )
                ids: List[str] = [r[0] for r in cur.fetchall()]
            if not ids:
                break
            t0 = time.monotonic()
            removed = kg.delete_drawers(ids)
            secs = round(time.monotonic() - t0, 1)
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {QUEUE_TABLE} WHERE drawer_id = ANY(%s)", (ids,))
            out["batches"] += 1
            out["ids"] += len(ids)
            out["removed"] += int(removed or 0)
            out["seconds"].append(secs)
            logger.info(
                "KG delete sweep: batch of %d id(s), %d vertex(es) removed, %.1f s",
                len(ids),
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
        logger.warning("KG delete sweep failed: %s", e)
        return {"error": type(e).__name__}
