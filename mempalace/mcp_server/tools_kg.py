# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== KNOWLEDGE GRAPH ====================


def _temporal_bound_key(value, *, end: bool = False) -> Optional[str]:
    if not value:
        return None
    text = str(value)
    if "T" in text:
        return text
    return f"{text}T23:59:59Z" if end else f"{text}T00:00:00Z"


def _fact_interval_bucket(row: dict, now_key: str) -> str:
    start_key = _temporal_bound_key(row.get("valid_from"), end=False)
    end_key = _temporal_bound_key(row.get("valid_to"), end=True)
    if start_key and start_key > now_key:
        return "future"
    if end_key and end_key < now_key:
        return "historical"
    return "active"


def tool_kg_query(entity: str, as_of: str = None, direction: str = "both"):
    """Query the knowledge graph for an entity's relationships."""
    try:
        entity = sanitize_kg_value(entity, "entity")
        as_of = sanitize_iso_temporal(as_of, "as_of")
    except ValueError as e:
        return {"error": str(e)}

    if direction not in ("outgoing", "incoming", "both"):
        return {"error": "direction must be 'outgoing', 'incoming', or 'both'"}

    results = _call_kg(lambda kg: kg.query_entity(entity, as_of=as_of, direction=direction))
    if as_of is None:
        now_key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        active = []
        historical = []
        future = []
        for row in results:
            bucket = _fact_interval_bucket(row, now_key)
            if bucket == "future":
                future.append(row)
            elif bucket == "historical":
                historical.append(row)
            else:
                active.append(row)
    else:
        active = results
        historical = []
        future = []
    payload = {
        "entity": entity,
        "as_of": as_of,
        "active_facts": active,
        "historical_facts": historical,
        "future_facts": future,
        "facts": results,
        "count": len(results),
    }
    if results:
        resolved_names = {
            r.get("subject") if r.get("direction") == "outgoing" else r.get("object")
            for r in results
        }
        resolved_names.discard(None)
        if len(resolved_names) == 1:
            resolved = next(iter(resolved_names))
            if resolved != entity:
                payload["resolved_from"] = entity
                payload["entity"] = resolved
    else:
        candidates = _call_kg(lambda kg: kg.find_entity_candidates(entity))
        if candidates:
            payload["candidates"] = candidates
    return payload


def tool_kg_add(
    subject: str,
    predicate: str,
    object: str,
    valid_from: str = None,
    valid_to: str = None,
    source_closet: str = None,
    source_file: str = None,
    source_drawer_id: str = None,
    context: str = None,
):
    """Add a relationship to the knowledge graph.

    All temporal and provenance fields are optional. ``valid_to`` lets callers
    backfill historical facts with a known end date/time in a single call
    instead of a separate ``kg_invalidate`` call.

    Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
    the form ``YYYY-MM-DDTHH:MM:SSZ``.

    ``context`` is the SPOC fourth-axis (techempower-org/mempalace#161):
    a free-form anchor naming where the fact was witnessed (e.g.
    ``drawer:abc123``, ``conversation:2026-05-28``). The AGE backend
    stores it on the RELATION edge and surfaces it through every read
    path; the SQLite backend silently accepts and ignores it (storage
    schema doesn't yet have a column for it) so callers don't need to
    branch on backend.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        object = sanitize_kg_value(object, "object")
        valid_from = sanitize_iso_temporal(valid_from, "valid_from")
        valid_to = sanitize_iso_temporal(valid_to, "valid_to")
        if context is not None:
            context = sanitize_kg_value(context, "context")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    _wal_log(
        "kg_add",
        {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_closet": source_closet,
            "source_file": source_file,
            "source_drawer_id": source_drawer_id,
            "context": context,
        },
    )

    def _add(kg):
        # SQLite KG accepts source_* kwargs; AGE KG accepts ``context``
        # (and a ``source`` string). Build kwargs per backend so each
        # gets only what its signature understands. Backend detection is
        # cheap (a class check) and avoids the TypeError-on-kwarg drift
        # that ``MEMPALACE_KG_BACKEND=age`` would otherwise hit.
        from ..knowledge_graph_age import KnowledgeGraphAGE

        if isinstance(kg, KnowledgeGraphAGE):
            return kg.add_triple(
                subject,
                predicate,
                object,
                valid_from=valid_from,
                valid_to=valid_to,
                source=source_drawer_id or source_file or source_closet,
                context=context,
            )
        return kg.add_triple(
            subject,
            predicate,
            object,
            valid_from=valid_from,
            valid_to=valid_to,
            source_closet=source_closet,
            source_file=source_file,
            source_drawer_id=source_drawer_id,
        )

    triple_id = _call_kg(_add)
    return {"success": True, "triple_id": triple_id, "fact": f"{subject} → {predicate} → {object}"}


def tool_kg_invalidate(subject: str, predicate: str, object: str, ended: str = None):
    """Mark a fact as no longer true.

    Returns the actual ``ended`` date/time that was stored. When the caller
    omits ``ended``, the underlying graph stamps ``date.today()`` and the
    response reflects that resolved value.

    Temporal values accept either ``YYYY-MM-DD`` or canonical UTC datetimes in
    the form ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        object = sanitize_kg_value(object, "object")
        ended = sanitize_iso_temporal(ended, "ended")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    resolved_ended = ended or date.today().isoformat()

    _wal_log(
        "kg_invalidate",
        {
            "subject": subject,
            "predicate": predicate,
            "object": object,
            "ended": resolved_ended,
        },
    )

    _call_kg(lambda kg: kg.invalidate(subject, predicate, object, ended=resolved_ended))
    return {
        "success": True,
        "fact": f"{subject} → {predicate} → {object}",
        "ended": resolved_ended,
    }


def tool_kg_supersede(
    subject: str,
    predicate: str,
    old_object: str,
    new_object: str,
    at: str = None,
):
    """Atomically replace one fact with another at a single shared boundary.

    Closes ``(subject, predicate, old_object)`` and opens
    ``(subject, predicate, new_object)`` at one shared instant, so a
    point-in-time query at the boundary returns only the new value. Use this
    instead of a separate ``kg_invalidate`` + ``kg_add`` when a single-valued
    fact changes (e.g. a model, employer, or address changes).

    ``at`` accepts ``YYYY-MM-DD`` or a canonical UTC datetime
    (``YYYY-MM-DDTHH:MM:SSZ``) and defaults to the current UTC instant.
    """
    try:
        subject = sanitize_kg_value(subject, "subject")
        predicate = sanitize_name(predicate, "predicate")
        old_object = sanitize_kg_value(old_object, "old_object")
        new_object = sanitize_kg_value(new_object, "new_object")
        at = sanitize_iso_temporal(at, "at")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    _wal_log(
        "kg_supersede",
        {
            "subject": subject,
            "predicate": predicate,
            "old_object": old_object,
            "new_object": new_object,
            "at": at,
        },
    )

    # Domain ValueErrors from kg.supersede (e.g. inverted boundary) are left to
    # bubble to the dispatcher, matching tool_kg_add / tool_kg_invalidate: the
    # -32000 response carries error_class + message in error.data. Only input
    # sanitization above returns the {success: False} envelope.
    triple_id = _call_kg(lambda kg: kg.supersede(subject, predicate, old_object, new_object, at=at))
    return {
        "success": True,
        "triple_id": triple_id,
        "fact": f"{subject} → {predicate} → {new_object}",
        "superseded": old_object,
    }


def tool_kg_timeline(entity: str = None, as_of: str = None, limit: int = 100, offset: int = 0):
    """Get chronological timeline of facts, optionally for one entity.

    ``as_of`` (techempower-org/mempalace#161) filters to facts whose
    temporal interval contains the given date/datetime; NULL ends are
    treated as open intervals (same semantics as ``mempalace_kg_query``).
    Omit ``as_of`` to see the full timeline including expired facts.

    Paginated with ``limit``/``offset`` following the ``tool_list_drawers``
    convention; defaults match the historical behavior (first 100 facts).
    """
    limit = max(1, min(limit, _MAX_RESULTS))
    offset = max(0, offset)
    if entity is not None:
        try:
            entity = sanitize_kg_value(entity, "entity")
        except ValueError as e:
            return {"error": str(e)}
    if as_of is not None:
        try:
            as_of = sanitize_iso_temporal(as_of, "as_of")
        except ValueError as e:
            return {"error": str(e)}

    def _query(kg):
        # The AGE backend takes ``as_of`` (#161); the SQLite backend's timeline() and timeline_total() do not, so
        # pass it only to AGE. Both paginate.
        from ..knowledge_graph_age import KnowledgeGraphAGE

        if isinstance(kg, KnowledgeGraphAGE):
            return {
                "timeline": kg.timeline(entity, limit=limit, offset=offset, as_of=as_of),
                "total": kg.timeline_total(entity, as_of=as_of),
            }
        return {
            "timeline": kg.timeline(entity, limit=limit, offset=offset),
            "total": kg.timeline_total(entity),
        }

    result = _call_kg(_query)
    return {
        "entity": entity or "all",
        "as_of": as_of,
        "timeline": result["timeline"],
        "count": len(result["timeline"]),
        "total": result["total"],
        "offset": offset,
        "limit": limit,
    }


def tool_kg_stats():
    """Knowledge graph overview: entities, triples, relationship types.

    Returns a structured error envelope on transient postgres failures
    (the connection dropped between `_call_kg` opening the handle and
    `kg.stats()` finishing its query — typically caused by a postgres
    OOM-kill or restart under load). The caller sees
    ``{"error": "backend_unavailable", "retryable": True, ...}`` and can
    surface "try again in a moment" instead of an opaque -32000
    internal error. See techempower-org/mempalace#299.

    Non-transient errors (cypher syntax, value-validation, schema
    mismatch) still propagate — those need a real fix, not a retry.
    """
    try:
        return _call_kg(lambda kg: kg.stats())
    except Exception as e:  # noqa: BLE001
        if _is_transient_postgres_error(e):
            logger.warning("mempalace_kg_stats: backend transiently unavailable: %s", e)
            return {
                "error": "backend_unavailable",
                "detail": str(e),
                "retryable": True,
            }
        raise


def _is_transient_postgres_error(exc: BaseException) -> bool:
    """True when ``exc`` is a psycopg connection-dropped error.

    Matches the same two error families ``kg_triple_worker._execute_with_retry``
    catches (techempower-org/mempalace#298): ``OperationalError`` (server
    closed the connection, OOM-restart, network blip) and
    ``InterfaceError`` (the connection is closed, pool returned a dead
    handle). Other psycopg errors — ``DataError``, ``ProgrammingError``
    on the postgres side, syntax errors — are bugs, not transient
    backend state, and should propagate.

    Returns False if psycopg isn't importable (sqlite-only deployments
    can't have postgres-transient errors).
    """
    try:
        import psycopg
    except ImportError:
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))
