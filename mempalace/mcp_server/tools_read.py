# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== READ TOOLS ====================


def _tool_status_via_sqlite() -> dict:
    """Pure-sqlite status reader for the #1222 fallback path.

    When the HNSW capacity probe detects divergence, opening the chromadb
    persistent client can segfault. This reader pulls the same wing/room
    breakdown directly from ``embedding_metadata`` so the operator still
    gets a working status response — and crucially the
    ``vector_disabled`` flag — without us touching the vector segment.
    """
    import sqlite3 as _sqlite3

    from ..backends._inproc_sqlite import open_reader as open_palace_reader

    db_path = os.path.join(_config.palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return _no_palace()
    collection_name = _config.collection_name

    wings: dict = {}
    rooms: dict = {}
    total = 0
    try:
        conn = open_palace_reader(db_path)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ?
                """,
                (collection_name,),
            ).fetchone()
            total = int(row[0]) if row and row[0] is not None else 0
            for key, target in (("wing", wings), ("room", rooms)):
                for value, count in conn.execute(
                    """
                    SELECT em.string_value, COUNT(*)
                    FROM embedding_metadata em
                    JOIN embeddings e ON em.id = e.id
                    JOIN segments s ON e.segment_id = s.id
                    JOIN collections c ON s.collection = c.id
                    WHERE c.name = ?
                      AND em.key = ?
                      AND em.string_value IS NOT NULL
                    GROUP BY em.string_value
                    """,
                    (collection_name, key),
                ):
                    target[value] = count
        finally:
            conn.close()
    except _sqlite3.Error:
        logger.exception("tool_status sqlite fallback read failed")

    result = {
        "total_drawers": total,
        "wings": wings,
        "rooms": rooms,
        "protocol": PALACE_PROTOCOL,
        "aaak_dialect": AAAK_SPEC,
        "backend": "chroma",
        "vector_disabled": True,
        "vector_disabled_reason": _vector_disabled_reason,
    }
    if _vector_capacity_status:
        result["hnsw_capacity"] = {
            "sqlite_count": _vector_capacity_status.get("sqlite_count"),
            "hnsw_count": _vector_capacity_status.get("hnsw_count"),
            "divergence": _vector_capacity_status.get("divergence"),
        }
    return result


# Postgres status cache. Every SessionStart hook calls status, and the three
# count queries are whole-table scans: on familiar (1.18M drawers, 2026-10-20)
# they missed the 3 s timeout whenever the disk was busy. Each miss fell back
# to the metadata sweep below, a full-table read, exactly when the disk was
# saturated. So a result is reused for _PG_STATUS_TTL_S, builds run one at a
# time, and a failed build serves the last result (marked stale) for up to
# _PG_STATUS_STALE_MAX_S instead of returning None.
_pg_status_lock = threading.Lock()
_pg_status_cache = None  # (result dict, time.time() it was built)
_PG_STATUS_TTL_S = 60.0
_PG_STATUS_STALE_MAX_S = 1800.0


def _tool_status_via_postgres() -> Optional[dict]:
    global _pg_status_cache
    with _pg_status_lock:
        cached = _pg_status_cache
        if cached is not None and time.time() - cached[1] < _PG_STATUS_TTL_S:
            return dict(cached[0])
        result = _tool_status_via_postgres_uncached()
        if result is not None:
            _pg_status_cache = (result, time.time())
            return dict(result)
        if cached is not None and time.time() - cached[1] < _PG_STATUS_STALE_MAX_S:
            age = int(time.time() - cached[1])
            logger.warning("tool_status postgres fast-path failed; serving a %ds-old result", age)
            return {**cached[0], "stale": True, "stale_age_s": age}
        return None


def _tool_status_via_postgres_uncached() -> Optional[dict]:
    """Postgres fast-path for tool_status: SQL group-by, sub-100ms on 370k rows.

    Mirrors palace-daemon's ``/status/fast`` (main.py ``status_fast``):
    three queries against ``mempalace_drawers`` wrapped in a 3s
    ``statement_timeout`` so a stuck query can't replace one slow path
    with another. Returns ``None`` when the postgres backend isn't
    actually reachable (no DSN configured, driver missing, connection
    refused) so the caller can fall back to the legacy metadata sweep
    rather than surfacing a hard failure. Issue #267.
    """
    dsn = _config.postgres_dsn
    if not dsn:
        return None
    table = _config.collection_name or "mempalace_drawers"
    try:
        import psycopg
        from psycopg import sql as _sql
    except ImportError:
        return None

    table_id = _sql.Identifier(table)
    wings: dict = {}
    rooms: dict = {}
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '3s'")
                cur.execute(_sql.SQL("SELECT count(*) FROM {}").format(table_id))
                total = cur.fetchone()[0]
                cur.execute(
                    _sql.SQL("SELECT wing, count(*) FROM {} GROUP BY wing").format(table_id)
                )
                for w, c in cur.fetchall():
                    wings[w if w is not None else "unknown"] = c
                cur.execute(
                    _sql.SQL(
                        "SELECT room, count(*) FROM {} WHERE room IS NOT NULL GROUP BY room"
                    ).format(table_id)
                )
                for r, c in cur.fetchall():
                    rooms[r] = c
    except Exception:
        logger.exception("tool_status postgres fast-path failed")
        return None
    return {
        "total_drawers": int(total or 0),
        "wings": wings,
        "rooms": rooms,
        "protocol": PALACE_PROTOCOL,
        "aaak_dialect": AAAK_SPEC,
    }


def _sqlite_taxonomy():
    """Fast wing→room tally from the backend's sqlite metadata (#1748 / #1379).

    Returns ``(total, {wing: {room: count}})`` or ``None`` to signal the
    caller to fall back to the collection pagination path. ``None`` means
    an unsupported backend, a missing/unbootstrapped palace, or a sqlite error.
    Chroma reads ``chroma.sqlite3`` so status does not cold-load HNSW.
    sqlite_exact reads ``sqlite_exact.sqlite3`` with one ``json_extract``
    GROUP BY so status does not page every metadata row.
    """
    global _taxonomy_cache, _taxonomy_cache_time
    cache_key = (_config.palace_path, _config.collection_name)
    # Taken before the query, so a write that lands during it invalidates.
    fingerprint = _palace_db_fingerprint()
    # The Chroma database/WAL fingerprint changes on committed writes, so a
    # different fingerprint recounts even inside the TTL. The TTL is the fallback
    # for backends whose file stat misses commits (sqlite_exact's WAL) and
    # for a palace whose file cannot be stat'ed.
    fingerprint_matches = (
        _taxonomy_cache is not None
        and fingerprint is not None
        and _taxonomy_cache[2] == fingerprint
    )
    ttl_fresh = fingerprint is None and (time.time() - _taxonomy_cache_time) < _TAXONOMY_CACHE_TTL
    if (
        _taxonomy_cache is not None
        and _taxonomy_cache[0] == cache_key
        and (fingerprint_matches or ttl_fresh)
    ):
        return _taxonomy_cache[1]
    counts = None
    try:
        if _is_chroma_backend():
            from ..backends.chroma import _sqlite_wing_room_counts

            counts = _sqlite_wing_room_counts(_config.palace_path, _config.collection_name)
        elif _selected_backend_name() in {"sqlite_exact", "rust_exact"}:
            from ..backends.sqlite_exact import sqlite_wing_room_counts

            counts = sqlite_wing_room_counts(_config.palace_path, _config.collection_name)
        else:
            return None
    except Exception:
        logger.debug("sqlite taxonomy fast path failed; falling back", exc_info=True)
        return None
    if counts is None:
        return None

    # Preserve the client path's output contract: drawers missing wing/room
    # read as "unknown" (the ``m.get("wing", "unknown")`` default), not the
    # sqlite COALESCE placeholder "?". Without this, the fast path would be an
    # observable API change for MCP clients on legacy/partial drawers.
    def _norm(key):
        return "unknown" if key in (None, "?") else key

    total, wing_rooms = counts
    normalized: dict = {}
    for wing, room_counts in wing_rooms.items():
        dest = normalized.setdefault(_norm(wing), {})
        for room, n in room_counts.items():
            rkey = _norm(room)
            dest[rkey] = dest.get(rkey, 0) + n
    result = total, normalized
    _taxonomy_cache = (cache_key, result, fingerprint)
    # Stamp once the query is done: stamped at the start, a query slower than
    # the TTL (a full GROUP BY on a multi-million-drawer palace) stored an
    # entry that had already expired, so every status call re-ran it.
    _taxonomy_cache_time = time.time()
    return result


def _sqlite_graph_stats():
    """Compute ``graph_stats`` from one grouped sqlite read (#1379, graph_stats
    half; follow-up to #1748).

    ``graph_stats`` only needs grouped counts, but the client path builds the
    whole graph by paging every metadata row (``build_graph`` →
    ``col.get(limit, offset)``) and cold-loads the HNSW index — which times out
    on six-figure palaces. This reads the same wing/room/hall grouping straight
    from ``chroma.sqlite3`` and reconstructs the stats.

    Returns the stats dict, or ``None`` to fall back to the client path
    (non-chroma backend, missing/unbootstrapped palace, sqlite error). The
    reconstruction mirrors ``palace_graph.build_graph`` /
    ``palace_graph.graph_stats`` exactly: a node is a room with a non-empty
    wing and a usable room name (including the catch-all ``"general"``), and
    edges are the per-hall cross-wing crossings of multi-wing rooms.
    """
    rows = None
    try:
        if _is_chroma_backend():
            rows = _chroma_room_wing_hall_counts()
        elif _selected_backend_name() in {"sqlite_exact", "rust_exact"}:
            from ..backends.sqlite_exact import sqlite_room_wing_hall_counts

            rows = sqlite_room_wing_hall_counts(_config.palace_path, _config.collection_name)
        else:
            return None
    except Exception:
        logger.debug("sqlite graph_stats fast path failed; falling back", exc_info=True)
        return None
    if rows is None:
        return None
    try:
        return _graph_stats_from_grouped_rows(rows)
    except Exception:
        logger.debug("sqlite graph_stats reconstruction failed; falling back", exc_info=True)
        return None


def _graph_stats_from_grouped_rows(rows):
    """Rebuild ``graph_stats`` from grouped sqlite metadata rows.

    Rows are ``(room, wing, hall, n)`` with an optional fifth ``last_date``
    column. Because grouping includes ``hall``, one room placement can occupy
    multiple SQL rows; room instances therefore use a distinct ``(wing, room)`` set.
    """
    from collections import Counter, defaultdict

    room_data = defaultdict(lambda: {"wings": set(), "halls": set(), "count": 0})
    room_instances = set()
    for row in rows:
        room, wing, hall, n = row[0], row[1], row[2], row[3]
        if not room or not wing:
            continue
        room_key = str(room)
        wing_key = str(wing)
        room_instances.add((wing_key, room_key))
        node = room_data[room_key]
        node["wings"].add(wing_key)
        if hall:
            node["halls"].add(str(hall))
        node["count"] += int(n)

    passive_tunnel_rooms = 0
    total_edges = 0
    wing_counts = Counter()
    for data in room_data.values():
        n_wings = len(data["wings"])
        for wing in data["wings"]:
            wing_counts[wing] += 1
        if n_wings >= 2:
            passive_tunnel_rooms += 1
            total_edges += (n_wings * (n_wings - 1) // 2) * len(data["halls"])

    top_tunnels = [
        {"room": room, "wings": sorted(data["wings"]), "count": data["count"]}
        for room, data in sorted(
            room_data.items(), key=lambda item: (-len(item[1]["wings"]), item[0])
        )[:10]
        if len(data["wings"]) >= 2
    ]
    explicit_tunnel_count = len(_load_graph_tunnels(_config))
    return {
        "total_rooms": len(room_data),
        "total_room_instances": len(room_instances),
        "tunnel_rooms": passive_tunnel_rooms,
        "passive_tunnel_rooms": passive_tunnel_rooms,
        "explicit_tunnels": explicit_tunnel_count,
        "total_edges": total_edges,
        "total_connections": total_edges + explicit_tunnel_count,
        "rooms_per_wing": dict(wing_counts.most_common()),
        "top_tunnels": top_tunnels,
    }


def _graph_sqlite_reader():
    """The sqlite grouped-counts reader for this palace, or ``None``.

    ``None`` means the graph tools must go through the collection, which is
    what keeps a missing or broken palace reporting a diagnostic instead of
    an empty graph.
    """
    from ..palace_graph import sqlite_grouped_counts_reader

    return sqlite_grouped_counts_reader(_config)


def _chroma_room_wing_hall_counts():
    global _graph_rows_cache
    if not _config.palace_path:
        return None
    from ..backends.chroma import sqlite_room_wing_hall_counts

    # Same full GROUP BY as the status taxonomy; reuse it while chroma.sqlite3
    # has not been written (see _palace_db_fingerprint).
    cache_key = (_config.palace_path, _config.collection_name)
    fingerprint = _palace_db_fingerprint()
    if (
        fingerprint is not None
        and _graph_rows_cache is not None
        and _graph_rows_cache[0] == cache_key
        and _graph_rows_cache[2] == fingerprint
    ):
        return _graph_rows_cache[1]
    rows = sqlite_room_wing_hall_counts(_config.palace_path, _config.collection_name)
    if rows is not None and fingerprint is not None:
        _graph_rows_cache = (cache_key, rows, fingerprint)
    return rows


def tool_status():
    _ensure_sqlite_integrity_status()
    if _sqlite_integrity_errors:
        result = _tool_status_via_sqlite()
        if isinstance(result, dict):
            result["sqlite_integrity"] = _sqlite_integrity_payload()
            result["sqlite_integrity_failed"] = True
            result["error"] = "SQLite integrity check failed"
            result["partial"] = True
        return result

    # Run the safe sqlite/pickle probe before we touch chromadb. In the
    # #1222 failure mode, opening the persistent client to call .count()
    # can segfault — short-circuit to a pure-sqlite path when divergence
    # is detected so status stays reachable.
    db_exists = _backend_db_exists()
    _refresh_vector_disabled_flag()

    if _vector_disabled:
        return _tool_status_via_sqlite()

    # Postgres backend: pull wing/room counts from indexed SQL rather than
    # sweeping every drawer's metadata in Python (~29s → <100ms on a 370k-
    # row palace; #267).
    if _config.backend == "postgres":
        fast = _tool_status_via_postgres()
        if fast is not None:
            return fast
        # Fast path unavailable (no DSN, driver missing, query failed) —
        # fall through to the legacy metadata sweep so the caller still
        # gets a response.

    # Fast path: tally wing/room straight from sqlite so overview tools stay
    # responsive on large palaces instead of cold-loading the HNSW index or
    # paging hundreds of MB of metadata through the client (#1748 / #1379).
    # ``None`` (non-chroma backend / non-standard layout) falls through to the
    # client path below.
    fast = _sqlite_taxonomy()
    if fast is not None:
        total, wing_rooms = fast
        wings = {}
        rooms = {}
        for w, room_counts in wing_rooms.items():
            wings[w] = wings.get(w, 0) + sum(room_counts.values())
            for r, n in room_counts.items():
                rooms[r] = rooms.get(r, 0) + n
        return {
            "total_drawers": total,
            "wings": wings,
            "rooms": rooms,
            "protocol": PALACE_PROTOCOL,
            "aaak_dialect": AAAK_SPEC,
            "backend": _selected_backend_name(),
            "embedding_device": _describe_device_safe(),
        }

    # Use create=True only when a palace DB already exists on disk -- this
    # bootstraps the ChromaDB collection on a valid-but-empty palace without
    # accidentally creating a palace in a non-existent directory (#830).
    col = _get_collection(create=db_exists)
    if not col:
        return _collection_error_or_no_palace()
    count = col.count()
    wings = {}
    rooms = {}
    result = {
        "total_drawers": count,
        "wings": wings,
        "rooms": rooms,
        "protocol": PALACE_PROTOCOL,
        "aaak_dialect": AAAK_SPEC,
        "backend": _selected_backend_name(),
        "embedding_device": _describe_device_safe(),
    }
    try:
        if _supports_metadata_facets(col):
            try:
                temp_wings = col.facet_counts("wing")
                wings.update(temp_wings)
                try:
                    unknown_wings = count - sum(temp_wings.values())
                    if unknown_wings > 0:
                        wings["unknown"] = wings.get("unknown", 0) + unknown_wings
                except (TypeError, ValueError):
                    pass

                temp_rooms = col.facet_counts("room")
                rooms.update(temp_rooms)
                try:
                    unknown_rooms = count - sum(temp_rooms.values())
                    if unknown_rooms > 0:
                        rooms["unknown"] = rooms.get("unknown", 0) + unknown_rooms
                except (TypeError, ValueError):
                    pass

            except Exception as e:
                logger.warning(
                    "Failed to fetch metadata facets, falling back to client-side loop: %s", e
                )
                rooms.clear()
                wings.clear()
                all_meta = _get_cached_metadata(col)
                for m in all_meta:
                    m = m or {}
                    w = m.get("wing", "unknown")
                    r = m.get("room", "unknown")
                    wings[w] = wings.get(w, 0) + 1
                    rooms[r] = rooms.get(r, 0) + 1
        else:
            all_meta = _get_cached_metadata(col)
            for m in all_meta:
                m = m or {}
                w = m.get("wing", "unknown")
                r = m.get("room", "unknown")
                wings[w] = wings.get(w, 0) + 1
                rooms[r] = rooms.get(r, 0) + 1
    except Exception as e:
        logger.exception("tool_status metadata fetch failed")
        result["error"] = str(e)
        result["partial"] = True
    return result


# ── AAAK Dialect Spec ─────────────────────────────────────────────────────────
# Included in status response so the AI learns it on first wake-up call.
# Also available via mempalace_get_aaak_spec tool.

PALACE_PROTOCOL = """IMPORTANT — MemPalace Memory Protocol:
1. ON WAKE-UP: Call mempalace_status to load palace overview + AAAK spec.
2. BEFORE RESPONDING about any person, project, or past event: verify with the tool that fits the question — mempalace_kg_query for known relationships or time-bound facts, mempalace_search for source text, or mempalace_diary_read for recent agent continuity. Use small search/diary limits and relevant known scopes. Stop when answered; search stored text or widen filters if recall is insufficient. Never guess.
3. IF UNSURE about a fact (name, gender, age, relationship): say "let me check" and query the palace. Wrong is worse than slow.
4. AFTER EACH SESSION: call mempalace_diary_write to record what happened, what you learned, what matters.
5. WHEN A SINGLE-VALUED FACT CHANGES (model, employer, address): call mempalace_kg_supersede(subject, predicate, old, new) to replace it atomically at one boundary — do NOT hand-roll invalidate + add, which leaves the old and new values overlapping at the boundary. Use mempalace_kg_invalidate for a fact that simply ended, and mempalace_kg_add to add an independent (possibly concurrent) fact.

This protocol ensures the AI KNOWS before it speaks. Storage is not memory — but storage + this protocol = memory."""

AAAK_SPEC = """AAAK is a compressed memory dialect that MemPalace uses for efficient storage.
It is designed to be readable by both humans and LLMs without decoding.

FORMAT:
  ENTITIES: 3-letter uppercase codes. ALC=Alice, JOR=Jordan, RIL=Riley, MAX=Max, BEN=Ben.
  EMOTIONS: *action markers* before/during text. *warm*=joy, *fierce*=determined, *raw*=vulnerable, *bloom*=tenderness.
  STRUCTURE: Pipe-separated fields. FAM: family | PROJ: projects | ⚠: warnings/reminders.
  DATES: ISO format (2026-03-31). COUNTS: Nx = N mentions (e.g., 570x).
  IMPORTANCE: ★ to ★★★★★ (1-5 scale).
  HALLS: hall_facts, hall_events, hall_discoveries, hall_preferences, hall_advice.
  WINGS: wing_user, wing_agent, wing_team, wing_code, wing_myproject, wing_hardware, wing_ue5, wing_ai_research.
  ROOMS: Hyphenated slugs representing named ideas (e.g., chromadb-setup, gpu-pricing).

EXAMPLE:
  FAM: ALC→♡JOR | 2D(kids): RIL(18,sports) MAX(11,chess+swimming) | BEN(contributor)

Read AAAK naturally — expand codes mentally, treat *markers* as emotional context.
When WRITING AAAK: use entity codes, mark emotions, keep structure tight."""


def tool_list_wings():
    fast = _sqlite_taxonomy()
    if fast is not None:
        _total, wing_rooms = fast
        wings = {}
        for w, room_counts in wing_rooms.items():
            wings[w] = wings.get(w, 0) + sum(room_counts.values())
        return {"wings": wings}
    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()
    wings = {}
    result = {"wings": wings}
    try:
        try:
            if not _supports_metadata_facets(col):
                raise ValueError("facets not supported")
            temp_wings = col.facet_counts("wing")
            wings.update(temp_wings)
            try:
                unknown_wings = col.count() - sum(temp_wings.values())
                if unknown_wings > 0:
                    wings["unknown"] = wings.get("unknown", 0) + unknown_wings
            except (TypeError, ValueError):
                pass
        except Exception as e:
            if _supports_metadata_facets(col):
                logger.warning(
                    "Failed to fetch metadata facets, falling back to client-side loop: %s", e
                )
            wings.clear()
            all_meta = _get_cached_metadata(col)
            for m in all_meta:
                m = m or {}
                w = m.get("wing", "unknown")
                wings[w] = wings.get(w, 0) + 1
    except Exception as e:
        logger.exception("tool_list_wings metadata fetch failed")
        result["error"] = str(e)
        result["partial"] = True
    return result


def tool_list_rooms(wing: str = None):
    try:
        wing = _sanitize_optional_name(wing, "wing")
    except ValueError as e:
        return {"error": str(e)}
    fast = _sqlite_taxonomy()
    if fast is not None:
        _total, wing_rooms = fast
        rooms = {}
        for w, room_counts in wing_rooms.items():
            if wing and w != wing:
                continue
            for r, n in room_counts.items():
                rooms[r] = rooms.get(r, 0) + n
        return {"wing": wing or "all", "rooms": rooms}
    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()
    rooms = {}
    result = {"wing": wing or "all", "rooms": rooms}
    where = {"wing": wing} if wing else None
    try:
        try:
            if not _supports_metadata_facets(col):
                raise ValueError("facets not supported")
            temp_rooms = col.facet_counts("room", where=where)
            rooms.update(temp_rooms)
            try:
                if wing:
                    wing_count = col.facet_counts("wing", where={"wing": wing}).get(wing, 0)
                    unknown_rooms = wing_count - sum(temp_rooms.values())
                else:
                    unknown_rooms = col.count() - sum(temp_rooms.values())
                if unknown_rooms > 0:
                    rooms["unknown"] = rooms.get("unknown", 0) + unknown_rooms
            except (TypeError, ValueError):
                pass
        except Exception as e:
            if _supports_metadata_facets(col):
                logger.warning(
                    "Failed to fetch metadata facets, falling back to client-side loop: %s", e
                )
            rooms.clear()
            all_meta = _fetch_all_metadata(col, where=where)
            for m in all_meta:
                m = m or {}
                r = m.get("room", "unknown")
                rooms[r] = rooms.get(r, 0) + 1
    except Exception as e:
        logger.exception("tool_list_rooms metadata fetch failed")
        result["error"] = str(e)
        result["partial"] = True
    return result


def tool_get_taxonomy():
    fast = _sqlite_taxonomy()
    if fast is not None:
        _total, wing_rooms = fast
        return {"taxonomy": {w: dict(room_counts) for w, room_counts in wing_rooms.items()}}
    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()
    taxonomy = {}
    result = {"taxonomy": taxonomy}
    try:
        try:
            if not _supports_metadata_facets(col):
                raise ValueError("facets not supported")
            from concurrent.futures import ThreadPoolExecutor

            wing_counts = col.facet_counts("wing")
            wings = list(wing_counts.keys())
            temp_taxonomy = {}
            with ThreadPoolExecutor(max_workers=max(1, min(8, len(wings)))) as executor:
                futures = {
                    wing: executor.submit(col.facet_counts, "room", where={"wing": wing})
                    for wing in wings
                }
                for wing, future in futures.items():
                    room_counts = future.result()
                    try:
                        unknown_rooms = wing_counts[wing] - sum(room_counts.values())
                        if unknown_rooms > 0:
                            room_counts["unknown"] = room_counts.get("unknown", 0) + unknown_rooms
                    except (TypeError, ValueError):
                        pass
                    temp_taxonomy[wing] = room_counts
                taxonomy.update(temp_taxonomy)
        except Exception as e:
            if _supports_metadata_facets(col):
                logger.warning(
                    "Failed to fetch metadata facets, falling back to client-side loop: %s", e
                )
            all_meta = _get_cached_metadata(col)
            for m in all_meta:
                m = m or {}
                w = m.get("wing", "unknown")
                r = m.get("room", "unknown")
                if w not in taxonomy:
                    taxonomy[w] = {}
                taxonomy[w][r] = taxonomy[w].get(r, 0) + 1
    except Exception as e:
        logger.exception("tool_get_taxonomy metadata fetch failed")
        result["error"] = str(e)
        result["partial"] = True
    return result


def tool_search(
    query: str,
    limit: int = 15,
    wing: str = None,
    room: str = None,
    tags: list = None,
    source_file: str = None,
    since: str = None,
    before: str = None,
    max_distance: float = None,
    min_similarity: float = None,
    context: str = None,
    candidate_strategy: str = None,
    fusion_mode: str = "convex",
    include_trace: bool = False,
    cli_compatible: bool = False,
    include_media: bool = False,
    query_task: str = "search",
):
    limit = max(1, min(limit, _MAX_RESULTS))
    try:
        wing = _sanitize_optional_name(wing, "wing")
        room = _resolve_room_alias(room)
        source_file = _sanitize_optional_source_file(source_file)
    except ValueError as e:
        return {"error": str(e)}
    from ..tags import normalise_tags

    normalised_tags = normalise_tags(tags)
    # since/before are validated inside search_memories (shared
    # parse_window), which returns the same {"error": ...} shape.
    # Fork default is "hybrid" (BM25 + graph). The default is applied AFTER
    # the cli_compatible checks below — that branch must see whether the
    # caller explicitly picked a strategy (rejected) or left it unset (fine),
    # matching upstream's contract while keeping the fork default.
    if candidate_strategy is not None and (
        not isinstance(candidate_strategy, str)
        or candidate_strategy not in {"vector", "union", "hybrid"}
    ):
        return {"error": "candidate_strategy must be one of ('vector', 'union', 'hybrid')"}
    if (
        not isinstance(include_media, bool)
        or not isinstance(query_task, str)
        or query_task not in {"search", "code"}
    ):
        return {"error": "include_media must be boolean and query_task must be 'search' or 'code'"}
    if cli_compatible and (include_media or query_task != "search"):
        return {"error": "cli_compatible cannot be combined with media/code task search"}
    experimental = {
        name: value
        for name, value, default in (
            ("include_media", include_media, False),
            ("query_task", query_task, "search"),
        )
        if value != default
    }
    # Mitigate system prompt contamination (Issue #333)
    sanitized = sanitize_query(query)
    if cli_compatible:
        import contextlib
        import io

        if source_file is not None:
            return {"error": "cli-compatible search does not support source_file"}
        unsupported_controls = []
        if candidate_strategy not in (None, "vector"):
            unsupported_controls.append("candidate_strategy")
        if min_similarity is not None:
            unsupported_controls.append("min_similarity")
        if max_distance is not None:
            unsupported_controls.append("max_distance")
        if context is not None:
            unsupported_controls.append("context")
        if unsupported_controls:
            return {
                "error": "cli-compatible search does not support " + ", ".join(unsupported_controls)
            }
        if sanitized["clean_query"] != query or sanitized["was_sanitized"]:
            return {"error": "cli-compatible search requires an unchanged query"}
        error_output = io.StringIO()
        try:
            with _cli_search_capture_lock, contextlib.redirect_stderr(error_output):
                _refresh_vector_disabled_flag()
                collection = None if _vector_disabled else _get_collection()
                if collection is None and not _vector_disabled:
                    return _collection_error_or_no_palace()
                if collection is not None:
                    from ..backends.base import (
                        DimensionMismatchError,
                        EmbedderIdentityMismatchError,
                    )
                    from ..palace import _enforce_embedder_identity

                    try:
                        _enforce_embedder_identity(
                            collection,
                            _config.palace_path,
                            _config.collection_name,
                            create=False,
                            repeat_unknown_warning=True,
                        )
                    except (EmbedderIdentityMismatchError, DimensionMismatchError) as exc:
                        return _model_mismatch_error(exc)
                _, output = _capture_fd_stdout(
                    lambda: cli_search(
                        query=query,
                        palace_path=_config.palace_path,
                        wing=wing,
                        room=room,
                        n_results=limit,
                        since=since,
                        before=before,
                        collection=collection,
                    )
                )
        except SearchError as exc:
            return {"error": str(exc)}
        result = {"query": query, "cli_output": output}
        if error_output.getvalue():
            result["cli_error_output"] = error_output.getvalue()
        return result

    # Backwards compat: convert old similarity scale (higher=stricter) to
    # distance scale (lower=stricter). Similarity 0.8 → distance 0.2.
    dist = (1.0 - min_similarity) if min_similarity is not None else max_distance
    if dist is None:
        dist = 1.5

    # Ensure the vector-disabled probe has been run via the safe
    # sqlite/pickle path before we touch chromadb. Calling _get_client()
    # here would defeat the fallback — it constructs a PersistentClient
    # which can segfault on segment load in the #1222 failure mode.
    _refresh_vector_disabled_flag()
    candidate_strategy = candidate_strategy or "hybrid"
    result = search_memories(
        sanitized["clean_query"],
        palace_path=_config.palace_path,
        wing=wing,
        room=room,
        tags=normalised_tags or None,
        source_file=source_file,
        since=since,
        before=before,
        n_results=limit,
        max_distance=dist,
        vector_disabled=_vector_disabled,
        candidate_strategy=candidate_strategy,
        collection_name=_config.collection_name,
        fusion_mode=fusion_mode,
        **experimental,
    )
    # Attach per-source trace if requested (Phase 4 hybrid retrieval).
    # Surfaces matched_via counts so callers can debug which channel
    # contributed which drawer.
    if include_trace and isinstance(result, dict) and "results" in result:
        from collections import Counter as _Counter

        sources = _Counter(r.get("matched_via", "vector") for r in result["results"])
        result["trace"] = {
            "candidate_strategy": candidate_strategy,
            "fusion_mode": fusion_mode,
            "sources": dict(sources),
        }
    if _is_transient_index_error(result):
        # Post-bulk-write HNSW flush window (#1315): drop caches, give
        # the segment a moment to settle, retry once. Caller never sees
        # the transient unless the second attempt also fails.
        _force_chroma_cache_reset()
        time.sleep(2)
        _refresh_vector_disabled_flag()
        result = search_memories(
            sanitized["clean_query"],
            palace_path=_config.palace_path,
            wing=wing,
            room=room,
            tags=normalised_tags or None,
            source_file=source_file,
            since=since,
            before=before,
            n_results=limit,
            max_distance=dist,
            vector_disabled=_vector_disabled,
            candidate_strategy=candidate_strategy,
            collection_name=_config.collection_name,
            **experimental,
        )
        if not _is_transient_index_error(result):
            result["index_recovered"] = True
    if _vector_disabled:
        result["vector_disabled"] = True
        result["vector_disabled_reason"] = _vector_disabled_reason
    # Attach sanitizer metadata for transparency
    if sanitized["was_sanitized"]:
        result["query_sanitized"] = True
        result["sanitizer"] = {
            "method": sanitized["method"],
            "original_length": sanitized["original_length"],
            "clean_length": sanitized["clean_length"],
            "clean_query": sanitized["clean_query"],
        }
    if context:
        result["context_received"] = True
    return result


def tool_check_duplicate(content: str, threshold: float = 0.9):
    _refresh_vector_disabled_flag()
    if _vector_disabled:
        # Without a usable HNSW we can't compute cosine similarity for
        # near-duplicate detection. Report the limitation rather than
        # silently returning "not a duplicate" — false negatives here
        # would let the AI re-file content the palace already holds.
        return {
            "is_duplicate": False,
            "matches": [],
            "vector_disabled": True,
            "vector_disabled_reason": _vector_disabled_reason,
            "hint": (
                "duplicate detection requires vector search; run `mempalace repair` to restore"
            ),
        }
    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()
    try:
        content = strip_lone_surrogates(content)
        results = col.query(
            query_texts=[content],
            n_results=5,
            include=["metadatas", "documents", "distances"],
        )
        duplicates = []
        if results["ids"] and results["ids"][0]:
            metric = _metric_for_collection(col)
            for i, drawer_id in enumerate(results["ids"][0]):
                dist = results["distances"][0][i]
                similarity = round(_distance_to_similarity(dist, metric), 3)
                if similarity >= threshold:
                    # Chroma 1.5.x can return None for partially-flushed rows;
                    # coerce to empty sentinels so downstream .get() is safe.
                    meta = _safe_meta(results["metadatas"][0][i])
                    doc = results["documents"][0][i] or ""
                    duplicates.append(
                        {
                            "id": drawer_id,
                            "wing": meta.get("wing", "?"),
                            "room": meta.get("room", "?"),
                            "similarity": similarity,
                            "content": doc[:200] + "..." if len(doc) > 200 else doc,
                        }
                    )
        return {
            "is_duplicate": len(duplicates) > 0,
            "matches": duplicates,
        }
    except Exception as e:
        refused = _model_mismatch_error(e)
        if refused is not None:
            return refused
        logger.exception("check_duplicate failed")
        return {"error": "Duplicate check failed"}


def tool_get_aaak_spec():
    """Return the AAAK dialect specification."""
    return {"aaak_spec": AAAK_SPEC}


def tool_traverse_graph(start_room: str, max_hops: int = 2):
    """Walk the palace graph from a room. Find connected ideas across wings."""
    max_hops = max(1, min(max_hops, 10))
    # sqlite metadata path does not open HNSW. When it cannot serve, open the
    # collection here so a missing/broken palace still reports why (#1379
    # follow-up) instead of looking like a palace with no such room.
    if _graph_sqlite_reader() is None:
        col = _get_collection()
        if not col:
            return _collection_error_or_no_palace()
        return traverse(start_room, col=col, max_hops=max_hops)
    return traverse(start_room, max_hops=max_hops, config=_config)


def tool_walk_palace(
    start_wing: str = None,
    start_room: str = None,
    start_entity: str = None,
    depth: int = 2,
    limit: int = 50,
):
    """Agent-facing palace walk via AGE Cypher traversal.

    Phase 6 of the AGE-integration goal. Exposes the "agent walks into the
    palace" metaphor as a single tool: pass a starting node (wing OR room
    OR entity) and a depth, get back the navigable subgraph it touches.

    Three traversal modes by starting node:

    - **start_wing="memorypalace"**: enumerate rooms in this wing
      (depth=1), plus drawers in those rooms (depth=2), plus mentioned
      entities (depth=3). The "walking into a wing" pattern.
    - **start_room="problems"**: enumerate drawers in this room across
      all wings (depth=1), plus their mentioned entities (depth=2). The
      "walking into a specific room" pattern.
    - **start_entity="pgvector"**: enumerate drawers mentioning this
      entity (depth=1), plus the rooms+wings containing them (depth=2).
      The "find where in the palace X is discussed" pattern (inverse
      walk — entity → drawer → room → wing).

    Exactly one of (start_wing, start_room, start_entity) must be given.

    Returns a structured walk result with:
      - ``start``: the input anchor
      - ``walk``: list of {wing, room, drawer, entity} rows, one per
        leaf reached
      - ``stats``: {wings_touched, rooms_touched, drawers_touched,
        entities_touched}

    Requires MEMPALACE_BACKEND=postgres and the AGE graph populated via
    kg_writethrough or backfill_age.
    """
    # Validate inputs
    anchors_set = sum(bool(x) for x in (start_wing, start_room, start_entity))
    if anchors_set != 1:
        return {
            "error": "exactly one of start_wing, start_room, start_entity must be provided",
            "got": {
                "start_wing": start_wing,
                "start_room": start_room,
                "start_entity": start_entity,
            },
        }
    depth = max(1, min(depth, 5))
    limit = max(1, min(limit, 500))

    # Require postgres + AGE
    if _config.backend != "postgres":
        return {"error": "tool_walk_palace requires MEMPALACE_BACKEND=postgres"}
    if _config.kg_backend != "age":
        return {"error": "tool_walk_palace requires MEMPALACE_KG_BACKEND=age"}
    dsn = _config.postgres_dsn
    if not dsn:
        return {"error": "MEMPALACE_POSTGRES_DSN not set"}

    try:
        # _get_kg() returns the cached KnowledgeGraphAGE instance when
        # kg_backend == "age". Direct ``KnowledgeGraphAGE(dsn)`` would
        # open a fresh postgres connection on every call — across
        # repeated tool_walk_palace invocations that's both expensive
        # and unnecessary. (Gemini PR #101 review.)
        kg = _get_kg()
    except Exception as e:
        return {"error": f"could not connect to AGE: {e}"}

    walk_rows: list[dict] = []
    # Map result-column-name → stats-key-name so 'entity' goes to 'entities_touched'
    # (not 'entitys_touched' if we naively pluralized).
    col_to_stat = {
        "wing": "wings_touched",
        "room": "rooms_touched",
        "drawer": "drawers_touched",
        "entity": "entities_touched",
    }
    stats = {v: set() for v in col_to_stat.values()}

    try:
        if start_wing:
            # Wing → Room → Drawer → MENTIONS → Entity
            cypher_by_depth = {
                1: """
                    MATCH (w:Wing {name: $anchor})-[:CONTAINS]->(r:Room)
                    RETURN w.name AS wing, r.name AS room LIMIT $limit
                """,
                2: """
                    MATCH (w:Wing {name: $anchor})-[:CONTAINS]->(r:Room)-[:CONTAINS]->(d:Drawer)
                    RETURN w.name AS wing, r.name AS room, d.id AS drawer LIMIT $limit
                """,
                3: """
                    MATCH (w:Wing {name: $anchor})-[:CONTAINS]->(r:Room)-[:CONTAINS]->(d:Drawer)
                    OPTIONAL MATCH (d)-[m:MENTIONS]->(e:Entity)
                    RETURN w.name AS wing, r.name AS room, d.id AS drawer, e.name AS entity LIMIT $limit
                """,
            }
            anchor = start_wing
        elif start_room:
            # Room (across wings) → Drawer → MENTIONS → Entity
            cypher_by_depth = {
                1: """
                    MATCH (w:Wing)-[:CONTAINS]->(r:Room {name: $anchor})-[:CONTAINS]->(d:Drawer)
                    RETURN w.name AS wing, r.name AS room, d.id AS drawer LIMIT $limit
                """,
                2: """
                    MATCH (w:Wing)-[:CONTAINS]->(r:Room {name: $anchor})-[:CONTAINS]->(d:Drawer)
                    OPTIONAL MATCH (d)-[m:MENTIONS]->(e:Entity)
                    RETURN w.name AS wing, r.name AS room, d.id AS drawer, e.name AS entity LIMIT $limit
                """,
            }
            anchor = start_room
        else:  # start_entity
            # Inverse walk: Entity → Drawer → Room → Wing
            cypher_by_depth = {
                1: """
                    MATCH (e:Entity {name: $anchor})<-[m:MENTIONS]-(d:Drawer)
                    RETURN d.id AS drawer, e.name AS entity LIMIT $limit
                """,
                2: """
                    MATCH (e:Entity {name: $anchor})<-[m:MENTIONS]-(d:Drawer)
                    OPTIONAL MATCH (w:Wing)-[:CONTAINS]->(r:Room)-[:CONTAINS]->(d)
                    RETURN w.name AS wing, r.name AS room, d.id AS drawer, e.name AS entity LIMIT $limit
                """,
            }
            anchor = start_entity

        effective_depth = min(depth, max(cypher_by_depth.keys()))
        cypher = cypher_by_depth[effective_depth]

        rows = kg._run_cypher(cypher, {"anchor": anchor, "limit": limit}, fetch=True)
        for r in rows:
            entry: dict = {}
            # Map columns based on what the chosen cypher returned.
            # Use aliases from the RETURN clause: wing, room, drawer, entity.
            # The order matches: wing first, then room, then drawer, then entity
            # (skipping any not present in the result).
            for i, key in enumerate(("wing", "room", "drawer", "entity")):
                if i >= len(r):
                    break
                v = kg._unwrap_agtype(r[i])
                if v is not None:
                    entry[key] = v
                    stat_key = col_to_stat.get(key)
                    if stat_key:
                        stats[stat_key].add(v)
            walk_rows.append(entry)
    finally:
        kg.close()

    return {
        "start": {"wing": start_wing, "room": start_room, "entity": start_entity},
        "depth": depth,
        "walk": walk_rows,
        "stats": {k: len(v) for k, v in stats.items()},
    }


def tool_find_tunnels(wing_a: str = None, wing_b: str = None):
    """Find rooms that bridge two wings — the hallways connecting domains."""
    try:
        wing_a = _sanitize_optional_name(wing_a, "wing_a")
        wing_b = _sanitize_optional_name(wing_b, "wing_b")
    except ValueError as e:
        return {"error": str(e)}
    if _graph_sqlite_reader() is None:
        col = _get_collection()
        if not col:
            return _collection_error_or_no_palace()
        return find_tunnels(wing_a, wing_b, col=col)
    return find_tunnels(wing_a, wing_b, config=_config)


def tool_graph_stats():
    """Palace graph overview: nodes, tunnels, edges, connectivity."""
    # Fast path: grouped sqlite read instead of paging all metadata and
    # cold-loading HNSW via build_graph(), which times out on large palaces
    # (#1379). Falls through to the client path for non-chroma backends.
    fast = _sqlite_graph_stats()
    if fast is not None:
        return fast
    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()
    return graph_stats(col=col)


def tool_mesh_peers():
    """Mesh estate snapshot — the committed compat surface for PalaceMind's
    mesh view: exactly the GET /sync/peers payload, produced by the same
    function so the tool and the endpoint can never drift. Read-only;
    peers.json tokens are never included."""
    return _mesh_peers_payload()


def tool_create_tunnel(
    source_wing: str,
    source_room: str,
    target_wing: str,
    target_room: str,
    label: str = "",
    source_drawer_id: str = None,
    target_drawer_id: str = None,
):
    """Create an explicit cross-wing tunnel between two palace locations.

    Use when you notice content in one project relates to another project.
    Example: an API design discussion in project_api connects to the
    database schema in project_database.
    """
    # sanitize_name and create_tunnel both raise ValueError for invalid or
    # missing endpoints (empty/non-string names, and create_tunnel's
    # room-existence checks). Catch both so the real reason is surfaced
    # instead of escaping and being wrapped as the opaque "Internal tool
    # error" (#1473), mirroring sibling tools.
    try:
        source_wing = sanitize_name(source_wing, "source_wing")
        source_room = sanitize_name(source_room, "source_room")
        target_wing = sanitize_name(target_wing, "target_wing")
        target_room = sanitize_name(target_room, "target_room")
        return create_tunnel(
            source_wing,
            source_room,
            target_wing,
            target_room,
            label=label,
            source_drawer_id=source_drawer_id,
            target_drawer_id=target_drawer_id,
        )
    except ValueError as e:
        return {"error": str(e)}


def tool_list_tunnels(wing: str = None, include_passive: bool = False):
    """List cross-wing tunnels, optionally filtered by wing.

    Default returns only explicit (agent-created) tunnels stored at
    ``~/.mempalace/tunnels.json``. Pass ``include_passive=True`` to also
    include passive tunnels (rooms appearing in 2+ wings, computed from
    ``graph_stats``). Each result is tagged with ``kind: 'explicit'|'passive'``.
    See techempower-org/mempalace#75 for the asymmetry that motivated the
    merged-result form.
    """
    try:
        wing = _sanitize_optional_name(wing, "wing")
    except ValueError as e:
        return {"error": str(e)}
    col = _get_collection() if include_passive else None
    return list_tunnels(wing, include_passive=include_passive, col=col)


def tool_delete_tunnel(tunnel_id: str):
    """Delete an explicit tunnel by its ID."""
    if not tunnel_id or not isinstance(tunnel_id, str):
        return {"error": "tunnel_id is required"}
    return delete_tunnel(tunnel_id)


def tool_list_hallways(wing: str = None, limit: int = 100, offset: int = 0):
    """List within-wing hallway records, strongest first, one page at a time.

    A palace of any size holds hundreds of thousands of hallways; returning
    them all in one response closed the MCP connection. The page is sorted by
    co-occurrence count so the first page is the one worth reading.
    """
    try:
        wing = _sanitize_optional_name(wing, "wing")
    except ValueError as e:
        return {"error": str(e)}
    try:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        return {"error": "limit and offset must be integers"}
    rows = sorted(list_hallways(wing), key=lambda h: -int(h.get("co_occurrence_count") or 0))
    page = rows[offset : offset + limit]
    return {
        "hallways": page,
        "total": len(rows),
        "count": len(page),
        "offset": offset,
        "limit": limit,
    }


def tool_delete_hallway(hallway_id: str):
    """Delete a hallway record by its ID."""
    if not hallway_id or not isinstance(hallway_id, str):
        return {"error": "hallway_id is required"}
    return {"deleted": delete_hallway(hallway_id)}


def tool_follow_tunnels(wing: str, room: str, record: bool = True):
    """Follow explicit tunnels from a room to see connected drawers in other wings.

    An agent calling this tool is crossing the tunnel, so the traversal is
    recorded (``access_count`` / ``strength``). Internal callers that only
    decorate another result, such as the light server's search enrichment,
    pass ``record=False``: a search hit is not a crossing. A read-only
    server or a peer without the writer lock never writes the tunnel file.
    """
    try:
        wing = sanitize_name(wing, "wing")
        room = sanitize_name(_config.resolve_room(room), "room")
    except ValueError as e:
        return {"error": str(e)}
    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()
    return follow_tunnels(
        wing, room, col=col, record=record and not (_READ_ONLY or _MCP_WRITER_READ_ONLY)
    )
