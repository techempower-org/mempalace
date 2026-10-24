# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_mine(args):
    from ..palace import MineAlreadyRunning, MineValidationError

    # Upstream v3.8 made --mode default to None so an unset flag can inherit
    # a per-source default; normalize once here since every branch below (and
    # the daemon payload) reads it.
    mode = getattr(args, "mode", None) or "projects"
    # --source flag: route through the adapter plugin contract. Upstream
    # #2062 formalized this dispatch (RFC 002 lineage) with typed exits —
    # unknown adapter/protocol → 2, palace contention → 1 — so cmd_mine
    # uses upstream's mine_source_adapter; the fork's earlier
    # _mine_via_adapter glue remains only for its direct-call tests.
    source_adapter = getattr(args, "source", None)
    if source_adapter:
        palace_path = (
            os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
        )
        try:
            drawers_written = mine_source_adapter(
                source_name=source_adapter,
                source_path=args.dir,
                palace_path=palace_path,
                dry_run=args.dry_run,
            )
        except (UnknownSourceAdapterError, UnsupportedSourceAdapterProtocolError) as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(2)
        except MineAlreadyRunning as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(1)
        suffix = " would be written" if args.dry_run else " written"
        print(f"  Source adapter {source_adapter!r}: {drawers_written} drawer(s){suffix}.")
        return

    # A .jsonl handed to projects mode is a mode mistake, not a mine: projects
    # mode would chunk raw JSON as prose, and until #451 made projects mode
    # accept a file at all it was silently a no-op (scan_project walks a file
    # and finds nothing). Name the mode that does work and exit 2, the same
    # usage-error code the --source dispatch above uses. A DIRECTORY whose
    # name happens to end in .jsonl is still a directory mine.
    _mine_target = Path(os.path.expanduser(args.dir))
    if mode == "projects" and _mine_target.suffix.lower() == ".jsonl" and not _mine_target.is_dir():
        print(
            f"mempalace: {args.dir} is a .jsonl transcript; projects mode files it as "
            "prose, not as exchanges. Use --mode convos (one drawer per exchange) "
            "or --mode session (one manifest drawer per file).",
            file=sys.stderr,
        )
        sys.exit(2)

    # Two different daemons can take this mine (both can take it off this terminal's hands):
    #  * upstream's local job-queue daemon, selected by the CLI write-routing policy (#2033): --daemon / --direct,
    #    or MEMPALACE_CLI_WRITE_ROUTING=prefer|require. An explicit flag or policy for it wins.
    #  * the fork's palace daemon over HTTP (PALACE_DAEMON_URL, daemon-strict), below.
    # Upstream's resolver rejects --background on a direct route, but the palace-daemon route accepts it (#456),
    # so with that route active and no --daemon, --background is left for the palace-daemon path.
    routes_to_palace_daemon = _daemon_strict() and not args.palace
    background = bool(getattr(args, "background", False))
    if routes_to_palace_daemon and background and not getattr(args, "daemon", False):
        routing = None
    else:
        routing = _resolve_cli_write_routing_or_exit(args, "mine")
    if routing is not None and routing.use_daemon:
        include_ignored = []
        for raw in args.include_ignored or []:
            include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())
        payload = {
            # Absolute, so the daemon resolves it against the caller's cwd, not its own (upstream #2467).
            "source": os.path.abspath(os.path.expanduser(args.dir)),
            "mode": mode,
            "wing": args.wing,
            "agent": args.agent,
            "limit": args.limit,
            "dry_run": args.dry_run,
            "extract": args.extract,
            "no_gitignore": args.no_gitignore,
            "include_ignored": include_ignored,
            "max_chunks_per_file": getattr(args, "max_chunks_per_file", None),
            "redetect_origin": getattr(args, "redetect_origin", False),
        }
        if source_adapter:
            payload["source_adapter"] = source_adapter
        if getattr(args, "no_tunnels", False):
            # The job-queue daemon's mine payload has no tunnels field, and
            # service.execute_job calls mine() without compute_derived, so
            # the flag would be accepted here and the expensive derived-graph
            # rebuild would run anyway. This branch returns before the
            # daemon-strict warning below, so it needs its own (#474).
            print(
                "  WARN: --daemon ignores --no-tunnels: the job-queue mine payload "
                "carries no tunnels field, so the derived-graph rebuild will still run",
                file=sys.stderr,
            )
        _submit_daemon_cli_job(
            "mine", payload, args, background=background, auto_start=routing.decision.auto_start_daemon
        )
        return

    if _daemon_strict() and not args.palace:
        # Daemon-strict: route to /mine. The daemon owns the canonical
        # palace and its filesystem layout, and translates client-side
        # paths via PALACE_DAEMON_PATH_MAP. Flags that only make sense
        # on the local FS (--redetect-origin, --include-ignored,
        # --no-gitignore, --dry-run) are warned about but not passed —
        # the daemon's /mine endpoint does not expose them.
        ignored_flags = []
        if getattr(args, "redetect_origin", False):
            ignored_flags.append("--redetect-origin")
        if getattr(args, "dry_run", False):
            ignored_flags.append("--dry-run")
        if getattr(args, "no_gitignore", False):
            ignored_flags.append("--no-gitignore")
        if getattr(args, "include_ignored", None):
            ignored_flags.append("--include-ignored")
        if getattr(args, "no_tunnels", False):
            # The daemon's /mine body has no `tunnels` field yet
            # (palace-daemon#262). Warn rather than accept the flag and
            # quietly do the expensive thing anyway — a skip the operator
            # asked for and did not get is worse than one they were told
            # about. Drop this branch when the daemon forwards it.
            ignored_flags.append("--no-tunnels")
        if ignored_flags:
            print(
                f"  WARN: daemon-strict mode ignores these local-only flags: {', '.join(ignored_flags)}",
                file=sys.stderr,
            )

        directory = os.path.abspath(os.path.expanduser(args.dir))
        wing = args.wing or _derive_daemon_mine_wing(directory, args.dir)
        ok = _post_daemon_mine_cli(
            directory,
            wing=wing,
            # `mode`, not `args.mode`: upstream v3.8 made --mode default to
            # None and this function normalized it at the top for exactly
            # this reason. Passing the raw attribute put `"mode": null` on
            # the wire and the daemon's MineBody rejected every daemon-strict
            # mine with 422 body.mode (#525).
            mode=mode,
            background=getattr(args, "background", False),
        )
        sys.exit(0 if ok else 1)

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    include_ignored = []
    for raw in args.include_ignored or []:
        include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())

    # A live HTTP hub for this palace holds the MCP writer lease, so a
    # direct mine here would be refused. Hand the job to the hub instead —
    # this is how the save hooks keep capturing transcripts on a machine
    # that runs `mempalace serve`.
    if _mine_args_forwardable(args, include_ignored) and _forward_mine_to_hub(
        args, palace_path, include_ignored=include_ignored
    ):
        return

    # --redetect-origin re-runs corpus_origin on the current corpus state
    # and overwrites <palace>/.mempalace/origin.json before mining proceeds.
    # Heuristic-only by design — full LLM detection lives on `mempalace init`.
    if getattr(args, "redetect_origin", False):
        _run_pass_zero(
            project_dir=args.dir,
            palace_dir=palace_path,
            llm_provider=None,
        )

    try:
        if mode == "session":
            # hybrid-search-taxonomy follow-up: per-session manifest mode.
            # One addressable drawer per session file (vs convos mode's
            # N chunked drawers). Use when you want a single navigable
            # anchor for "did session X exist? what did it cover?" —
            # complements convos mode, doesn't replace it.
            from ..convo_miner import mine_sessions

            mine_sessions(
                convo_dir=args.dir,
                palace_path=palace_path,
                wing=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        elif mode == "convos":
            from ..convo_miner import mine_convos

            mine_convos(
                convo_dir=args.dir,
                palace_path=palace_path,
                wing=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
                extract_mode=args.extract,
                include_subagents=getattr(args, "include_subagents", False),
            )
        elif mode == "extract":
            from ..format_miner import mine_formats

            mine_formats(
                format_dir=args.dir,
                palace_path=palace_path,
                wing=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        else:
            from ..miner import mine

            mine(
                project_dir=args.dir,
                palace_path=palace_path,
                wing_override=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
                respect_gitignore=not args.no_gitignore,
                include_ignored=include_ignored,
                max_chunks_per_file=getattr(args, "max_chunks_per_file", None),
                workers=getattr(args, "workers", 1),
                compute_derived=not getattr(args, "no_tunnels", False),
            )
    except MineAlreadyRunning as exc:
        # A live MCP server or another mine is already writing to this
        # palace. Surface the holder identity so the operator knows what
        # to wait for (or stop), and exit non-zero so wrappers like
        # nohup / scripts can detect the contention.
        print(f"mempalace: {exc}", file=sys.stderr)
        sys.exit(1)
    except MineValidationError as exc:
        # PRAGMA quick_check on chroma.sqlite3 returned errors at end of mine.
        # The corruption may pre-date the mine; we surface it here so automation
        # cannot proceed against a half-broken palace. Reuse cmd_repair's
        # recovery banner so the operator sees one consistent message regardless
        # of which command surfaces it.
        from ..repair import print_sqlite_integrity_abort

        print_sqlite_integrity_abort(exc.palace_path, exc.errors)
        print(
            "\n  PRAGMA quick_check after this mine reported errors (the corruption\n"
            "  may pre-date the mine itself). Drawers may still be intact for direct\n"
            "  lookup; wing-filtered or full-text search will fail until the FTS5\n"
            "  index is rebuilt. `mempalace repair --yes` rebuilds the FTS5 virtual\n"
            "  table automatically (step 6 of the recovery above).",
            file=sys.stderr,
        )
        sys.exit(1)


class UnknownSourceAdapterError(ValueError):
    """Raised when an explicit ``--source`` name is absent from the registry."""


class UnsupportedSourceAdapterProtocolError(ValueError):
    """Raised when an adapter requires runner semantics not implemented yet."""


class _DryRunCollectionProxy:
    """Empty collection facade that records, but never persists, writes.

    Source adapters are deliberately allowed to access ``drawer_collection``
    directly.  A dry run must not open the real backend: even read-only-looking
    opens can create or repair backend artifacts (for example SQLite WAL files).
    """

    def __init__(self):
        self.operations = []

    def add(self, **kwargs):
        self.operations.append(("add", kwargs))

    def upsert(self, **kwargs):
        self.operations.append(("upsert", kwargs))

    def delete(self, **kwargs):
        self.operations.append(("delete", kwargs))

    def update(self, **kwargs):
        self.operations.append(("update", kwargs))

    def query(self, **kwargs):
        from ..backends import QueryResult

        query_input = kwargs.get("query_texts", kwargs.get("query_embeddings"))
        num_queries = len(query_input) if isinstance(query_input, (list, tuple)) else 1
        include = kwargs.get("include") or []
        return QueryResult.empty(
            num_queries=num_queries,
            embeddings_requested="embeddings" in include,
        )

    def get(self, **kwargs):
        from ..backends import GetResult

        return GetResult.empty()

    def count(self):
        return 0


class _DryRunKnowledgeGraphProxy:
    """Recording no-op facade for the KG mutation surface published to adapters."""

    def __init__(self):
        self.operations = []

    def add_entity(self, *args, **kwargs):
        self.operations.append(("add_entity", args, kwargs))

    def add_triple(self, *args, **kwargs):
        self.operations.append(("add_triple", args, kwargs))

    def invalidate(self, *args, **kwargs):
        self.operations.append(("invalidate", args, kwargs))

    def supersede(self, *args, **kwargs):
        self.operations.append(("supersede", args, kwargs))


def mine_source_adapter(
    *,
    source_name: str,
    source_path: str,
    palace_path: str,
    dry_run: bool = False,
) -> int:
    """Run an explicitly selected RFC 002 source adapter through ``PalaceContext``.

    This deliberately sits alongside, rather than inside, the legacy mode
    miners.  Until those miners are migrated to first-party adapters, no-flag
    and ``--mode`` calls must retain their established dispatch paths.
    """
    from ..knowledge_graph import KnowledgeGraph
    from ..palace import get_collection, mine_palace_lock
    from ..sources import (
        DrawerRecord,
        PalaceContext,
        SourceRef,
        SourceItemMetadata,
        get_adapter,
        resolve_adapter_for_source,
    )

    adapter_name = resolve_adapter_for_source(explicit=source_name)
    try:
        adapter = get_adapter(adapter_name)
    except KeyError as exc:
        raise UnknownSourceAdapterError(
            f"unknown source adapter {adapter_name!r}; install its adapter package or "
            "check the adapter name with `mempalace mine --help`"
        ) from exc

    if "supports_incremental" in adapter.capabilities:
        raise UnsupportedSourceAdapterProtocolError(
            f"source adapter {adapter_name!r} requires incremental ingestion, which "
            "mempalace mine does not support yet"
        )

    # A dry run must never open a collection: backend opens can create or
    # repair storage even when requested as read-only.  Non-dry runs hold one
    # writer lease from handle creation through adapter iteration, including
    # direct KG mutations by adapters.
    lock = mine_palace_lock(palace_path) if not dry_run else contextlib.nullcontext()
    with lock:
        knowledge_graph = None
        try:
            if dry_run:
                drawer_collection = _DryRunCollectionProxy()
                knowledge_graph = _DryRunKnowledgeGraphProxy()
            else:
                drawer_collection = get_collection(palace_path)
                knowledge_graph = KnowledgeGraph(
                    db_path=os.path.join(palace_path, "knowledge_graph.sqlite3")
                )
            context = PalaceContext(
                drawer_collection=drawer_collection,
                knowledge_graph=knowledge_graph,
                palace_path=palace_path,
                config=MempalaceConfig(palace_path=palace_path),
                adapter_name=adapter.name,
                adapter_version=adapter.adapter_version,
            )
            drawers_written = 0
            for result in adapter.ingest(
                source=SourceRef(local_path=source_path),
                palace=context,
            ):
                if isinstance(result, SourceItemMetadata):
                    # Non-incremental adapters may report a cursor or version
                    # while still doing a complete re-extract.  Incremental
                    # adapters are rejected before ingest above, so accepting
                    # this avoids a late partial-ingest failure.
                    warnings.warn(
                        f"Source adapter {adapter_name!r} yielded non-incremental item "
                        "metadata; ignoring it during complete ingest",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue
                if isinstance(result, DrawerRecord):
                    drawers_written += 1
                    context.upsert_drawer(result)
                    continue
                raise TypeError(
                    f"source adapter {adapter_name!r} yielded unsupported result type "
                    f"{type(result).__name__}"
                )
            return drawers_written
        finally:
            if knowledge_graph is not None and hasattr(knowledge_graph, "close"):
                knowledge_graph.close()


def cmd_sweep(args):
    """Sweep a transcript file or directory.

    The sweeper deduplicates against its own prior writes via
    deterministic drawer IDs + a timestamp cursor. It does NOT currently
    coordinate with the file-level miners (miner.py / convo_miner.py) —
    those produce char-chunked drawers without compatible message
    metadata, so running both miners may store overlapping content under
    different IDs.
    """
    from ..sweeper import sweep, sweep_directory

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    # Resolved here, the way the direct route reads it: the daemon keeps the
    # cwd it was started in. A file keeps its own name, which becomes its
    # drawers' source_file as `sweep <dir>` would file it, so only the
    # directories above it are resolved. A blank target is passed on as typed,
    # as before: resolving "" (or spaces, on Windows) would give the cwd.
    target = os.path.expanduser(args.target)
    if target.strip():
        head, tail = os.path.split(target)
        if tail in ("", os.curdir, os.pardir):
            target = os.path.realpath(target)
        else:
            target = os.path.join(os.path.realpath(head or os.curdir), tail)

    routing = _resolve_cli_write_routing_or_exit(
        args,
        "sweep",
    )
    if routing.use_daemon:
        _submit_daemon_cli_job(
            "sweep",
            {"target": target},
            args,
            background=bool(getattr(args, "background", False)),
            auto_start=routing.decision.auto_start_daemon,
        )
        return
    if os.path.isfile(target):
        result = sweep(target, palace_path)
        print(
            f"  Swept {target}: +{result['drawers_added']} new, "
            f"{result['drawers_already_present']} already present, "
            f"{result['drawers_skipped']} skipped (< cursor)."
        )
    elif os.path.isdir(target):
        result = sweep_directory(target, palace_path)
        print(
            f"  Swept {result['files_succeeded']}/{result['files_attempted']} "
            f"files from {target}: +{result['drawers_added']} new, "
            f"{result['drawers_already_present']} already present, "
            f"{result['drawers_skipped']} skipped (< cursor)."
        )
        failures = result.get("failures") or []
        if failures:
            print(
                f"  WARNING: {len(failures)} file(s) failed to sweep - see stderr / logs for details.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        print(f"  ERROR: Not a file or directory: {target}", file=sys.stderr)
        sys.exit(1)


def _print_sync_report(report: dict, *, dry_run: bool) -> None:
    """Render a sync report. Shared by the local and daemon-routed paths."""
    removed_suffix = "(would remove)" if dry_run else "(removed)"
    print(f"  Scanned:        {report['scanned']}")
    print(f"  Kept:           {report['kept']}")
    print(f"  Gitignored:     {report['gitignored']}  {removed_suffix}")
    print(f"  Missing:        {report['missing']}  {removed_suffix}")
    print(f"  Unresolved:     {report['unresolved']}  (kept)")
    print(f"  No source:      {report['no_source']}  (kept)")
    print(f"  Out of scope:   {report['out_of_scope']}  (kept)")

    by_source = report.get("by_source") or {}
    if by_source:
        top = sorted(by_source.items(), key=lambda kv: -kv[1])[:5]
        label = "Top sources to remove" if dry_run else "Top sources removed"
        print(f"\n  {label}:")
        for src, n in top:
            print(f"    {src}  ({n})")

    if report["unresolved"]:
        print("\n  Unresolved drawers are kept: nothing here could show their source file is gone.")
        unresolved_sources = report.get("unresolved_by_source") or {}
        if unresolved_sources:
            top = sorted(unresolved_sources.items(), key=lambda kv: -kv[1])[:5]
            for src, n in top:
                print(f"    {src}  ({n})")
            rest = len(unresolved_sources) - len(top)
            if rest:
                print(f"    and {rest} more source file(s)")

    if dry_run:
        if report["gitignored"] + report["missing"] > 0:
            print("\n  Re-run with --apply to commit these deletions.")
    else:
        print(
            f"\n  Removed {report['removed_drawers']} drawers, {report['removed_closets']} closets."
        )

    print(f"\n{'=' * 55}\n")


def _sync_should_apply(preview: dict, *, target: str, label: str) -> bool:
    """Confirm a destructive sync after showing what it would remove (#418 review).

    ``sync --apply`` deletes drawers. Until the backend-resolution fix it was
    a guaranteed no-op on a service-backed palace — the directory precheck
    refused it — so nothing ever asked. Making it work turns it into a live
    bulk delete, and one wing's top 400 sources were measured at 2,619
    drawers removed unconditionally. ``purge`` has always confirmed; this is
    the same gate, reading the preview pass for the count.
    """
    from ..migrate import confirm_destructive_action

    removable = int(preview.get("gitignored") or 0) + int(preview.get("missing") or 0)
    if removable == 0:
        print("  Nothing to remove — no scanned drawer is gitignored or missing.\n")
        return False
    return confirm_destructive_action(f"Sync removal of {removable:,} drawers from {label}", target)


def _sync_via_daemon(args) -> None:
    """Run `sync` against the daemon's palace under daemon-strict (#418).

    Without this, a client with no local palace fell into the directory
    precheck and could not even preview with ``--dry-run``. ``mempalace_sync``
    resolves source files against the *daemon host's* filesystem, which is
    the right reference: that host is where the drawers were mined from.
    """
    target = _daemon_url() or "the palace daemon"
    project_dirs = [os.path.expanduser(d) for d in ([args.dir] if args.dir else [])]
    project_dirs += [os.path.expanduser(r) for r in (args.root or [])]
    if len(project_dirs) > 1:
        print(
            "mempalace: the daemon-routed sync takes one project root at a time; "
            "re-run per root, or pass --palace <dir> for a local palace.",
            file=sys.stderr,
        )
        sys.exit(2)

    project_dir = project_dirs[0] if project_dirs else None

    def _call(apply_: bool) -> dict:
        payload = {"project_dir": project_dir, "wing": args.wing, "apply": apply_}
        try:
            data = _call_daemon_tool("mempalace_sync", payload)
        except DaemonError as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(1)
        if not data.get("success", False):
            print(f"mempalace: {data.get('error', 'sync failed')}", file=sys.stderr)
            sys.exit(1)
        return data

    print(f"\n{'=' * 55}")
    print("  MemPalace Sync -- Gitignore-aware drawer prune")
    print(f"{'=' * 55}")
    print(f"  Target:   {target}")
    if args.wing:
        print(f"  Wing:     {args.wing}")
    if project_dir:
        print(f"  Project:  {project_dir}")
    print(
        "  Mode:     DRY RUN (no deletions)"
        if args.dry_run
        else "  Mode:     APPLY (deleting drawers)"
    )
    print(f"{'-' * 55}\n")

    if not args.dry_run:
        from ..sync import validate_apply_scope

        try:
            validate_apply_scope(args.wing, project_dirs)
        except ValueError as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(2)

    if not args.dry_run and not getattr(args, "yes", False):
        preview = _call(False)
        _print_sync_report(preview, dry_run=True)
        if not _sync_should_apply(preview, target=target, label=args.wing or "this palace"):
            return

    _print_sync_report(_call(not args.dry_run), dry_run=args.dry_run)
