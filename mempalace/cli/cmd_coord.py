# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


# ── Logstream (RFC 003 agent coordination) ────────────────────────────────


def _open_logstream(args):
    """Open the palace logstream database for a CLI command.

    Direct SQLite access is safe alongside a running hub: logstream.sqlite3
    is WAL-mode and independent of Chroma, so CLI writes are immediately
    visible to hub readers without the mine-style forwarding Chroma needs.
    """
    from ..logstream import LOGSTREAM_DB_FILENAME, Logstream

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    return Logstream(db_path=os.path.join(palace_path, LOGSTREAM_DB_FILENAME))


def _logstream_fail(message: str, as_json: bool):
    import json

    if as_json:
        print(json.dumps({"error": message}))
    else:
        print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _read_stdin_exact() -> str:
    """Read stdin as bytes and decode — never through the text layer.

    ``sys.stdin.read()`` applies universal-newline translation, turning
    CRLF into LF before we ever see it. For a store addressed by sha256
    over the exact bytes, that is silent corruption: a patch piped in from
    a Windows agent would be stored as different content with a different
    digest than the one that produced it. ``.buffer`` is absent when stdin
    has been replaced by a plain StringIO, so fall back to the text read
    rather than crashing.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    return buffer.read().decode("utf-8")


def _write_stdout_exact(content: str) -> None:
    """Write content to stdout as bytes, bypassing newline translation.

    The counterpart to :func:`_read_stdin_exact` — ``mempalace artifact get
    ID | git apply`` must deliver the stored bytes, not a re-translated
    copy of them.
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(content)
        return
    sys.stdout.flush()
    buffer.write(content.encode("utf-8"))
    buffer.flush()


def _read_text_arg(inline, file_arg, default=""):
    """Resolve inline text vs --*-file (with '-' meaning stdin).

    File and stdin reads are byte-exact (see :func:`_read_stdin_exact`):
    the logstream's contract is verbatim content, so line endings must
    reach the store exactly as the author wrote them.
    """
    if inline is not None and file_arg is not None:
        raise ValueError("pass inline text or a file, not both")
    if file_arg is not None:
        if file_arg == "-":
            return _read_stdin_exact()
        return Path(os.path.expanduser(file_arg)).read_bytes().decode("utf-8")
    if inline is not None:
        return inline
    return default


def _parse_metadata_arg(raw):
    import json

    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--metadata is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError("--metadata must be a JSON object")
    return value


def _print_event_line(event):
    target = event["to_agent"] or "*"
    corr = f" corr={event['correlation_id']}" if event["correlation_id"] else ""
    topic = f" topic={event['topic']}" if event.get("topic") else ""
    status = f" [{event['status']}]" if event["status"] else ""
    arts = f" artifacts={len(event['artifact_ids'])}" if event["artifact_ids"] else ""
    body = event["body"].replace("\n", " ")
    if len(body) > 80:
        body = body[:77] + "..."
    body = f" :: {body}" if body else ""
    print(
        f"  {event['id']}  {event['created_at']}  {event['type']}  "
        f"{event['stream']}/{event['room']}  {event['from_agent']}->{target}"
        f"{status}{topic}{corr}{arts}{body}"
    )


def _watch_json(payload, *, follow: bool) -> str:
    """Serialize one ``logstream watch`` record.

    A single-shot watch prints exactly one document, so it is pretty-printed
    and ``json.load``-able as-is. Under ``--follow`` there are many records
    on one stream: indented documents concatenated back-to-back are *not*
    valid JSON, and ``json.load`` / ``jq`` reject them with trailing data —
    which defeats the point of a machine-readable flag on the mode meant for
    daemons. Follow mode therefore emits NDJSON, one compact record per line.
    """
    import json

    return (
        json.dumps(payload, ensure_ascii=False)
        if follow
        else json.dumps(payload, indent=2, ensure_ascii=False)
    )


def _watch_spec(args, as_json) -> dict:
    """Validate ``logstream watch`` arguments and build its filter spec.

    Kept apart from the watch loop so every bad input is rejected before any
    polling, any cursor resolution, and any checkpoint write — several of the
    bugs on this path were arguments that only failed once the loop had
    already started and persisted state.
    """
    from ..logstream import normalize_watch_values, sanitize_watch_spec

    if args.poll_timeout_ms is not None and args.poll_timeout_ms <= 0:
        # A configured zero would make watch_events' expired-deadline branch
        # yield forever without ever polling, burning a core.
        _logstream_fail("--poll-timeout-ms must be a positive number of milliseconds", as_json)
    if args.limit is not None and args.limit < 1:
        # argparse accepts it; list_events would raise mid-loop, where the
        # only handler is for KeyboardInterrupt — a traceback, and under
        # --json no error document at all.
        _logstream_fail("--limit must be a positive integer", as_json)
    if args.idle_exit_ms is not None and args.idle_exit_ms < 0:
        # Only 0 means "wait forever". A negative value arriving from config
        # or from timeout arithmetic would otherwise take that same branch
        # silently, leaving a harness waiting on a watcher it believes will
        # time out.
        _logstream_fail(
            "--idle-exit-ms must be zero (wait forever) or a positive number of milliseconds",
            as_json,
        )

    to_agents = list(args.to_agent or [])
    exclude = list(args.exclude_from_agent or [])
    if args.agent:
        # The whole point of --agent: to_agent=<me> also matches '*'
        # broadcasts, and your own broadcasts are broadcasts — so a watcher
        # without this exclusion wakes itself every time it posts a status.
        to_agents.append(args.agent)
        exclude.append(args.agent)
    spec = {
        "streams": normalize_watch_values(args.stream),
        "rooms": normalize_watch_values(args.room),
        "topics": normalize_watch_values(getattr(args, "topic", None)),
        "types": normalize_watch_values(args.type),
        "statuses": normalize_watch_values(args.status),
        "to_agents": normalize_watch_values(to_agents),
        "from_agents": normalize_watch_values(args.from_agent),
        "exclude_from_agents": normalize_watch_values(exclude),
        "correlation_ids": normalize_watch_values(args.correlation_id),
    }
    try:
        spec = sanitize_watch_spec(spec)
    except ValueError as exc:
        _logstream_fail(str(exc), as_json)

    return spec


def _logstream_watch(ls, args, as_json):
    """Run ``logstream watch`` — block until interesting events arrive.

    Split out of ``cmd_logstream`` so that dispatcher stays under the
    complexity gate: this branch carries cursor persistence, an idle timeout,
    and follow-vs-exit semantics that no other subcommand needs.

    Exit contract, chosen so a harness can background this process and treat
    its exit as a wake-up: return (0) when a match was printed, 2 when the
    idle timeout expired having seen nothing — the same convention
    ``logstream wait`` uses for a timeout.
    """
    import time

    from ..logstream import (
        WATCH_STATE_ABSENT,
        WATCH_STATE_CORRUPT,
        read_watch_state,
        resolve_watch_state_file,
        write_watch_cursor,
    )

    args.state_file = resolve_watch_state_file(args.state_file, args.agent)
    spec = _watch_spec(args, as_json)

    stored_cursor, state_condition = read_watch_state(args.state_file)
    # A missing cursor is four different facts, and only one of them may
    # start at the tip. Treating a corrupt file or an empty-log restart as a
    # first run skips everything that arrived since, then checkpoints past
    # it — the loss is silent and permanent.
    recovery_failed = state_condition == WATCH_STATE_CORRUPT
    cursor = args.since_event_id or stored_cursor
    skipped_from = None
    if cursor is None and not args.from_start and state_condition == WATCH_STATE_ABSENT:
        # Start at the tip, like the SSE live-tail does at connect time.
        # Starting from the beginning of a long fleet log means a fresh
        # watcher wakes holding weeks of history and cannot tell it is
        # stale — measured 41 events, the oldest 49 days old, on a real
        # shared brain. Backlog is the inbox sweep's job; a watcher is for
        # what arrives from now on. Never silent: say what was skipped.
        cursor = ls.latest_event_id()
        skipped_from = cursor

    # One place decides the starting position; one place records it. That
    # position can arrive three ways — an explicit --since-event-id, the tip
    # above, or None (an empty log, or --from-start) — and every one of them
    # needs the same immediate checkpoint when no state file exists yet.
    # Deferring to the first watch_events yield leaves a window of up to a
    # full poll timeout in which an interrupt leaves no file behind, and the
    # next launch calls itself a first run and jumps to the tip, skipping
    # whatever arrived in between. Unlike later checkpoints this one cannot
    # fail quietly: losing it costs a skipped event, not a replay.
    if cursor is not None:
        # Verify the anchor before it is written anywhere. list_events raises
        # on an unknown since_event_id, and the required startup write below
        # happens first — so a typo'd or stale --since-event-id would be
        # persisted, then crash the first poll, and every later run without
        # the flag would reload it and crash again until someone deleted the
        # file by hand. Fail cleanly instead, leaving no state behind.
        try:
            ls.list_events(since_event_id=cursor, limit=1)
        except ValueError as exc:
            if args.since_event_id:
                # Explicitly supplied and wrong: user error, so refuse
                # without leaving anything behind to reload next time.
                _logstream_fail(f"{exc}. Nothing was written to the state file.", as_json)
            # A *stored* cursor whose event has gone (log rebuilt, replica
            # reset) is corrupt state rather than user error, and refusing to
            # start would strand the watcher exactly as an unreadable file
            # would. Replay instead: a duplicate, never a missed delegation.
            print(
                f"Stored cursor {cursor} no longer exists in this log; replaying from the "
                "start rather than refusing to run.",
                file=sys.stderr,
            )
            cursor = None

    if args.state_file and state_condition == WATCH_STATE_ABSENT:
        try:
            write_watch_cursor(args.state_file, cursor, agent=args.agent, required=True)
        except OSError as exc:
            _logstream_fail(
                f"could not write the initial checkpoint to {args.state_file}: {exc}. "
                "Refusing to start: without it a restart would skip every event "
                "that arrives before then.",
                as_json,
            )
    if not as_json:
        where = args.agent or ", ".join(sorted(spec["to_agents"] or [])) or "everything"
        print(f"Watching {where} from {cursor or 'now'}; Ctrl-C to stop.", file=sys.stderr)
    if skipped_from:
        print(
            f"Starting at the tip ({skipped_from}); earlier events are not replayed. "
            "Use --from-start to replay them, or sweep with `mempalace logstream list`.",
            file=sys.stderr,
        )
    if recovery_failed:
        print(
            f"State file {args.state_file} is unreadable; replaying from the start "
            "rather than skipping to the tip, so nothing since the last good "
            "checkpoint is lost.",
            file=sys.stderr,
        )

    idle_s = args.idle_exit_ms / 1000.0 if args.idle_exit_ms and args.idle_exit_ms > 0 else None
    deadline = time.monotonic() + idle_s if idle_s else None
    matched_any = False

    def _poll_timeout_ms():
        if deadline is None:
            return args.poll_timeout_ms
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        return min(args.poll_timeout_ms, remaining_ms)

    try:
        for matched, cursor in ls.watch_events(
            cursor=cursor,
            poll_timeout_ms=_poll_timeout_ms,
            limit=args.limit,
            **spec,
        ):
            if matched:
                matched_any = True
                if idle_s:
                    deadline = time.monotonic() + idle_s
                if as_json:
                    print(
                        _watch_json(
                            {
                                "events": matched,
                                "count": len(matched),
                                "cursor": cursor,
                                "timed_out": False,
                            },
                            follow=args.follow,
                        ),
                        flush=True,
                    )
                else:
                    print(f"{len(matched)} event(s):")
                    for event in matched:
                        _print_event_line(event)
                    sys.stdout.flush()
                # Matched batches checkpoint *after* stdout so a kill or
                # broken pipe between the two replays the event instead of
                # skipping it. Unmatched advances (below) are safe immediately:
                # those events were examined and rejected.
                write_watch_cursor(args.state_file, cursor, agent=args.agent)
                if not args.follow:
                    return
                continue
            write_watch_cursor(args.state_file, cursor, agent=args.agent)
            if deadline is not None and time.monotonic() >= deadline:
                if as_json:
                    print(
                        _watch_json(
                            {"events": [], "count": 0, "cursor": cursor, "timed_out": True},
                            follow=args.follow,
                        )
                    )
                else:
                    print("Idle timeout; no matching events.")
                sys.exit(0 if matched_any else 2)
    except KeyboardInterrupt:
        # Exit 0 is the documented "a match was printed" signal, so an
        # interrupted watcher must not use it — a supervisor would report
        # mail that never arrived. 128 + SIGINT, the shell convention.
        if not as_json:
            print("Stopped.", file=sys.stderr)
        sys.exit(130)
    except ValueError as exc:
        # Backstop. Every known bad input is rejected before the loop
        # starts, but a validation error escaping mid-poll would otherwise
        # surface as a traceback — and under --json as no error document at
        # all, which a machine consumer cannot distinguish from a crash.
        _logstream_fail(str(exc), as_json)


# ── mempalace diary / kg / walk / rate ────────────────────────────────
#
# Slices of #191: four MCP tool families that had no CLI verb —
# ``mempalace_diary_write`` / ``mempalace_diary_read`` (#354),
# ``mempalace_kg_add`` / ``_invalidate`` / ``_timeline`` (#357),
# ``mempalace_walk_palace`` + ``mempalace_traverse`` (#359), and
# ``mempalace_rate_memory`` (#361).
#
# Routing mirrors cmd_wakeup / cmd_mined: daemon-strict and no
# ``--palace`` → the daemon's /mcp endpoint; otherwise the local
# ``mempalace.mcp_server`` tool function, imported inside the command so
# the CLI's import cost is unchanged and ``--palace`` can seed
# MEMPALACE_PALACE_PATH before mcp_server builds its module config.
#
# Exit codes follow the sibling read commands: 1 = daemon unreachable,
# 2 = client error or inner-error envelope.


def _resolve_tool_format(args) -> str:
    """``--format`` wins, then the ``--json`` shorthand, default ``table``.

    Same contract as the per-command ``_resolve_*_format`` helpers, shared
    across the diary/kg/walk/rate families so four identical clones don't
    accumulate.
    """
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _call_tool_routed(args, daemon_tool: str, local_fn: str, payload: dict) -> dict:
    """Run one MCP tool through the daemon, or locally when not daemon-strict.

    Raises :class:`DaemonError` when the daemon path fails. Local failures
    surface as the tool's own ``{"error": ...}`` / ``{"success": False}``
    envelope — the tool functions never raise for bad input.

    The local path goes through ``_local_mcp_server`` (the read family's
    context manager, #356/#362) rather than importing ``mcp_server``
    directly: it composes the drawer family's ``_import_mcp_server``
    stdout repair (#355) with a *scoped* ``MEMPALACE_PALACE_PATH``
    override, so a ``--palace`` on one invocation cannot leak into a
    later command that passed none.
    """
    palace = getattr(args, "palace", None)
    if _daemon_strict() and not palace:
        return _call_daemon_tool(daemon_tool, payload)
    with _local_mcp_server(palace) as mcp_server:
        fn = getattr(mcp_server, local_fn, None)
        if fn is None:  # pragma: no cover — guards a rename of the tool function
            raise DaemonError(f"local tool function {local_fn} is missing")
        return fn(**payload)


#: The daemon release that first serves /window and /source. Named in the
#:404 refusal, so it has to track palace-daemon's VERSION or the message
#: becomes a lie. A test in the daemon repo asserts the bump.
_WINDOW_MIN_DAEMON = "1.10.0"


def _window_route_failed(path: str, result, want_json: bool) -> None:
    """Map a non-200 from /window or /source onto cli.py's exit contract.

    The contract (#44): 0 success, 1 no results, 2 palace unavailable, 64
    bad args. Resolved here as:

    * **404 -> 2**, naming the route and the minimum daemon version. The
      route is missing, which is a deployment fact, not a user error.
    * **401/403 -> 2**, naming *auth*. Deliberately NOT the deploy message:
      a key mismatch reported as "deploy a newer daemon" is a refusal
      naming the wrong reason.
    * **400/422 -> 64**, carrying the daemon's own message. The daemon is
      the thing that knows which bound failed to parse, so it says so.
    * **anything else (5xx) -> 2**, palace unavailable.
    """
    code = result.code
    detail = (result.detail or "").strip()

    if code == 404:
        msg = (
            f"daemon lacks {path} — deploy palace-daemon >= {_WINDOW_MIN_DAEMON} "
            f"(current daemon at {_daemon_url()} does not serve this route)"
        )
        exit_code, error_key = 2, "route_missing"
    elif code in (401, 403):
        msg = (
            f"palace daemon at {_daemon_url()} rejected the credentials "
            f"({code}) — check PALACE_API_KEY" + (f": {detail}" if detail else "")
        )
        # 64, not 2 (#518). #512 chose 2 here deliberately, and its reason
        # still stands — an auth failure must not be reported with the DEPLOY
        # message — but that was an argument about the *message*, not the
        # code. A refused credential is "the daemon answered and rejected a
        # well-formed request", which the #44 contract numbers 64.
        exit_code, error_key = 64, "auth_failed"
    elif code in (400, 422):
        msg = detail or f"daemon rejected the request ({code})"
        exit_code, error_key = 64, "bad_request"
    else:
        msg = f"palace daemon at {_daemon_url()} failed ({code})" + (
            f": {detail}" if detail else ""
        )
        exit_code, error_key = 2, "daemon_error"

    if want_json:
        # Option C (#521). This family put the KEY in `error` and the prose in
        # `message`; the daemon family did the opposite. A caller could branch
        # on `window`/`source` and had to pattern-match English everywhere
        # else. Now the prose is in `error` everywhere and the key is in
        # `code` everywhere.
        #
        # `message` is kept as a duplicate of `error` for ONE release and is
        # DEPRECATED: the readers #512 created read it, and dropping it in the
        # same release that adds `code` would break them for no reason.
        _emit_json(
            {
                "error": msg,
                "code": error_key,
                "message": msg,  # DEPRECATED (#521) — remove one release after this
                "status": code,
                "route": path,
                "source": "daemon",
            }
        )
    else:
        print(msg, file=sys.stderr)
    sys.exit(exit_code)


def _window_require_daemon(verb: str, want_json: bool) -> None:
    """These verbs are daemon-strict; there is no local path to fall back to."""
    if _daemon_url():
        return
    msg = (
        f"{verb} requires the palace-daemon. Set PALACE_DAEMON_URL "
        "(the listing is served by the daemon's SQL, not by a local palace)."
    )
    if want_json:
        _emit_json(
            {
                "error": msg,
                "code": "daemon_required",
                "message": msg,  # DEPRECATED (#521)
                "source": "daemon",
            }
        )
    else:
        print(msg, file=sys.stderr)
    sys.exit(2)


def _print_window_drawers(payload: dict, *, show_ordering: bool) -> None:
    drawers = payload.get("drawers") or []
    for d in drawers:
        filed = d.get("filed_at") or "(no filed_at)"
        room = d.get("room") or "-"
        print(f"  {filed}  [{d.get('wing', '')}/{room}]  {d.get('drawer_id', '')}")
        preview = (d.get("content_preview") or "").replace("\n", " ")
        if preview:
            print(f"      {preview[:140]}")
    print(f"\n  {len(drawers)} drawer(s)")
    if show_ordering and payload.get("ordering"):
        # Say which time semantics answered. The contract is wall-clock
        # today and may become normalised instants (#506); a reader should
        # not have to infer which answer they got.
        print(f"  ordering: {payload['ordering']}")
    excluded = payload.get("excluded_no_filed_at") or 0
    if excluded:
        print(
            f"  excluded: {excluded} drawer(s) in this wing have no filed_at and "
            "cannot appear in a time window"
        )
    if payload.get("next_cursor"):
        print(f"  more: --cursor {payload['next_cursor']}")


def cmd_window(args) -> None:
    """Drawers in filed order between two bounds — unranked, paged.

    #500: ``search --since`` filters a *ranked* search, so inside a window
    you get whatever scores highest rather than the sequence, and ``list``
    has no time filter at all (against a 201K-drawer wing a date is ~200
    pages away). This is the chronological walk.

    Semantics are the palace's existing ones, not new: ``--from`` inclusive,
    ``--to`` exclusive, wall-clock comparison, and a drawer with no
    ``filed_at`` excluded while a bound is active — the same contract
    ``list --since`` and ``search --since`` use. What changed is that the
    daemon evaluates it in SQL instead of filtering in Python after
    fetching every row.
    """
    want_json = bool(getattr(args, "json", False))
    _window_require_daemon("window", want_json)

    params: dict = {}
    if getattr(args, "wing", None):
        params["wing"] = args.wing
    if getattr(args, "room", None):
        params["room"] = args.room
    if getattr(args, "since", None):
        params["since"] = args.since
    if getattr(args, "before", None):
        params["before"] = args.before
    if getattr(args, "source_file", None):
        params["source_file"] = args.source_file
    if getattr(args, "cursor", None):
        params["cursor"] = args.cursor
    params["limit"] = int(getattr(args, "limit", 100) or 100)

    try:
        result = _window_daemon_get("/window", params)
    except DaemonError as err:
        _fail_daemon_unavailable(err, want_json)
        return
    if result.code != 200:
        _window_route_failed("/window", result, want_json)
        return

    payload = result.payload or {}
    drawers = payload.get("drawers") or []
    if want_json:
        _emit_json(payload)
    else:
        _print_window_drawers(payload, show_ordering=True)
    # 1 means "ran and selected nothing" — a real answer about an empty
    # window, distinct from 0 (found some) and 2 (could not ask).
    sys.exit(0 if drawers else 1)


def cmd_source(args) -> None:
    """Every drawer from one source file, in chunk order (#502).

    Search hits carry ``source_file``; the natural next question is "give
    me the drawers from THAT file, in order", which had no command. Order
    is chunk order within the file — ``chunk_index`` numerically, so chunk
    10 follows chunk 2 — then filed time, then id.
    """
    want_json = bool(getattr(args, "json", False))
    _window_require_daemon("source", want_json)

    source_file = (getattr(args, "file", None) or "").strip()
    if not source_file:
        # 64 is "bad args", and this one is the CLI's own judgement rather
        # than the daemon's.
        msg = "source requires --file <transcript.jsonl>"
        if want_json:
            _emit_json({"error": "bad_request", "message": msg})
        else:
            print(msg, file=sys.stderr)
        sys.exit(64)

    params: dict = {"source_file": source_file}
    if getattr(args, "wing", None):
        params["wing"] = args.wing
    params["limit"] = int(getattr(args, "limit", 1000) or 1000)

    try:
        result = _window_daemon_get("/source", params)
    except DaemonError as err:
        _fail_daemon_unavailable(err, want_json)
        return
    if result.code != 200:
        _window_route_failed("/source", result, want_json)
        return

    payload = result.payload or {}
    drawers = payload.get("drawers") or []
    if want_json:
        _emit_json(payload)
    else:
        print(f"  source_file: {payload.get('source_file', source_file)}")
        _print_window_drawers(payload, show_ordering=False)
    sys.exit(0 if drawers else 1)


def _fail_daemon_unavailable(err, want_json: bool) -> None:
    """Transport failure on a window/source call -> exit 2.

    **Do not collapse this into ``_fail_daemon``.** That helper exits **1**,
    and it still does after #498/#499 landed (checked on merged main at
    ``f7db659c``: that PR's new ``sys.exit(2)`` is inside ``cmd_pending``'s
    import guard and does not touch ``_fail_daemon``). These two verbs are
    specified at 2 for a transport failure, because "the daemon is not
    answering" is *palace unavailable* under the #44 contract, not "no
    results".

    An earlier version of this docstring said the consolidation would
    become correct "once #499 lands". It did not, and leaving that sentence
    in place would have invited a tidy-up that silently changed these verbs
    from 2 to 1.

    ⚠️ That version also said: "if ``_fail_daemon`` ever does move to 2,
    delete this helper and that test still passes." #523 moved it to 2 and
    the test *does* still pass — which is exactly why the helper is still
    here. The retirement condition was written in exit-code terms, and the
    exit code is not the only contract:

        _fail_daemon_unavailable   {"error": "daemon_unavailable", "message": …}
        _window_route_failed       {"error": <key>, "message": …, "status", "route"}
        _fail_daemon               {"error": <text>, "source": "daemon"}

    ``window`` and ``source`` use **key + message** across the whole family,
    deliberately, so a client can branch on ``error`` without parsing prose.
    ``_fail_daemon`` puts the prose *in* ``error``. Folding would therefore
    change these two verbs' machine output and leave them inconsistent with
    their own sibling — while every existing test passed, because none of
    them asserted the shape.

    ⭐ The real retirement condition: delete this helper when the two
    families agree on a JSON shape, not when they agree on an exit code.
    ``test_transport_failure_json_keeps_the_window_family_shape`` now pins
    that, so the next fold attempt fails loudly instead of silently.
    """
    text = str(err)
    if want_json:
        _emit_json(
            {
                "error": text,
                "code": "daemon_unavailable",
                "message": text,  # DEPRECATED (#521) — see _window_route_failed
                "source": "daemon",
            }
        )
    else:
        print(
            f"palace daemon unreachable at {_daemon_url()} — "
            f"see mempalace status for diagnostics ({text})",
            file=sys.stderr,
        )
    sys.exit(2)


def _fail_daemon(
    err, want_json: bool, *, route: str | None = None, tool: str | None = None, **extra
) -> None:
    """The daemon could not serve the request → exit 2 (palace unavailable).

    ``DaemonError`` covers two situations and the distinction matters to
    whoever reads the line: a transport failure (the daemon is asleep,
    wrong port, no route) versus a JSON-RPC error the daemon itself
    returned (the tool raised server-side — e.g. a dropped postgres
    connection under an AGE query). Reporting the second as "unreachable"
    sends the reader after the wrong problem, so the MESSAGE still
    separates them even though the exit code no longer does: both mean
    the palace could not serve the request, which the contract calls 2.

    A 4xx refusal is NOT this. The daemon answered and rejected a
    well-formed request; that is 64, and it arrives through
    ``DaemonRequestError`` / ``_exit_daemon_request_error`` (#499).

    ``route``, ``tool`` and ``extra`` exist because the call sites this
    replaced did not all say the same thing, and a helper that cannot
    express what they said would make the CLI report LESS than it did:

    * ``err=None`` is the 404/401/403 case — ``_call_daemon_rest`` returns
      ``None`` and **nothing was raised**, so there is no exception to
      interpolate. The prose drops the parenthetical rather than printing
      "(None)", and ``route`` carries the only identifying information
      those sites ever had.
    * ``tool`` names the MCP tool that failed, in prose and in JSON, so
      consolidating ``_daemon_tool_or_fail``'s message loses neither.
    * ``extra`` carries structured fields a site already published —
      ``status`` from the two ``/cypher`` sites. Dropping a JSON field is
      a breaking change to a machine interface and is invisible to a
      prose diff (#523).
    """
    text = str(err) if err is not None else ""
    # A refusal that reached here is still a refusal (#518). Five of the eight
    # `_call_daemon_rest` consumers catch DaemonRequestError before DaemonError
    # and would exit 64 on their own; `_fast_hits`, `cmd_status` and cmd_doctor
    # do not, and would have reported a wrong API key as an outage. Routing it
    # here means no call site can get this wrong by the ORDER of its except
    # clauses — the "fixed one of two paths, read as success" shape.
    if isinstance(err, DaemonRequestError):
        _exit_daemon_request_error(err, want_json=want_json)

    if isinstance(err, DaemonBusyError):
        # A dict LITERAL on purpose: the contract test walks literals only, so
        # this is how `daemon_busy` in the header is shown to be emitted (#526).
        if want_json:
            payload = {
                "error": text,
                "code": "daemon_busy",
                "source": "daemon",
                "detail": err.detail,
            }
            if err.route:
                payload["route"] = err.route
            if tool is not None:
                payload["tool"] = tool
            payload.update(extra)
            _emit_json(payload)
        else:
            print(
                f"palace daemon at {_daemon_url()} is busy — {err.detail}; retry shortly",
                file=sys.stderr,
            )
        sys.exit(2)

    # Same predicate the tool helper used, kept verbatim so the JSON
    # `reachable` flag means exactly what it meant before the message moved.
    unreachable = not text.startswith("daemon error")
    if want_json:
        payload = {
            "code": "daemon_unreachable" if unreachable else "daemon_error",
            "error": f"daemon {route} unavailable" if err is None and route else text,
            "source": "daemon",
        }
        if tool is not None:
            payload["tool"] = tool
            payload["reachable"] = not unreachable
        payload.update(extra)
        _emit_json(payload)
    elif not unreachable:
        target = tool if tool else "the call"
        print(
            f"palace daemon at {_daemon_url()} rejected {target} — {text}",
            file=sys.stderr,
        )
    else:
        detail = f" ({err})" if err is not None else ""
        print(
            f"palace daemon unreachable at {_daemon_url()} — "
            f"see mempalace status for diagnostics{detail}",
            file=sys.stderr,
        )
    # 2, not 1: the operation could not run against the palace. 1 is
    # reserved for "it ran and selected nothing" (#44 contract, #514).
    sys.exit(2)


def _fail_tool(data, want_json: bool) -> None:
    """Inner-error envelope from a tool call → exit 2."""
    payload = data if isinstance(data, dict) else {"error": str(data)}
    msg = str(payload.get("error") or "tool call failed")
    if want_json:
        _emit_json(payload)
    else:
        print(f"\n  {msg}", file=sys.stderr)
    sys.exit(2)


def _fail_client(msg: str, want_json: bool, **extra) -> None:
    """Bad CLI input → exit 2 (the issue #44 client-error code)."""
    if want_json:
        _emit_json({"error": msg, "source": "cli", **extra})
    else:
        print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


def _tool_failed(data) -> bool:
    """True when a tool envelope reports failure.

    The write tools answer ``{"success": False, "error": ...}``; the read
    tools answer a bare ``{"error": ...}``. Treat either as a failure.
    """
    if not isinstance(data, dict):
        return False
    if data.get("success") is False:
        return True
    return "error" in data


# ── mempalace diary (#354) ────────────────────────────────────────────

_DIARY_DEFAULT_LIMIT = 10
_DIARY_MAX_LIMIT = 100  # tool_diary_read clamps last_n to 100
_DIARY_ACTIONS = ("write", "read", "agents")


def _resolve_agent_name(args):
    """``--agent`` wins, else ``MEMPALACE_AGENT_NAME`` from the environment.

    ``mempalace_diary_write`` / ``_read`` both require ``agent_name`` — the
    diary is per-agent by design — so issue #354's proposed shape
    (``mempalace diary write "text" --topic X``) needs one more input than
    the issue text shows. The env var keeps hook and agent wrappers from
    repeating the flag on every call.
    """
    agent = getattr(args, "agent", None)
    if agent and str(agent).strip():
        return str(agent).strip()
    env_agent = os.environ.get("MEMPALACE_AGENT_NAME", "").strip()
    return env_agent or None


def _read_diary_entry(args, want_json: bool) -> str:
    """Resolve the entry text: positional argument, or stdin for ``-``.

    Reading from stdin keeps hooks and shell pipelines from having to
    shell-quote a multi-line AAAK entry.
    """
    entry = getattr(args, "entry", None)
    if entry is None or entry == "-":
        try:
            entry = sys.stdin.read()
        except (OSError, ValueError) as e:
            _fail_client(f"could not read the diary entry from stdin: {e}", want_json)
    if not entry or not entry.strip():
        _fail_client(
            "diary write requires entry text (positional argument, or '-' to read stdin)",
            want_json,
        )
    return entry


def _print_diary_write(data: dict) -> None:
    """One-line confirmation: entry id, wing/topic, chunk count."""
    chunks = int(data.get("chunks") or 1)
    chunk_note = f" ({chunks} chunks)" if chunks > 1 else ""
    print(f"\n  Diary entry filed{chunk_note}")
    print(f"    id     {data.get('entry_id') or '?'}")
    print(f"    agent  {data.get('agent') or '?'}")
    print(f"    topic  {data.get('topic') or 'general'}")
    if data.get("timestamp"):
        print(f"    when   {data['timestamp']}")
    for warning in data.get("warnings") or []:
        print(f"    warn   {warning}")
    print()


def _filter_diary_entries(entries: list, topic=None, since=None) -> list:
    """Client-side ``--topic`` / ``--since`` narrowing.

    ``mempalace_diary_read`` takes only ``(agent_name, last_n, wing)``, so
    issue #354's ``--topic`` / ``--since`` filters run here over the
    fetched page rather than in the tool. Prefix-compare on the ISO
    timestamp keeps ``--since 2026-06-28`` working against both
    ``date`` (YYYY-MM-DD) and full ``filed_at`` timestamps.
    """
    rows = list(entries or [])
    if topic:
        wanted = str(topic).strip().lower()
        rows = [r for r in rows if str(r.get("topic") or "").lower() == wanted]
    if since:
        floor = str(since).strip()
        rows = [
            r for r in rows if str(r.get("timestamp") or r.get("date") or "")[: len(floor)] >= floor
        ]
    return rows


def _print_diary_entries(agent, entries: list, total=None, wing=None) -> None:
    """Newest-first entry list — header line per entry, then its content.

    ``agent`` may be ``None`` (#501): the identity-less read lists the diary
    room itself, so the writer goes on each row. Without that the listing
    cannot tell the reader which ``--agent`` to ask for next, which is the
    whole reason the entries were unreachable. The agent-scoped rendering is
    byte-for-byte unchanged — a name on every row would just be noise there.
    """
    if not entries:
        if agent:
            print(f"\n  No diary entries for '{agent}'.\n")
        else:
            scope = f" in wing '{wing}'" if wing else ""
            print(f"\n  No diary entries{scope}.\n")
            print("    'mempalace diary agents' lists the agents that have entries.\n")
        return
    suffix = f" of {total}" if total is not None else ""
    label = agent or (f"{wing} — all agents" if wing else "all agents")
    print(
        f"\n  DIARY — {label}  ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}{suffix})"
    )
    for entry in entries:
        stamp = entry.get("timestamp") or entry.get("date") or "?"
        topic = entry.get("topic") or "general"
        who = "" if agent else f"  {entry.get('agent') or '?'}"
        print(f"\n  [{stamp}]{who}  {topic}  ({entry.get('drawer_id') or '?'})")
        for line in str(entry.get("content") or "").splitlines():
            print(f"    {line}")
    print()


def _print_diary_agents(data: dict) -> None:
    """``diary agents`` — who has written, how much, how recently (#501).

    A ``truncated`` scan makes every count a LOWER BOUND; saying "1713"
    when the scan stopped at the cap would be a report that disagrees with
    what was measured, so the caveat is printed rather than implied.
    """
    agents = data.get("agents") or []
    wing = data.get("wing")
    if not agents:
        scope = f" in wing '{wing}'" if wing else ""
        print(f"\n  No diary entries{scope} — no agent has written one.\n")
        return
    scope = f" — wing {wing}" if wing else ""
    print(f"\n  DIARY AGENTS{scope}  ({len(agents)})")
    width = max(len(str(row.get("agent") or "?")) for row in agents)
    for row in agents:
        name = str(row.get("agent") or "?")
        print(f"    {name:<{width}}  {row.get('entries') or 0:>7}  {row.get('latest') or ''}")
    if data.get("truncated"):
        print(f"\n    counts are a lower bound — the scan stopped at {data.get('scanned')} drawers")
    print()


def cmd_diary(args):
    """``mempalace diary write|read`` — the agent diary at the CLI (#354).

    ``write`` wraps ``mempalace_diary_write``; ``read`` wraps
    ``mempalace_diary_read``; ``agents`` wraps ``mempalace_diary_agents``.
    ``read``'s ``--topic`` / ``--since`` filters are applied client-side —
    the tool has no such parameters.

    Only ``write`` needs an agent name (#501). A read does not: the hook
    writes ``agent_name=<harness>`` (``claude-code`` / ``codex`` /
    ``gemini-cli``) and never consults ``identity.txt`` or
    ``MEMPALACE_AGENT_NAME``, so requiring one on read meant entries
    plainly visible in ``list --wing W`` answered "No diary entries" to
    every name a reader could guess. ``agents`` exists to discover the
    names that do work.
    """
    fmt = _resolve_tool_format(args)
    want_json = fmt == "json"
    action = getattr(args, "diary_action", None)

    agent = _resolve_agent_name(args)

    # Structural dispatch (#501). This function previously had NO
    # sub-dispatch: `write` returned and EVERYTHING else fell through to the
    # read path, so a verb registered in argparse but unhandled here ran a
    # read, printed a plausible listing and exited 0. Measured before the
    # fix: action "bogus-verb" called mempalace_diary_read and exited 0. A
    # table cannot fall through, and an unknown verb is a usage error.
    handler = {
        "agents": lambda: _diary_agents(args, want_json),
        "write": lambda: _diary_write(args, agent, want_json),
        "read": lambda: _diary_read(args, agent, want_json),
    }.get(action)
    if handler is None:
        # Exit 2, not 64. A verb argparse rejects (`diary bogus`) and a verb it
        # accepts but this table does not handle are the same mistake from the
        # user's side, and argparse's own parse-error code is 2 — so 64 here
        # would split one user error across two codes on the accident of
        # whether the subparser happens to be registered. 64 is reserved for
        # "the daemon rejected a well-formed request" (#514).
        # `_fail_client` rather than a bare print: it is the established
        # client-error path and it emits a JSON document under --json, which a
        # hand-rolled stderr print does not.
        _fail_client(
            f"mempalace diary: choose an action ({', '.join(_DIARY_ACTIONS)}). "
            "See `mempalace diary --help`.",
            want_json,
        )
    handler()


def _diary_agents(args, want_json: bool) -> None:
    """``diary agents`` — which agent names have diary entries (#501)."""
    payload = {}
    if getattr(args, "wing", None):
        payload["wing"] = args.wing
    try:
        data = _call_tool_routed(args, "mempalace_diary_agents", "tool_diary_agents", payload)
    except DaemonError as e:
        _fail_daemon(e, want_json)
    if _tool_failed(data):
        _fail_tool(data, want_json)
    found = bool((data or {}).get("agents"))
    if want_json:
        _emit_json(data)
    else:
        _print_diary_agents(data or {})
    # Contract #44: 1 is "the operation ran and selected nothing". The message
    # prints either way — the code is for the caller's script.
    if not found:
        sys.exit(1)


def _diary_write(args, agent, want_json: bool) -> None:
    """``diary write`` — the one action that genuinely needs an identity.

    The agent guard lives HERE and nowhere else. Before #501 it ran ahead of
    the action branch, so every verb demanded an identity that only this one
    uses.
    """
    if not agent:
        _fail_client(
            "diary write requires an agent name — pass --agent NAME or set MEMPALACE_AGENT_NAME",
            want_json,
        )
    payload = {"agent_name": agent, "entry": _read_diary_entry(args, want_json)}
    if getattr(args, "topic", None):
        payload["topic"] = args.topic
    if getattr(args, "wing", None):
        payload["wing"] = args.wing
    if getattr(args, "session_id", None):
        payload["session_id"] = args.session_id
    try:
        data = _call_tool_routed(args, "mempalace_diary_write", "tool_diary_write", payload)
    except DaemonError as e:
        _fail_daemon(e, want_json)
    if _tool_failed(data):
        _fail_tool(data, want_json)
    if want_json:
        _emit_json(data)
        return
    _print_diary_write(data or {})


def _diary_read(args, agent, want_json: bool) -> None:
    """``diary read`` — keyed on drawers; ``agent`` may be ``None`` (#501)."""
    topic = getattr(args, "topic", None)
    since = getattr(args, "since", None)
    limit = max(1, min(int(getattr(args, "limit", None) or _DIARY_DEFAULT_LIMIT), _DIARY_MAX_LIMIT))
    # With a client-side filter in play, ask for the whole page the tool
    # will give us (capped at 100) and narrow afterwards — otherwise
    # ``--topic X --limit 5`` could return nothing while matching entries
    # sit just outside the requested window.
    fetch_n = _DIARY_MAX_LIMIT if (topic or since) else limit
    payload = {"last_n": fetch_n}
    # Omitted, not blank: a blank agent_name is what the pre-#501 tool
    # rejects with "agent_name must be a non-empty string".
    if agent:
        payload["agent_name"] = agent
    if getattr(args, "wing", None):
        payload["wing"] = args.wing
    try:
        data = _call_tool_routed(args, "mempalace_diary_read", "tool_diary_read", payload)
    except DaemonError as e:
        # MEASURED against the production daemon 2026-09-17: a daemon whose
        # mempalace predates #501 rejects an omitted agent_name at SCHEMA
        # validation, before dispatch — `-32602: Missing required parameter
        # 'agent_name' for tool mempalace_diary_read`, which arrives here as
        # a DaemonError, not as a tool-error envelope. An earlier draft
        # guarded only the envelope below and would never have fired; the
        # real-CLI probe is what caught that.
        if not agent and "agent_name" in str(e):
            _fail_daemon(
                DaemonError(
                    "daemon error: this daemon's mempalace predates the "
                    "identity-less diary read (#501), so it still requires an "
                    "agent. Pass --agent NAME, or update the daemon. "
                    f"Daemon said: {e}"
                ),
                want_json,
            )
        _fail_daemon(e, want_json)
    if _tool_failed(data):
        # The same situation reported as a tool envelope instead of a
        # JSON-RPC error — reachable when a caller sends agent_name="" rather
        # than omitting it. Kept deliberately: it is the shape the tool
        # itself produces, and it costs one branch.
        if not agent and "agent_name" in str((data or {}).get("error") or ""):
            # The reader passed no --agent, so echoing the tool's complaint
            # about agent_name sends them after a flag they never used.
            # Name the real cause: the daemon is older than this feature.
            data = dict(data or {})
            data["error"] = (
                "this daemon's mempalace is older than the identity-less diary read "
                "(#501) — pass --agent NAME, or run 'mempalace diary agents' to see "
                "which names exist, or update the daemon"
            )
            data["reason"] = "daemon_predates_identityless_diary_read"
        _fail_tool(data, want_json)

    data = data or {}
    entries = _filter_diary_entries(data.get("entries") or [], topic=topic, since=since)[:limit]
    if want_json:
        out = dict(data)
        out["entries"] = entries
        out["showing"] = len(entries)
        if topic:
            out["topic_filter"] = topic
        if since:
            out["since_filter"] = since
        _emit_json(out)
    else:
        _print_diary_entries(
            data.get("agent") or agent,
            entries,
            total=data.get("total"),
            wing=data.get("wing") or getattr(args, "wing", None),
        )
    # Counted AFTER the client-side --topic/--since narrowing: a page that
    # arrived full but filtered to nothing still selected nothing.
    if not entries:
        sys.exit(1)


# ── mempalace kg (#357) ───────────────────────────────────────────────
#
# ``mempalace kg stats`` is deliberately absent: issue #357 asked us to
# verify the overlap first, and ``mempalace stats --section kg`` already
# renders the same entity/triple/relationship-type block (and
# ``mempalace graph`` embeds ``kg_stats`` in its snapshot). A third
# spelling of the same read would be redundant surface.

_KG_TIMELINE_DEFAULT_LIMIT = 20
# The graph backends' ``timeline()`` returns at most this many rows and the MCP
# tool doesn't expose the parameter, so a larger ``--limit`` is a request the
# read path cannot satisfy. Confirmed live: ``--limit 200`` → ``count: 100``.
_KG_TIMELINE_TOOL_CAP = 100


def _print_kg_fact(data: dict, verb: str) -> None:
    """Confirmation line for kg add / kg invalidate."""
    fact = data.get("fact") or ""
    print(f"\n  {verb}: {fact}")
    for key in ("valid_from", "valid_to", "ended", "context"):
        if data.get(key):
            print(f"    {key:<11}{data[key]}")
    print()


def _print_kg_timeline(data: dict, rows: list) -> None:
    """Chronological fact rows: valid_from · subject → predicate → object."""
    entity = data.get("entity") or "all"
    if not rows:
        print(f"\n  No timeline facts for '{entity}'.\n")
        return
    as_of = data.get("as_of")
    scope = f"  as_of={as_of}" if as_of else ""
    print(f"\n  TIMELINE — {entity}  ({len(rows)} of {data.get('count', len(rows))} facts){scope}")
    for row in rows:
        start = str(row.get("valid_from") or "—")
        end = str(row.get("valid_to") or "")
        window = f"{start} → {end}" if end else start
        subject = row.get("subject") or "?"
        predicate = row.get("predicate") or "?"
        obj = row.get("object") or "?"
        line = f"    {window:<24}  {subject} → {predicate} → {obj}"
        if row.get("context"):
            line += f"   [{row['context']}]"
        print(line)
    print()


def _kg_invalidate_confirmed(args, fact_label: str, want_json: bool) -> bool:
    """Confirmation gate before retracting a fact.

    ``--confirm`` skips the gate. On a TTY (and not json) we prompt and
    proceed only on y/yes. Non-interactive or json without ``--confirm``
    is refused with exit 2 — mirrors ``_bulk_move_confirm`` so no pipeline
    can silently rewrite graph history.
    """
    if bool(getattr(args, "confirm", False)):
        return True
    msg = f"refusing to invalidate '{fact_label}' without --confirm in a non-interactive shell"
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if want_json or not interactive:
        if want_json:
            _emit_json({"error": "confirmation_required", "hint": msg, "fact": fact_label})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)
    answer = input(f"Invalidate '{fact_label}'? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def cmd_kg(args):
    """``mempalace kg add|invalidate|timeline`` — KG writes + temporal read (#357).

    Deviations from issue #357's sketch, driven by the tool schemas:
    ``kg invalidate`` addresses a fact by ``--subject/--predicate/--object``
    (``mempalace_kg_invalidate`` has no triple ids and no reason field), and
    ``kg timeline``'s ``--limit`` truncates client-side because
    ``mempalace_kg_timeline`` takes only ``(entity, as_of)``.

    A ``--limit`` above ``_KG_TIMELINE_TOOL_CAP`` cannot be honoured: the
    graph backend's own ``timeline()`` stops at that many rows and the tool
    doesn't expose the parameter, so the CLI reports how many it actually
    received rather than implying the requested window was searched. Seen
    live: ``kg timeline JP --limit 200`` returns ``count: 100``.
    """
    fmt = _resolve_tool_format(args)
    want_json = fmt == "json"
    action = getattr(args, "kg_action", None)

    if action == "add":
        payload = {
            "subject": args.subject,
            "predicate": args.predicate,
            "object": args.object,
        }
        for flag, key in (
            ("valid_from", "valid_from"),
            ("valid_to", "valid_to"),
            ("source_closet", "source_closet"),
            ("source_file", "source_file"),
            ("source_drawer_id", "source_drawer_id"),
            ("context", "context"),
        ):
            value = getattr(args, flag, None)
            if value:
                payload[key] = value
        try:
            data = _call_tool_routed(args, "mempalace_kg_add", "tool_kg_add", payload)
        except DaemonError as e:
            _fail_daemon(e, want_json)
        if _tool_failed(data):
            _fail_tool(data, want_json)
        if want_json:
            _emit_json(data)
            return
        _print_kg_fact(data or {}, "Added")
        return

    if action == "invalidate":
        fact_label = f"{args.subject} → {args.predicate} → {args.object}"
        if not _kg_invalidate_confirmed(args, fact_label, want_json):
            print("  Aborted.")
            return
        payload = {
            "subject": args.subject,
            "predicate": args.predicate,
            "object": args.object,
        }
        if getattr(args, "ended", None):
            payload["ended"] = args.ended
        try:
            data = _call_tool_routed(args, "mempalace_kg_invalidate", "tool_kg_invalidate", payload)
        except DaemonError as e:
            _fail_daemon(e, want_json)
        if _tool_failed(data):
            _fail_tool(data, want_json)
        if want_json:
            _emit_json(data)
            return
        _print_kg_fact(data or {}, "Invalidated")
        return

    # timeline
    payload = {}
    if getattr(args, "entity", None):
        payload["entity"] = args.entity
    if getattr(args, "as_of", None):
        payload["as_of"] = args.as_of
    try:
        data = _call_tool_routed(args, "mempalace_kg_timeline", "tool_kg_timeline", payload)
    except DaemonError as e:
        _fail_daemon(e, want_json)
    if _tool_failed(data):
        _fail_tool(data, want_json)

    data = data or {}
    limit = max(1, int(getattr(args, "limit", None) or _KG_TIMELINE_DEFAULT_LIMIT))
    rows = list(data.get("timeline") or [])[:limit]
    if want_json:
        out = dict(data)
        out["timeline"] = rows
        out["showing"] = len(rows)
        _emit_json(out)
        return
    _print_kg_timeline(data, rows)


# ── mempalace walk (#359) ─────────────────────────────────────────────

_WALK_DEFAULT_DEPTH = 2
_WALK_DEFAULT_LIMIT = 50


def _print_walk_rows(data: dict) -> None:
    """Render ``mempalace_walk_palace``'s wing/room/drawer/entity rows."""
    rows = data.get("walk") or []
    start = data.get("start") or {}
    anchor = ", ".join(f"{k}={v}" for k, v in start.items() if v) or "?"
    if not rows:
        print(f"\n  Walk from {anchor} reached nothing.\n")
        return
    print(f"\n  WALK — {anchor}  ({len(rows)} rows)")
    for row in rows:
        parts = [
            f"{key}={row[key]}"
            for key in ("wing", "room", "drawer", "entity")
            if row.get(key) is not None
        ]
        print("    " + "  ".join(parts))
    stats = data.get("stats") or {}
    if stats:
        print("  " + "  ".join(f"{k}={v}" for k, v in stats.items()))
    print()


_TRAVERSE_LIST_PREVIEW = 12


def _traverse_rows(data) -> list:
    """Normalise ``mempalace_traverse``'s payload to a list of hop records.

    Verified live against the production daemon (2026-08-20): the tool
    answers with a **bare JSON list** of ``{room, wings, halls, ...}``
    records, not a dict envelope. Older/other backends have wrapped the
    same rows under a key, so accept both rather than betting on one.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("connections", "rooms", "path", "tunnels"):
            rows = data.get(key)
            if isinstance(rows, list):
                return rows
    return []


def _print_traverse_rows(data) -> None:
    """Render the tunnel hops one room at a time, list fields summarised."""
    rows = _traverse_rows(data)
    if not rows:
        print("\n  Tunnel walk returned no connections.\n")
        return
    print(f"\n  TUNNEL WALK — {len(rows)} hop(s)")
    for row in rows:
        if not isinstance(row, dict):
            print(f"    {row}")
            continue
        label = row.get("room") or row.get("target_room") or "?"
        print(f"    room={label}")
        for key, value in row.items():
            if key in ("room", "target_room"):
                continue
            if isinstance(value, list):
                preview = ", ".join(str(v) for v in value[:_TRAVERSE_LIST_PREVIEW])
                if len(value) > _TRAVERSE_LIST_PREVIEW:
                    preview += " …"
                print(f"      {key} ({len(value)}): {preview}")
            elif value is not None:
                print(f"      {key}: {value}")
    print()


def cmd_walk(args):
    """``mempalace walk`` — traverse the palace graph (#359).

    Two tools behind one verb: ``--follow palace`` (default) calls
    ``mempalace_walk_palace`` from a ``--wing`` / ``--room`` / ``--entity``
    anchor, and ``--follow tunnels`` calls ``mempalace_traverse`` from a
    ``--room`` anchor with ``--depth`` as its hop budget. Issue #359's
    ``--from <drawer_id>`` is not offered: neither tool accepts a drawer
    anchor (``mempalace why <drawer_id>`` is the per-drawer view).
    """
    fmt = _resolve_tool_format(args)
    want_json = fmt == "json"

    depth = max(1, min(int(getattr(args, "depth", None) or _WALK_DEFAULT_DEPTH), 5))
    limit = max(1, min(int(getattr(args, "limit", None) or _WALK_DEFAULT_LIMIT), 500))
    wing = getattr(args, "wing", None)
    room = getattr(args, "room", None)
    entity = getattr(args, "entity", None)
    anchors = [a for a in (wing, room, entity) if a]

    if getattr(args, "follow", "palace") == "tunnels":
        if not room or wing or entity:
            _fail_client(
                "walk --follow tunnels traverses from a room — pass exactly --room NAME",
                want_json,
            )
        try:
            data = _call_tool_routed(
                args,
                "mempalace_traverse",
                "tool_traverse_graph",
                {"start_room": room, "max_hops": depth},
            )
        except DaemonError as e:
            _fail_daemon(e, want_json)
        if _tool_failed(data):
            _fail_tool(data, want_json)
        if want_json:
            _emit_json(data)
            return
        _print_traverse_rows(data or {})
        return

    if len(anchors) != 1:
        _fail_client(
            "walk needs exactly one anchor — pass one of --wing, --room, --entity",
            want_json,
        )
    payload: dict = {"depth": depth, "limit": limit}
    if wing:
        payload["start_wing"] = wing
    elif room:
        payload["start_room"] = room
    else:
        payload["start_entity"] = entity
    try:
        data = _call_tool_routed(args, "mempalace_walk_palace", "tool_walk_palace", payload)
    except DaemonError as e:
        _fail_daemon(e, want_json)
    if _tool_failed(data):
        _fail_tool(data, want_json)
    if want_json:
        _emit_json(data)
        return
    _print_walk_rows(data or {})


# ── mempalace rate (#361) ─────────────────────────────────────────────


def cmd_rate(args):
    """``mempalace rate <drawer_id> --useful|--not-useful`` (#361).

    ``mempalace_rate_memory`` records a boolean, not a 1–5 score, and has
    no field for a free-text reason — so issue #361's ``--score N
    --reason "..."`` sketch is expressed as the two boolean flags. The
    rating lands in drawer metadata and becomes a bounded ranking signal;
    it never touches verbatim content.
    """
    fmt = _resolve_tool_format(args)
    want_json = fmt == "json"

    drawer_id = getattr(args, "drawer_id", None)
    if not drawer_id or not str(drawer_id).strip():
        _fail_client("rate requires a drawer ID (positional argument)", want_json)

    useful = getattr(args, "useful", None)
    if useful is None:
        _fail_client("rate requires --useful or --not-useful", want_json)

    payload = {"drawer_id": str(drawer_id).strip(), "useful": bool(useful)}
    try:
        data = _call_tool_routed(args, "mempalace_rate_memory", "tool_rate_memory", payload)
    except DaemonError as e:
        _fail_daemon(e, want_json)
    if _tool_failed(data):
        _fail_tool(data, want_json)
    if want_json:
        _emit_json(data)
        return
    data = data or {}
    verdict = "useful" if payload["useful"] else "not useful"
    print(f"\n  Rated {payload['drawer_id']} {verdict}")
    print(
        f"    useful={data.get('rating_useful', '?')}  "
        f"not_useful={data.get('rating_not_useful', '?')}  "
        f"net={data.get('net_rating', '?')}\n"
    )


def cmd_logstream(args):
    import json

    as_json = getattr(args, "json", False)
    try:
        ls = _open_logstream(args)
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    try:
        if args.logstream_action == "append":
            try:
                body = _read_text_arg(args.body, args.body_file)
                event = ls.append_event(
                    type=args.type,
                    stream=args.stream,
                    room=args.room,
                    topic=getattr(args, "topic", None),
                    from_agent=args.from_agent,
                    to_agent=args.to_agent,
                    correlation_id=args.correlation_id,
                    branch=args.branch,
                    base_commit=args.base_commit,
                    status=args.status,
                    body=body,
                    metadata=_parse_metadata_arg(args.metadata),
                    artifact_ids=args.artifact_id or None,
                )
            except (ValueError, OSError) as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(event, indent=2, ensure_ascii=False))
            else:
                print("Appended:")
                _print_event_line(event)
        elif args.logstream_action in ("list", "wait"):
            filters = {
                "stream": args.stream,
                "room": args.room,
                "topic": getattr(args, "topic", None),
                "type": args.type,
                "to_agent": args.to_agent,
                "from_agent": args.from_agent,
                "correlation_id": args.correlation_id,
                "status": args.status,
                "since_event_id": args.since_event_id,
                "since_created_at": args.since_created_at,
            }
            try:
                if args.logstream_action == "list":
                    events = ls.list_events(
                        limit=args.limit,
                        order=getattr(args, "order", "asc"),
                        before_event_id=getattr(args, "before_event_id", None),
                        **filters,
                    )
                    result = {"events": events, "count": len(events)}
                else:
                    result = ls.wait_events(timeout_ms=args.timeout_ms, limit=args.limit, **filters)
                    result["count"] = len(result["events"])
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                if result.get("timed_out"):
                    print("Timed out; no matching events.")
                elif not result["events"]:
                    print("No matching events.")
                else:
                    print(f"{result['count']} event(s):")
                    for event in result["events"]:
                        _print_event_line(event)
            if result.get("timed_out"):
                sys.exit(2)
        elif args.logstream_action == "watch":
            _logstream_watch(ls, args, as_json)
        elif args.logstream_action == "sync":
            from ..logsync import load_peers, sync_all, sync_with_peer

            palace_path = (
                os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
            )
            try:
                if args.peer:
                    results = [sync_with_peer(ls, args.peer, args.token or "")]
                else:
                    if not load_peers(palace_path):
                        _logstream_fail(
                            f"no peers configured ({palace_path}/peers.json) and no --peer given",
                            as_json,
                        )
                    results = sync_all(ls, palace_path)
            except Exception as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            else:
                for stats in results:
                    if stats.get("error"):
                        print(
                            f"  {stats.get('peer_name', stats['peer_url'])}: ERROR {stats['error']}"
                        )
                    else:
                        print(
                            f"  {stats.get('peer_name', stats['peer_url'])} "
                            f"({stats['peer_replica']}): +{stats['pulled_events']} events, "
                            f"+{stats['pulled_artifacts']} artifacts"
                        )
            if any(s.get("error") for s in results):
                sys.exit(1)
        elif args.logstream_action == "ack":
            try:
                event = ls.ack_event(
                    args.event_id,
                    from_agent=args.from_agent,
                    status=args.status,
                    body=args.body or "",
                    topic=getattr(args, "topic", None),
                )
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(event, indent=2, ensure_ascii=False))
            else:
                print("Acknowledged:")
                _print_event_line(event)
    finally:
        ls.close()


def _codex_task_runner(workspace: Path, prompt: str) -> tuple[list[str], dict]:
    return ["codex", "exec", "--cd", str(workspace), prompt], {}


def _claude_task_runner(workspace: Path, prompt: str) -> tuple[list[str], dict]:
    return ["claude", "--print", prompt], {"cwd": str(workspace)}


_TASK_RUNNER_ADAPTERS = {
    "codex": ("-codex", _codex_task_runner),
    "claude": ("-claude", _claude_task_runner),
}


def _validate_task_workspace(workspace: Path, task: dict) -> None:
    """Prove a controlled launch starts from the task's exact clean Git state."""
    import subprocess

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
            raise ValueError(f"workspace Git validation failed: {detail}")
        return completed.stdout.strip()

    branch = git("branch", "--show-current")
    if branch != task["branch"]:
        raise ValueError(
            f"workspace branch is {branch or '(detached)'!r}; task requires {task['branch']!r}"
        )
    try:
        expected_commit = git("rev-parse", "--verify", f"{task['base_commit']}^{{commit}}")
    except ValueError as exc:
        raise ValueError(
            f"task base commit {task['base_commit']!r} does not resolve in the workspace"
        ) from exc
    head_commit = git("rev-parse", "HEAD")
    if head_commit != expected_commit:
        raise ValueError(f"workspace base commit is {head_commit}; task requires {expected_commit}")
    if git("status", "--porcelain"):
        raise ValueError(
            "workspace has uncommitted changes; controlled launch requires a clean checkout"
        )


def cmd_task(args):
    """Create and run complete logstream tasks through a small public interface."""
    import json

    from ..tasks import create_task, task_handoff, validate_task_request

    as_json = getattr(args, "json", False)
    task_file = getattr(args, "task_file", None)
    ls = None
    if args.task_action == "create" or not task_file:
        try:
            ls = _open_logstream(args)
        except Exception as exc:
            _logstream_fail(str(exc), as_json)
    try:
        if args.task_action == "create":
            try:
                goal = _read_text_arg(args.goal, args.goal_file)
                done = _read_text_arg(args.done, args.done_file)
                result = create_task(
                    ls,
                    project=args.project,
                    from_agent=args.from_agent,
                    to_agent=args.to_agent,
                    goal=goal,
                    branch=args.branch,
                    base_commit=args.base_commit,
                    done=done,
                )
            except (ValueError, OSError) as exc:
                _logstream_fail(str(exc), as_json)
            event = result["task"]
            handoff = result["handoff"]
            correlation_id = event["correlation_id"]
            if as_json:
                print(json.dumps({"task": event, "handoff": handoff}, indent=2, ensure_ascii=False))
            else:
                print(f"Task created: {correlation_id}")
                print(f"Event: {event['id']}")
                print(f"To: {args.to_agent}")
                print("\nReady to paste:")
                print(handoff)
        elif args.task_action == "launch":
            import subprocess

            try:
                if task_file:
                    task = json.loads(Path(task_file).read_text(encoding="utf-8"))
                    validate_task_request(task, source="--task-file")
                    correlation_id = task["correlation_id"]
                else:
                    correlation_id = args.correlation_id
                    events = ls.list_events(
                        type="task.request", correlation_id=correlation_id, limit=2
                    )
                    if not events:
                        raise ValueError(f"task {correlation_id!r} not found")
                    if len(events) > 1:
                        raise ValueError(
                            f"task {correlation_id!r} has multiple requests; refusing to guess"
                        )
                    task = validate_task_request(events[0], source=f"task {correlation_id!r}")
                addressed_agent = task["to_agent"]
                if args.agent and addressed_agent not in (None, "*", args.agent):
                    raise ValueError(
                        f"task is addressed to {addressed_agent!r}, not {args.agent!r}"
                    )
                agent = args.agent or addressed_agent
                if not agent or agent == "*":
                    raise ValueError(
                        "broadcast tasks require --agent for a concrete worker identity"
                    )
                expected_suffix, runner_adapter = _TASK_RUNNER_ADAPTERS[args.runner]
                actual_suffix = next(
                    (
                        suffix
                        for suffix, _adapter in _TASK_RUNNER_ADAPTERS.values()
                        if agent.endswith(suffix)
                    ),
                    None,
                )
                if actual_suffix is not None and actual_suffix != expected_suffix:
                    raise ValueError(
                        f"runner {args.runner} does not match addressed identity {agent!r}"
                    )
                workspace = Path(os.path.expanduser(args.workspace)).resolve()
                if not workspace.is_dir():
                    raise ValueError(f"workspace is not a directory: {workspace}")
                _validate_task_workspace(workspace, task)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                _logstream_fail(str(exc), as_json)

            prompt = task_handoff(correlation_id, agent)
            command, runner_kwargs = runner_adapter(workspace, prompt)
            print(f"Launching {correlation_id} with {args.runner} as {agent}")
            # Release the SQLite handle before the child connects back to the
            # same logstream. The child owns its own process lifetime and MCP
            # connection; no shell is involved in constructing this command.
            if ls is not None:
                ls.close()
                ls = None
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    **runner_kwargs,
                )
            except OSError as exc:
                _logstream_fail(f"could not start {args.runner}: {exc}", as_json)
            if completed.returncode:
                sys.exit(completed.returncode)
    finally:
        if ls is not None:
            ls.close()


def cmd_artifact(args):
    import json

    as_json = getattr(args, "json", False)
    try:
        ls = _open_logstream(args)
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    try:
        if args.artifact_action == "put":
            try:
                content = _read_text_arg(args.content, args.file, default=None)
                if content is None:
                    content = _read_stdin_exact()
                artifact = ls.put_artifact(
                    kind=args.kind,
                    content=content,
                    created_by=args.created_by,
                    metadata=_parse_metadata_arg(args.metadata),
                )
            except (ValueError, OSError) as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(artifact, indent=2, ensure_ascii=False))
            else:
                print(f"Stored {artifact['id']}  kind={artifact['kind']}")
                print(f"  sha256={artifact['sha256']}")
                print(f"  size={artifact['size_bytes']} bytes")
            # Warnings go to stderr in both modes so `--json | jq` stays
            # clean while interactive callers still can't miss them.
            for warning in artifact.get("warnings", []):
                print(f"Warning: {warning}", file=sys.stderr)
        elif args.artifact_action == "get":
            try:
                artifact = ls.get_artifact(args.artifact_id)
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if artifact is None:
                _logstream_fail(f"artifact {args.artifact_id!r} not found", as_json)
            if args.out:
                # write_bytes, not write_text: on Windows the text layer
                # expands LF back to CRLF, so the file on disk would not
                # match the sha256 the user is told to verify.
                Path(os.path.expanduser(args.out)).write_bytes(artifact["content"].encode("utf-8"))
            if as_json:
                if args.out:
                    artifact = {**artifact, "content_written_to": args.out}
                    artifact.pop("content")
                print(json.dumps(artifact, indent=2, ensure_ascii=False))
            elif args.out:
                print(f"Wrote {artifact['size_bytes']} bytes to {args.out}")
                print(f"  sha256={artifact['sha256']}")
            else:
                # Exact content on stdout so `mempalace artifact get ID | git apply`
                # works; metadata would corrupt the stream. Written through
                # .buffer because the text layer would re-translate newlines
                # on Windows — the pipe must carry the stored bytes.
                _write_stdout_exact(artifact["content"])
    finally:
        ls.close()
