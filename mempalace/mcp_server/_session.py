# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


def _stat_palace_db() -> tuple:
    """Return ``(st_ino, st_mtime)`` of the palace's chroma.sqlite3, or ``(0, 0.0)``."""
    try:
        st = os.stat(os.path.join(_config.palace_path, "chroma.sqlite3"))
    except OSError:
        return 0, 0.0
    return st.st_ino, st.st_mtime


def _restamp_palace_db() -> None:
    """Re-baseline the freshness stat after this session's own writes.

    Building the client, opening the collection, and every write move
    chroma.sqlite3's mtime. ``_get_client`` compares against the stat taken
    before those writes, so it read the session's own footprint as an external
    change and rebuilt the client, reloading the whole HNSW index, on nearly
    every call. Same fix as ``ChromaBackend._restamp`` (#2307): record the
    stat after our own writes, so a later difference is someone else's. A
    write from another process that lands during our own operation is absorbed
    the same way there and picked up on the next change.
    """
    global _palace_db_inode, _palace_db_mtime
    if _client_cache is None:
        return
    _palace_db_inode, _palace_db_mtime = _stat_palace_db()
    _note_own_db_stamp(_config.palace_path, (_palace_db_inode, _palace_db_mtime))


def _drop_session_client_for_system_reset() -> None:
    """Close the session client before the shared Chroma System is dropped.

    Search goes through ``ChromaBackend`` and the other tools through this
    client. Both share Chroma's process-wide System. When one of them sees a
    peer write it clears that System; the client left open keeps the discarded
    segment, and the fresh stat the reset records would look like a write this
    process made itself. Registered with
    :func:`mempalace.backends.chroma.register_before_system_cache_reset`.

    Does not clear the System again, and does not move the recorded stat: the
    next ``_get_client`` reopens onto the System the other owner just built.
    """
    global \
        _client_cache, \
        _collection_cache, \
        _collection_cache_backend, \
        _collection_cache_palace, \
        _collection_open_error
    cached = _client_cache
    _client_cache = None
    _collection_cache = None
    _collection_cache_backend = None
    _collection_cache_palace = None
    _collection_open_error = None
    _invalidate_overview_caches()
    if cached is None:
        return
    try:
        close = getattr(cached, "close", None)
        if callable(close):
            close()
    except Exception:
        logger.debug(
            "Failed to close session Chroma client before system reset",
            exc_info=True,
        )


register_before_system_cache_reset(_drop_session_client_for_system_reset)


class _SessionFreshness:
    """Stands in for ``ChromaCollection``'s owning backend so the session's own
    writes re-baseline its freshness stat (see :func:`_restamp_palace_db`)."""

    @staticmethod
    def _restamp(palace_path: str) -> None:
        if palace_path == _config.palace_path:
            _restamp_palace_db()


def _get_client():
    """Return a ChromaDB PersistentClient, reconnecting if the database changed on disk.

    Detects palace rebuilds (repair/nuke/purge) by checking the inode of
    chroma.sqlite3.  A full rebuild replaces the file, changing the inode.
    Also detects external writes (scripts, CLI) via mtime changes — the
    inode check alone misses in-place modifications that invalidate the
    in-memory HNSW index.

    Note: FAT/exFAT may return 0 for st_ino — the ``current_inode != 0``
    guard skips reconnect detection on those filesystems (safe fallback).
    """
    global \
        _client_cache, \
        _collection_cache, \
        _collection_cache_backend, \
        _collection_cache_palace, \
        _collection_open_error, \
        _palace_db_inode, \
        _palace_db_mtime, \
        _client_system_generation, \
        _metadata_cache, \
        _metadata_cache_time
    if not _is_chroma_backend():
        raise RuntimeError("_get_client is only available for the Chroma backend")
    db_path = os.path.join(_config.palace_path, "chroma.sqlite3")
    try:
        st = os.stat(db_path)
        current_inode = st.st_ino
        current_mtime = st.st_mtime
    except OSError:
        current_inode = 0
        current_mtime = 0.0

    # If the DB file disappeared (e.g. during rebuild) but we have a cached
    # collection, invalidate so we don't serve stale data.  Without this,
    # both stored and current values are 0 on the first call after deletion,
    # making inode_changed and mtime_changed both False.
    if not os.path.isfile(db_path) and _collection_cache is not None:
        _client_cache = None
        _collection_cache = None
        _collection_cache_backend = None
        _collection_cache_palace = None
        _collection_open_error = None
        _palace_db_inode = 0
        _palace_db_mtime = 0.0
        # Fall through to normal reconnect which will handle missing DB

    inode_changed = current_inode != 0 and current_inode != _palace_db_inode
    mtime_changed = current_mtime != 0.0 and abs(current_mtime - _palace_db_mtime) > 0.01
    # A client opened before the last system reset is not reading the segment
    # a peer write rebuilt, even when the new stat was recorded as our own.
    client_is_current = (
        _client_cache is not None and _client_system_generation == chroma_system_generation()
    )
    if (
        client_is_current
        and mtime_changed
        and not inode_changed
        and _is_own_db_stamp(
            _config.palace_path,
            (current_inode, current_mtime),
            generation=_client_system_generation,
        )
    ):
        # Written through the System this client was opened on (see
        # _OWN_DB_STAMPS). Nothing to reload.
        _palace_db_mtime = current_mtime
        mtime_changed = False

    if _client_cache is None or inode_changed or mtime_changed or not client_is_current:
        # Run the HNSW capacity probe BEFORE chromadb opens the segment --
        # if the index is severely undersized, segment load can segfault
        # the whole MCP server (#1222). The probe is pure sqlite +
        # metadata read; never touches the HNSW binary files.
        _refresh_vector_disabled_flag()
        if client_is_current and (inode_changed or mtime_changed):
            ChromaBackend._quarantined_paths.discard(_config.palace_path)
            # #2002: a peer process changed chroma.sqlite3 on disk. chromadb
            # caches its System (and the live HNSW segment) keyed by path, so
            # make_client() below would hand back the STALE segment, which then
            # persists its outdated index over the peer's writes, driving the
            # persisted count backwards. Drop chromadb's shared cache first so
            # make_client() rebuilds the segment from the on-disk state.
            _force_chroma_cache_reset()
        elif _client_cache is not None:
            # Another owner already reset the shared System and this client
            # is the one it left behind. Reopen onto the System it built;
            # clearing again would discard that System too.
            _drop_session_client_for_system_reset()
        _client_cache = ChromaBackend.make_client(_config.palace_path)
        _collection_cache = None
        _collection_cache_backend = None
        _collection_cache_palace = None
        _collection_open_error = None
        _invalidate_overview_caches()
        # Stat again: constructing the client just wrote to chroma.sqlite3.
        _palace_db_inode, _palace_db_mtime = _stat_palace_db()
        _client_system_generation = chroma_system_generation()
        _note_own_db_stamp(_config.palace_path, (_palace_db_inode, _palace_db_mtime))
    return _client_cache


def _get_collection(create=False):
    """Return the configured backend's collection, caching between calls.

    Backend routing (mirrors ``_get_kg``):

    - ``_config.backend == "postgres"`` → ``_get_collection_postgres``
      (uses ``mempalace.backends.postgres.PostgresBackend``). Requires
      ``MEMPALACE_POSTGRES_DSN`` or equivalent config.
    - ``_config.backend == "chroma"`` (default) → existing ChromaDB
      path below. Preserved unchanged for legacy palaces still on
      ChromaDB — the SIGSEGV recovery logic, HNSW preflight, stale-
      handle retry, etc. all stay relevant for that backend and only
      that backend.

    Both paths return a ``BaseCollection``-conforming object; callers
    interact with it via the same API surface (``count``, ``get``,
    ``query``, ``add``, ``upsert``, ``delete``) and don't need to
    branch.
    """
    if _config.backend == "postgres":
        return _get_collection_postgres(create=create)

    global \
        _client_cache, \
        _collection_cache, \
        _collection_cache_backend, \
        _collection_cache_palace, \
        _collection_open_error, \
        _palace_db_inode, \
        _palace_db_mtime, \
        _metadata_cache, \
        _metadata_cache_time
    # Operator read-only mode must never bootstrap a collection. In
    # particular, sqlite_exact's normal create/open path initializes WAL,
    # schema, FTS metadata, and commits before the first read.
    if _READ_ONLY:
        create = False
    try:
        backend_name = _selected_backend_name()
    except (BackendMismatchError, KeyError) as exc:
        logger.warning("backend resolution failed for %s: %s", _config.palace_path, exc)
        _collection_open_error = {
            "error": "Backend mismatch"
            if isinstance(exc, BackendMismatchError)
            else "Unknown backend",
            "details": str(exc),
            "hint": "Select the matching backend or use a fresh palace directory.",
        }
        _collection_cache = None
        _collection_cache_backend = None
        _collection_cache_palace = None
        return None

    if backend_name != "chroma":
        # Normal stdio MCP remains capable of promotion to writer, but until
        # it actually owns the palace it must not open sqlite_exact through
        # the schema-initializing read/write path. This lets recall coexist
        # with a daemon/HTTP writer. _acquire_mcp_writer_lock() discards this
        # cached read-only collection before a promoted mutation is handled.
        collection_read_only = _READ_ONLY or (
            backend_name in {"sqlite_exact", "rust_exact"}
            and getattr(_args, "transport", "stdio") == "stdio"
            and _MCP_WRITER_LOCK_CM is None
        )
        if collection_read_only:
            create = False
        for attempt in range(2):
            try:
                if (
                    _collection_cache is not None
                    and _collection_cache_backend == backend_name
                    and _collection_cache_palace == _config.palace_path
                ):
                    _collection_open_error = None
                    return _collection_cache
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
                if _collection_cache is None:
                    from ..palace import get_collection as palace_get_collection

                    _collection_cache = palace_get_collection(
                        _config.palace_path,
                        collection_name=_config.collection_name,
                        create=create,
                        backend=backend_name,
                        read_only=collection_read_only,
                    )
                    _collection_cache_backend = backend_name
                    _collection_cache_palace = _config.palace_path
                    _collection_open_error = None
                    _invalidate_overview_caches()
                return _collection_cache
            except (BackendMismatchError, KeyError) as exc:
                logger.warning("backend open failed for %s: %s", _config.palace_path, exc)
                _collection_open_error = {
                    "error": "Backend mismatch"
                    if isinstance(exc, BackendMismatchError)
                    else "Unknown backend",
                    "details": str(exc),
                    "hint": "Select the matching backend or use a fresh palace directory.",
                }
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
                _invalidate_overview_caches()
                return None
            except Exception:
                logger.exception(
                    "_get_collection generic attempt %d/2 failed (palace=%s, create=%s)",
                    attempt + 1,
                    _config.palace_path,
                    create,
                )
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
                _invalidate_overview_caches()
                _collection_open_error = {
                    "error": "Backend open failed",
                    "details": "Could not open the selected backend collection.",
                    "hint": "Run: mempalace status or mempalace repair-status for diagnostics.",
                }
        return None
    return _get_collection_chroma(create=create)


def _get_collection_postgres(create=False):
    """Postgres-backed branch of ``_get_collection``.

    Caches a single PostgresBackend instance (lazily constructed on first
    call) plus the resolved ``PostgresCollection`` in ``_collection_cache``.
    The chromadb-specific ``_client_cache`` / ``_pin_hnsw_threads`` /
    ``_quarantine_*`` machinery is bypassed entirely — none of it
    applies to a postgres backend.

    Failure semantics differ from the chroma branch's stale-handle
    retry: postgres connection errors are surfaced clearly (no HNSW
    staleness class to heal), so we log + return None rather than retry.

    On failure, the exception type+message is captured in the module-level
    ``_last_backend_error`` slot so ``_no_palace()`` can distinguish a
    truly-uninitialised palace from a reachable-but-down backend (power-
    resilience design 2026-05-21).
    """
    global _collection_cache, _postgres_backend_cache, _metadata_cache, _metadata_cache_time
    global _last_backend_error
    try:
        if _postgres_backend_cache is None:
            from ..backends.postgres import PostgresBackend

            _postgres_backend_cache = PostgresBackend(dsn=_config.postgres_dsn)
            # When backend changes underneath us, drop the chroma-shaped cache.
            _collection_cache = None

        if _collection_cache is None or create:
            from ..backends.base import PalaceRef

            palace_ref = PalaceRef(
                id=_config.palace_path,
                local_path=_config.palace_path,
            )
            _collection_cache = _postgres_backend_cache.get_collection(
                palace=palace_ref,
                collection_name=_config.collection_name,
                create=create,
            )
            _metadata_cache = None
            _metadata_cache_time = 0

            if hasattr(_collection_cache, "set_kg_writethrough"):
                from ..palace import _maybe_attach_writethrough

                _maybe_attach_writethrough(_collection_cache, _config.postgres_dsn)

        _last_backend_error = None
        return _collection_cache
    except Exception as e:
        logger.exception(
            "_get_collection_postgres failed (palace=%s, create=%s, dsn-set=%s)",
            _config.palace_path,
            create,
            bool(_config.postgres_dsn),
        )
        _last_backend_error = {
            "type": type(e).__name__,
            "message": str(e),
            "ts": time.time(),
        }
        # Drop the cached backend so a re-imported PostgresBackend re-resolves
        # the DSN cleanly when postgres comes back. Without this, the failed
        # connection in PostgresBackend's pool keeps being reused.
        _postgres_backend_cache = None
        return None


def _check_local_palace_retired(palace_path: str) -> None:
    """Refuse to open the default local palace when a RETIRED marker exists.

    Recurring confusion source: an agent / CLI invocation that doesn't set
    ``PALACE_DAEMON_URL`` silently falls through to the default chroma
    palace at ``~/.mempalace/palace`` and reports a smaller, stale drawer
    count instead of erroring. When a user has retired their local
    palace in favor of a daemon-routed setup, they can drop a
    ``~/.mempalace/RETIRED`` text file. This check refuses to open ANY
    palace whose path resolves to the default location while that
    marker is present, and surfaces the marker's content as the error
    message.

    Escape hatch: ``MEMPALACE_ALLOW_RETIRED_PALACE=1`` lets forensic
    reads of the archived palace proceed. Useful when explicitly
    passing ``--palace <archived-dir>``.
    """
    if os.environ.get("MEMPALACE_ALLOW_RETIRED_PALACE", "").strip():
        return
    palace_root = os.path.expanduser("~/.mempalace")
    marker_path = os.path.join(palace_root, "RETIRED")
    if not os.path.exists(marker_path):
        return
    default_palace = os.path.join(palace_root, "palace")
    if os.path.abspath(palace_path) != os.path.abspath(default_palace):
        return  # explicit non-default path; let the caller through
    try:
        with open(marker_path) as f:
            note = f.read().strip()
    except OSError:
        note = ""
    raise RuntimeError(
        f"local palace at {default_palace} is retired (see "
        f"~/.mempalace/RETIRED).\n\n{note}\n\n"
        "Set MEMPALACE_ALLOW_RETIRED_PALACE=1 to override (forensic only)."
    )


def _get_collection_chroma(create=False):
    """ChromaDB-backed branch of ``_get_collection``.

    Preserved unchanged from the pre-routing implementation. Retry on
    failure + cache reset is the right behavior for ChromaDB's stale-
    handle and HNSW-corruption failure classes — see the original
    inline comments below.
    """
    global _client_cache, _collection_cache, _metadata_cache, _metadata_cache_time
    global _last_backend_error
    global _collection_cache_backend, _collection_cache_palace, _collection_open_error
    # Honor the ~/.mempalace/RETIRED marker — refuse to silently open a
    # default-path palace when the user has retired it. Surfaces a clear
    # error via _no_palace() instead of returning a stale drawer count.
    try:
        _check_local_palace_retired(_config.palace_path)
    except RuntimeError as e:
        _last_backend_error = {
            "type": "LocalPalaceRetired",
            "message": str(e),
            "ts": time.time(),
        }
        return None

    db_path = os.path.join(_config.palace_path, "chroma.sqlite3")
    if not create and not os.path.isfile(db_path):
        _force_chroma_cache_reset()
        _collection_open_error = {
            "error": "Chroma database missing",
            "details": f"Could not open missing database at {db_path}.",
            "hint": "Run: mempalace status or mempalace repair-status for diagnostics.",
        }
        return None

    for attempt in range(2):
        try:
            if _collection_cache is not None and (
                _collection_cache_backend not in (None, "chroma")
                or _collection_cache_palace not in (None, _config.palace_path)
            ):
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
            client = _get_client()
            # ChromaDB 1.x persists the EF *identity* (its ``name()``) with the
            # collection but not the EF *instance/configuration*. So a reader or
            # writer that omits ``embedding_function=`` silently gets chromadb's
            # built-in ``DefaultEmbeddingFunction`` — its ``name()`` matches the
            # one we spoof in ``mempalace.embedding`` (both report ``"default"``,
            # the identity check passes), but the *provider list* is chromadb's
            # default rather than the user's resolved device. On bleeding-edge
            # interpreters (#1299: python 3.14 + chromadb 1.5.x on Apple Silicon)
            # that default provider selection can SIGSEGV the host process on
            # first ``col.add()``. The miner / Stop hook ingest path avoids this
            # because it routes through ``ChromaBackend.get_collection``, which
            # resolves the EF via ``ChromaBackend._resolve_embedding_function``;
            # the MCP server bypassed that abstraction. Resolve the EF inside the
            # branches that actually open a collection so warm-cache reads stay
            # zero-cost. Reuse the backend helper so the two call sites can't
            # drift on logging or fallback semantics.
            if create:
                ef = ChromaBackend._resolve_embedding_function()
                ef_kwargs = {"embedding_function": ef} if ef is not None else {}
                # hnsw:num_threads=1 disables ChromaDB's multi-threaded ParallelFor
                # HNSW insert path, which has a race in repairConnectionsForUpdate /
                # addPoint (see issues #974, #965). Set via metadata on fresh
                # collections and re-applied via _pin_hnsw_threads() for legacy
                # palaces whose collections were created before this fix (the
                # runtime config does not persist cross-process in chromadb 1.5.x,
                # so the retrofit runs every time _get_collection opens a cache).
                #
                # ChromaDB 1.5.x's Rust binding SIGSEGVs when get_or_create_collection
                # is called with metadata that differs from what's stored. The split
                # below skips the metadata-comparison codepath for existing
                # collections, mirroring the backend-layer fix from #1262.
                try:
                    raw = client.get_collection(_config.collection_name, **ef_kwargs)
                except _ChromaNotFoundError:
                    raw = client.create_collection(
                        _config.collection_name,
                        metadata={
                            "hnsw:space": "cosine",
                            "hnsw:num_threads": 1,
                            **_HNSW_WRITE_DEFAULTS,
                        },
                        **ef_kwargs,
                    )
                _pin_hnsw_threads(raw)
                _collection_cache = ChromaCollection(
                    raw, palace_path=_config.palace_path, backend=_SessionFreshness
                )
                _restamp_palace_db()
                _collection_cache_backend = "chroma"
                _collection_cache_palace = _config.palace_path
                _collection_open_error = None
                _invalidate_overview_caches()
            elif _collection_cache is None:
                ef = ChromaBackend._resolve_embedding_function()
                ef_kwargs = {"embedding_function": ef} if ef is not None else {}
                raw = client.get_collection(_config.collection_name, **ef_kwargs)
                _pin_hnsw_threads(raw)
                _collection_cache = ChromaCollection(
                    raw, palace_path=_config.palace_path, backend=_SessionFreshness
                )
                _restamp_palace_db()
                _collection_cache_backend = "chroma"
                _collection_cache_palace = _config.palace_path
                _collection_open_error = None
                _invalidate_overview_caches()
            return _collection_cache
        except (BackendMismatchError, KeyError) as exc:
            _collection_open_error = {
                "error": "Backend mismatch"
                if isinstance(exc, BackendMismatchError)
                else "Unknown backend",
                "details": str(exc),
                "hint": "Select the matching backend or use a fresh palace directory.",
            }
            _client_cache = None
            _collection_cache = None
            _collection_cache_backend = None
            _collection_cache_palace = None
            _palace_db_inode = 0
            _palace_db_mtime = 0.0
            _invalidate_overview_caches()
            return None
        except Exception:
            logger.exception(
                "_get_collection attempt %d/2 failed (palace=%s, create=%s)",
                attempt + 1,
                _config.palace_path,
                create,
            )
            if attempt == 0:
                # Reset all caches so the next attempt forces _get_client()
                # to rebuild the chromadb client from scratch, reopening
                # the collection cleanly and healing the common
                # stale-handle case.
                _client_cache = None
                _collection_cache = None
                _collection_cache_backend = None
                _collection_cache_palace = None
                _palace_db_inode = 0
                _palace_db_mtime = 0.0
                _invalidate_overview_caches()
                _collection_open_error = {
                    "error": "Backend open failed",
                    "details": "Could not open the Chroma collection.",
                    "hint": "Run: mempalace repair-status for diagnostics.",
                }
    _client_cache = None
    _collection_cache = None
    _collection_cache_backend = None
    _collection_cache_palace = None
    _palace_db_inode = 0
    _palace_db_mtime = 0.0
    _invalidate_overview_caches()
    _collection_open_error = _collection_open_error or {
        "error": "Backend open failed",
        "details": "Could not open the selected backend collection.",
        "hint": "Run: mempalace status or mempalace repair-status for diagnostics.",
    }
    return None


def _no_palace():
    """Build a "no palace" response, distinguishing two failure modes.

    * **Backend unreachable** — the configured postgres DSN refused the
      connection (most commonly because the ``mempalace-db`` container is
      stopped). The fix is to start the backend, not to re-init the
      palace. This is the failure mode that bit JP for 3 days during the
      2026-05-17 power event.
    * **No palace configured** — the daemon is wired up and the backend
      responded, but the collection is empty / palace was never built.
      The fix is to ``mempalace init`` / ``mempalace mine``.

    The module-level ``_last_backend_error`` slot is set by
    ``_get_collection_postgres`` on connection failure; we inspect it
    here so the response carries the actual cause.
    """
    err = _last_backend_error
    if err and err.get("type") == "LocalPalaceRetired":
        # User has explicitly retired the local palace (RETIRED marker
        # under ~/.mempalace/). Tell them exactly what to do — usually
        # set PALACE_DAEMON_URL — rather than the legacy "init" hint.
        return {
            "error": "palace.local_retired",
            "message": err.get("message", "local palace retired"),
            "hint": "Set PALACE_DAEMON_URL to use the daemon, or MEMPALACE_ALLOW_RETIRED_PALACE=1 for forensic local reads.",
        }
    if err and err.get("type") == "OperationalError":
        return {
            "error": "palace.backend_unreachable",
            "message": f"daemon up, but its postgres backend is unreachable: {err.get('message', 'unknown')}",
            "hint": "Check: docker ps mempalace-db (and `cd /opt/mediaserver && docker compose start mempalace-db`)",
        }
    if err:
        # Other backend errors — surface the type+message so the user
        # sees something actionable instead of the misleading init hint.
        return {
            "error": "palace.backend_error",
            "message": f"{err.get('type', 'Error')}: {err.get('message', 'unknown')}",
            "hint": "Check daemon logs (sudo journalctl -u palace-daemon -n 50)",
        }
    return {
        "error": "No palace found",
        "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
    }


def _collection_error_or_no_palace():
    if not _collection_open_error:
        return _no_palace()
    result = dict(_collection_open_error)
    try:
        result["backend"] = _selected_backend_name()
    except Exception:
        pass
    return result


def _selected_backend_name() -> str:
    from ..palace import resolve_backend_name

    return resolve_backend_name(
        _config.palace_path,
        explicit=os.environ.get("MEMPALACE_BACKEND_EXPLICIT"),
    )


def _is_chroma_backend() -> bool:
    try:
        return _selected_backend_name() == "chroma"
    except Exception:
        logger.debug("backend resolution failed", exc_info=True)
        return False


def _backend_db_exists() -> bool:
    try:
        return detect_backend_for_path(_config.palace_path) is not None
    except Exception:
        logger.debug("backend artifact detection failed", exc_info=True)
        return False


# ==================== HELPERS ====================


def _safe_meta(meta):
    """Coerce a Chroma metadata value to a dict.

    ChromaDB's ``col.get()`` / ``col.query()`` can return ``None`` for the
    metadata cell of a partially-flushed row (or any row written without
    metadata in older formats). Indexing the result then yields ``None``,
    and downstream ``.get(...)`` calls raise::

        AttributeError: 'NoneType' object has no attribute 'get'

    This bug bricked the embeddings_queue cleanup path in issue #1426 —
    the handler crashed before reaching the ``DELETE FROM embeddings_queue``
    step, so the queue grew without bound while writes kept appearing
    successful.

    Centralizing the coercion through this helper makes the contract
    explicit and keeps the fix self-documenting at every call site:
    *metadata is always a dict by the time it leaves the boundary*.
    """
    return meta if isinstance(meta, dict) else {}


def _fetch_all_metadata(col, where=None):
    """Fetch every matching record's metadata via the backend's best strategy.

    Delegates to BaseCollection.get_all_metadata() (#1796), which Chroma
    satisfies with the same offset-paginated loop this function used to do
    inline, and which Qdrant overrides with a single _scroll_all() pass.
    Routing through one contract method means every backend gets its own
    correct strategy without this caller needing to know which backend it's
    talking to.
    """
    get_all = getattr(col, "get_all_metadata", None)
    if callable(get_all):
        return get_all(where=where)

    # Defensive fallback for any collection object that predates the
    # get_all_metadata() contract method (e.g. a third-party backend not yet
    # updated). Preserves the exact previous behavior.
    total = col.count()
    all_meta = []
    offset = 0
    while offset < total:
        kwargs = {"include": ["metadatas"], "limit": 1000, "offset": offset}
        if where:
            kwargs["where"] = where
        batch = col.get(**kwargs)
        if not batch["metadatas"]:
            break
        all_meta.extend(batch["metadatas"])
        offset += len(batch["metadatas"])
    return all_meta


def _supports_metadata_facets(col) -> bool:
    """Return True if the collection's backend implements metadata facets."""
    backend = getattr(col, "_backend", None)
    if backend is None:
        return False
    capabilities = getattr(backend, "capabilities", None)
    return isinstance(capabilities, (set, frozenset)) and "supports_metadata_facets" in capabilities


_metadata_cache = None
_metadata_cache_time = 0
_METADATA_CACHE_TTL = 5.0  # seconds
_taxonomy_cache = None
_taxonomy_cache_time = 0.0
_TAXONOMY_CACHE_TTL = 5.0  # seconds — same idea as the palace-graph cache
_graph_rows_cache = None
_MAX_RESULTS = 100  # upper bound for search/list limit params
_DIARY_READ_PAGE_SIZE = 1000

# IDF table cache for auto-tag extraction (#201). One entry per (wing, room).
# Built lazily on first auto-tagged write; refreshed when the TTL expires so a
# write-heavy session eventually picks up new vocabulary.
_idf_cache = None  # mempalace.tag_extraction.IdfCache, lazy init
_IDF_CORPUS_CAP = 2000  # snapshot at most this many docs when (re)building


def _get_idf_table(col, wing: str, room: str) -> dict:
    """Return the IDF weights for ``(wing, room)`` via the process-local cache.

    The corpus builder caps the snapshot at ``_IDF_CORPUS_CAP`` to keep
    rebuild latency bounded on large palaces — the IDF is a ranking aid,
    not a corpus inventory, and the top tokens stabilise long before
    the full corpus is read.
    """
    global _idf_cache
    if _idf_cache is None:
        from ..tag_extraction import IdfCache

        _idf_cache = IdfCache()

    def _corpus():
        try:
            batch = col.get(
                where={"$and": [{"wing": wing}, {"room": room}]},
                limit=_IDF_CORPUS_CAP,
                include=["documents"],
            )
        except Exception:
            logger.debug("IDF corpus fetch failed", exc_info=True)
            return []
        docs = (
            batch.get("documents") if isinstance(batch, dict) else getattr(batch, "documents", None)
        )
        return [d for d in (docs or []) if isinstance(d, str)]

    return _idf_cache.get(wing, room, _corpus)


def _invalidate_overview_caches():
    """Drop status/list_wings taxonomy and metadata page caches after writes."""
    global _metadata_cache, _metadata_cache_time, _taxonomy_cache, _taxonomy_cache_time
    global _graph_rows_cache
    _metadata_cache = None
    _metadata_cache_time = 0
    _taxonomy_cache = None
    _taxonomy_cache_time = 0.0
    _graph_rows_cache = None


def _palace_db_fingerprint():
    """A stat of chroma.sqlite3 that changes with every committed write, or None.

    chromadb keeps chroma.sqlite3 in rollback-journal mode (see
    ``backends.chroma``), so a commit from any process rewrites the main file.
    Counts grouped from the file cannot change while this value holds, which
    lets overview caches outlive their TTL on a palace nobody is writing to.
    ``None`` for other backends (sqlite_exact writes through a WAL, so the main
    file's stat would miss commits) and when the file cannot be stat'ed.
    """
    if not _is_chroma_backend():
        return None
    try:
        st = os.stat(os.path.join(_config.palace_path, "chroma.sqlite3"))
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _get_cached_metadata(col, where=None):
    """Return cached metadata if fresh, else fetch and cache."""
    global _metadata_cache, _metadata_cache_time
    if (
        where is None
        and _metadata_cache is not None
        and (time.time() - _metadata_cache_time) < _METADATA_CACHE_TTL
    ):
        return _metadata_cache
    result = _fetch_all_metadata(col, where=where)
    if where is None:
        _metadata_cache = result
        # Stamp once the fetch is done: stamped at the start, a fetch slower
        # than the TTL stored an entry that had already expired.
        _metadata_cache_time = time.time()
    return result


def _sanitize_optional_name(value: str = None, field_name: str = "name") -> str:
    """Validate optional wing/room-style filters."""
    if value is None or not value.strip():
        return None
    return sanitize_name(value, field_name)


def _resolve_room_alias(value: str = None) -> str:
    """Sanitize and resolve room aliases via config."""
    sanitized = _sanitize_optional_name(value, "room")
    if sanitized is None:
        return None
    return _config.resolve_room(sanitized)


# Bounds the whole stored source_file string (often an absolute path), so it is
# Linux PATH_MAX rather than the 128-char wing/room NAME limit.
_MAX_SOURCE_FILE_LENGTH = 4096


def _sanitize_optional_source_file(value: str = None) -> str:
    """Validate an optional source_file search filter (#1815).

    Unlike wing/room, a source_file is a path: ``/``, ``\\`` and ``.`` are
    legal, so it is NOT run through ``sanitize_name`` (which rejects path
    characters as traversal attempts). The value is matched verbatim as a
    ChromaDB metadata-equality / parameterized-SQL value — never used as a
    filesystem path — so there is no traversal risk to guard against. A null
    byte or a pathological length can still upset the backend (chromadb
    add/upsert chokes on null bytes / lone surrogates, #1235), so guard those
    for parity with ``sanitize_name``. Blank / whitespace-only is "no filter".
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("source_file must be a string")
    value = value.strip()
    if not value:
        return None
    if "\x00" in value:
        raise ValueError("source_file contains null bytes")
    if value != strip_lone_surrogates(value):
        raise ValueError("source_file contains invalid surrogate characters")
    if len(value) > _MAX_SOURCE_FILE_LENGTH:
        raise ValueError(
            f"source_file exceeds maximum length of {_MAX_SOURCE_FILE_LENGTH} characters"
        )
    return value


# The #1128 date-filter helpers moved to ``mempalace.date_window`` so the
# search-side window (#463) can share them without importing this module
# (whose import installs MCP stdio protection). Aliased under their
# historical private names — every call site and test keeps working.
_parse_date_filter = parse_date_bound
_filed_at_in_window = filed_at_in_window
