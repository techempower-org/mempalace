# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


_HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
_HUB_HEALTH_TIMEOUT_S = 0.75
# A backfill mine over a large transcript tree can legitimately run for many
# minutes inside the hub; the forwarder is a background/CLI process, not a
# hook-budgeted one, so it waits.
_HUB_MINE_TIMEOUT_S = 3600.0
_HUB_SEARCH_TIMEOUT_S = 600.0
_HUB_SEARCH_MAX_RESULTS = 100
_SEARCH_OVERRIDE_ENV_VARS = (
    "MEMPALACE_BACKEND",
    "MEMPALACE_BACKEND_EXPLICIT",
    "MEMPALACE_EMBEDDING_API_KEY",
    "MEMPALACE_EMBEDDING_API_MODEL",
    "MEMPALACE_EMBEDDING_API_URL",
    "MEMPALACE_EMBEDDING_DEVICE",
    "MEMPALACE_EMBEDDING_MODEL",
    "MEMPALACE_EMBEDDING_THREADS",
    "MEMPALACE_LANG",
    "MEMPAL_LANG",
    "MEMPALACE_MILVUS_CONSISTENCY_LEVEL",
    "MEMPALACE_MILVUS_DB_NAME",
    "MEMPALACE_MILVUS_NAMESPACE",
    "MEMPALACE_MILVUS_TOKEN",
    "MEMPALACE_MILVUS_URI",
    "MEMPALACE_PGVECTOR_DSN",
    "MEMPALACE_PGVECTOR_NAMESPACE",
    "MEMPALACE_QDRANT_API_KEY",
    "MEMPALACE_QDRANT_NAMESPACE",
    "MEMPALACE_QDRANT_TIMEOUT",
    "MEMPALACE_QDRANT_URL",
)


def _hub_forward_disabled() -> bool:
    return os.environ.get(_HUB_FORWARD_ENV, "").strip().lower() in {"0", "false", "no", "off"}


def _search_args_forwardable(args) -> bool:
    """Return whether ``mempalace_search`` preserves this CLI search exactly."""
    if _backend_arg(args) or not 1 <= args.results <= _HUB_SEARCH_MAX_RESULTS:
        return False
    if any(os.environ.get(name, "").strip() for name in _SEARCH_OVERRIDE_ENV_VARS):
        return False

    # The MCP tool sanitizes queries longer than its safe passthrough window.
    # Keep any query it would rewrite on the direct CLI path so forwarding
    # never changes the user's search text silently.
    from ..config import sanitize_name, strip_lone_surrogates
    from ..query_sanitizer import SAFE_QUERY_LENGTH

    cleaned = strip_lone_surrogates(args.query.strip())
    if cleaned != args.query or len(cleaned) > SAFE_QUERY_LENGTH:
        return False

    for field_name in ("wing", "room"):
        value = getattr(args, field_name, None)
        if value is None:
            continue
        try:
            if sanitize_name(value, field_name) != value:
                return False
        except ValueError:
            return False
    return True


def _print_hub_search_result(args, result: dict) -> bool:
    """Render an MCP search result using the CLI's human-readable shape."""
    if not isinstance(result, dict):
        return False

    error = result.get("error")
    if error:
        details = result.get("details")
        message = f"{error}: {details}" if details else str(error)
        print(f"mempalace: hub search failed: {message}", file=sys.stderr)
        return False

    cli_output = result.get("cli_output")
    if isinstance(cli_output, str):
        cli_error_output = result.get("cli_error_output")
        if isinstance(cli_error_output, str):
            print(cli_error_output, end="", file=sys.stderr)
        print(cli_output, end="")
        return True

    hits = result.get("results")
    if not isinstance(hits, list):
        return False

    if result.get("vector_disabled") or result.get("fallback") == "bm25_only_via_sqlite":
        print(
            "\n  NOTICE: vector search disabled — showing BM25-only results.\n"
            "          Run `mempalace repair` to restore vector search.\n"
        )

    if not hits:
        print(f'\n  No results found for: "{args.query}"')
        return True

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{args.query}"')
    if args.wing:
        print(f"  Wing: {args.wing}")
    if args.room:
        print(f"  Room: {args.room}")
    if args.since:
        print(f"  Since: {args.since}")
    if args.before:
        print(f"  Before: {args.before}")
    print(f"{'=' * 60}\n")

    for index, hit in enumerate(hits, 1):
        wing = hit.get("wing", "?")
        room = hit.get("room", "?")
        source = hit.get("source_file", "?")
        similarity = hit.get("similarity")
        bm25 = hit.get("bm25_score", 0.0)

        print(f"  [{index}] {wing} / {room}")
        print(f"      Source: {source}")
        if similarity is None:
            print(f"      Match:  bm25={bm25}  (vector disabled)")
        else:
            print(f"      Match:  similarity={similarity}  bm25={bm25}")
        print()
        for line in (hit.get("text") or "").strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'-' * 56}")

    print()
    return True


def _forward_search_to_hub(args, palace_path: str) -> bool:
    """Run a CLI search in the palace's HTTP hub, if one is alive.

    A local Chroma search cold-loads the palace's full HNSW index. Reusing the
    long-lived hub prevents every shell command or agent worker from holding a
    private copy. If the hub accepts the request but fails mid-flight, do not
    fall back locally: that would recreate the memory spike this path avoids.
    """
    import json
    import urllib.error
    import urllib.request

    from .. import server_registry

    if _hub_forward_disabled():
        return False
    info = server_registry.read_live_serverinfo(palace_path)
    if not info:
        return False
    if "search_cli_compatible" not in info.get("capabilities", []):
        return False
    current_config = MempalaceConfig(palace_path=palace_path)
    if info.get("search_config_fingerprint") != current_config.search_config_fingerprint:
        return False

    base_url = server_registry.client_base_url(info)
    headers = {"Content-Type": "application/json"}

    try:
        health = urllib.request.Request(f"{base_url}/healthz", headers=headers)
        with urllib.request.urlopen(health, timeout=_HUB_HEALTH_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
    except (urllib.error.URLError, OSError, ValueError):
        return False

    arguments = {
        "query": args.query,
        "limit": args.results,
        "cli_compatible": True,
    }
    for name in ("wing", "room", "since", "before"):
        value = getattr(args, name, None)
        if value:
            arguments[name] = value

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mempalace_search", "arguments": arguments},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        with server_registry.urlopen_with_server_tokens(
            palace_path,
            f"{base_url}/mcp",
            data=body,
            headers=headers,
            timeout=_HUB_SEARCH_TIMEOUT_S,
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Authentication failed before the Hub accepted the search, so
            # direct execution is still safe. This covers Hubs started with
            # an explicit/env token that is intentionally not persisted in
            # the per-palace token file, as well as a stale local token.
            return False
        print(f"mempalace: hub rejected search ({exc.code} {exc.reason})", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        print(
            f"mempalace: hub at {base_url} did not complete the search ({exc}); "
            "not retrying directly because that would load another full index. "
            f"Set {_HUB_FORWARD_ENV}=0 to force a direct search.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"mempalace: forwarding search to palace hub {base_url} (pid {info.get('pid')})",
        file=sys.stderr,
    )

    if payload.get("error"):
        err = payload["error"]
        print(
            f"mempalace: hub refused search: {err.get('message', 'unknown error')}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = json.loads(payload["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        print("mempalace: hub returned an unrecognized search response", file=sys.stderr)
        sys.exit(1)

    if not _print_hub_search_result(args, result):
        sys.exit(1)
    return True


def _mine_args_forwardable(args, include_ignored) -> bool:
    """Only forward mines the ``mempalace_mine`` MCP tool can express.

    Flags the tool has no parameters for (kg-extract, disabling gitignore,
    chunking overrides, origin redetection, explicit backend) keep the
    direct path — where a held writer lease still surfaces as the existing
    MineAlreadyRunning error rather than being silently dropped.
    """
    if getattr(args, "kg_extract", False) or getattr(args, "redetect_origin", False):
        return False
    if args.no_gitignore:
        return False
    if include_ignored and (getattr(args, "mode", None) or "projects") != "projects":
        return False
    if getattr(args, "max_chunks_per_file", None) is not None:
        return False
    if _backend_arg(args):
        return False
    return True


def _forward_mine_to_hub(args, palace_path: str, *, include_ignored=None) -> bool:
    """Run this mine inside the palace's HTTP hub, if one is alive.

    A long-lived hub (``mempalace serve``) holds the MCP writer lease for
    its whole lifetime, so a direct mine from this process — including the
    background save hooks, which spawn exactly this CLI — would be refused
    and transcript capture would silently stop on the hub machine. The hub
    itself may mine (the palace lock is process-re-entrant there, #1859),
    so the fix is to hand it the job over HTTP.

    Returns True when the hub handled the mine (this function has already
    printed the outcome and exited non-zero on failure). Returns False when
    there is no usable hub — the caller proceeds with the direct path.
    Once the hub has accepted the request there is no fallback: the job may
    already be running, and re-mining directly would race it.
    """
    import json
    import urllib.error
    import urllib.request

    from .. import server_registry

    if _hub_forward_disabled():
        return False
    info = server_registry.read_live_serverinfo(palace_path)
    if not info or info.get("read_only"):
        return False

    base_url = server_registry.client_base_url(info)
    headers = {"Content-Type": "application/json"}

    try:
        health = urllib.request.Request(f"{base_url}/healthz", headers=headers)
        with urllib.request.urlopen(health, timeout=_HUB_HEALTH_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
    except (urllib.error.URLError, OSError, ValueError):
        return False

    # Older hubs cannot express this override. Never discard it or compete
    # with their lifetime writer lease by silently opening a local writer.
    if include_ignored and "mine_include_ignored" not in (info.get("capabilities") or []):
        print(
            f"mempalace: palace hub {base_url} (pid {info.get('pid')}) does not support "
            "--include-ignored; upgrade MemPalace in the hub's environment and restart "
            "the hub before retrying. No mine was submitted or run locally.",
            file=sys.stderr,
        )
        sys.exit(1)

    arguments = {
        "source": os.path.abspath(os.path.expanduser(args.dir)),
        # Normalized, never the raw attribute: --mode defaults to None
        # upstream, and a null here reaches the hub's own validator
        # the same way it reached the daemon's in #525.
        "mode": getattr(args, "mode", None) or "projects",
        "agent": args.agent,
        "limit": args.limit or 0,
        "dry_run": bool(args.dry_run),
        "extract": args.extract,
    }
    if args.wing:
        arguments["wing"] = args.wing
    if include_ignored:
        arguments["include_ignored"] = include_ignored
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mempalace_mine", "arguments": arguments},
        }
    ).encode("utf-8")

    print(f"mempalace: forwarding mine to palace hub {base_url} (pid {info.get('pid')})")
    try:
        with server_registry.urlopen_with_server_tokens(
            palace_path,
            f"{base_url}/mcp",
            data=body,
            headers=headers,
            timeout=_HUB_MINE_TIMEOUT_S,
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The hub answered — the request reached it, so no direct fallback.
        print(f"mempalace: hub rejected mine ({exc.code} {exc.reason})", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(
            f"mempalace: hub at {base_url} did not complete the mine ({exc}); "
            "not retrying directly — the hub may still be running the job. "
            f"Set {_HUB_FORWARD_ENV}=0 to force direct mines.",
            file=sys.stderr,
        )
        sys.exit(1)

    if payload.get("error"):
        err = payload["error"]
        print(
            f"mempalace: hub refused mine: {err.get('message', 'unknown error')}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        result = json.loads(payload["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        print("mempalace: hub returned an unrecognized mine response", file=sys.stderr)
        sys.exit(1)

    output = result.get("output")
    if output:
        print(output)
    if not result.get("success", False):
        print(f"mempalace: hub mine failed: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
    return True


def _mine_via_adapter(args) -> None:
    """Route ``mempalace mine --source <adapter>`` through the adapter plugin contract.

    Constructs a :class:`PalaceContext`, calls :meth:`BaseSourceAdapter.ingest`,
    and upserts every yielded :class:`DrawerRecord` into the palace. This is
    the CLI-side glue between the source-adapter subsystem and the existing
    mine command — ``cmd_mine`` delegates here when ``--source`` is present.
    """
    from ..config import normalize_wing_name
    from ..sources import (
        DrawerRecord,
        PalaceContext,
        SourceItemMetadata,
        SourceRef,
        available_adapters,
        get_adapter,
    )

    adapter_name = args.source

    # ``--source list`` is a convenience alias: show installed adapters and exit.
    if adapter_name == "list":
        names = available_adapters()
        if names:
            print("Installed source adapters:")
            for name in names:
                print(f"  {name}")
        else:
            print("No source adapters installed.")
        return

    try:
        adapter = get_adapter(adapter_name)
    except KeyError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    directory = os.path.abspath(os.path.expanduser(args.dir))
    wing = args.wing or normalize_wing_name(Path(directory).name)
    agent = getattr(args, "agent", "mempalace")
    dry_run = getattr(args, "dry_run", False)
    limit = getattr(args, "limit", 0)

    source = SourceRef(
        local_path=directory,
        options={
            "wing": wing,
            "agent": agent,
            "limit": limit,
        },
    )

    if dry_run:
        print(f"\n  DRY RUN: would mine {directory} via adapter '{adapter_name}'")
        print(f"  Wing: {wing}")
        summary = adapter.source_summary(source=source)
        if summary.item_count is not None:
            print(f"  Items: {summary.item_count}")
        print(f"  Description: {summary.description}")
        print()
        return

    # Build palace context — open the drawer collection and knowledge graph.
    from ..knowledge_graph import KnowledgeGraph
    from ..palace import get_collection

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    try:
        collection = get_collection(palace_path, create=True)
    except Exception as e:
        print(f"  ERROR: cannot open palace at {palace_path}: {e}", file=sys.stderr)
        sys.exit(1)

    kg_path = os.path.join(palace_path, ".mempalace", "knowledge_graph.sqlite3")
    os.makedirs(os.path.dirname(kg_path), exist_ok=True)
    kg = KnowledgeGraph(db_path=kg_path)

    palace_ctx = PalaceContext(
        drawer_collection=collection,
        knowledge_graph=kg,
        palace_path=palace_path,
        adapter_name=adapter_name,
        adapter_version=adapter.adapter_version,
    )

    print(f"\n  Mining {directory} via adapter '{adapter_name}' (v{adapter.adapter_version})")
    print(f"  Wing: {wing}")

    drawer_count = 0
    item_count = 0

    try:
        for result in adapter.ingest(source=source, palace=palace_ctx):
            if isinstance(result, SourceItemMetadata):
                item_count += 1
                # Check incremental: skip if palace already has current version
                if adapter.is_current(item=result, existing_metadata=None):
                    palace_ctx.skip_current_item()
                continue

            if isinstance(result, DrawerRecord):
                if palace_ctx.is_skip_requested():
                    continue

                # Apply route hint: prefer adapter-supplied wing/room, fall
                # back to CLI-specified wing and a default room.
                meta = dict(result.metadata)
                hint = result.route_hint
                meta["wing"] = hint.wing if hint and hint.wing else wing
                meta["room"] = hint.room if hint and hint.room else "general"
                meta["agent"] = agent
                meta["source_file"] = result.source_file

                enriched = DrawerRecord(
                    content=result.content,
                    source_file=result.source_file,
                    chunk_index=result.chunk_index,
                    metadata=meta,
                    route_hint=result.route_hint,
                )
                palace_ctx.upsert_drawer(enriched)
                drawer_count += 1
    except KeyboardInterrupt:
        print(f"\n  Interrupted. Filed {drawer_count} drawers from {item_count} items.")
        sys.exit(130)
    finally:
        try:
            kg.close()
        except Exception:
            pass

    print(f"  Filed {drawer_count} drawers from {item_count} items.\n")


def _derive_daemon_mine_wing(directory: str, raw_arg: str) -> str:
    """Wing for a daemon-strict mine when ``--wing`` was omitted.

    Matches local-mine semantics: a directory is its own wing, and a single
    file takes its PROJECT's wing rather than its own name (#451) — so
    ``~/Projects/2g/CLAUDE.md`` is wing ``2g``.

    The catch is that this runs on the CLIENT while the daemon mines ITS
    host's copy, which may exist where ours does not (synced, or remapped by
    ``PALACE_DAEMON_PATH_MAP``). A path we cannot classify locally is not
    "wrong", it is unknown — and the two rules disagree about it.

    The disagreement is one-sided, which is what makes this tractable:

    * for a DIRECTORY the rule is "basename", which is right whether or not
      we can see it. ``/home/u/proj`` is wing ``proj`` from anywhere.
    * for a FILE the wing comes from somewhere else entirely (its project
      root), so falling back to "basename" yields the FILENAME: measured,
      ``~/Projects/2g/CLAUDE.md`` lands in a wing called ``claude.md``.

    So only a file misfiles, and this refuses exactly when the unresolvable
    path looks like a document — a suffix the miner reads as text. Anything
    else keeps the historic directory rule, which
    ``tests/test_cli_daemon.py::TestCmdMineDaemon::test_routes_projects_mode_to_daemon``
    has pinned since before single-file mining existed: a remote-only path,
    no ``--wing``, exit 0.

    Residual hole, stated rather than papered over: a remote-only file with
    NO suffix (``Makefile``, ``CHANGELOG``) is indistinguishable from a
    directory here and still takes the basename rule. Pass ``--wing`` for
    those. Closing it would mean refusing every unresolvable path, which
    breaks the contract above.
    """
    from ..config import normalize_wing_name

    wing_source = Path(directory)
    if wing_source.is_file():
        from ..miner import resolve_project_root

        wing_source = resolve_project_root(wing_source)
    elif not wing_source.is_dir() and _looks_like_a_document(wing_source):
        print(
            f"mempalace: cannot derive a wing for {raw_arg} — it looks like a document but "
            "does not exist on THIS machine, so its project (which is where a file's wing "
            "comes from) cannot be resolved here. Deriving the wing from the path would "
            f"file it under {normalize_wing_name(wing_source.name)!r}. Pass --wing <slug>.",
            file=sys.stderr,
        )
        sys.exit(2)
    return normalize_wing_name(wing_source.name)


def _looks_like_a_document(path: Path) -> bool:
    """True when ``path``'s suffix is one the project miner reads as text.

    Only used to decide whether an unresolvable path is file-shaped enough to
    refuse a guessed wing for. Reuses the miner's own whitelist so the answer
    tracks what mining actually accepts.
    """
    try:
        from ..miner import READABLE_EXTENSIONS
    except Exception:  # pragma: no cover — miner import is not optional in practice
        return bool(path.suffix)
    return path.suffix.lower() in READABLE_EXTENSIONS
