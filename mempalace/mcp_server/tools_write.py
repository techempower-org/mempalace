# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


# ==================== WRITE TOOLS ====================


def _chroma_field(result, name, default=None):
    if result is None:
        return default
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _chunk_index(meta):
    try:
        return int((meta or {}).get("chunk_index", 0))
    except (TypeError, ValueError):
        return 0


def _response_safe_meta(meta):
    safe_meta = _safe_meta(meta)
    if safe_meta.get("source_file"):
        safe_meta["source_file"] = Path(safe_meta["source_file"]).name
    return safe_meta


def _content_preview(content):
    return content[:200] + "..." if len(content) > 200 else content


def _single_drawer_record(col, drawer_id: str):
    result = col.get(ids=[drawer_id], include=["documents", "metadatas"])
    ids = _chroma_field(result, "ids", []) or []
    if not ids:
        return None

    docs = _chroma_field(result, "documents", []) or []
    metas = _chroma_field(result, "metadatas", []) or []
    doc = docs[0] if docs else ""
    meta = _safe_meta(metas[0] if metas else {})

    return {
        "drawer_id": ids[0],
        "ids": [ids[0]],
        "documents": [doc or ""],
        "metadatas": [meta],
        "content": doc or "",
        "metadata": meta,
        "chunked": False,
    }


# Two write paths stamp the logical-group id under different keys:
# ``tool_add_drawer`` chunks carry ``parent_drawer_id`` (#1539, resolved as a
# logical drawer by #1782) while ``tool_diary_write`` chunks carry
# ``parent_entry_id`` (#1539). Both mean the same thing -- "physical chunk of
# this logical drawer" -- so every read path must resolve either one, or the
# id a write path hands back is unusable with get/update/delete (#2185).
# New diary writes stamp both keys; the read paths below still accept the
# ``parent_entry_id``-only shape so palaces written before this fix keep
# working with no data migration.
_PARENT_ID_KEYS = ("parent_drawer_id", "parent_entry_id")


def _logical_parent_id(meta):
    """Return the logical-group id from chunk metadata, whichever key holds it.

    Returns ``None`` for rows that are not chunks of a larger drawer.
    """
    for key in _PARENT_ID_KEYS:
        value = (meta or {}).get(key)
        if value:
            return value
    return None


def _logical_parent_where(drawer_id: str) -> dict:
    """Chroma ``where`` matching every chunk of ``drawer_id`` under either key.

    A chunk carrying both keys (diary writes after #2185) matches both
    branches of the ``$or`` but is still returned once -- Chroma dedupes by
    physical id -- so the joined content never repeats a chunk.
    """
    return {"$or": [{key: drawer_id} for key in _PARENT_ID_KEYS]}


def _logical_chunk_group(col, drawer_id: str):
    try:
        result = col.get(
            where=_logical_parent_where(drawer_id),
            include=["documents", "metadatas"],
        )
    except Exception:
        logger.debug("chunk group lookup failed for %s", drawer_id, exc_info=True)
        return None

    ids = _chroma_field(result, "ids", []) or []
    if not ids:
        return None

    docs = _chroma_field(result, "documents", []) or []
    metas = _chroma_field(result, "metadatas", []) or []

    rows = []
    for idx, chunk_id in enumerate(ids):
        doc = docs[idx] if idx < len(docs) else ""
        meta = _safe_meta(metas[idx] if idx < len(metas) else {})
        rows.append((_chunk_index(meta), chunk_id, doc or "", meta))

    rows.sort(key=lambda row: (row[0], row[1]))

    chunk_ids = [row[1] for row in rows]
    chunk_docs = [row[2] for row in rows]
    chunk_metas = [row[3] for row in rows]
    first_meta = chunk_metas[0] if chunk_metas else {}

    return {
        "drawer_id": drawer_id,
        "ids": chunk_ids,
        "documents": chunk_docs,
        "metadatas": chunk_metas,
        "content": "".join(chunk_docs),
        "metadata": first_meta,
        "chunked": True,
    }


def _logical_drawer_record(col, drawer_id: str):
    direct = _single_drawer_record(col, drawer_id)
    if direct is not None:
        return direct
    return _logical_chunk_group(col, drawer_id)


def _drawer_payload(record):
    safe_meta = _response_safe_meta(record["metadata"])

    payload = {
        "drawer_id": record["drawer_id"],
        "content": record["content"],
        "wing": safe_meta.get("wing", ""),
        "room": safe_meta.get("room", ""),
        "metadata": safe_meta,
    }

    if record.get("chunked"):
        payload["chunks"] = len(record["ids"])
        payload["chunk_ids"] = record["ids"]
        payload["metadata"]["chunks"] = len(record["ids"])
        payload["metadata"]["chunk_ids"] = record["ids"]

    return payload


def _fetch_drawer_rows(col, where=None, page_size: int = 1000, include=None):
    include = include or ["documents", "metadatas"]
    ids = []
    documents = []
    metadatas = []
    offset = 0
    want_docs = "documents" in include
    want_meta = "metadatas" in include

    while True:
        kwargs = {
            "include": include,
            "limit": page_size,
            "offset": offset,
        }
        if where:
            kwargs["where"] = where

        result = col.get(**kwargs)
        batch_ids = _chroma_field(result, "ids", []) or []
        if not batch_ids:
            break

        batch_docs = _chroma_field(result, "documents", []) or []
        batch_metas = _chroma_field(result, "metadatas", []) or []

        ids.extend(batch_ids)

        for idx in range(len(batch_ids)):
            documents.append(batch_docs[idx] if want_docs and idx < len(batch_docs) else "")
            metadatas.append(batch_metas[idx] if want_meta and idx < len(batch_metas) else {})

        offset += len(batch_ids)
        if len(batch_ids) < page_size:
            break

    return ids, documents, metadatas


def _page_physical_ids(page: list) -> list:
    """The physical row ids backing one page of logical drawers."""
    physical_ids = []
    for drawer in page:
        chunk_ids = drawer.get("chunk_ids") or (drawer.get("metadata") or {}).get("chunk_ids")
        if chunk_ids:
            physical_ids.extend(chunk_ids)
        else:
            physical_ids.append(drawer["drawer_id"])
    return physical_ids


def _apply_drawer_previews(page: list, docs_by_id: dict) -> None:
    """Set ``content_preview`` from an already-fetched ``{id: document}`` map."""
    for drawer in page:
        chunk_ids = drawer.get("chunk_ids") or (drawer.get("metadata") or {}).get("chunk_ids")
        if chunk_ids:
            content = "".join(docs_by_id.get(cid, "") for cid in chunk_ids)
        else:
            content = docs_by_id.get(drawer["drawer_id"], "")
        drawer["content_preview"] = _content_preview(content)


def _fill_drawer_previews(col, page: list) -> None:
    """Hydrate ``content_preview`` for a page of logical drawers only."""
    physical_ids = _page_physical_ids(page)
    if not physical_ids:
        return
    result = col.get(ids=physical_ids, include=["documents"])
    ids = _chroma_field(result, "ids", []) or []
    docs = _chroma_field(result, "documents", []) or []
    docs_by_id = {doc_id: (docs[i] if i < len(docs) else "") or "" for i, doc_id in enumerate(ids)}
    _apply_drawer_previews(page, docs_by_id)


def _fill_drawer_previews_from_sqlite(page: list) -> None:
    """Same, reading the page's documents straight from ``chroma.sqlite3``.

    The list itself is answered from metadata only — joining documents into
    that scan would pull the palace's entire verbatim text into memory to
    render one page. This fetches just the rows on screen.
    """
    physical_ids = _page_physical_ids(page)
    if not physical_ids:
        return
    from ..backends.chroma import sqlite_documents_for_ids

    docs_by_id = sqlite_documents_for_ids(
        _config.palace_path, _config.collection_name, physical_ids
    )
    if docs_by_id is None:
        # sqlite went unreadable between the two reads; previews are a
        # display detail, so degrade to blank rather than fail the listing.
        logger.debug("sqlite preview hydration failed; leaving previews empty")
        return
    _apply_drawer_previews(page, docs_by_id)


def _collapse_drawer_rows(ids, documents, metadatas):
    groups = {}
    singles = []

    for idx, drawer_id in enumerate(ids):
        doc = documents[idx] if idx < len(documents) else ""
        meta = _safe_meta(metadatas[idx] if idx < len(metadatas) else {})
        parent_id = _logical_parent_id(meta)

        if parent_id:
            groups.setdefault(parent_id, []).append(
                (_chunk_index(meta), drawer_id, doc or "", meta)
            )
        else:
            singles.append((drawer_id, doc or "", meta))

    grouped_ids = set(groups)
    drawers = []

    for drawer_id, doc, meta in singles:
        # If both a legacy logical row and chunks exist, display one logical row.
        if drawer_id in grouped_ids:
            continue

        safe_meta = _response_safe_meta(meta)
        drawers.append(
            {
                "drawer_id": drawer_id,
                "wing": safe_meta.get("wing", ""),
                "room": safe_meta.get("room", ""),
                "content_preview": _content_preview(doc),
                "metadata": safe_meta,
            }
        )

    for parent_id, parts in groups.items():
        parts.sort(key=lambda row: (row[0], row[1]))
        chunk_ids = [row[1] for row in parts]
        content = "".join(row[2] for row in parts)

        safe_meta = _response_safe_meta(parts[0][3] if parts else {})
        safe_meta["chunks"] = len(chunk_ids)
        safe_meta["chunk_ids"] = chunk_ids

        drawers.append(
            {
                "drawer_id": parent_id,
                "wing": safe_meta.get("wing", ""),
                "room": safe_meta.get("room", ""),
                "content_preview": _content_preview(content),
                "metadata": safe_meta,
                "chunks": len(chunk_ids),
                "chunk_ids": chunk_ids,
            }
        )

    drawers.sort(key=lambda item: item["drawer_id"])
    return drawers


def _build_chunk_rows(drawer_id: str, content: str, meta: dict, chunk_size: int):
    chunk_size = max(1, int(chunk_size or 1))

    base_meta = _safe_meta(meta)
    base_meta.pop("chunk_index", None)
    base_meta["parent_drawer_id"] = drawer_id

    spans = (
        [(0, "")]
        if content == ""
        else [
            (start, content[start : start + chunk_size])
            for start in range(0, len(content), chunk_size)
        ]
    )

    chunk_ids = []
    chunk_docs = []
    chunk_metas = []

    for start, chunk_doc in spans:
        chunk_index = start // chunk_size
        chunk_ids.append(f"{drawer_id}_chunk_{chunk_index:06d}")
        chunk_docs.append(chunk_doc)

        chunk_meta = dict(base_meta)
        chunk_meta["chunk_index"] = chunk_index
        chunk_metas.append(chunk_meta)

    return chunk_ids, chunk_docs, chunk_metas


def tool_add_drawer(
    wing: str,
    room: str,
    content: str,
    source_file: str = None,
    added_by: str = "mcp",
    tags: list = None,
    session_id: str = "",
):
    """File verbatim content into a wing/room. Checks for duplicates first.

    Content above ``chunk_size`` is split into bounded per-chunk drawers
    via a single batched upsert. Each chunk carries ``parent_drawer_id``
    linkage and ``chunk_index`` metadata so search can rejoin them. The
    returned ``drawer_id`` is the LOGICAL group handle on the chunked
    path; physical drawer ids are in ``chunk_ids`` (#1539).
    ``tool_get_drawer(drawer_id)`` automatically hydrates and reassembles all
    chunks, and ``tool_delete_drawer(drawer_id)`` removes both the logical group
    and all constituent physical chunks.

    ``tags`` is an optional list of cross-cutting labels (multi-label
    additive layer over the strict wing/room hierarchy). See
    ``mempalace.tags`` for normalisation rules.
    """
    global _metadata_cache
    # Observation-grade write sanitizer (#40) runs first: strips control
    # chars, normalizes whitespace, caps at 1 MiB. The strict layer below
    # then enforces the existing wing/room shape and the 100K hard limit.
    wing_pass = sanitize_write_name(wing, "wing")
    if wing_pass["error"]:
        return {"success": False, "error": wing_pass["error"]}
    room = _config.resolve_room(room) if room else room
    room_pass = sanitize_write_name(room, "room")
    if room_pass["error"]:
        return {"success": False, "error": room_pass["error"]}
    content_pass = sanitize_write_content(content)
    if content_pass["error"]:
        return {"success": False, "error": content_pass["error"]}
    sanitize_flags = sorted(
        set(wing_pass["flags"]) | set(room_pass["flags"]) | set(content_pass["flags"])
    )
    try:
        wing = sanitize_name(wing_pass["cleaned"], "wing")
        room = sanitize_name(room_pass["cleaned"], "room")
        content = sanitize_content(content_pass["cleaned"])
        if source_file:
            source_file = strip_lone_surrogates(source_file)
        added_by = strip_lone_surrogates(added_by)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    from ..tags import apply_tags_to_metadata, normalise_tags

    auto_tagged = tags is None
    normalised_tags = normalise_tags(tags)

    col = _get_collection(create=True)
    if not col:
        return _collection_error_or_no_palace()

    if auto_tagged:
        try:
            from ..tag_extraction import extract_tags as _extract_tags

            idf = _get_idf_table(col, wing, room)
            normalised_tags = _extract_tags(content, idf)
        except Exception:
            logger.debug("auto tag extraction failed", exc_info=True)
            normalised_tags = []

    drawer_id = (
        f"drawer_{wing}_{room}_{hashlib.sha256((wing + room + content).encode()).hexdigest()[:24]}"
    )
    drawer_id = make_drawer_id_from_content(wing, room, content)

    _wal_log(
        "add_drawer",
        {
            "drawer_id": drawer_id,
            "wing": wing,
            "room": room,
            "added_by": added_by,
            "content_length": len(content),
            "content_preview": content[:200],
            "tags": normalised_tags,
        },
    )

    # Per #86 — non-canonical rooms are accepted with a soft warning
    # rather than rejected by an FK. Compute warnings up-front so they
    # ride out on both the new-insert and already-exists paths.
    from ..room_taxonomy import validate_room

    warnings = validate_room(room)

    chunk_size = _config.chunk_size
    base_meta = {
        "wing": wing,
        "room": room,
        "source_file": source_file or "",
        "added_by": added_by,
        "filed_at": datetime.now().isoformat(),
        "id_recipe": ID_RECIPE,
    }
    # #408: the session that filed this drawer, so a checkpoint's diary
    # entry and the batch it summarizes can be recovered together. Absent
    # rather than placeholder when nothing usable survives sanitization --
    # every drawer written before this existed has no key either, and a
    # synthetic id would pool them all under one name.
    session_id = sanitize_session_id(session_id)
    if session_id:
        base_meta["session_id"] = session_id
    apply_tags_to_metadata(base_meta, normalised_tags)

    from ..novelty_wiring import compute_novelty_tag

    novelty_tag = compute_novelty_tag(col, wing, room, content, config=_config)
    if novelty_tag is not None:
        base_meta["novelty_tag"] = novelty_tag

    # Idempotency. Three cases to detect a prior committed write:
    # (a) Single-doc path: drawer_id row exists (the only id used).
    # (b) Chunked path: probe the LAST chunk id — its presence implies
    #     every earlier chunk also landed, since the batched upsert
    #     is all-or-nothing.
    # (c) Legacy pre-#1539 single-row write of oversized content under
    #     drawer_id: probe drawer_id alongside the last chunk id so a
    #     re-call with identical oversized content does not duplicate
    #     the legacy row by adding fresh chunks under different ids.
    if len(content) <= chunk_size:
        idempotency_probe_ids = [drawer_id]
    else:
        last_chunk_idx = (len(content) - 1) // chunk_size
        idempotency_probe_ids = [drawer_id, f"{drawer_id}_chunk_{last_chunk_idx:06d}"]
    try:
        existing = col.get(ids=idempotency_probe_ids, include=[])
        if _get_result_ids(existing):
            return {
                "success": True,
                "reason": "already_exists",
                "drawer_id": drawer_id,
                "tags": normalised_tags,
                "warnings": warnings,
                "sanitize_flags": sanitize_flags,
            }
    except Exception as e:
        logger.warning("Idempotency pre-check failed for %s", idempotency_probe_ids, exc_info=True)
        return {"success": False, "error": f"Idempotency check failed before write: {e}"}

    try:
        if len(content) <= chunk_size:
            col.upsert(
                ids=[drawer_id],
                documents=[content],
                metadatas=[{**base_meta, "chunk_index": 0}],
            )
            inserted = col.get(ids=[drawer_id], include=[])
            if not _get_result_ids(inserted):
                raise RuntimeError(
                    "Drawer write was acknowledged but the new ID is not readable. "
                    "The palace index may be stale; run reconnect or repair."
                )
            _invalidate_overview_caches()
            logger.info(f"Filed drawer: {drawer_id} -> {wing}/{room}")
            for _w in warnings:
                logger.warning("room taxonomy: %s (drawer=%s)", _w, drawer_id)
            return {
                "success": True,
                "drawer_id": drawer_id,
                "wing": wing,
                "room": room,
                "tags": normalised_tags,
                "warnings": warnings,
                "sanitize_flags": sanitize_flags,
                "chunks": 1,
            }

        # Oversized content: split into bounded per-chunk drawers so the
        # embedding model never sees a document above ``chunk_size``.
        # Single batched ``upsert`` so the embedding pass either commits
        # every chunk or none — no half-written palace if the embedding
        # model fails mid-loop (#1539).
        chunk_ids: list[str] = []
        chunk_docs: list[str] = []
        chunk_metas: list[dict] = []
        for i in range(0, len(content), chunk_size):
            chunk_idx = i // chunk_size
            chunk_ids.append(f"{drawer_id}_chunk_{chunk_idx:06d}")
            chunk_docs.append(content[i : i + chunk_size])
            chunk_metas.append(
                {**base_meta, "chunk_index": chunk_idx, "parent_drawer_id": drawer_id}
            )
        assert_no_collisions(list(zip(chunk_ids, chunk_metas)), col)
        col.upsert(ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)
        # Probe the LAST chunk id, not the first — its presence confirms
        # the whole batch landed, not just the leading row.
        inserted = col.get(ids=[chunk_ids[-1]], include=[])
        if not _get_result_ids(inserted):
            raise RuntimeError(
                "Drawer write was acknowledged but the new ID is not readable. "
                "The palace index may be stale; run reconnect or repair."
            )
        _invalidate_overview_caches()
        logger.info(f"Filed drawer: {drawer_id} -> {wing}/{room} ({len(chunk_ids)} chunks)")
        for _w in warnings:
            logger.warning("room taxonomy: %s (drawer=%s)", _w, drawer_id)
        return {
            "success": True,
            "drawer_id": drawer_id,
            "wing": wing,
            "room": room,
            "tags": normalised_tags,
            "warnings": warnings,
            "sanitize_flags": sanitize_flags,
            "chunks": len(chunk_ids),
            "chunk_ids": chunk_ids,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "warnings": warnings}


def tool_delete_drawer(drawer_id: str):
    """Delete a single logical drawer by ID."""
    global _metadata_cache

    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    try:
        record = _logical_drawer_record(col, drawer_id)
        if record is None:
            return {"success": False, "error": f"Drawer not found: {drawer_id}"}

        _wal_log(
            "delete_drawer",
            {
                "drawer_id": drawer_id,
                "deleted_ids": record["ids"],
                "deleted_meta": record["metadata"],
                "content_preview": record["content"][:200],
            },
        )

        col.delete(ids=record["ids"])
        _invalidate_overview_caches()

        # Closets are keyed by source_file, not drawer_id (#1722), so a
        # drawer-only delete strands a closet quoting the now-deleted text (#2325).
        source_file = record["metadata"].get("source_file")
        closets_deleted = _purge_source_closets(source_file, commit=True) if source_file else 0

        logger.info(
            "Deleted drawer: %s (%s rows, %s closet(s) purged)",
            drawer_id,
            len(record["ids"]),
            closets_deleted,
        )

        return {
            "success": True,
            "drawer_id": drawer_id,
            "deleted_ids": record["ids"],
            "chunks_deleted": len(record["ids"]),
            "closets_deleted": closets_deleted,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


class _ProtocolStdoutRestoreFailure(BaseException):
    """Fatal loss of the MCP protocol stream after fd-level redirection."""


def _capture_fd_stdout(fn):
    """Run ``fn()`` with its stdout captured at both the Python and fd level.

    The mining engines (``miner.mine`` / ``convo_miner.mine_convos`` /
    ``format_miner.mine_formats``) print progress and a summary to stdout. In
    the MCP server stdout is the JSON-RPC channel (``_restore_stdout`` runs once
    in ``main`` before the protocol loop), so that output would corrupt the
    protocol. Two layers are needed:

    * ``contextlib.redirect_stdout`` captures Python-level ``print`` into a
      buffer — this is what becomes the returned summary, and it works even when
      ``sys.stdout`` has been swapped (e.g. under pytest capture).
    * an ``os.dup2`` of fd 1 to a temp file contains C-level banners emitted by
      onnxruntime / chromadb during embedding, which bypass ``sys.stdout``
      entirely (the same reason the module redirects fd 1 at import, #225), and
      keeps any direct fd-1 write off the live JSON-RPC channel.

    Returns ``(result, captured_text)``. ``captured_text`` is handed back to the
    caller verbatim as an opaque summary; it is never parsed into fields. Falls
    back to Python-level capture alone on platforms without fd-level stdio
    (embedded interpreters), matching the import-time fallback.
    """
    import contextlib
    import io
    import tempfile

    buf = io.StringIO()

    def _capture_python_stdout():
        with contextlib.redirect_stdout(buf):
            result = fn()
        return result, buf.getvalue()

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, AttributeError, ValueError):
        return _capture_python_stdout()

    try:
        saved_fd = os.dup(1)
    except (OSError, AttributeError, ValueError):
        return _capture_python_stdout()

    redirected = False
    try:
        try:
            tmp_file = tempfile.TemporaryFile()
        except (OSError, AttributeError, ValueError):
            return _capture_python_stdout()

        with tmp_file as tmp:
            try:
                os.dup2(tmp.fileno(), 1)
            except (OSError, AttributeError, ValueError):
                # No callback has run and fd 1 was not replaced. Use the
                # documented Python-level fallback.
                return _capture_python_stdout()
            redirected = True
            try:
                with contextlib.redirect_stdout(buf):
                    result = fn()
            finally:
                flush_error = None
                try:
                    sys.stdout.flush()
                except (OSError, AttributeError, ValueError) as exc:
                    flush_error = exc
                try:
                    os.dup2(saved_fd, 1)
                except (OSError, AttributeError, ValueError) as exc:
                    # Ordinary tool and protocol handlers catch Exception. A
                    # failed restore is process-fatal instead: continuing could
                    # emit JSON-RPC into the temporary file and hang the client.
                    # Keep saved_fd open for diagnostics/emergency recovery;
                    # process exit will release it.
                    raise _ProtocolStdoutRestoreFailure(
                        "failed to restore MCP protocol stdout"
                    ) from exc
                redirected = False
                if flush_error is not None:
                    raise flush_error
            tmp.seek(0)
            fd_text = tmp.read().decode("utf-8", "replace")
        return result, buf.getvalue() + fd_text
    finally:
        if not redirected:
            os.close(saved_fd)


def tool_mine(
    source: str,
    mode: str = "projects",
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract: str = "exchange",
):
    """Mine a directory into the palace — the MCP equivalent of ``mempalace mine``.

    Lets MCP clients that cannot shell out (Claude Desktop, LM Studio, Aionui,
    Desktop Commander) trigger indexing in-conversation (#1662). Wraps the same
    in-process miners the CLI's ``cmd_mine`` calls; it adds no new ingestion
    logic of its own.

    mode:
        ``"projects"`` (default) — code/docs via ``miner.mine``.
        ``"convos"``             — chat transcripts via ``convo_miner.mine_convos``.
        ``"extract"``            — office documents (PDF/DOCX/RTF/…) via
                                   ``format_miner.mine_formats``; requires the
                                   optional ``mempalace[extract]`` dependency.
    wing:    target wing (default: derived from the source directory name).
    agent:   recorded on every drawer (default ``"mempalace"``).
    limit:   max files to process (0 = all).
    dry_run: walk + chunk and report, but file nothing.
    extract: convos extraction strategy — ``"exchange"`` (default) or
             ``"general"``; ignored by the other modes.

    Runs synchronously and mirrors the :func:`tool_sync` contract: success
    returns ``{success: True, mode, dry_run, output[, output_truncated]}`` where ``output`` is
    the miner's human-readable summary (captured so it cannot corrupt the
    JSON-RPC stream); failure returns ``{success: False, error[, error_class]}``.
    The palace write lock is held by the miners themselves, so a concurrent mine
    surfaces as a structured already-running error. Orphan cleanup is not part of
    mining — use ``mempalace_sync`` for that.
    """
    global _metadata_cache
    from ..daemon import LOCK_REFUSAL_ERROR_CLASS
    from ..palace import MineAlreadyRunning, MineValidationError

    if not _config.palace_path:
        np = _no_palace()
        return {"success": False, "error": np.get("error", "no palace"), "hint": np.get("hint")}

    valid_modes = ("projects", "convos", "extract")
    if mode not in valid_modes:
        return {
            "success": False,
            "error": f"invalid mode '{mode}'; expected one of: {', '.join(valid_modes)}",
        }

    src = os.path.expanduser(source) if source else ""
    # convos accepts one conversation file as well as a directory — the CLI has
    # always documented it that way ("Directory to mine, or one conversation
    # file with --mode convos"), and the hooks rely on it: _ingest_transcript
    # submits a single .jsonl. Because cmd_mine forwards to the hub whenever one
    # is live, a directory-only precondition here made that documented form
    # unreachable in the configuration most users run, so every hook transcript
    # ingest failed against a running hub (#2281). The other modes still walk a
    # tree, so they keep the directory requirement.
    if not src or not (os.path.isdir(src) or (mode == "convos" and os.path.isfile(src))):
        return {"success": False, "error": f"source not found: {source!r}"}

    def _run():
        if mode == "convos":
            from ..convo_miner import mine_convos

            return mine_convos(
                convo_dir=src,
                palace_path=_config.palace_path,
                wing=wing,
                agent=agent,
                limit=limit,
                dry_run=dry_run,
                extract_mode=extract,
            )
        if mode == "extract":
            from ..format_miner import mine_formats

            return mine_formats(
                format_dir=src,
                palace_path=_config.palace_path,
                wing=wing,
                agent=agent,
                limit=limit,
                dry_run=dry_run,
            )
        from ..miner import mine

        return mine(
            project_dir=src,
            palace_path=_config.palace_path,
            wing_override=wing,
            agent=agent,
            limit=limit,
            dry_run=dry_run,
        )

    try:
        try:
            _result, output = _capture_fd_stdout(_run)
        # Order matters: typed handlers precede the bare Exception (mirroring
        # tool_sync) so MineAlreadyRunning / MineValidationError / ValueError
        # don't fall into the generic "mine failed" branch.
        except MineAlreadyRunning as exc:
            return {
                "success": False,
                "error": f"another mine is in progress: {exc}",
                "error_class": LOCK_REFUSAL_ERROR_CLASS,
            }
        except MineValidationError as exc:
            return {
                "success": False,
                "error": f"palace integrity check failed after mine: {exc}",
                "error_class": "MineValidationError",
            }
        except ImportError as exc:
            # 'extract' mode pulls in the optional mempalace[extract] stack;
            # name it so the caller knows to install the extra. Other modes have
            # no optional imports, so an ImportError there is a real bug, not a
            # missing extra — log the traceback and surface its type.
            if mode == "extract":
                return {
                    "success": False,
                    "error": f"mode 'extract' needs the mempalace[extract] extra: {exc}",
                    "error_class": "MissingDependency",
                }
            logger.exception("tool_mine: unexpected ImportError (mode=%s)", mode)
            return {"success": False, "error": f"mine failed: {exc}", "error_class": "ImportError"}
        except ValueError as exc:
            return {"success": False, "error": str(exc), "error_class": "ValueError"}
        except SystemExit as exc:
            # A library mine() must never terminate the MCP server. miner.mine
            # converts Ctrl-C into sys.exit(130) (CLI semantics); in-process
            # that SystemExit is a BaseException that would slip past the
            # protocol loop's `except Exception` and kill the server with no
            # response. Convert it to a structured error instead.
            return {
                "success": False,
                "error": f"mine exited early (code {exc.code})",
                "error_class": "Interrupted",
            }
        except Exception as exc:
            logger.exception("tool_mine: mine failed (mode=%s)", mode)
            return {
                "success": False,
                "error": f"mine failed: {exc}",
                "error_class": type(exc).__name__,
            }
        # Cap the echoed summary so a very large mine cannot return a multi-MB
        # payload to the MCP client. The useful summary is at the tail, so keep
        # the end and flag the truncation (never silently).
        payload = {"success": True, "mode": mode, "dry_run": dry_run, "output": output}
        cap = 4000
        if len(output) > cap:
            payload["output"] = output[-cap:]
            payload["output_truncated"] = True
        return payload
    finally:
        if not dry_run:
            _invalidate_overview_caches()


def _purge_source_closets(source_file: str, *, commit: bool) -> int:
    """Count, and optionally delete, closets matching ``source_file`` exactly.

    The closets collection is the searchable AAAK index layer; it is keyed by
    ``source_file`` independently of the drawers collection, so a drawer-only
    delete would strand stale index pointers at the deleted source (#1722).
    Mirrors the closet-purge step in :func:`mempalace.sync.sync_palace` and the
    re-mine purge in :func:`mempalace.palace.purge_file_closets`.

    Best-effort: a missing or unavailable closet collection yields 0 and never
    raises, so it can never abort a drawer delete that has already committed.
    Deletion is pushed down via ``delete(where=...)`` so it survives palaces
    larger than the 10k ``get()`` truncation; the returned count is the (best
    effort) number of matching closets observed before the delete.
    """
    from ..palace import get_closets_collection

    try:
        closets_col = get_closets_collection(_config.palace_path, create=False)
    except Exception as exc:
        logger.warning("Closet purge skipped (collection unavailable): %s", exc)
        return 0
    if closets_col is None:
        return 0
    try:
        ids = _get_result_ids(closets_col.get(where={"source_file": source_file}, include=[]))
        count = len(ids)
        if commit and count:
            closets_col.delete(where={"source_file": source_file})
        return count
    except Exception as exc:
        logger.warning("Closet purge failed for %s: %s", source_file, exc)
        return 0


def tool_delete_by_source(source_file: str, dry_run: bool = True):
    """Delete every drawer whose ``source_file`` metadata matches exactly.

    Bulk cleanup for the contamination case in #1722, where benchmark/eval
    files (ShareGPT dumps, ``results_mempal_*.jsonl``, language config JSON)
    get mined into the same wing as real user data and drown out semantic
    search. Previously the only recourse was hand-rolled SQLite ``DELETE``
    against ``chroma.sqlite3``.

    Matching is exact on the stored ``source_file`` value and pushed down to
    the backend via ``delete(where=...)`` — the same idiom used by the miner
    and diary ingest paths — so there is no client-side id list and the
    SQLite "too many variables" limit cannot be hit, regardless of how many
    drawers share the source (the reporter had 55k).

    Also purges the matching closets (the AAAK index layer) so deleting the
    drawers doesn't strand stale index pointers at the dead source (#1722).

    Defaults to a dry run: it reports the drawer match count, the closet match
    count, and a small sample so the caller can confirm the blast radius before
    anything is removed. Pass ``dry_run=False`` to commit the deletion
    (irreversible).
    """
    global _metadata_cache
    if not isinstance(source_file, str) or not source_file.strip():
        return {"success": False, "error": "source_file must be a non-empty string"}
    # Mirror the ingestion-side normalization (tool_add_drawer strips lone
    # surrogates from source_file before storing) so exact matching still hits
    # rows mined from non-ASCII paths that arrived via a cp1252 stdin (#1488).
    source_file = strip_lone_surrogates(source_file)

    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    where = {"source_file": source_file}
    try:
        # Paginated to survive palaces larger than the 10k get() truncation.
        metas = _fetch_all_metadata(col, where=where)
    except Exception as e:
        return {"success": False, "error": str(e)}

    match_count = len(metas)
    # Distinct (wing, room) pairs so the caller sees where the hits live.
    sample = []
    seen = set()
    for meta in metas:
        meta = _safe_meta(meta)
        # Default missing wing/room to "" for consistency with the rest of the
        # file (drawers are always stored with both, but be defensive).
        wing = meta.get("wing", "")
        room = meta.get("room", "")
        key = (wing, room)
        if key in seen:
            continue
        seen.add(key)
        sample.append({"wing": wing, "room": room})
        if len(sample) >= 5:
            break

    if dry_run:
        closet_match_count = _purge_source_closets(source_file, commit=False)
        return {
            "success": True,
            "dry_run": True,
            "source_file": source_file,
            "match_count": match_count,
            "closet_match_count": closet_match_count,
            "sample": sample,
            "hint": (
                "No drawers were deleted. Re-run with dry_run=false to remove "
                f"these {match_count} drawer(s) and {closet_match_count} index "
                "entr(y/ies)."
                if match_count
                else "No drawers match this source_file."
            ),
        }

    if match_count == 0:
        # Idempotent: deleting an absent source is a no-op, not an error.
        return {
            "success": True,
            "dry_run": False,
            "source_file": source_file,
            "deleted": 0,
        }

    _wal_log(
        "delete_by_source",
        {"source_file": source_file, "match_count": match_count, "sample": sample},
    )
    try:
        col.delete(where=where)
        _invalidate_overview_caches()
        # Purge the matching closets too so the AAAK index doesn't keep stale
        # pointers at the now-deleted drawers (#1722). Done after the drawer
        # delete and intentionally best-effort: the drawers are already gone,
        # so a closet-purge hiccup must not turn a successful delete into an
        # error — it just leaves index cruft a later `repair` / re-mine clears.
        closets_deleted = _purge_source_closets(source_file, commit=True)
        logger.info(
            "Deleted %d drawer(s) and %d closet(s) from source: %s",
            match_count,
            closets_deleted,
            source_file,
        )
        return {
            "success": True,
            "dry_run": False,
            "source_file": source_file,
            "deleted": match_count,
            "closets_deleted": closets_deleted,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_sync(project_dir: str = None, wing: str = None, apply: bool = False):
    """Prune drawers whose source files are gitignored, missing, or moved (#1252)."""
    global _metadata_cache
    from ..daemon import LOCK_REFUSAL_ERROR_CLASS
    from ..palace import MineAlreadyRunning
    from ..sync import sync_palace

    if not _config.palace_path:
        np = _no_palace()
        return {"success": False, "error": np.get("error", "no palace"), "hint": np.get("hint")}
    project_dirs = [project_dir] if project_dir else None
    try:
        try:
            report = sync_palace(
                palace_path=_config.palace_path,
                project_dirs=project_dirs,
                wing=wing,
                dry_run=not apply,
                wal_log=_wal_log,
            )
            return {"success": True, **report}
        # Order matters: typed handlers must precede the bare Exception
        # below, otherwise MineAlreadyRunning and ValueError fall into the
        # generic "sync failed" branch and break the structured-error tests.
        except MineAlreadyRunning as exc:
            return {
                "success": False,
                "error": f"another mine is in progress: {exc}",
                "error_class": LOCK_REFUSAL_ERROR_CLASS,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"sync failed: {exc}"}
    finally:
        if apply:
            _invalidate_overview_caches()


def tool_get_drawer(drawer_id: str):
    """Fetch a single logical drawer by ID. Returns full content and metadata."""
    from ..tags import extract_tags_from_metadata

    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    try:
        record = _logical_drawer_record(col, drawer_id)
        if record is None:
            return {"error": f"Drawer not found: {drawer_id}"}
        payload = _drawer_payload(record)
        payload["tags"] = extract_tags_from_metadata(payload.get("metadata") or {})
        return payload
    except Exception as e:
        return {"error": str(e)}


def tool_list_drawers(
    wing: str = None,
    room: str = None,
    since: str = None,
    before: str = None,
    tags: list = None,
    limit: int = 20,
    offset: int = 0,
):
    """List logical drawers with pagination. Optional wing/room/tag filter.

    Optional ``since`` / ``before`` filter by drawer ``filed_at`` (ISO date or
    timestamp): ``since`` is inclusive, ``before`` is exclusive (#1128). A
    drawer whose ``filed_at`` is missing or unparseable is excluded while a
    date bound is active. The filter is applied in Python after the rows are
    fetched — ChromaDB rejects string operands for ``$gte``/``$lt`` (1.5.7),
    and ``filed_at`` is stored as an ISO string, so a server-side ``where``
    comparison is not available.
    """
    from ..tags import extract_tags_from_metadata, normalise_tags

    limit = max(1, min(limit, _MAX_RESULTS))
    offset = max(0, offset)

    try:
        wing = _sanitize_optional_name(wing, "wing")
        room = _resolve_room_alias(room)
        since_dt = _parse_date_filter(since, "since")
        before_dt = _parse_date_filter(before, "before")
        if since_dt is not None and before_dt is not None and since_dt >= before_dt:
            raise ValueError(f"since ({since!r}) must be earlier than before ({before!r})")
    except ValueError as e:
        return {"error": str(e)}
    normalised_tags = normalise_tags(tags) if tags else []
    try:
        where = None
        conditions = []

        if wing:
            conditions.append({"wing": wing})
        if room:
            conditions.append({"room": room})
        if normalised_tags:
            conditions.append({"tags": {"$contains_all": normalised_tags}})
        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}

        listed = None
        if _is_chroma_backend() and _config.palace_path:
            from ..backends.chroma import sqlite_list_id_metadata

            listed = sqlite_list_id_metadata(
                _config.palace_path, _config.collection_name, where=where
            )
        if listed is not None:
            # Documents are fetched for the displayed page only, below.
            ids, metadatas = listed
            documents = []
        else:
            col = _get_collection()
            if not col:
                return _collection_error_or_no_palace()
            ids, documents, metadatas = _fetch_drawer_rows(col, where=where, include=["metadatas"])
        drawers = _collapse_drawer_rows(ids, documents, metadatas)

        if since_dt is not None or before_dt is not None:
            drawers = [
                d
                for d in drawers
                if _filed_at_in_window(d.get("metadata", {}).get("filed_at"), since_dt, before_dt)
            ]

        page = drawers[offset : offset + limit]
        if listed is not None:
            _fill_drawer_previews_from_sqlite(page)
        else:
            col = _get_collection()
            if col:
                _fill_drawer_previews(col, page)

        # Surface the tag list on each entry without dropping upstream's
        # logical-drawer shape (wing/room/content_preview/metadata).
        for entry in page:
            entry["tags"] = extract_tags_from_metadata(entry.get("metadata") or {})
        return {
            "drawers": page,
            "total": len(drawers),
            "count": len(page),
            "offset": offset,
            "limit": limit,
        }
    except Exception as e:
        logger.exception("tool_list_drawers failed")
        return {"error": str(e)}


def tool_update_drawer(
    drawer_id: str,
    content: str = None,
    wing: str = None,
    room: str = None,
    tags: list = None,
):
    """Update an existing logical drawer's content and/or metadata.

    ``tags`` semantics:
        * ``None`` — leave the existing tag list untouched.
        * ``[]``   — clear all tags.
        * non-empty list — replace the existing tag list with the
          normalised input.
    """
    global _metadata_cache

    if content is None and wing is None and room is None and tags is None:
        return {"success": True, "drawer_id": drawer_id, "noop": True}

    from ..tags import apply_tags_to_metadata, extract_tags_from_metadata, normalise_tags

    col = _get_collection()
    if not col:
        return _collection_error_or_no_palace()

    try:
        record = _logical_drawer_record(col, drawer_id)
        if record is None:
            return {"success": False, "error": f"Drawer not found: {drawer_id}"}

        old_meta = _safe_meta(record["metadata"])
        old_doc = record["content"]

        # Observation-grade write sanitizer (#40) — see tool_add_drawer for rationale.
        update_sanitize_flags: list[str] = []

        new_doc = old_doc
        if content is not None:
            content_pass = sanitize_write_content(content)
            if content_pass["error"]:
                return {"success": False, "error": content_pass["error"]}
            update_sanitize_flags.extend(content_pass["flags"])
            try:
                new_doc = sanitize_content(content_pass["cleaned"])
            except ValueError as e:
                return {"success": False, "error": str(e)}

        new_meta = dict(old_meta)

        if wing is not None:
            wing_pass = sanitize_write_name(wing, "wing")
            if wing_pass["error"]:
                return {"success": False, "error": wing_pass["error"]}
            update_sanitize_flags.extend(wing_pass["flags"])
            try:
                new_meta["wing"] = sanitize_name(wing_pass["cleaned"], "wing")
            except ValueError as e:
                return {"success": False, "error": str(e)}
            if wing.lower() != str(old_meta.get("wing") or "").lower():
                new_meta["wing"] = wing

        if room is not None:
            room = _config.resolve_room(room)
            room_pass = sanitize_write_name(room, "room")
            if room_pass["error"]:
                return {"success": False, "error": room_pass["error"]}
            update_sanitize_flags.extend(room_pass["flags"])
            try:
                new_meta["room"] = sanitize_name(room_pass["cleaned"], "room")
            except ValueError as e:
                return {"success": False, "error": str(e)}
            if room.lower() != str(old_meta.get("room") or "").lower():
                new_meta["room"] = room

        update_sanitize_flags = sorted(set(update_sanitize_flags))

        if tags is not None:
            apply_tags_to_metadata(new_meta, normalise_tags(tags))

        _wal_log(
            "update_drawer",
            {
                "drawer_id": drawer_id,
                "old_wing": old_meta.get("wing", ""),
                "old_room": old_meta.get("room", ""),
                "new_wing": new_meta.get("wing", ""),
                "new_room": new_meta.get("room", ""),
                "old_tags": extract_tags_from_metadata(old_meta),
                "new_tags": extract_tags_from_metadata(new_meta),
                "content_changed": content is not None,
                "content_preview": new_doc[:200] if content is not None else None,
            },
        )

        # A closet quotes the source file, not the stored drawer, so it only
        # goes stale on a content change; wing/room alone leaves it correct (#2325).
        closets_deleted = 0
        source_file = old_meta.get("source_file")
        if content is not None and source_file:
            closets_deleted = _purge_source_closets(source_file, commit=True)

        chunk_size = max(1, int(getattr(_config, "chunk_size", 800) or 800))
        should_chunk = bool(record.get("chunked")) or len(new_doc) > chunk_size

        if should_chunk:
            chunk_ids, chunk_docs, chunk_metas = _build_chunk_rows(
                drawer_id,
                new_doc,
                new_meta,
                chunk_size,
            )

            col.upsert(ids=chunk_ids, documents=chunk_docs, metadatas=chunk_metas)

            keep_ids = set(chunk_ids)
            stale_ids = [old_id for old_id in record["ids"] if old_id not in keep_ids]
            if stale_ids:
                col.delete(ids=stale_ids)

            _invalidate_overview_caches()

            logger.info("Updated drawer: %s (%s rows)", drawer_id, len(chunk_ids))

            return {
                "success": True,
                "drawer_id": drawer_id,
                "wing": new_meta.get("wing", ""),
                "room": new_meta.get("room", ""),
                "chunks": len(chunk_ids),
                "chunk_ids": chunk_ids,
                "closets_deleted": closets_deleted,
            }

        update_kwargs = {"ids": [record["ids"][0]]}
        if content is not None:
            update_kwargs["documents"] = [new_doc]
        update_kwargs["metadatas"] = [new_meta]

        col.update(**update_kwargs)
        _invalidate_overview_caches()

        # Per #86 — if the caller reassigned the room to a non-canonical
        # value, surface the warning. When room wasn't touched we don't
        # warn (the drawer keeps its existing room; this update isn't
        # responsible for whatever taxonomy it was filed under).
        from ..room_taxonomy import validate_room

        warnings = validate_room(new_meta.get("room", "")) if room is not None else []

        logger.info(f"Updated drawer: {drawer_id}")
        for _w in warnings:
            logger.warning("room taxonomy: %s (drawer=%s)", _w, drawer_id)
        return {
            "success": True,
            "drawer_id": drawer_id,
            "wing": new_meta.get("wing", ""),
            "room": new_meta.get("room", ""),
            "tags": extract_tags_from_metadata(new_meta),
            "warnings": warnings,
            "sanitize_flags": update_sanitize_flags,
            "closets_deleted": closets_deleted,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_rate_memory(drawer_id: str, useful: bool):
    """Record feedback on whether a search result was helpful (#159).

    Stores the rating as drawer *metadata* — the verbatim content is never
    touched. Each call increments one of two counters (``rating_useful`` /
    ``rating_not_useful``); the net of the two becomes a bounded, capped
    ranking signal in ``search_memories`` that can reorder neighbours but
    never excludes a drawer (recall is preserved).
    """
    global _metadata_cache

    from ..ratings import apply_rating_to_metadata, extract_rating_from_metadata, net_rating

    if not isinstance(useful, bool):
        return {"success": False, "error": "`useful` must be a boolean"}

    col = _get_collection()
    if not col:
        return _no_palace()
    try:
        existing = col.get(ids=[drawer_id], include=["metadatas"])
        if not existing["ids"]:
            return {"success": False, "error": f"Drawer not found: {drawer_id}"}

        new_meta = dict(_safe_meta(existing["metadatas"][0]))
        apply_rating_to_metadata(new_meta, useful)
        useful_count, not_useful_count = extract_rating_from_metadata(new_meta)

        _wal_log(
            "rate_memory",
            {
                "drawer_id": drawer_id,
                "useful": useful,
                "rating_useful": useful_count,
                "rating_not_useful": not_useful_count,
            },
        )

        # Metadata-only update: never pass `documents`, so the stored
        # verbatim text is left exactly as written.
        col.update(ids=[drawer_id], metadatas=[new_meta])
        _metadata_cache = None

        logger.info(
            "Rated drawer %s useful=%s (useful=%d not_useful=%d)",
            drawer_id,
            useful,
            useful_count,
            not_useful_count,
        )
        return {
            "success": True,
            "drawer_id": drawer_id,
            "useful": useful,
            "rating_useful": useful_count,
            "rating_not_useful": not_useful_count,
            "net_rating": net_rating(new_meta),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_rename_wing(from_wing: str, to_wing: str, batch_size: int = 500):
    """Rename all drawers in one wing to another, server-side.

    Iterates through the source wing in batches and updates each
    drawer's metadata. Much faster than individual update_drawer calls
    over HTTP since it operates directly on the collection.
    """
    global _metadata_cache

    try:
        from_wing = sanitize_name(from_wing, "from_wing")
        to_wing = sanitize_name(to_wing, "to_wing")
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if from_wing == to_wing:
        return {"success": True, "renamed": 0, "message": "source and target are the same"}

    col = _get_collection()
    if not col:
        return _no_palace()

    try:
        result = col.rename_wing(
            from_wing=from_wing,
            to_wing=to_wing,
            batch_size=batch_size,
        )
        _metadata_cache = None

        _wal_log(
            "rename_wing",
            {
                "from_wing": from_wing,
                "to_wing": to_wing,
                **result,
            },
        )

        logger.info(f"Renamed wing: {from_wing} -> {to_wing} ({result['renamed']} drawers)")
        return {
            "success": True,
            "from_wing": from_wing,
            "to_wing": to_wing,
            **result,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def tool_list_tags(wing: str = None, room: str = None, min_count: int = 1):
    """Return every unique tag in the palace with the number of drawers carrying it.

    Results are sorted by count (descending). ``wing`` and ``room`` scope
    the count to a subset of the palace. ``min_count`` drops tags below
    the threshold from the result; default 1 keeps any tag with at least
    one drawer.
    """
    from collections import Counter

    from ..tags import extract_tags_from_metadata

    try:
        wing = _sanitize_optional_name(wing, "wing")
        room = _resolve_room_alias(room)
    except ValueError as e:
        return {"error": str(e)}
    min_count = max(1, int(min_count))

    col = _get_collection()
    if not col:
        return _no_palace()
    try:
        where = None
        if wing and room:
            where = {"$and": [{"wing": wing}, {"room": room}]}
        elif wing:
            where = {"wing": wing}
        elif room:
            where = {"room": room}

        counter: Counter = Counter()
        page_size = 1000
        offset = 0
        kwargs = {"include": ["metadatas"], "limit": page_size}
        if where:
            kwargs["where"] = where
        while True:
            kwargs["offset"] = offset
            batch = col.get(**kwargs)
            ids = batch["ids"] or []
            metas = batch["metadatas"] or []
            for meta in metas:
                for tag in extract_tags_from_metadata(meta):
                    counter[tag] += 1
            if not ids or len(ids) < page_size:
                break
            offset += len(ids)

        items = [
            {"tag": tag, "count": count}
            for tag, count in counter.most_common()
            if count >= min_count
        ]
        return {
            "tags": items,
            "total_unique_tags": len(items),
            "filters": {"wing": wing, "room": room, "min_count": min_count},
        }
    except Exception as e:
        return {"error": str(e)}
