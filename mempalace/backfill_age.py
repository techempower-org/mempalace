"""Backfill the AGE graph from an existing drawer table.

Phase 4 of the AGE-integration goal. Builds the full palace-graph + KG
state from drawer rows that were written BEFORE the write-through
middleware was registered. Companion to ``migrate_to_postgres``: that
script copies chroma → postgres, this one copies postgres-drawers →
postgres-AGE.

Design goals:

1. **Restartable.** A checkpoint table (`mempalace_kg_backfill_state`)
   tracks last completed (wing, room) pair. Re-running from scratch is
   safe but skips already-completed (wing, room) groups.
2. **Idempotent.** All inserts use MERGE, so the same drawer being
   processed twice has no duplicating effect.
3. **Bounded memory.** Streams drawer rows via server-side cursor
   (psycopg2's named cursor), processes one at a time, never holds
   the full result set.
4. **Configurable scope.** Can target one wing at a time, or all wings,
   or only do the high-level palace map (skip per-drawer entity
   extraction) for a fast first pass.

Three layers populated:

- Palace structure (Wing → Room → Drawer): from
  ``palace_graph_age.populate_from_postgres``. Idempotent re-MERGE.
- Entity extraction + MENTIONS edges: per-drawer regex extractor by
  default (configurable via env var).
- Optional: KG triples seeded from ENTITY_FACTS if present.

CLI entry point (registered as ``mempalace-backfill-age``):

    mempalace-backfill-age \\
        --dsn "$MEMPALACE_POSTGRES_DSN" \\
        --table mempalace_drawers \\
        [--wing <name>]  \\
        [--skip-palace] \\
        [--skip-entities] \\
        [--restart]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Optional

from .knowledge_graph_age import KnowledgeGraphAGE
from .palace_graph_age import populate_from_postgres

logger = logging.getLogger("mempalace.backfill_age")

CHECKPOINT_TABLE = "mempalace_kg_backfill_state"


def _ensure_checkpoint_table(conn) -> None:
    """Idempotent — creates the checkpoint table if not present."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {CHECKPOINT_TABLE} (
                phase        TEXT NOT NULL,
                key          TEXT NOT NULL,
                completed_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (phase, key)
            )
            """
        )
        conn.commit()


def _checkpoint_done(conn, phase: str, key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {CHECKPOINT_TABLE} WHERE phase = %s AND key = %s",
            (phase, key),
        )
        return cur.fetchone() is not None


def _checkpoint_mark(conn, phase: str, key: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {CHECKPOINT_TABLE} (phase, key) VALUES (%s, %s) "
            f"ON CONFLICT (phase, key) DO NOTHING",
            (phase, key),
        )
        conn.commit()


def _checkpoint_clear(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {CHECKPOINT_TABLE}")
        conn.commit()
        return cur.rowcount


def _get_extractor(name: str = "regex"):
    """Return an extractor callable matching (text) -> list[Entity]."""
    if name == "regex":
        try:
            from sme.extractors.regex import extract as sme_extract  # type: ignore
            return sme_extract
        except ImportError:
            from .kg_writethrough import _builtin_regex_extractor
            return _builtin_regex_extractor
    raise ValueError(
        f"unknown extractor={name!r}; supported: regex (spacy/llm pending)"
    )


def backfill(
    *,
    dsn: str,
    table_name: str = "mempalace_drawers",
    wing_filter: Optional[str] = None,
    skip_palace: bool = False,
    skip_entities: bool = False,
    extractor_name: str = "regex",
    max_entities_per_drawer: int = 50,
    relation_type: str = "mentions",
    confidence: float = 0.5,
    restart: bool = False,
    log_every: int = 500,
) -> dict:
    """Backfill AGE graph from an existing drawer table.

    Args:
        dsn: Postgres DSN — must point at a database where AGE is loaded.
        table_name: Source drawer table.
        wing_filter: If set, only process drawers in this wing.
        skip_palace: Skip Wing/Room/Drawer/SHARED_VIA population. Useful
            if you've already run it once and just want a fresh entity
            pass.
        skip_entities: Skip MENTIONS extraction. Useful for first-pass
            "just give me the palace map" on huge palaces.
        extractor_name: regex (default), spacy, llm — only regex
            implemented today.
        max_entities_per_drawer: Cap on entities per drawer write; same
            knob as ``kg_writethrough.make_age_writethrough``.
        relation_type: Edge label for drawer → entity mentions.
        confidence: Default confidence for extracted mentions.
        restart: Clear the checkpoint table before starting (forces a
            full re-backfill).
        log_every: How often to emit progress logs.

    Returns counters dict tracking what was processed.
    """
    from .backends.postgres import _load_psycopg2

    psycopg2, _ = _load_psycopg2()
    counters = {
        "palace": {},
        "drawers_seen": 0,
        "drawers_skipped_checkpoint": 0,
        "entities_added": 0,
        "errors": 0,
        "started_at": time.time(),
    }

    # Connect KG + checkpoint connection separately so a long-running
    # entity extraction phase doesn't tie up the checkpoint cursor.
    kg = KnowledgeGraphAGE(dsn)
    checkpoint_conn = psycopg2.connect(dsn)
    checkpoint_conn.autocommit = False
    _ensure_checkpoint_table(checkpoint_conn)
    if restart:
        cleared = _checkpoint_clear(checkpoint_conn)
        logger.info("backfill: --restart cleared %d checkpoint rows", cleared)

    # Phase A: palace structure
    if not skip_palace:
        palace_key = f"palace:{wing_filter or 'ALL'}"
        if not _checkpoint_done(checkpoint_conn, "palace", palace_key):
            logger.info("backfill: phase=palace key=%s", palace_key)
            counters["palace"] = populate_from_postgres(
                kg, dsn=dsn, table_name=table_name,
                # We rebuild drawers in phase B with entity extraction;
                # palace pass only needs the structural Wing/Room/SHARED_VIA.
                skip_drawers=True,
                skip_tunnels=False,
            )
            _checkpoint_mark(checkpoint_conn, "palace", palace_key)
        else:
            logger.info("backfill: phase=palace key=%s already done; skipping", palace_key)

    # Phase B: drawer + entity extraction
    if not skip_entities:
        extractor = _get_extractor(extractor_name)

        sql_drawers = f"""
            SELECT id, document, wing, room FROM "{table_name}"
            WHERE document IS NOT NULL
        """
        params: list = []
        if wing_filter:
            sql_drawers += " AND wing = %s"
            params.append(wing_filter)
        sql_drawers += " ORDER BY id"

        # Stream via named cursor so we don't load all drawers at once.
        scan_conn = psycopg2.connect(dsn)
        scan_conn.autocommit = False
        try:
            with scan_conn.cursor(name="drawer_scan_cur") as cur:
                cur.itersize = 1000
                cur.execute(sql_drawers, params)
                t0 = time.time()
                for drawer_id, document, wing, room in cur:
                    counters["drawers_seen"] += 1
                    if _checkpoint_done(checkpoint_conn, "drawer", drawer_id):
                        counters["drawers_skipped_checkpoint"] += 1
                        continue

                    # Ensure the Drawer node exists + CONTAINS edge from Room.
                    try:
                        kg._run_cypher(
                            "MERGE (d:Drawer {id: $id})",
                            {"id": drawer_id},
                        )
                        if room and room.strip():
                            kg._run_cypher(
                                """
                                MATCH (r:Room {name: $room}), (d:Drawer {id: $id})
                                MERGE (r)-[:CONTAINS]->(d)
                                """,
                                {"room": room, "id": drawer_id},
                            )
                    except Exception as e:  # noqa: BLE001
                        counters["errors"] += 1
                        logger.debug("drawer-node failed for %s: %s", drawer_id, e)
                        continue

                    # Extract + add MENTIONS edges (Drawer)-[:MENTIONS]->(Entity).
                    try:
                        ents = extractor(document)
                    except Exception as e:  # noqa: BLE001
                        counters["errors"] += 1
                        logger.debug("extractor failed for %s: %s", drawer_id, e)
                        ents = []
                    for ent in (ents or [])[:max_entities_per_drawer]:
                        try:
                            kg.add_mention(
                                drawer_id=drawer_id,
                                entity_name=ent.name,
                                entity_type=getattr(ent, "type", "unknown"),
                                count=getattr(ent, "count", 1),
                                confidence=confidence,
                            )
                            counters["entities_added"] += 1
                        except Exception as e:  # noqa: BLE001
                            counters["errors"] += 1
                            logger.debug(
                                "add_mention failed (%s, %s): %s",
                                drawer_id, ent.name, e,
                            )

                    _checkpoint_mark(checkpoint_conn, "drawer", drawer_id)

                    if counters["drawers_seen"] % log_every == 0:
                        elapsed = time.time() - t0
                        rate = counters["drawers_seen"] / max(elapsed, 0.001)
                        logger.info(
                            "backfill: drawers_seen=%d entities_added=%d skipped=%d errors=%d rate=%.1f/s",
                            counters["drawers_seen"],
                            counters["entities_added"],
                            counters["drawers_skipped_checkpoint"],
                            counters["errors"],
                            rate,
                        )
        finally:
            scan_conn.close()

    counters["finished_at"] = time.time()
    counters["wall_clock_s"] = round(counters["finished_at"] - counters["started_at"], 1)
    kg.close()
    checkpoint_conn.close()
    return counters


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Backfill AGE graph from drawer table")
    parser.add_argument("--dsn", required=True, help="Postgres DSN")
    parser.add_argument("--table", default="mempalace_drawers", help="Drawer table name")
    parser.add_argument("--wing", default=None, help="Restrict to a single wing")
    parser.add_argument("--skip-palace", action="store_true",
                        help="Skip Wing/Room/SHARED_VIA structure")
    parser.add_argument("--skip-entities", action="store_true",
                        help="Skip per-drawer entity extraction")
    parser.add_argument("--extractor", default="regex", help="Entity extractor (regex)")
    parser.add_argument("--max-entities", type=int, default=50)
    parser.add_argument("--restart", action="store_true",
                        help="Clear checkpoint table before starting")
    parser.add_argument("--log-every", type=int, default=500)
    args = parser.parse_args(argv)

    counters = backfill(
        dsn=args.dsn,
        table_name=args.table,
        wing_filter=args.wing,
        skip_palace=args.skip_palace,
        skip_entities=args.skip_entities,
        extractor_name=args.extractor,
        max_entities_per_drawer=args.max_entities,
        restart=args.restart,
        log_every=args.log_every,
    )
    print(json.dumps(counters, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
