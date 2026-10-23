# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_sync(args):
    """Prune drawers whose source files are gitignored, deleted, or moved (#1252).

    Where it runs (#418): ``--daemon`` submits to the local job queue;
    daemon-strict with no ``--palace`` routes to the palace daemon; otherwise
    the backend is resolved *before* the palace-directory precheck, and that
    precheck is skipped for a service-backed store, which has no local
    database file by design.
    """
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    routing = _resolve_cli_write_routing_or_exit(
        args,
        "sync",
    )
    if routing.use_daemon:
        # Resolved here, as sync_palace resolves them on the direct route: the
        # daemon keeps the cwd it was started in.
        payload = {
            "dir": os.path.realpath(os.path.expanduser(args.dir)) if args.dir else None,
            "root": [os.path.realpath(os.path.expanduser(root)) for root in args.root or []],
            "wing": args.wing,
            "dry_run": args.dry_run,
        }
        _submit_daemon_cli_job(
            "sync",
            payload,
            args,
            background=bool(getattr(args, "background", False)),
            auto_start=routing.decision.auto_start_daemon,
        )
        return

    if _daemon_strict() and not getattr(args, "palace", None):
        _sync_via_daemon(args)
        return

    from ..palace import MineAlreadyRunning
    from ..wal import _wal_log
    from ..backends import detect_backend_for_path
    from ..palace import (
        _backend_artifact_label,
        backend_stores_data_locally,
        resolve_backend_name,
    )
    from ..sync import sync_palace

    # Resolve the backend BEFORE any palace-directory precheck (#418): a
    # service-backed palace has no local database file by design, so the
    # precheck refused every Postgres palace -- including `--dry-run`, which
    # could not even preview.
    try:
        backend_name = resolve_backend_name(palace_path, explicit=_backend_arg(args))
    except Exception as exc:  # noqa: BLE001 - user-facing CLI guard
        print(f"\n  Could not resolve palace backend: {exc}", file=sys.stderr)
        return
    if backend_stores_data_locally(backend_name):
        if not os.path.isdir(palace_path):
            _print_retired_local_palace_or_default(palace_path)
            return
        if detect_backend_for_path(palace_path) is None:
            print(
                f"\n  Palace dir at {palace_path} exists but has no "
                f"{_backend_artifact_label(backend_name)} yet."
            )
            print("  Run: mempalace mine <dir>")
            return

    project_dirs = []
    if args.dir:
        project_dirs.append(os.path.expanduser(args.dir))
    project_dirs.extend(os.path.expanduser(r) for r in args.root)
    project_dirs = project_dirs or None

    print(f"\n{'=' * 55}")
    print("  MemPalace Sync -- Gitignore-aware drawer prune")
    print(f"{'=' * 55}")
    print(f"  Palace:   {palace_path}")
    print(f"  Backend:  {backend_name}")
    if args.wing:
        print(f"  Wing:     {args.wing}")
    if project_dirs:
        for p in project_dirs:
            print(f"  Project:  {p}")
    if args.dry_run:
        print("  Mode:     DRY RUN (no deletions)")
    else:
        print("  Mode:     APPLY (deleting drawers)")
    print(f"{'-' * 55}\n")

    def _run(dry_run: bool) -> dict:
        try:
            return sync_palace(
                palace_path=palace_path,
                project_dirs=project_dirs,
                wing=args.wing,
                dry_run=dry_run,
                wal_log=_wal_log,
            )
        except MineAlreadyRunning as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(1)
        except ValueError as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(2)
        except Exception as exc:
            print(f"mempalace: sync failed: {exc}", file=sys.stderr)
            sys.exit(1)

    # --apply deletes, so show the blast radius and ask first. The preview
    # costs a second scan, which is why --yes skips both the prompt and the
    # scan rather than only the prompt.
    if not args.dry_run:
        # The apply-scope rule has to run BEFORE the preview: the preview is a
        # dry run, so it passes that guard, and an unscoped --apply would
        # reach the prompt instead of exiting 2.
        from ..sync import validate_apply_scope

        try:
            validate_apply_scope(args.wing, project_dirs)
        except ValueError as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(2)

    if not args.dry_run and not getattr(args, "yes", False):
        preview = _run(True)
        _print_sync_report(preview, dry_run=True)
        if not _sync_should_apply(preview, target=palace_path, label=args.wing or "this palace"):
            return

    _print_sync_report(_run(args.dry_run), dry_run=args.dry_run)


def _resolve_search_format(args) -> str:
    """Pick the output format for ``mempalace search``.

    ``--format`` always wins. ``--json`` (legacy flag) maps to ``json``.
    Defaults to ``table`` — the enhanced multi-line view. ``getattr``
    fallbacks keep older test fixtures (which build ``argparse.Namespace``
    without the new attrs) working unchanged.
    """
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _resolve_search_limit(args) -> int:
    """Pick the result limit. ``--limit`` overrides legacy ``--results``."""
    limit = getattr(args, "limit", None)
    if limit is not None:
        return limit
    return getattr(args, "results", 5)


# Reordering can only promote a hit the ranker actually returned. On the
# measured #451 case the curated card sat at rank 13 of 20 while the reader
# asked for 10, so it was never in the window to promote. When NOTHING
# curated came back, one wider fetch is made and the result is reordered and
# truncated; when a curated hit is already present — the common case — no
# extra call happens at all. Bounded deliberately: this wave is cutting
# daemon load, not adding to it.
# How deep the conditional second fetch goes. #477 widened to
# ``min(n_results * 2, 40)``, which is SIX at ``--limit 3`` — and in a wing with
# ~941K transcript drawers the curated layer begins at rank 14-27, so that widen
# fired and landed short (#526). The floor is absolute, not a multiple.
#
# It stays CONDITIONAL because the depth is not free: a limit-30 hybrid call on
# wing 2g measured 19-22 s (#533), so an unconditional deep fetch would put a
# ~20 s floor under every interactive search on a large wing.
_DEEP_FETCH_DEPTH = 30

# What counts as a curated document. ``file`` and ``memory`` both mean "something
# a human maintains", matching ``no_curated_source`` and ``prefer_curated``'s
# kind ranking. #526 says "source_kind: file"; using only that would let a
# memory-only result trigger the deep fetch (``no_curated_source`` counts it)
# while reporting ``curated_first_rank: null``, so the trigger and the reported
# rank would disagree about the same result set.
_CURATED_KINDS = ("file", "memory")


def _is_curated_hit(hit) -> bool:
    return isinstance(hit, dict) and hit.get("source_kind") in _CURATED_KINDS


def _first_curated_rank(hits) -> int | None:
    """1-based rank of the first curated hit, or None. Caller must pass the list
    in the RANKER's order — this is a statement about corpus depth, so taking it
    after ``prefer_curated`` would report 1 almost every time and say nothing."""
    if not isinstance(hits, list):
        return None
    for i, hit in enumerate(hits, 1):
        if _is_curated_hit(hit):
            return i
    return None


def _deep_fetch_when_nothing_curated(
    hits: list, n_results: int, fetch, *, short: bool = False, warnings=None
):
    """``(hits_to_rank, curated_first_rank)`` — one deeper fetch when needed.

    Fires when the shallow result contains no curated document at all, or when
    ``short`` — collapsing identical-text copies left it shorter than the
    requested limit, so the freed slot has nothing to fill it from. Either way
    it is ONE extra call, only when a shallow list has shown it is needed. The
    deeper list is annotated and collapsed before return so the caller can rank
    it. A failed or unhelpful second call degrades to the shallow hits: the
    extra fetch is an optimisation, never a dependency — but never a SILENT one.
    When the deeper call fails the daemon's own words are appended to
    ``warnings`` (the list the search header prints), so ``curated_first_rank:
    null`` is read as "the depth was not checked", not as "nothing curated
    exists" (#526, PR 4).
    """
    if not short and not no_curated_source(hits):
        return hits, _first_curated_rank(hits)
    depth = max(n_results, _DEEP_FETCH_DEPTH)
    if depth <= len(hits):
        return hits, _first_curated_rank(hits)
    try:
        deeper = fetch(depth)
    except DaemonError as e:
        if warnings is not None:
            warnings.append(f"deeper fetch unavailable: {getattr(e, 'detail', None) or e}")
        return hits, _first_curated_rank(hits)
    if not isinstance(deeper, list) or len(deeper) <= len(hits):
        return hits, _first_curated_rank(hits)
    annotate(deeper)
    collapse_identical_text(deeper)
    return deeper, _first_curated_rank(deeper)


def _truncate_reserving_curated(ordered: list, n_results: int, curated_hit, rank):
    """Truncate to ``n_results``, reserving the LAST slot for a curated hit.

    Only when the top N would otherwise contain none and the deeper fetch found
    one. #477's near-duplicate ordering cannot do this job: it lifts a curated
    document over transcripts that QUOTE it, and here the document and the
    transcripts above it share no 3-grams at all (measured: similarity 0.0 and
    0.008 against a 0.35 threshold). Same topic, different words — a different
    defect needing a reserved slot rather than a re-rank.

    The promotion is MARKED on the returned hit. A silent one would be #526's
    own error in reverse: the reader could not tell "ranked here" from
    "reserved here".
    """
    top = list(ordered[:n_results])
    if curated_hit is None or rank is None:
        return top
    if any(_is_curated_hit(h) for h in top):
        return top
    promoted = dict(curated_hit)
    promoted["promoted"] = True
    promoted["promoted_from_rank"] = rank
    return top[: max(0, n_results - 1)] + [promoted]


def _fast_hits(query: str, limit: int, wing: str = None) -> list | None:
    """GET /search/fast, normalised to the shared hit shape. None if unusable."""
    rest_params = {"q": query, "limit": limit}
    if wing:
        rest_params["wing"] = wing
    raw = _call_daemon_rest("/search/fast", rest_params)
    if raw is None:
        return None
    if isinstance(raw, dict):
        hits = raw.get("results")
    elif isinstance(raw, list):
        hits = raw
    else:
        hits = None
    if not isinstance(hits, list):
        return None
    for hit in hits:
        if "snippet" in hit:
            hit["text"] = hit.pop("snippet")
        elif "text" not in hit:
            hit["text"] = ""
        if "rank" in hit:
            hit["bm25_score"] = round(hit.pop("rank"), 3)
        if hit.get("source_file"):
            hit["source"] = hit["source_file"]
    return hits


def _daemon_search_fast(query: str, n_results: int, wing: str = None) -> dict | None:
    """BM25 fast path via GET /search/fast. Returns normalised data dict or None."""
    hits = _fast_hits(query, n_results, wing)
    if hits is None:
        return None
    annotate(hits)
    # Collapse BEFORE the depth decision and the reserved slot, so the trigger,
    # curated_first_rank and the truncation all see one list (#526 PR 3).
    shallow_n = len(hits)
    collapse_identical_text(hits)
    warnings: list = []
    hits, curated_rank = _deep_fetch_when_nothing_curated(
        hits,
        n_results,
        lambda limit: _fast_hits(query, limit, wing),
        short=len(hits) < min(n_results, shallow_n),
        warnings=warnings,
    )
    curated_hit = hits[curated_rank - 1] if curated_rank else None
    prefer_curated(hits)
    data = {
        "results": _truncate_reserving_curated(hits, n_results, curated_hit, curated_rank),
        "query": query,
        "source": "bm25-fast",
        "curated_first_rank": curated_rank,
        # #549: how many distinct hits were checked for a curated document —
        # the deep fetch's 30 when it ran, the shallow list otherwise. The
        # results are then truncated to the limit, so len(results) undercounts.
        "depth_examined": len(hits),
    }
    if warnings:
        data["warnings"] = warnings
    return data


def _daemon_search_hybrid(
    query: str, n_results: int, wing: str = None, room: str = None
) -> dict | None:
    """Hybrid search via POST /search/hybrid (vector + BM25 + AGE graph)."""
    body = {"query": query, "limit": n_results}
    if wing:
        body["wing"] = wing
    if room:
        body["room"] = room
    data = _post_daemon_rest("/search/hybrid", body)
    if data is None:
        return None
    data.setdefault("source", "hybrid")
    hits = data.get("results")
    annotate(hits)
    if isinstance(hits, list):
        shallow_n = len(hits)
        collapse_identical_text(hits)

        def _refetch(limit):
            wider_body = dict(body, limit=limit)
            wider = _post_daemon_rest("/search/hybrid", wider_body)
            return (wider or {}).get("results")

        warnings: list = list(data.get("warnings") or [])
        hits, curated_rank = _deep_fetch_when_nothing_curated(
            hits,
            n_results,
            _refetch,
            short=len(hits) < min(n_results, shallow_n),
            warnings=warnings,
        )
        if warnings:
            data["warnings"] = warnings
        curated_hit = hits[curated_rank - 1] if curated_rank else None
        prefer_curated(hits)
        data["results"] = _truncate_reserving_curated(hits, n_results, curated_hit, curated_rank)
        data["curated_first_rank"] = curated_rank
        data["depth_examined"] = len(hits)  # #549: before truncation
    return data


def _submit_daemon_cli_job(
    kind: str,
    payload: dict,
    args,
    *,
    background: bool,
    auto_start: bool = True,
) -> None:
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    backend = _backend_arg(args)
    from ..daemon import DaemonError, submit_job

    try:
        job = submit_job(
            kind,
            payload,
            palace_path=palace_path,
            backend=backend,
            wait=not background,
            auto_start=auto_start,
            # A job refused the palace lock is deferred, not failed (#2014), so
            # it never becomes terminal while the holder lives. Waiting it out
            # would strand this terminal behind a peer that can outlive the
            # default hour; report the parked job instead. A job that is really
            # running (a long mine) is still waited out.
            stop_on_lock_deferral=not background,
        )
    except DaemonError as exc:
        print(f"mempalace: daemon submission failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if background:
        print(f"Submitted daemon job {job['id']} ({kind})")
        return

    from ..daemon import job_deferred_by_lock

    if job_deferred_by_lock(job):
        reason = (job.get("error") or {}).get("message") or "the palace write lock is held"
        # --palace is global, so it has to be echoed back ahead of the
        # subcommand: without it the suggestion silently lists the DEFAULT
        # palace's queue (or nothing at all) instead of the one this job is
        # parked in -- a wrong answer that looks authoritative.
        # `daemon jobs` and not `daemon wait`: we just declined to wait out the
        # holder, so pointing the operator at a command that blocks on the very
        # state we could not wait for would undo the point of this branch.
        palace_flag = f"--palace {shlex.quote(args.palace)} " if args.palace else ""
        print(f"mempalace: {reason}", file=sys.stderr)
        print(
            f"mempalace: job {job['id']} is queued and runs when the holder exits "
            f"(check it with: mempalace {palace_flag}daemon jobs)",
            file=sys.stderr,
        )
        sys.exit(1)

    result = job.get("result") or {}
    from ..service import print_job_result

    exit_code = print_job_result(result)
    if job.get("state") != "succeeded" and exit_code == 0:
        error = job.get("error") or {}
        print(
            f"mempalace: daemon job failed: {error.get('message', 'unknown error')}",
            file=sys.stderr,
        )
        exit_code = 1
    if exit_code:
        sys.exit(exit_code)


def cmd_daemon(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    backend = _backend_arg(args)
    from ..daemon import (
        TERMINAL_STATES,
        DaemonError,
        QueueStore,
        get_client_if_running,
        job_to_dict,
        queue_path,
        start_daemon,
        stop_daemon,
    )

    action = getattr(args, "daemon_action", None)
    try:
        if action == "start":
            if args.foreground:
                start_daemon(palace_path, backend=backend, foreground=True)
                return
            client = start_daemon(palace_path, backend=backend, foreground=False)
            health = client.health()
            print(f"MemPalace daemon running on 127.0.0.1:{client.port}")
            print(f"  Palace: {health.get('palace_path')}")
            print(f"  PID:    {health.get('pid')}")
            return

        if action == "stop":
            if stop_daemon(palace_path):
                print("MemPalace daemon stopping")
            else:
                print("MemPalace daemon is not running")
            return

        if action == "status":
            client = get_client_if_running(palace_path)
            if client is None:
                print("MemPalace daemon is not running")
                sys.exit(1)
            health = client.health()
            print("MemPalace daemon is running")
            print(f"  Palace: {health.get('palace_path')}")
            print(f"  PID:    {health.get('pid')}")
            print(f"  Active: {health.get('active_job_id') or '-'}")
            print(f"  Jobs:   {health.get('counts') or {}}")
            return

        if action == "jobs":
            client = get_client_if_running(palace_path)
            if client is not None:
                jobs = client.list_jobs(limit=args.limit)
            else:
                qpath = queue_path(palace_path)
                if not qpath.exists():
                    jobs = []
                else:
                    jobs = [
                        job_to_dict(job, include_payload=False)
                        for job in QueueStore(qpath).list(args.limit)
                    ]
            for job in jobs:
                print(f"{job['id']}  {job['state']:<9}  {job['kind']:<10}  {job['created_at']}")
            return

        if action == "wait":
            client = get_client_if_running(palace_path)
            if client is not None:
                job = client.wait(args.job_id)
            else:
                qpath = queue_path(palace_path)
                if not qpath.exists():
                    raise DaemonError("daemon is not running")
                job = job_to_dict(QueueStore(qpath).get(args.job_id))
                if job.get("state") not in TERMINAL_STATES:
                    raise DaemonError(f"daemon is not running; job {args.job_id} is {job['state']}")
            result = job.get("result") or {}
            from ..service import print_job_result

            exit_code = print_job_result(result)
            if job.get("state") != "succeeded" and exit_code == 0:
                print(f"mempalace: daemon job failed: {job.get('error')}", file=sys.stderr)
                exit_code = 1
            if exit_code:
                sys.exit(exit_code)
            return
    except DaemonError as exc:
        print(f"mempalace: daemon error: {exc}", file=sys.stderr)
        sys.exit(1)


def _daemon_search_auto(args, n_results: int, tags):
    """``--mode auto``: BM25-fast first, hybrid fallback when fast under-shoots.

    BM25-fast is a pure keyword search, so paraphrased or synonym-only
    queries get 0 hits even when the palace has the content. When fast
    succeeded but under-shot the requested limit, retry through hybrid
    (vector + BM25 + AGE graph) and prefer those results when they cover
    more ground; the user sees a "hybrid (auto-fallback from bm25-fast)"
    banner so the rescue is visible. The fallback only fires when fast
    returned a real response — ``None`` (daemon /search/fast 404 or
    unreachable) is returned as-is so the caller routes to the MCP
    envelope, which already runs hybrid-shaped. See #283.

    When BOTH arms come back empty, say so in the source banner and carry
    hybrid's ``warnings``: a bare "0 results" under a bm25-fast banner read
    as "fast route, no fallback" and sent a fleet session chasing a misroute
    that wasn't one — the real cause was a filtered-kNN under-return the
    daemon HAD warned about (2026-09-03).

    Extracted from cmd_search (ruff C901).
    """
    if args.room or tags:
        return None
    data = _daemon_search_fast(args.query, n_results, wing=args.wing)
    if data is None:
        return None
    bm25_hits = data.get("results") or []
    if len(bm25_hits) >= n_results:
        return data
    try:
        fb = _daemon_search_hybrid(args.query, n_results, wing=args.wing, room=args.room)
    except DaemonError as e:
        # The fallback is an optimisation; losing it must not lose the search.
        # But a reader of bm25's short list must know hybrid was not consulted,
        # in the daemon's own words (#526, PR 4).
        fb = None
        data["warnings"] = list(data.get("warnings") or []) + [
            f"hybrid fallback unavailable: {getattr(e, 'detail', None) or e}"
        ]
    fb_hits = (fb.get("results") or []) if fb else []
    if fb_hits and len(fb_hits) > len(bm25_hits):
        fb["source"] = "hybrid (auto-fallback from bm25-fast)"
        return fb
    if fb is not None and not bm25_hits:
        # ``source`` stays "bm25-fast" — it is a stable machine-read field
        # (hooks and tests key on it). The fact that hybrid was tried and
        # also came back empty goes in its own field, and hybrid's warnings
        # are carried so the header shows the daemon's diagnostic.
        data["fallback"] = "hybrid also returned 0 hits"
        if fb.get("warnings"):
            data["warnings"] = list(fb.get("warnings") or [])
    return data
