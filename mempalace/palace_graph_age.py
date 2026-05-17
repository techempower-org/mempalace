"""Palace structure (Wing → Room → Drawer) as native AGE graph nodes.

Phase 3 of the AGE-integration goal. Today's ``palace_graph.build_graph``
aggregates wing/room/tunnel structure from the drawer table via SQL on
every call. This module mirrors that hierarchy into AGE so:

1. Cypher MATCH walks the palace structure natively — no SQL aggregation
   per query.
2. The Entity / MENTIONS layer (from kg_writethrough.py) connects to the
   palace structure layer via shared Drawer nodes.
3. The "agent walks into the palace" metaphor becomes a Cypher pattern:

   MATCH (w:Wing {name: $wing})-[:CONTAINS]->(r:Room)-[:CONTAINS]->
         (d:Drawer)-[:MENTIONS]->(e:Entity)
   RETURN w, r, d, e

Node labels:
    Wing       — top-level grouping (project / repo / domain)
    Room       — topic within a wing
    Drawer     — individual drawer (one per stored memory)
    Entity     — extracted name (person, project, identifier, ...)

Edge labels:
    CONTAINS   — Wing → Room, Room → Drawer (hierarchical)
    MENTIONS   — Drawer → Entity (from kg_writethrough)
    SHARED_VIA — Wing ↔ Wing where they share a Room (tunnels)

The populate functions are idempotent: re-running on the same source
data MERGEs by name/id rather than blindly creating duplicates. They
are restartable.

Read-side helpers (walk_wing, find_drawers_in_room, etc.) are bundled
here for convenience, but the canonical query interface is
KnowledgeGraphAGE._run_cypher with arbitrary Cypher.
"""

from __future__ import annotations

import logging
from typing import Optional

from .knowledge_graph_age import KnowledgeGraphAGE, sanitize_kg_value

logger = logging.getLogger("mempalace.palace_graph_age")


def populate_from_postgres(
    kg: KnowledgeGraphAGE,
    *,
    dsn: str,
    table_name: str = "mempalace_drawers",
    skip_drawers: bool = False,
    skip_tunnels: bool = False,
    batch_log_every: int = 500,
) -> dict:
    """Populate palace structure into AGE from the drawer table.

    Reads the drawer table once, builds Wing/Room/Drawer/SHARED_VIA in
    AGE. Idempotent — re-runs MERGE on identifier (wing.name, room.name,
    drawer.id) so existing nodes aren't duplicated.

    Args:
        kg: A KnowledgeGraphAGE instance (already connected, graph
            initialized).
        dsn: Postgres DSN to read drawers from (typically the same
            DSN the KG uses, but kept explicit so cross-database
            populates remain possible).
        table_name: Drawer table to read.
        skip_drawers: If True, only build Wing/Room/SHARED_VIA edges and
            skip the per-drawer Drawer nodes + CONTAINS edges. Faster
            for "I just want the high-level palace map" use cases.
        skip_tunnels: If True, skip SHARED_VIA edges (room→wing
            adjacency). Useful for first-pass population on huge
            palaces where you want CONTAINS first.

    Returns a counters dict: {wings, rooms, drawers, contains_edges,
    shared_via_edges}.
    """
    from .backends.postgres import _load_psycopg2

    psycopg2, _ = _load_psycopg2()

    counters = {
        "wings": 0,
        "rooms": 0,
        "drawers": 0,
        "contains_edges": 0,
        "shared_via_edges": 0,
    }

    # Pass 1: aggregate wing → set of rooms; room → set of wings.
    # Single SQL scan, same approach as palace_graph._build_graph_postgres.
    sql_rooms = f"""
        SELECT room, array_agg(DISTINCT wing ORDER BY wing) AS wings
        FROM "{table_name}"
        WHERE wing IS NOT NULL AND wing <> ''
          AND room IS NOT NULL AND room <> ''
        GROUP BY room
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql_rooms)
                rooms_by_wings: list[tuple[str, list[str]]] = [
                    (r[0], list(r[1] or [])) for r in cur.fetchall()
                ]
    finally:
        conn.close()

    all_wings = sorted({w for _, wings in rooms_by_wings for w in wings})
    logger.info(
        "palace_graph_age: %d unique rooms across %d wings",
        len(rooms_by_wings), len(all_wings),
    )

    # Wing nodes
    for wing in all_wings:
        wing_clean = sanitize_kg_value(wing, "wing")
        kg._run_cypher(
            "MERGE (w:Wing {name: $name})",
            {"name": wing_clean},
        )
        counters["wings"] += 1

    # Room nodes + CONTAINS edges (Wing → Room)
    for room, wings in rooms_by_wings:
        room_clean = sanitize_kg_value(room, "room")
        kg._run_cypher(
            "MERGE (r:Room {name: $name})",
            {"name": room_clean},
        )
        counters["rooms"] += 1
        for wing in wings:
            wing_clean = sanitize_kg_value(wing, "wing")
            # Idempotent CONTAINS via MATCH+MERGE. AGE's MERGE on edges
            # is supported as long as we don't combine with ON CREATE SET.
            kg._run_cypher(
                """
                MATCH (w:Wing {name: $wing}), (r:Room {name: $room})
                MERGE (w)-[:CONTAINS]->(r)
                """,
                {"wing": wing_clean, "room": room_clean},
            )
            counters["contains_edges"] += 1
        if (counters["rooms"] % batch_log_every) == 0:
            logger.info(
                "palace_graph_age: %d rooms / %d Wing-CONTAINS-Room edges so far",
                counters["rooms"], counters["contains_edges"],
            )

    # Pass 2 (optional): Drawer nodes + Room → CONTAINS → Drawer edges.
    if not skip_drawers:
        sql_drawers = f"""
            SELECT id, wing, room FROM "{table_name}"
            WHERE wing IS NOT NULL AND wing <> ''
              AND room IS NOT NULL AND room <> ''
        """
        conn = psycopg2.connect(dsn)
        try:
            with conn:
                with conn.cursor(name="palace_drawers_cur") as cur:
                    cur.itersize = 1000
                    cur.execute(sql_drawers)
                    for drawer_id, _wing, room in cur:
                        d_id = sanitize_kg_value(drawer_id, "drawer_id")
                        room_clean = sanitize_kg_value(room, "room")
                        kg._run_cypher(
                            "MERGE (d:Drawer {id: $id})",
                            {"id": d_id},
                        )
                        kg._run_cypher(
                            """
                            MATCH (r:Room {name: $room}), (d:Drawer {id: $id})
                            MERGE (r)-[:CONTAINS]->(d)
                            """,
                            {"room": room_clean, "id": d_id},
                        )
                        counters["drawers"] += 1
                        counters["contains_edges"] += 1
                        if (counters["drawers"] % batch_log_every) == 0:
                            logger.info(
                                "palace_graph_age: %d drawers so far",
                                counters["drawers"],
                            )
        finally:
            conn.close()

    # Pass 3 (optional): SHARED_VIA tunnels — Wing ↔ Wing where they
    # share a Room. One bidirectional edge per (wing_a, wing_b) pair.
    if not skip_tunnels:
        seen_pairs: set[tuple[str, str]] = set()
        for room, wings in rooms_by_wings:
            if len(wings) < 2:
                continue
            for i, wa in enumerate(wings):
                for wb in wings[i + 1 :]:
                    pair = (min(wa, wb), max(wa, wb))
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    wa_clean = sanitize_kg_value(wa, "wing_a")
                    wb_clean = sanitize_kg_value(wb, "wing_b")
                    kg._run_cypher(
                        """
                        MATCH (a:Wing {name: $a}), (b:Wing {name: $b})
                        MERGE (a)-[:SHARED_VIA {via_room: $room}]->(b)
                        """,
                        {"a": wa_clean, "b": wb_clean, "room": sanitize_kg_value(room, "room")},
                    )
                    counters["shared_via_edges"] += 1

    logger.info("palace_graph_age: populate complete: %s", counters)
    return counters


# ── Read-side helpers ─────────────────────────────────────────────────


def walk_wing(kg: KnowledgeGraphAGE, wing_name: str, depth: int = 2, limit: int = 100) -> list:
    """Return a structured walk of a wing's contents.

    Default depth=2 expands Wing → Room → Drawer; depth=3 also pulls in
    MENTIONS → Entity. Result is a list of dicts:
    {wing, room, drawer, entity?} — one row per leaf reached at the
    requested depth.

    The "agent walks the palace" primitive — this is what an MCP tool
    or RLM-orchestrator would call to enumerate what's inside a wing.
    """
    wing_clean = sanitize_kg_value(wing_name, "wing")
    if depth >= 3:
        # AGE doesn't support edge-type union (`[:A|B]`) in MATCH patterns.
        # The kg_writethrough hook (Phase 2) writes triples as :RELATION edges
        # with relation_type='mentions' on the edge properties, so we filter
        # by property rather than label.
        cypher = """
            MATCH (w:Wing {name: $wing})-[:CONTAINS]->(r:Room)-[:CONTAINS]->(d:Drawer)
            OPTIONAL MATCH (d)-[rel:RELATION]->(e:Entity)
            WHERE rel.relation_type = 'mentions'
            RETURN w.name AS wing, r.name AS room, d.id AS drawer, e.name AS entity
            LIMIT $limit
        """
    elif depth == 2:
        cypher = """
            MATCH (w:Wing {name: $wing})-[:CONTAINS]->(r:Room)-[:CONTAINS]->(d:Drawer)
            RETURN w.name AS wing, r.name AS room, d.id AS drawer
            LIMIT $limit
        """
    else:
        cypher = """
            MATCH (w:Wing {name: $wing})-[:CONTAINS]->(r:Room)
            RETURN w.name AS wing, r.name AS room
            LIMIT $limit
        """

    rows = kg._run_cypher(cypher, {"wing": wing_clean, "limit": limit}, fetch=True)
    out = []
    for r in rows:
        row: dict[str, Optional[str]] = {
            "wing": kg._unwrap_agtype(r[0]),
            "room": kg._unwrap_agtype(r[1]),
        }
        if len(r) >= 3:
            row["drawer"] = kg._unwrap_agtype(r[2])
        if len(r) >= 4:
            row["entity"] = kg._unwrap_agtype(r[3])
        out.append(row)
    return out


def list_wings(kg: KnowledgeGraphAGE, limit: int = 100) -> list[str]:
    """Return all wing names in the palace."""
    rows = kg._run_cypher(
        "MATCH (w:Wing) RETURN w.name AS name LIMIT $limit",
        {"limit": limit},
        fetch=True,
    )
    return sorted(filter(None, (kg._unwrap_agtype(r[0]) for r in rows)))


def list_rooms_in_wing(kg: KnowledgeGraphAGE, wing_name: str, limit: int = 100) -> list[str]:
    """Return all rooms in the named wing."""
    wing_clean = sanitize_kg_value(wing_name, "wing")
    rows = kg._run_cypher(
        """
        MATCH (w:Wing {name: $wing})-[:CONTAINS]->(r:Room)
        RETURN r.name AS name LIMIT $limit
        """,
        {"wing": wing_clean, "limit": limit},
        fetch=True,
    )
    return sorted(filter(None, (kg._unwrap_agtype(r[0]) for r in rows)))


def list_drawers_in_room(kg: KnowledgeGraphAGE, room_name: str, limit: int = 100) -> list[str]:
    """Return all drawer ids in the named room (across any wing)."""
    room_clean = sanitize_kg_value(room_name, "room")
    rows = kg._run_cypher(
        """
        MATCH (r:Room {name: $room})-[:CONTAINS]->(d:Drawer)
        RETURN d.id AS id LIMIT $limit
        """,
        {"room": room_clean, "limit": limit},
        fetch=True,
    )
    return sorted(filter(None, (kg._unwrap_agtype(r[0]) for r in rows)))


def tunnels_from_wing(kg: KnowledgeGraphAGE, wing_name: str) -> list[dict]:
    """Return all other wings reachable from this one via SHARED_VIA."""
    wing_clean = sanitize_kg_value(wing_name, "wing")
    rows = kg._run_cypher(
        """
        MATCH (a:Wing {name: $wing})-[r:SHARED_VIA]-(b:Wing)
        RETURN b.name AS to_wing, r.via_room AS via_room
        """,
        {"wing": wing_clean},
        fetch=True,
    )
    return [
        {"to_wing": kg._unwrap_agtype(r[0]), "via_room": kg._unwrap_agtype(r[1])}
        for r in rows
    ]
