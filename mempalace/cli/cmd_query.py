# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_search(args):
    fmt = _resolve_search_format(args)
    want_json = fmt == "json"
    n_results = _resolve_search_limit(args)
    tags = list(args.tags) if getattr(args, "tags", None) else None
    search_mode = getattr(args, "mode", None) or "auto"
    quiet = bool(getattr(args, "quiet", False)) or want_json
    if _daemon_strict() and not args.palace:
        arguments = {"query": args.query, "limit": n_results}
        if args.wing:
            arguments["wing"] = args.wing
        if args.room:
            arguments["room"] = args.room
        if tags:
            arguments["tags"] = tags
        try:
            data = None

            if search_mode == "hybrid":
                data = _daemon_search_hybrid(args.query, n_results, wing=args.wing, room=args.room)
            elif search_mode == "fast":
                data = _daemon_search_fast(args.query, n_results, wing=args.wing)
            elif search_mode == "auto":
                data = _daemon_search_auto(args, n_results, tags)

            if data is None:
                data = _call_daemon_tool("mempalace_search", arguments)
                results = data.get("results") if isinstance(data, dict) else None
                annotate(results)
                collapse_identical_text(results)
                prefer_curated(results)
        except DaemonError as e:
            # One renderer (#536's contract): `code` is branchable, `error` is
            # prose, and busy is its own key. Before this the search verb emitted
            # `error`+`source` with no `code` — the one search-shaped site #536
            # did not reach.
            _fail_daemon(e, want_json, route=f"search/{search_mode}", query=args.query)
        if want_json:
            data.setdefault("query", args.query)
            _emit_json(data)
            sys.exit(0 if (data.get("results") or []) else 1)
        if not quiet:
            source = data.get("source", "mcp")
            print(f"  [{source}]", file=sys.stderr)
        _print_daemon_search(args.query, data, wing=args.wing, room=args.room, fmt=fmt, quiet=quiet)
        if "error" in data and not data.get("results"):
            sys.exit(2)
        return

    from ..searcher import SearchError, search, search_memories

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    # Upstream v3.9: a live HTTP hub holds the writer lease and keeps a warm
    # palace instance — prefer it for local-path searches when forwardable.
    if _search_args_forwardable(args) and _forward_search_to_hub(args, palace_path):
        return

    if want_json:
        _emit_local_search_json(
            query=args.query,
            palace_path=palace_path,
            wing=args.wing,
            room=args.room,
            tags=tags,
            n_results=n_results,
            search_memories=search_memories,
        )
        return

    # Local-path fallback for ``compact`` / ``full`` reuses the daemon
    # renderer by calling ``search_memories`` directly. Plain ``table``
    # stays on the legacy ``searcher.search`` printer so existing local
    # callers keep their familiar output.
    if fmt in ("compact", "full"):
        result = search_memories(
            args.query,
            palace_path,
            wing=args.wing,
            room=args.room,
            tags=tags,
            n_results=n_results,
        )
        _print_daemon_search(
            args.query, result, wing=args.wing, room=args.room, fmt=fmt, quiet=quiet
        )
        if "error" in result and not result.get("results"):
            sys.exit(2)
        sys.exit(0 if (result.get("results") or []) else 1)

    try:
        search(
            query=args.query,
            palace_path=palace_path,
            wing=args.wing,
            room=args.room,
            tags=tags,
            n_results=n_results,
            since=getattr(args, "since", None),
            before=getattr(args, "before", None),
        )
    except SearchError:
        sys.exit(1)


def _emit_local_search_json(
    *,
    query: str,
    palace_path: str,
    wing: str,
    room: str,
    n_results: int,
    search_memories,
    tags: list = None,
) -> None:
    """JSON search against a local palace — mirrors the MCP
    ``tool_search`` response shape. ``search_memories`` is injected so
    tests can substitute a fake without monkey-patching the module.
    """
    # Mirror the filesystem-first probes from ``searcher.search`` so we
    # return a clear ``palace_unavailable`` error before the backend
    # would silently create a chroma.sqlite3 on first open.
    if not os.path.isdir(palace_path):
        _emit_json(
            {
                "error": "palace_unavailable",
                "hint": f"No palace found at {palace_path}. Run: mempalace init <dir>",
                "palace_path": palace_path,
                "query": query,
            }
        )
        sys.exit(2)
    if not os.path.isfile(os.path.join(palace_path, "chroma.sqlite3")):
        _emit_json(
            {
                "error": "palace_unavailable",
                "hint": f"Palace dir at {palace_path} has no chroma.sqlite3 yet. Run: mempalace mine <dir>",
                "palace_path": palace_path,
                "query": query,
            }
        )
        sys.exit(2)

    result = search_memories(
        query, palace_path, wing=wing, room=room, tags=tags, n_results=n_results
    )
    result.setdefault("query", query)
    _emit_json(result)
    if "error" in result and not result.get("results"):
        sys.exit(2)
    sys.exit(0 if (result.get("results") or []) else 1)


# ── mempalace list — fast direct-to-daemon drawer browser (#191) ────────
#
# Pure metadata browse: no ranking, no exclusion, no embedding. Wraps
# the daemon's GET /list endpoint (which itself wraps the
# ``mempalace_list_drawers`` MCP tool). Read-only, safe to run during
# backfill — it just paginates the metadata table.
#
# Recall-preserving by design: every drawer matching the wing/room
# filter is reachable via offset, and no drawer is dropped. This is the
# human/script counterpart to the existing ``mempalace_list_drawers``
# MCP tool (which serves the AI path).


_LIST_LIMIT_MAX = 1000  # sanity ceiling; the daemon clamps to 100 anyway


def _print_list_table(data: dict) -> None:
    """Human-readable multi-line render — drawer_id (short) + wing/room + preview."""
    drawers = data.get("drawers") or []
    total = data.get("total", len(drawers))
    offset = data.get("offset", 0)
    limit = data.get("limit", len(drawers))
    if not drawers:
        print("\n  No drawers found.")
        return
    end = offset + len(drawers)
    print(f"\n  Drawers {offset + 1}–{end} of {total} (limit {limit})\n")
    for d in drawers:
        did = d.get("drawer_id", "")
        short = did[:12] if did else "(no-id)"
        wing = d.get("wing") or "(no-wing)"
        room = d.get("room") or "(no-room)"
        preview = (d.get("content_preview") or "").replace("\n", " ").strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."
        print(f"  {short}  {wing}/{room}")
        if preview:
            print(f"    {preview}")
    print()


def _print_list_compact(data: dict) -> None:
    """One line per drawer: ``<id12> <wing>/<room>: <preview[:120]>``."""
    for d in data.get("drawers") or []:
        did = (d.get("drawer_id") or "")[:12]
        wing = d.get("wing") or "-"
        room = d.get("room") or "-"
        preview = (d.get("content_preview") or "").replace("\n", " ").strip()
        if len(preview) > 120:
            preview = preview[:117] + "..."
        print(f"{did} {wing}/{room}: {preview}")


def _print_list_full(data: dict) -> None:
    """Labelled sections, no truncation; separators between drawers."""
    drawers = data.get("drawers") or []
    total = data.get("total", len(drawers))
    offset = data.get("offset", 0)
    if not drawers:
        print("\n  No drawers found.")
        return
    print(f"\n  Drawers {offset + 1}–{offset + len(drawers)} of {total}\n")
    sep = "  " + "─" * 70
    for d in drawers:
        print(sep)
        print(f"  drawer_id: {d.get('drawer_id', '')}")
        print(f"  wing:      {d.get('wing') or ''}")
        print(f"  room:      {d.get('room') or ''}")
        tags = d.get("tags") or []
        if tags:
            print(f"  tags:      {', '.join(tags)}")
        print("  content:")
        for line in (d.get("content_preview") or "").splitlines() or [""]:
            print(f"    {line}")
    print(sep)
    print()


def _resolve_list_format(args) -> str:
    """Pick ``mempalace list`` output format. ``--format`` wins, then ``--json``."""
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def cmd_list(args):
    """Fast direct-to-daemon drawer browser (issue #191).

    Pure metadata listing — wraps ``GET /list?wing=&room=&limit=&offset=``
    on the palace daemon. Output formats: ``table`` (default), ``compact``,
    ``full``, ``json``. Daemon unreachable → stderr error + exit 1.
    """
    fmt = _resolve_list_format(args)
    want_json = fmt == "json"

    limit = max(1, min(int(getattr(args, "limit", 20) or 20), _LIST_LIMIT_MAX))
    offset = max(0, int(getattr(args, "offset", 0) or 0))

    params: dict = {"limit": limit, "offset": offset}
    if getattr(args, "wing", None):
        params["wing"] = args.wing
    if getattr(args, "room", None):
        params["room"] = args.room

    try:
        data = _call_daemon_rest("/list", params)
    except DaemonRequestError as e:
        # The daemon answered and refused: say what IT said (#499).
        _exit_daemon_request_error(e, want_json=want_json)
    except DaemonError as e:
        # Match cmd_status's daemon-down fallback (line 2230) and the
        # graceful 401/403 + unreachable handling added in 850e08c. On
        # JSON output, emit a structured error so machine callers see
        # the same shape as other failure paths.
        _fail_daemon(e, want_json)

    if data is None:
        # _call_daemon_rest returns None on 404/401/403 — endpoint
        # missing on an older daemon, or auth mismatch. Same exit code
        # as the unreachable case so scripts can treat "no daemon list"
        # uniformly without parsing the message.
        _fail_daemon(None, want_json, route="/list")

    # Daemon /list mirrors mempalace_list_drawers' shape: error key when
    # the underlying palace is unreachable from inside the daemon.
    if "error" in data and not data.get("drawers"):
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  {data['error']}", file=sys.stderr)
        sys.exit(2)

    if want_json:
        # Stable top-level shape — drawers/total/count/offset/limit pass
        # through unchanged so scripts can rely on the keys.
        out = {
            "drawers": data.get("drawers") or [],
            "total": data.get("total", 0),
            "count": data.get("count", len(data.get("drawers") or [])),
            "offset": data.get("offset", offset),
            "limit": data.get("limit", limit),
        }
        _emit_json(out)
        return

    if fmt == "compact":
        _print_list_compact(data)
    elif fmt == "full":
        _print_list_full(data)
    else:
        _print_list_table(data)


# ── mempalace move ────────────────────────────────────────────────────
#
# Single-drawer metadata relocation: wraps the daemon's
# ``PATCH /memory/{drawer_id}`` (palace-daemon main.py:1522). The route
# accepts content/wing/room, but `move` deliberately exposes only
# --wing / --room — the fork's verbatim-always principle forbids the
# human CLI from ever mutating stored drawer text. `move` relocates
# metadata only; the bulk wing-rename complement is `rename-wing`.


def _resolve_move_format(args) -> str:
    """Pick ``mempalace move`` output format. ``--format`` wins, then ``--json``."""
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def cmd_move(args):
    """Fast direct-to-daemon single-drawer relocation (issue #191).

    Wraps ``PATCH /memory/{drawer_id}`` with a body carrying only the
    supplied ``wing`` / ``room`` keys. At least one is required — an empty
    PATCH is an ambiguous no-op the daemon would 400, so we refuse it
    client-side with a clear message. No ``--content`` flag exists by
    design: verbatim-always means the human CLI never edits drawer text.
    Daemon unreachable / 404 / 401 / 403 → exit 1 (sibling parity with
    cmd_list / cmd_graph / cmd_cypher / cmd_stats); inner-error envelope
    (daemon reachable but the move failed) → exit 2.
    """
    fmt = _resolve_move_format(args)
    want_json = fmt == "json"

    drawer_id = args.drawer_id
    new_wing = getattr(args, "wing", None)
    new_room = getattr(args, "room", None)

    if new_wing is None and new_room is None:
        msg = "move requires at least one of --wing / --room (nothing to change)."
        if want_json:
            _emit_json({"error": "no_change", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    if not _daemon_url():
        msg = (
            "move requires the palace-daemon. Set PALACE_DAEMON_URL "
            "(or daemon_url in ~/.mempalace/config.json) and retry."
        )
        if want_json:
            _emit_json({"error": "daemon_required", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    body: dict = {}
    if new_wing is not None:
        body["wing"] = new_wing
    if new_room is not None:
        body["room"] = new_room

    try:
        data = _patch_daemon_rest(f"/memory/{drawer_id}", body)
    except DaemonError as e:
        _fail_daemon(e, want_json)

    if data is None:
        # _patch_daemon_rest returns None on 404/401/403 — route missing on
        # an older daemon, or auth mismatch. Exit 1 matches the unreachable
        # case so scripts treat "no daemon move" uniformly.
        _fail_daemon(None, want_json, route="PATCH /memory")

    # mempalace_update_drawer returns success=False on a not-found drawer or
    # an inner sanitize/validation failure — daemon reachable, move failed.
    if not data.get("success", True):
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  ERROR: {data.get('error', 'move failed')}", file=sys.stderr)
        sys.exit(2)

    if want_json:
        _emit_json(data)
        return

    _print_move_result(data, drawer_id, requested_wing=new_wing, requested_room=new_room)


def _print_move_result(data, drawer_id, requested_wing=None, requested_room=None):
    """Human-readable old→new confirmation for a single-drawer move.

    The daemon's update_drawer response carries only the *new* wing/room
    (there's no cheap single-drawer GET route to read the prior values), so
    the "old" column shows the requested change as ``→ new`` and unchanged
    fields are marked ``(unchanged)``. Warnings (e.g. non-canonical room
    per the taxonomy) are surfaced if the daemon returned any.
    """
    final_wing = data.get("wing", "")
    final_room = data.get("room", "")
    print()
    print(f"  Moved drawer {drawer_id}")
    if requested_wing is not None:
        print(f"    wing → {final_wing}")
    else:
        print(f"    wing   {final_wing}  (unchanged)")
    if requested_room is not None:
        print(f"    room → {final_room}")
    else:
        print(f"    room   {final_room}  (unchanged)")
    warnings = data.get("warnings") or []
    for w in warnings:
        print(f"    warning: {w}")
    print()


# ── mempalace bulk-move ───────────────────────────────────────────────
#
# Bulk metadata relocation: the multi-drawer complement to `move`. Selects
# drawers by source wing/room (``GET /list`` with offset pagination) and
# PATCHes each one independently to a target wing/room. Same verbatim-always
# constraint as `move` — only metadata moves, never drawer text, so there
# is no ``--content`` flag.
#
# The safety model is deliberately conservative because this mutates many
# drawers at once:
#   * a source filter (--wing and/or --room) is *required* — never operate
#     on the whole palace by accident;
#   * a target (--to-wing and/or --to-room) is required;
#   * dry-run is the DEFAULT — you must pass --apply to mutate;
#   * --apply prompts for confirmation on a TTY (skip with --yes) and
#     *refuses* to run unattended (non-TTY without --yes) so a pipeline
#     can't silently mass-mutate the palace;
#   * one drawer's PATCH failing does not abort the batch — failures are
#     collected and reported, exit 2 if any failed.


def _resolve_bulk_move_format(args) -> str:
    """Pick ``mempalace bulk-move`` output format. ``--format`` wins, then ``--json``."""
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _gather_bulk_move_matches(wing, room, want_json):
    """Page through ``GET /list`` collecting every drawer matching wing/room.

    Returns the list of drawer dicts (id + current wing/room). Exits the
    process on daemon failure, mirroring ``cmd_list`` exit codes:
      * DaemonError / None return (404/401/403) → exit 1 (sibling parity);
      * inner-error envelope (``data["error"]`` and no drawers) → exit 2.
    """
    matches: list[dict] = []
    offset = 0
    page = _LIST_LIMIT_MAX
    while True:
        params: dict = {"limit": page, "offset": offset}
        if wing:
            params["wing"] = wing
        if room:
            params["room"] = room
        try:
            data = _call_daemon_rest("/list", params)
        except DaemonRequestError as _req_err:
            # A refusal is not an outage (#499).
            _exit_daemon_request_error(_req_err, want_json=want_json)
        except DaemonError as e:
            _fail_daemon(e, want_json)

        if data is None:
            _fail_daemon(None, want_json, route="/list")

        drawers = data.get("drawers") or []
        if "error" in data and not drawers:
            if want_json:
                _emit_json(data)
            else:
                print(f"\n  {data['error']}", file=sys.stderr)
            sys.exit(2)

        matches.extend(drawers)

        total = int(data.get("total", len(matches)) or 0)
        offset += len(drawers)
        # Stop when we've collected everything, or the daemon returned an
        # empty page (defensive — avoids an infinite loop if total is stale).
        if not drawers or offset >= total:
            break
    return matches


def _bulk_move_drawer_id(drawer: dict) -> str:
    """Pull the id out of a /list drawer dict (daemon uses ``drawer_id``)."""
    return drawer.get("drawer_id") or drawer.get("id") or ""


def _bulk_move_label(wing, room) -> str:
    """``wing/room`` label for prompts/previews; ``*`` marks an unconstrained side."""
    return f"{wing or '*'}/{room or '*'}"


def _validate_bulk_move_args(src_wing, src_room, to_wing, to_room, want_json):
    """Enforce the safety preconditions; exit 2 with guidance if violated.

    Source filter required (never touch the whole palace), target required,
    daemon required — mirrors ``cmd_move``'s no-change / daemon-required
    exit-2 contract.
    """
    if src_wing is None and src_room is None:
        msg = (
            "bulk-move requires a source filter: at least one of "
            "--wing / --room (refusing to operate on the whole palace)."
        )
        if want_json:
            _emit_json({"error": "no_source_filter", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    if to_wing is None and to_room is None:
        msg = "bulk-move requires at least one of --to-wing / --to-room (nothing to change)."
        if want_json:
            _emit_json({"error": "no_change", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    if not _daemon_url():
        msg = (
            "bulk-move requires the palace-daemon. Set PALACE_DAEMON_URL "
            "(or daemon_url in ~/.mempalace/config.json) and retry."
        )
        if want_json:
            _emit_json({"error": "daemon_required", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)


def _print_bulk_move_preview(matches, to_wing, to_room, src_label) -> None:
    """Human-readable dry-run preview: one ``id  cur → target`` line per drawer."""
    print()
    print(f"  Matched {len(matches)} drawer(s) in {src_label}")
    for d in matches:
        did = _bulk_move_drawer_id(d)
        short = did[:12] if did else "(no-id)"
        cur_w = d.get("wing") or ""
        cur_r = d.get("room") or ""
        tgt_w = to_wing if to_wing is not None else cur_w
        tgt_r = to_room if to_room is not None else cur_r
        print(f"    {short}  {cur_w}/{cur_r} → {tgt_w}/{tgt_r}")
    print(f"\n  DRY RUN — re-run with --apply to move {len(matches)} drawers.\n")


def _bulk_move_confirm(args, matched, src_label, dst_label, want_json) -> bool:
    """Confirmation gate before a mass mutation.

    Returns True to proceed. ``--yes`` skips the gate. On a TTY (and not
    json) we prompt and proceed only on y/yes. Non-interactive / json
    without ``--yes`` is *refused* (exit 2) — no silent mass mutation in a
    pipeline. Returns False on an interactive decline (caller prints
    ``aborted`` and exits 0).
    """
    if bool(getattr(args, "yes", False)):
        return True
    if want_json or not sys.stdin.isatty():
        msg = f"refusing to bulk-move {matched} drawers without --yes in a non-interactive shell"
        if want_json:
            _emit_json({"error": "confirmation_required", "hint": msg, "matched": matched})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)
    answer = input(f"Move {matched} drawers {src_label} → {dst_label}? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _bulk_move_execute(matches, body_template):
    """PATCH each drawer independently; never abort the batch on one failure.

    Returns ``(moved_ids, failures)`` where a failure is a
    ``{"id", "error"}`` dict (DaemonError, None return, or
    ``success is False``).
    """
    moved: list[str] = []
    failed: list[dict] = []
    for d in matches:
        did = _bulk_move_drawer_id(d)
        if not did:
            failed.append({"id": "", "error": "missing drawer id in /list response"})
            continue
        try:
            result = _patch_daemon_rest(f"/memory/{did}", dict(body_template))
        except DaemonError as e:
            failed.append({"id": did, "error": str(e)})
            continue
        if result is None:
            failed.append({"id": did, "error": "daemon PATCH /memory unavailable"})
            continue
        if result.get("success") is False:
            failed.append({"id": did, "error": result.get("error", "move failed")})
            continue
        moved.append(did)
    return moved, failed


def cmd_bulk_move(args):
    """Bulk drawer relocation by source wing/room (issue #191).

    The multi-drawer complement to ``move``. Selects drawers via
    ``GET /list`` (offset-paginated) and PATCHes each match to the target
    wing/room. Dry-run by default; ``--apply`` mutates (TTY prompt unless
    ``--yes``; refuses unattended without ``--yes``). Verbatim-always:
    metadata only, no ``--content`` flag. Daemon unreachable / 404 / 401 /
    403 during listing → exit 1; selection/target missing or any PATCH
    failure → exit 2.
    """
    fmt = _resolve_bulk_move_format(args)
    want_json = fmt == "json"

    src_wing = getattr(args, "wing", None)
    src_room = getattr(args, "room", None)
    to_wing = getattr(args, "to_wing", None)
    to_room = getattr(args, "to_room", None)

    _validate_bulk_move_args(src_wing, src_room, to_wing, to_room, want_json)

    # Only the supplied target keys go into each PATCH body / the json target.
    body_template: dict = {}
    if to_wing is not None:
        body_template["wing"] = to_wing
    if to_room is not None:
        body_template["room"] = to_room
    target_obj = dict(body_template)
    source_obj = {"wing": src_wing, "room": src_room}

    src_label = _bulk_move_label(src_wing, src_room)
    dst_label = _bulk_move_label(
        to_wing if to_wing is not None else src_wing,
        to_room if to_room is not None else src_room,
    )

    matches = _gather_bulk_move_matches(src_wing, src_room, want_json)
    matched = len(matches)

    # Dry-run is the DEFAULT — preview and exit, no PATCH calls.
    if not bool(getattr(args, "apply", False)):
        if want_json:
            _emit_json(
                {
                    "matched": matched,
                    "dry_run": True,
                    "moved": [],
                    "failed": [],
                    "target": target_obj,
                    "source": source_obj,
                }
            )
            return
        _print_bulk_move_preview(matches, to_wing, to_room, src_label)
        return

    if matched == 0:
        # Nothing to do — report and exit cleanly. No prompt, no PATCH.
        if want_json:
            _emit_json(
                {
                    "matched": 0,
                    "dry_run": False,
                    "moved": [],
                    "failed": [],
                    "target": target_obj,
                    "source": source_obj,
                }
            )
            return
        print(f"\n  No drawers match {src_label} — nothing to move.\n")
        return

    if not _bulk_move_confirm(args, matched, src_label, dst_label, want_json):
        print("  aborted")
        return

    moved, failed = _bulk_move_execute(matches, body_template)

    if want_json:
        _emit_json(
            {
                "matched": matched,
                "dry_run": False,
                "moved": moved,
                "failed": failed,
                "target": target_obj,
                "source": source_obj,
            }
        )
    else:
        print(f"\n  moved {len(moved)}, failed {len(failed)}")
        if failed:
            print("  failed drawers:")
            for f in failed:
                fid = (f.get("id") or "")[:12] or "(no-id)"
                print(f"    {fid}  {f.get('error', '')}")
        print()

    if failed:
        sys.exit(2)


# ── mempalace graph ───────────────────────────────────────────────────
#
# Pre-aggregated structural snapshot: wings, rooms, passive tunnels,
# plus a KG slice (top-N entities + sample RELATION/MENTIONS triples +
# global kg_stats). Wraps the daemon's ``GET /graph?limit=`` endpoint.
# Read-only, safe to run during backfill — the daemon assembles the
# snapshot from pre-aggregated tables.
#
# Recall-preserving by design: this is a *structural* snapshot of the
# palace shape and KG, not a drawer search. It never excludes content;
# the limit only caps how many KG entities (and 2×limit MENTIONS) ship
# back. Operators querying for more granularity hit AGE Cypher
# directly via ``POST /cypher`` (per the daemon openapi note).


_GRAPH_LIMIT_MAX = 50000  # matches the daemon's hard ceiling on /graph


def _print_graph_table(data: dict) -> None:
    """Human-readable summary: palace structure + KG snapshot."""
    wings = data.get("wings") or {}
    rooms = data.get("rooms") or []
    tunnels = data.get("tunnels") or []
    kg_stats = data.get("kg_stats") or {}
    kg_entities = data.get("kg_entities") or []
    kg_triples = data.get("kg_triples") or []
    kg_mentions = data.get("kg_mentions") or []

    total_drawers = sum(int(v or 0) for v in wings.values())
    print()
    print("  Palace structure")
    print(f"    wings:    {len(wings):>10}")
    print(f"    rooms:    {sum(len(r.get('rooms') or {}) for r in rooms):>10}")
    print(f"    tunnels:  {len(tunnels):>10}")
    print(f"    drawers:  {total_drawers:>10}")

    if wings:
        print()
        print("  Top wings by drawer count")
        top_wings = sorted(wings.items(), key=lambda kv: int(kv[1] or 0), reverse=True)[:10]
        for name, count in top_wings:
            print(f"    {int(count or 0):>8}  {name}")

    print()
    print("  Knowledge graph")
    print(f"    entities: {int(kg_stats.get('entities', 0) or 0):>10}")
    print(f"    triples:  {int(kg_stats.get('triples', 0) or 0):>10}")
    print(f"    mentions: {int(kg_stats.get('mentions', 0) or 0):>10}")
    rel_types = kg_stats.get("relationship_types") or []
    if rel_types:
        print(f"    rel-types: {', '.join(rel_types)}")
    print(f"    sample entities (capped at --limit): {len(kg_entities)}")
    print(f"    sample triples:                       {len(kg_triples)}")
    print(f"    sample mentions:                      {len(kg_mentions)}")

    if kg_entities:
        print()
        print("  Sample entities")
        for ent in kg_entities[:10]:
            name = ent.get("name") or ent.get("id") or "(unnamed)"
            etype = ent.get("type") or ""
            suffix = f"  [{etype}]" if etype else ""
            print(f"    {name}{suffix}")

    if kg_triples:
        print()
        print("  Sample triples")
        for tr in kg_triples[:10]:
            subj = tr.get("subject") or "?"
            pred = tr.get("predicate") or "?"
            obj = tr.get("object") or "?"
            print(f"    {subj} —[{pred}]→ {obj}")

    print()


def _print_graph_full(data: dict) -> None:
    """Labelled sections, no truncation — every wing, every triple, every mention."""
    wings = data.get("wings") or {}
    rooms = data.get("rooms") or []
    tunnels = data.get("tunnels") or []
    kg_stats = data.get("kg_stats") or {}
    kg_entities = data.get("kg_entities") or []
    kg_triples = data.get("kg_triples") or []
    kg_mentions = data.get("kg_mentions") or []
    sep = "  " + "─" * 70

    print()
    print(sep)
    print("  WINGS")
    for name in sorted(wings):
        print(f"    {int(wings[name] or 0):>8}  {name}")

    print(sep)
    print("  ROOMS (per wing)")
    for entry in rooms:
        wing = entry.get("wing") or "(no-wing)"
        breakdown = entry.get("rooms") or {}
        cells = ", ".join(f"{r}:{int(c or 0)}" for r, c in sorted(breakdown.items()))
        print(f"    {wing}: {cells}")

    print(sep)
    print("  TUNNELS (rooms appearing in 2+ wings)")
    for tun in tunnels:
        room = tun.get("room") or "(no-room)"
        wings_list = tun.get("wings") or []
        print(f"    {room}: {len(wings_list)} wings → {', '.join(wings_list)}")

    print(sep)
    print("  KG STATS")
    print(f"    entities:  {int(kg_stats.get('entities', 0) or 0)}")
    print(f"    triples:   {int(kg_stats.get('triples', 0) or 0)}")
    print(f"    mentions:  {int(kg_stats.get('mentions', 0) or 0)}")
    rel_types = kg_stats.get("relationship_types") or []
    if rel_types:
        print(f"    rel-types: {', '.join(rel_types)}")

    print(sep)
    print(f"  KG ENTITIES (sample, n={len(kg_entities)})")
    for ent in kg_entities:
        eid = ent.get("id") or ""
        name = ent.get("name") or "(unnamed)"
        etype = ent.get("type") or ""
        props = ent.get("properties") or {}
        suffix = f"  [{etype}]" if etype else ""
        print(f"    {name}{suffix}  id={eid}")
        if props:
            print(f"      properties: {props}")

    print(sep)
    print(f"  KG TRIPLES (sample, n={len(kg_triples)})")
    for tr in kg_triples:
        subj = tr.get("subject") or "?"
        pred = tr.get("predicate") or "?"
        obj = tr.get("object") or "?"
        conf = tr.get("confidence")
        vf = tr.get("valid_from")
        vt = tr.get("valid_to")
        meta_bits = []
        if conf is not None:
            meta_bits.append(f"conf={conf}")
        if vf:
            meta_bits.append(f"from={vf}")
        if vt:
            meta_bits.append(f"to={vt}")
        meta = f"  ({', '.join(meta_bits)})" if meta_bits else ""
        print(f"    {subj} —[{pred}]→ {obj}{meta}")

    print(sep)
    print(f"  KG MENTIONS (sample, n={len(kg_mentions)})")
    for mn in kg_mentions:
        subj = mn.get("subject") or "?"
        obj = mn.get("object") or "?"
        src = mn.get("source_file") or ""
        suffix = f"  [{src}]" if src else ""
        print(f"    {subj} → {obj}{suffix}")

    print(sep)
    print()


def _resolve_graph_format(args) -> str:
    """Pick ``mempalace graph`` output format. ``--format`` wins, then ``--json``."""
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def cmd_graph(args):
    """Fast direct-to-daemon KG + palace structural snapshot (issue #191).

    Pure read — wraps ``GET /graph?limit=`` on the palace daemon, which
    returns pre-aggregated wing/room/tunnel counts plus a KG slice
    (top-N entities, sample RELATION/MENTIONS triples, global kg_stats).
    Output formats: ``table`` (default summary), ``full`` (every wing,
    every sampled triple, no truncation), ``json`` (pass-through shape).
    Daemon unreachable → stderr error + exit 1; inner-error payload → exit 2.
    """
    fmt = _resolve_graph_format(args)
    want_json = fmt == "json"

    raw_limit = getattr(args, "limit", 500)
    if raw_limit is None:
        raw_limit = 500
    limit = max(1, min(int(raw_limit), _GRAPH_LIMIT_MAX))
    params: dict = {"limit": limit}

    try:
        data = _call_daemon_rest("/graph", params)
    except DaemonRequestError as _req_err:
        # A refusal is not an outage (#499).
        _exit_daemon_request_error(_req_err, want_json=want_json)
    except DaemonError as e:
        # Match cmd_list / cmd_status daemon-down fallback. JSON callers
        # get a structured error on stdout; humans get the standard
        # "daemon unreachable" line on stderr.
        _fail_daemon(e, want_json)

    if data is None:
        # _call_daemon_rest returns None on 404/401/403 — endpoint
        # missing on an older daemon, or auth mismatch. Treat the same
        # as unreachable so scripts get one failure shape.
        _fail_daemon(None, want_json, route="/graph")

    # Daemon may surface an inner error envelope (palace unreachable
    # from inside the daemon) — match cmd_list's exit-2 contract.
    if (
        "error" in data
        and not data.get("kg_stats")
        and not data.get("kg_entities")
        and not data.get("kg_triples")
        and not data.get("wings")
    ):
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  {data['error']}", file=sys.stderr)
        sys.exit(2)

    if want_json:
        # Stable top-level shape — pass through daemon keys unchanged so
        # scripts can rely on them. Defaults make missing keys explicit
        # rather than KeyError-ing downstream consumers.
        out = {
            "wings": data.get("wings") or {},
            "rooms": data.get("rooms") or [],
            "tunnels": data.get("tunnels") or [],
            "kg_entities": data.get("kg_entities") or [],
            "kg_triples": data.get("kg_triples") or [],
            "kg_mentions": data.get("kg_mentions") or [],
            "kg_stats": data.get("kg_stats") or {},
        }
        _emit_json(out)
        return

    if fmt == "full":
        _print_graph_full(data)
    else:
        _print_graph_table(data)


# ── mempalace cypher (issue #191) ─────────────────────────────────────
#
# Read-only Cypher query CLI: wraps the daemon's ``POST /cypher``
# endpoint, which executes arbitrary Cypher against the AGE knowledge
# graph inside a ``READ ONLY`` postgres transaction. Write verbs
# (CREATE/MERGE/SET/DELETE/REMOVE) fail server-side with SQLSTATE 25006
# → HTTP 403. We trust the server enforcement instead of blocklisting
# client-side: simpler, can't drift from the daemon's policy, and the
# spec is explicit (see PR #228 for the statement_timeout side of the
# safety story).
#
# Composes with ``mempalace graph`` (pre-aggregated snapshot) — cypher
# is the arbitrary-walk escape hatch when the snapshot isn't enough.


_CYPHER_DEFAULT_GRAPH = "mempalace_kg"


def _post_cypher(body: dict) -> tuple[dict | None, int | None]:
    """POST to ``/cypher`` and classify the HTTP status.

    Returns ``(data, status_code)`` where ``status_code`` is the HTTP
    status on a non-2xx response and ``None`` on success. We classify
    rather than just raising ``DaemonError`` because the spec needs to
    distinguish 403 (read-only enforcement) from 401/404 (auth / older
    daemon) so the CLI can emit a friendly "rewrite as MATCH/RETURN"
    hint on write attempts. Network failures still raise ``DaemonError``.
    """
    import urllib.error
    import urllib.request

    url = f"{_daemon_url()}/cypher"
    headers = {"content-type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen_with_wake(req, timeout=_daemon_timeout()) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace")), None
    except urllib.error.HTTPError as e:
        return None, e.code
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e


def _resolve_cypher_format(args) -> str:
    """``--format`` wins, then ``--json`` shorthand, default ``table``.

    Same precedence shape as ``_resolve_graph_format`` / ``_resolve_search_format``.
    """
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _extract_cypher_rows(data: dict) -> list[dict]:
    """Pull rows out of the daemon's /cypher envelope.

    The daemon parses RETURN aliases out of the Cypher source and ships
    back ``{"rows": [{...}, ...]}`` (plus optional metadata). Defensive
    against future shape drift: accept top-level ``rows`` or ``data``.
    """
    rows = data.get("rows")
    if rows is None:
        rows = data.get("data") or []
    return rows if isinstance(rows, list) else []


def _print_cypher_table(rows: list[dict]) -> None:
    """Aligned-column table: one row per Cypher result row."""
    if not rows:
        print("\n  No rows.\n")
        return

    # Stable column order: union of all keys, preserving first-seen order.
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                columns.append(key)
                seen.add(key)

    def _cell(v) -> str:
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return str(v)

    widths = {c: len(c) for c in columns}
    str_rows: list[dict] = []
    for row in rows:
        str_row = {c: _cell(row.get(c)) for c in columns}
        for c in columns:
            widths[c] = max(widths[c], len(str_row[c]))
        str_rows.append(str_row)

    header = "  " + "  ".join(c.ljust(widths[c]) for c in columns)
    sep = "  " + "  ".join("─" * widths[c] for c in columns)
    print()
    print(header)
    print(sep)
    for r in str_rows:
        print("  " + "  ".join(r[c].ljust(widths[c]) for c in columns))
    print(f"\n  {len(rows)} row{'s' if len(rows) != 1 else ''}.\n")


def _print_cypher_csv(rows: list[dict]) -> None:
    """CSV to stdout — pipe-friendly, no header decoration."""
    import csv

    if not rows:
        return

    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                columns.append(key)
                seen.add(key)

    writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = {}
        for c in columns:
            v = row.get(c)
            if isinstance(v, (dict, list)):
                flat[c] = json.dumps(v, ensure_ascii=False)
            else:
                flat[c] = "" if v is None else v
        writer.writerow(flat)


def cmd_cypher(args):
    """Run a read-only Cypher query against the AGE knowledge graph (issue #191).

    Wraps the daemon's ``POST /cypher``, which executes inside a
    ``READ ONLY`` postgres transaction (write verbs fail with HTTP 403,
    SQLSTATE 25006). Output formats: ``table`` (aligned columns),
    ``json`` (pass-through), ``csv`` (pipe-friendly). The optional
    ``--limit`` is advisory — the daemon's own statement_timeout is the
    real ceiling.

    Daemon unreachable → stderr error + exit 1; 403 read-only write
    attempt → friendly hint + exit 2; inner-error payload → exit 2.
    """
    fmt = _resolve_cypher_format(args)
    want_json = fmt == "json"

    query = getattr(args, "query", "")
    if not query or not str(query).strip():
        if want_json:
            _emit_json({"error": "missing required positional QUERY", "source": "cli"})
        else:
            print("error: missing required positional QUERY", file=sys.stderr)
        sys.exit(2)

    graph = getattr(args, "graph", None) or _CYPHER_DEFAULT_GRAPH
    body: dict = {"cypher": str(query), "graph": str(graph)}

    try:
        data, status = _post_cypher(body)
    except DaemonError as e:
        # Match cmd_graph / cmd_list daemon-down fallback. JSON callers
        # get a structured error on stdout; humans get the standard
        # "daemon unreachable" line on stderr.
        _fail_daemon(e, want_json)

    if status == 403:
        # Server-enforced read-only: SQLSTATE 25006 surfaces as HTTP 403.
        # Don't dump traceback noise — give the operator a one-line hint
        # that maps to the next action.
        hint = (
            "daemon /cypher returned 403 — this endpoint is read-only; "
            "rewrite as MATCH / RETURN, or use the mempalace_kg_* MCP tools to mutate"
        )
        if want_json:
            _emit_json({"error": hint, "code": "read_only", "source": "daemon", "status": 403})
        else:
            print(hint, file=sys.stderr)
        # 64, not 2 (#518). This 403 is not an auth failure — it is the
        # daemon enforcing read-only (SQLSTATE 25006) — but it is the same
        # CLASS of event: the daemon answered and refused a well-formed
        # request. Same status, different cause, same contract sentence.
        sys.exit(64)

    if status in (401, 403):
        # A refused credential is a rejection, not an outage (#518). Before
        # this it fell into the branch below and was reported as "treat the
        # same as unreachable so scripts get one failure shape" — one shape
        # bought at the cost of sending the operator to check whether the
        # daemon was running when the fix was one line of config.
        _exit_daemon_request_error(
            DaemonAuthError(f"daemon /cypher refused the credential ({status})", status=status),
            want_json=want_json,
        )

    if status is not None:
        # 404/503 etc — route missing on an older daemon, or a non-postgres
        # backend. Unavailability for this verb, which the contract numbers 2.
        _fail_daemon(
            f"daemon /cypher returned {status}",
            want_json,
            status=status,
        )

    # Daemon may surface an inner error envelope — match cmd_graph's exit-2.
    if data is not None and "error" in data and "rows" not in data and "data" not in data:
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  {data['error']}", file=sys.stderr)
        sys.exit(2)

    rows = _extract_cypher_rows(data or {})

    if want_json:
        # Stable top-level shape — pass through the daemon envelope so
        # scripts can rely on it. Defaults make missing keys explicit.
        out = {
            "rows": rows,
            "count": len(rows),
            "graph": graph,
        }
        # Surface any extra metadata the daemon adds without crowding it
        # into "rows" — e.g. elapsed_ms, warnings.
        if isinstance(data, dict):
            for k, v in data.items():
                if k not in ("rows", "data", "count", "graph"):
                    out[k] = v
        _emit_json(out)
        return

    if fmt == "csv":
        _print_cypher_csv(rows)
        return

    _print_cypher_table(rows)


def cmd_wakeup(args):
    """Show L0 (identity) + L1 (essential story) — the wake-up context.

    Daemon-routes when ``_daemon_strict()`` is on and the user didn't
    pass ``--palace`` (#285). The daemon-native ``mempalace_wakeup``
    tool (palace-daemon #96) returns ``{text, tokens, wing}``; we
    print in the same shape as the local-path fallback below.
    """
    if _daemon_strict() and not args.palace:
        try:
            args_payload = {"wing": args.wing} if getattr(args, "wing", None) else {}
            data = _call_daemon_tool("mempalace_wakeup", args_payload)
        except DaemonError as e:
            print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        text = data.get("text", "")
        tokens = data.get("tokens") or (len(text) // 4)
        print(f"Wake-up text (~{tokens} tokens):")
        print("=" * 50)
        print(text)
        return

    from ..layers import MemoryStack

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    stack = MemoryStack(palace_path=palace_path)

    text = stack.wake_up(wing=args.wing)
    tokens = len(text) // 4
    print(f"Wake-up text (~{tokens} tokens):")
    print("=" * 50)
    print(text)


def cmd_split(args):
    """Split concatenated transcript mega-files into per-session files."""
    from ..split_mega_files import main as split_main
    import sys

    # Rebuild argv for split_mega_files argparse
    # Expand ~ and resolve to absolute path so split_mega_files sees a real path
    argv = ["--source", str(Path(args.dir).expanduser().resolve())]
    if args.output_dir:
        argv += ["--output-dir", args.output_dir]
    if args.dry_run:
        argv.append("--dry-run")
    if args.min_sessions != 2:
        argv += ["--min-sessions", str(args.min_sessions)]

    old_argv = sys.argv
    sys.argv = ["mempalace split"] + argv
    try:
        split_main()
    finally:
        sys.argv = old_argv


def cmd_export(args):
    from ..exporter import export_palace

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    output_dir = os.path.expanduser(args.output)

    print(f"\n{'=' * 55}")
    print("  MemPalace Export")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")
    print(f"  Output: {output_dir}\n")

    export_palace(palace_path=palace_path, output_dir=output_dir)

    print(f"\n{'=' * 55}\n")


def cmd_migrate(args):
    """Migrate palace from a different ChromaDB version."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if not _maintenance_requires_chroma(palace_path, "migrate"):
        raise SystemExit(2)
    from ..migrate import migrate

    migrate(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


def cmd_migrate_to_postgres(args):
    """Migrate a ChromaDB palace to Postgres (pgvector + AGE).

    Different from `cmd_migrate` (which handles intra-ChromaDB version
    upgrades). This one moves the entire substrate. See
    `mempalace/migrate_to_postgres.py` for the 7-phase pipeline.
    """
    from ..migrate_to_postgres import run_migration

    chroma_path = os.path.expanduser(args.from_palace)
    run_migration(
        chroma_path=chroma_path,
        postgres_dsn=args.to_dsn,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )


def _cmd_rooms_via_daemon(cmd: str, args) -> None:
    """Route ``mempalace rooms <cmd>`` to palace-daemon's MCP tools (#285).

    palace-daemon PR #96 added the four daemon-native tools we call here.
    Each returns a small JSON envelope; we format the same human shape the
    local postgres path produces so consumers can't tell the routing
    changed (mod the cache-invalidation hint, which the daemon side
    handles automatically).
    """
    if cmd == "list":
        try:
            rows = _call_daemon_tool("mempalace_rooms_list", {})
        except DaemonError as e:
            print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(rows, list) or not rows:
            print("(no canonical rooms registered)")
            return
        print(f"{'name':14}  {'added_at':25}  description")
        print("-" * 80)
        for row in rows:
            name = row.get("name", "")
            desc = row.get("description") or ""
            added_at = row.get("added_at") or ""
            # Daemon returns ISO-ish timestamp string; trim to date for
            # parity with the local datetime.strftime('%Y-%m-%d').
            ts = added_at.split(" ")[0] if isinstance(added_at, str) and added_at else ""
            print(f"{name:14}  {ts:25}  {desc}")
        return

    if cmd == "add":
        name = args.name.strip().lower()
        if not name or not name.replace("_", "").isalnum():
            print(f"error: room name must be lowercase snake_case alphanumeric, got {args.name!r}")
            sys.exit(1)
        try:
            payload = {"name": name}
            if getattr(args, "description", None):
                payload["description"] = args.description
            data = _call_daemon_tool("mempalace_rooms_add", payload)
        except DaemonError as e:
            print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        action = data.get("action", "added")
        verb = "added" if action == "added" else "updated description for"
        print(f"{verb} canonical room {name!r}")
        return

    if cmd == "rename":
        old = args.old.strip().lower()
        new = args.new.strip().lower()
        if not new.replace("_", "").isalnum():
            print(
                f"error: new room name must be lowercase snake_case alphanumeric, got {args.new!r}"
            )
            sys.exit(1)
        try:
            data = _call_daemon_tool("mempalace_rooms_rename", {"old": old, "new": new})
        except DaemonError as e:
            # Daemon's -32602 for "no canonical room named X" surfaces here.
            print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        affected = data.get("affected_drawers", 0)
        print(f"renamed canonical room {old!r} → {new!r} ({affected:,} drawers cascade-renamed)")
        return

    if cmd == "remove":
        name = args.name.strip().lower()
        try:
            data = _call_daemon_tool("mempalace_rooms_remove", {"name": name})
        except DaemonError as e:
            # The daemon refuses removes whose rooms still have drawers; the
            # error message includes "affected_drawers" — surface verbatim.
            print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        if data.get("removed"):
            print(f"removed canonical room {name!r}")
        else:
            # Defensive: daemon returned ok but didn't confirm removal.
            print(f"error: no canonical room named {name!r}")
            sys.exit(1)
        return

    print(f"error: unknown rooms subcommand {cmd!r}")
    sys.exit(1)


def cmd_rooms(args):
    """Manage the canonical room set (mempalace_canonical_rooms table).

    hybrid-search-taxonomy follow-up. The FK constraint on mempalace_drawers.room
    means this CLI is the supported way to add/rename/remove canonical
    rooms without breaking the DB. ON UPDATE CASCADE on the FK makes
    renames safe (all drawers auto-update); removes fail if any drawer
    still in the target room.

    Daemon-routes when ``_daemon_strict()`` is on (#285). palace-daemon
    PR #96 added the four ``mempalace_rooms_{list,add,rename,remove}``
    tools that wrap the same SQL the local path runs. When daemon-strict
    is off, falls back to direct postgres via ``MEMPALACE_POSTGRES_DSN``.
    """
    if getattr(args, "rooms_action", None) in {"propose", "apply"}:
        cmd_rooms_maintenance(args)
        return
    cmd = getattr(args, "rooms_cmd", None)

    if _daemon_strict():
        _cmd_rooms_via_daemon(cmd, args)
        return

    try:
        import psycopg as psycopg2  # noqa: F401
    except ImportError:
        print(
            "error: rooms CLI requires the psycopg driver. Install with: pip install mempalace[postgres]"
        )
        sys.exit(1)

    import psycopg as psycopg2

    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        print("error: MEMPALACE_POSTGRES_DSN env var is not set", file=sys.stderr)
        sys.exit(1)
    try:
        with psycopg2.connect(dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                if cmd == "list":
                    cur.execute(
                        "SELECT name, COALESCE(description, '') AS description, added_at FROM mempalace_canonical_rooms ORDER BY name"
                    )
                    rows = cur.fetchall()
                    if not rows:
                        print("(no canonical rooms registered)")
                        return
                    print(f"{'name':14}  {'added_at':25}  description")
                    print("-" * 80)
                    for name, desc, added_at in rows:
                        ts = added_at.strftime("%Y-%m-%d") if added_at else ""
                        print(f"{name:14}  {ts:25}  {desc}")

                elif cmd == "add":
                    name = args.name.strip().lower()
                    if not name or not name.replace("_", "").isalnum():
                        print(
                            f"error: room name must be lowercase snake_case alphanumeric, got {args.name!r}"
                        )
                        sys.exit(1)
                    cur.execute(
                        "INSERT INTO mempalace_canonical_rooms (name, description) VALUES (%s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description "
                        "RETURNING (xmax = 0) AS inserted",
                        (name, args.description or None),
                    )
                    inserted = cur.fetchone()[0]
                    action = "added" if inserted else "updated description for"
                    print(f"{action} canonical room {name!r}")
                    print("hint: POST /admin/refresh-rooms on the daemon to invalidate its cache")

                elif cmd == "rename":
                    old = args.old.strip().lower()
                    new = args.new.strip().lower()
                    if not new.replace("_", "").isalnum():
                        print(
                            f"error: new room name must be lowercase snake_case alphanumeric, got {args.new!r}"
                        )
                        sys.exit(1)
                    # UPDATE PK triggers ON UPDATE CASCADE on the drawers FK,
                    # renaming the room across all drawers atomically.
                    cur.execute(
                        "UPDATE mempalace_canonical_rooms SET name = %s WHERE name = %s",
                        (new, old),
                    )
                    if cur.rowcount == 0:
                        print(f"error: no canonical room named {old!r}")
                        sys.exit(1)
                    cur.execute("SELECT count(*) FROM mempalace_drawers WHERE room = %s", (new,))
                    affected = cur.fetchone()[0]
                    print(
                        f"renamed canonical room {old!r} → {new!r} ({affected:,} drawers cascade-renamed)"
                    )
                    print("hint: POST /admin/refresh-rooms on the daemon to invalidate its cache")

                elif cmd == "remove":
                    name = args.name.strip().lower()
                    cur.execute("SELECT count(*) FROM mempalace_drawers WHERE room = %s", (name,))
                    n_drawers = cur.fetchone()[0]
                    if n_drawers > 0:
                        print(
                            f"error: cannot remove {name!r} — {n_drawers:,} drawers still in this room.\n"
                            f"  Move them first: UPDATE mempalace_drawers SET room = 'discoveries' WHERE room = '{name}';\n"
                            f"  Or via mempalace purge --room {name}"
                        )
                        sys.exit(1)
                    cur.execute("DELETE FROM mempalace_canonical_rooms WHERE name = %s", (name,))
                    if cur.rowcount == 0:
                        print(f"error: no canonical room named {name!r}")
                        sys.exit(1)
                    print(f"removed canonical room {name!r}")
                    print("hint: POST /admin/refresh-rooms on the daemon to invalidate its cache")

                else:
                    print(f"error: unknown rooms subcommand {cmd!r}")
                    sys.exit(1)
    except psycopg2.errors.UndefinedTable:
        print(
            "error: mempalace_canonical_rooms table doesn't exist yet.\n"
            "  Run the hybrid-search-taxonomy migration first (see hybrid-search-taxonomy spec).",
            file=sys.stderr,
        )
        sys.exit(1)


def _purge_where(wing, room, source_file):
    """Build the metadata filter and the human label for a purge selection."""
    clauses = []
    label_parts = []
    if wing:
        clauses.append({"wing": wing})
        label_parts.append(f"wing={wing}")
    if room:
        clauses.append({"room": room})
        label_parts.append(f"room={room}")
    if source_file:
        clauses.append({"source_file": source_file})
        label_parts.append(f"source-file={source_file}")
    if not clauses:
        return None, ""
    where = clauses[0] if len(clauses) == 1 else {"$and": clauses}
    return where, " ".join(label_parts)


def _purge_no_match(label: str, target: str) -> None:
    """Report a zero-match purge distinguishably, and exit non-zero (#418).

    A purge that matched nothing used to print a one-line notice and exit 0 —
    the same shape a successful purge has. Run from a client whose local
    palace was not the palace holding the drawers, that is indistinguishable
    from success: the reporter saw "No drawers found matching source-file=…",
    exit 0, and 115 drawers still listed for that exact path a moment later.
    Naming the target and exiting 1 (grep's "selected nothing" convention)
    makes the two outcomes tell apart from output *and* status.
    """
    print(f"\n  No drawers matched {label}")
    print(f"  Target: {target}")
    print("  Nothing was deleted. Check the filter and the target above.\n")
    sys.exit(1)


def _purge_via_daemon(*, wing, room, source_file, label, assume_yes) -> None:
    """Run a purge against the daemon's palace under daemon-strict (#418).

    The daemon exposes exactly one bulk delete, ``mempalace_delete_by_source``
    (which also clears the matching closets), and it filters on
    ``source_file`` ALONE. So two selections are refused here rather than
    approximated:

    * ``--wing``/``--room`` on their own — there is no wing/room bulk delete
      to route to. Falling through to whatever local palace directory happens
      to exist is the dangerous half of #418: "it deleted nothing from the
      wrong palace" is not a safer failure than an error, only a less visible
      one.
    * ``--wing``/``--room`` **combined with** ``--source-file`` — the daemon
      would match that source across EVERY wing while the receipt printed
      ``wing=W source-file=F``. A narrowing flag silently dropped on a
      destructive command is the same output-disagrees-with-reality defect
      this command was fixed for, with a blast radius: sources really do live
      in more than one wing (27 of 5,477 sampled in production).

    Shape mirrors the local path: probe with ``dry_run`` for the blast
    radius, confirm, then commit.
    """
    from ..migrate import confirm_destructive_action

    target = _daemon_url() or "the palace daemon"
    if wing or room:
        if source_file:
            print(
                "mempalace: purge --wing/--room cannot be combined with --source-file over "
                "the palace daemon: its bulk delete matches source_file across every wing, "
                "so the narrowing flag would be silently dropped.",
                file=sys.stderr,
            )
        else:
            print(
                "mempalace: purge --wing/--room is not available over the palace daemon "
                "(no bulk wing/room delete is exposed).",
                file=sys.stderr,
            )
        print(
            f"mempalace: run it on the palace host, or pass --palace <dir> to purge a "
            f"local palace instead of {target}.",
            file=sys.stderr,
        )
        sys.exit(2)

    def _call(dry_run: bool) -> dict:
        try:
            return _call_daemon_tool(
                "mempalace_delete_by_source",
                {"source_file": source_file, "dry_run": dry_run},
            )
        except DaemonError as exc:
            print(f"\n  ERROR: {exc}", file=sys.stderr)
            sys.exit(2)

    preview = _call(True)
    if not preview.get("success", False):
        print(f"mempalace: {preview.get('error', 'purge failed')}", file=sys.stderr)
        sys.exit(1)

    match_count = int(preview.get("match_count") or 0)
    if match_count == 0:
        _purge_no_match(label, target)

    closet_count = int(preview.get("closet_match_count") or 0)
    print(f"\n  Found {match_count:,} drawers matching {label}")
    print(f"  Target: {target}")
    if closet_count:
        print(f"  Index entries to clear: {closet_count:,}")

    if not assume_yes and not confirm_destructive_action(
        f"Purge of {match_count:,} drawers", target
    ):
        return

    print("  Deleting matching drawers...")
    result = _call(False)
    if not result.get("success", False):
        print(f"\n  Delete failed: {result.get('error', 'unknown error')}\n", file=sys.stderr)
        sys.exit(1)

    deleted = int(result.get("deleted") or 0)
    closets = int(result.get("closets_deleted") or 0)
    print(f"\n  Purged {deleted:,} drawers and {closets:,} index entries from {target}\n")


def cmd_purge(args):
    """Delete drawers by wing, room, and/or source-file.

    Uses ``collection.delete(where=...)`` — the backend's filter-delete path
    doesn't go through ``updatePoint`` / ``repairConnectionsForUpdate``,
    which is the upsert-only race from #521 that an earlier draft of this
    command tried to side-step with a nuke-and-rebuild. The simpler path
    works without losing drawers if the process is interrupted, without
    re-embedding the survivors under a default model, and without
    bypassing the backend abstraction.

    ``--room`` without ``--wing`` purges that room across ALL wings.

    Where it runs (#418): daemon-strict with no ``--palace`` routes to the
    daemon's palace; otherwise the backend is resolved *before* any
    palace-directory precheck, and the precheck is skipped entirely for a
    service-backed store, which has no local database file by design. The
    resolved target is printed on every outcome, including the zero-match
    one, which exits 1 rather than 0.
    """
    from ..migrate import confirm_destructive_action

    source_file = getattr(args, "source_file", None)
    where, label = _purge_where(args.wing, args.room, source_file)
    if where is None:
        print("  Error: specify at least one of --wing, --room, --source-file")
        return

    if _daemon_strict() and not getattr(args, "palace", None):
        _purge_via_daemon(
            wing=args.wing,
            room=args.room,
            source_file=source_file,
            label=label,
            assume_yes=args.yes,
        )
        return

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    col, _backend_name, target = _open_drawers_or_refuse(
        palace_path, explicit_backend=_backend_arg(args)
    )

    # Probe match count via a `where=`-filtered get with no payload.
    try:
        matched = col.get(where=where, include=[])
    except Exception as e:
        print(f"\n  Error querying drawers: {e}")
        sys.exit(2)

    match_ids = matched.get("ids") if isinstance(matched, dict) else getattr(matched, "ids", [])
    match_ids = match_ids or []
    match_count = len(match_ids)

    if match_count == 0:
        _purge_no_match(label, target)

    print(f"\n  Found {match_count:,} drawers matching {label}")
    print(f"  Target: {target}")

    if not args.yes:
        if not confirm_destructive_action(f"Purge of {match_count:,} drawers", palace_path):
            return

    print("  Deleting matching drawers...")
    try:
        col.delete(where=where)
    except Exception as e:
        print(f"\n  Delete failed: {e}\n")
        sys.exit(2)

    remaining = col.count()
    print(f"\n  Purged {match_count:,} drawers from {target}. Remaining: {remaining:,}\n")


def cmd_prune(args):
    """Delete drawers older than ``--stale-days N`` (dry-run by default).

    Age is the span between a drawer's ``filed_at`` timestamp and now. Unlike
    ``purge``'s metadata-equality filter, the staleness predicate is a string
    timestamp that chromadb ``where=`` can't range-compare reliably, so we
    fetch candidate metadata and decide age in Python (``mempalace.recency``),
    then delete by explicit id list.

    Safety: this is the only command that destroys data on a *time* predicate
    rather than an explicit selection, so it is **dry-run by default**. Nothing
    is deleted unless ``--confirm`` is passed. A drawer with no parseable
    ``filed_at`` is treated as ageless and is **never** pruned — we never
    delete a drawer we can't date.
    """
    from datetime import datetime, timezone

    from ..recency import age_days

    want_json = getattr(args, "json", False)
    stale_days = args.stale_days
    confirm = getattr(args, "confirm", False)

    if stale_days is None or stale_days <= 0:
        msg = "--stale-days must be a positive integer"
        print(json.dumps({"error": msg}) if want_json else f"  Error: {msg}")
        return

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    # Resolve the backend before any local-directory test (#459): a
    # service-backed palace has no database file in its directory by design,
    # and prune's copy of the precheck refused every Postgres palace with
    # exit 0 — the same defect #418 removed from purge and sync.
    # Optional wing/room scope — without it, prune spans the whole palace.
    clauses = []
    if args.wing:
        clauses.append({"wing": args.wing})
    if args.room:
        clauses.append({"room": args.room})
    where = None
    if clauses:
        where = clauses[0] if len(clauses) == 1 else {"$and": clauses}

    col, backend_name, target = _open_drawers_or_refuse(
        palace_path, explicit_backend=_backend_arg(args), want_json=want_json
    )

    # Pull ids + metadata for the scope; age is decided in Python.
    try:
        got = (
            col.get(where=where, include=["metadatas"]) if where else col.get(include=["metadatas"])
        )
    except Exception as e:
        print(
            json.dumps({"error": str(e), "palace": palace_path, "backend": backend_name})
            if want_json
            else f"\n  Error querying drawers: {e}"
        )
        sys.exit(1)

    if isinstance(got, dict):
        all_ids = got.get("ids") or []
        all_metas = got.get("metadatas") or []
    else:
        all_ids = getattr(got, "ids", []) or []
        all_metas = getattr(got, "metadatas", []) or []

    now = datetime.now(timezone.utc)
    stale_ids = []
    undated = 0
    for did, meta in zip(all_ids, all_metas):
        age = age_days(meta or {}, now=now)
        if age is None:
            undated += 1
            continue
        if age >= stale_days:
            stale_ids.append(did)

    scope_parts = []
    if args.wing:
        scope_parts.append(f"wing={args.wing}")
    if args.room:
        scope_parts.append(f"room={args.room}")
    scope = " ".join(scope_parts) if scope_parts else "entire palace"

    if want_json:
        print(
            json.dumps(
                {
                    "stale_days": stale_days,
                    "scope": scope,
                    "palace": palace_path,
                    "backend": backend_name,
                    "scanned": len(all_ids),
                    "stale": len(stale_ids),
                    "undated_skipped": undated,
                    "confirmed": bool(confirm),
                    "deleted": 0,
                }
            )
        )
    else:
        print(f"\n  Scanned {len(all_ids):,} drawers in {scope}")
        print(f"  Target: {target}")
        print(f"  {len(stale_ids):,} older than {stale_days} days; {undated:,} undated (kept)")

    if not stale_ids:
        if not want_json:
            print("  Nothing to prune.\n")
        return

    if not confirm:
        if not want_json:
            print(
                f"\n  DRY RUN — nothing deleted. Re-run with --confirm to delete "
                f"{len(stale_ids):,} drawers.\n"
            )
        return

    try:
        col.delete(ids=stale_ids)
    except Exception as e:
        print(
            json.dumps({"error": str(e), "palace": palace_path, "backend": backend_name})
            if want_json
            else f"\n  Delete failed: {e}\n"
        )
        sys.exit(1)

    remaining = col.count()
    if want_json:
        print(
            json.dumps(
                {
                    "stale_days": stale_days,
                    "scope": scope,
                    "palace": palace_path,
                    "backend": backend_name,
                    "deleted": len(stale_ids),
                    "remaining": remaining,
                }
            )
        )
    else:
        print(f"\n  Pruned {len(stale_ids):,} drawers from {target}. Remaining: {remaining:,}\n")


def cmd_rename_wing(args):
    want_json = getattr(args, "json", False)
    from_wing = args.from_wing
    to_wing = args.to_wing
    dry_run = getattr(args, "dry_run", False)
    batch_size = getattr(args, "batch_size", 500)

    if _daemon_strict():
        if dry_run:
            try:
                data = _call_daemon_tool(
                    "mempalace_list_drawers",
                    {
                        "wing": from_wing,
                        "limit": 1,
                    },
                )
            except DaemonError as e:
                if want_json:
                    _emit_json({"error": str(e)})
                else:
                    print(f"\n  ERROR: {e}", file=sys.stderr)
                sys.exit(2)
            total = data.get("total", 0)
            if want_json:
                _emit_json(
                    {"dry_run": True, "from_wing": from_wing, "to_wing": to_wing, "count": total}
                )
            else:
                print(
                    f"\n  Dry run: {total:,} drawers would be renamed from '{from_wing}' to '{to_wing}'\n"
                )
            return

        try:
            data = _call_daemon_tool(
                "mempalace_rename_wing",
                {
                    "from_wing": from_wing,
                    "to_wing": to_wing,
                    "batch_size": batch_size,
                },
            )
        except DaemonError as e:
            if want_json:
                _emit_json({"error": str(e)})
            else:
                print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(2)

        if data.get("success") is False or ("error" in data and "renamed" not in data):
            error_msg = data.get("error", "Unknown error")
            if want_json:
                _emit_json(data)
            else:
                print(f"\n  ERROR: {error_msg}", file=sys.stderr)
            sys.exit(2)

        if want_json:
            _emit_json(data)
        else:
            renamed = data.get("renamed", 0)
            errors = data.get("errors", 0)
            print(f"\n  Renamed {renamed:,} drawers: '{from_wing}' -> '{to_wing}'")
            if errors:
                print(f"  Errors: {errors:,}")
            print()
        return

    from ..backends.chroma import ChromaBackend
    from ..backends.base import PalaceRef

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    backend = ChromaBackend()
    try:
        col = backend.get_collection(
            palace=PalaceRef(id=palace_path, local_path=palace_path),
            collection_name="mempalace_drawers",
        )
    except Exception as e:
        print(f"\n  Error reading palace: {e}")
        sys.exit(1)

    if dry_run:
        matched = col.get(where={"wing": from_wing}, include=[])
        count = len(matched.ids) if hasattr(matched, "ids") else len(matched.get("ids", []))
        if want_json:
            _emit_json(
                {"dry_run": True, "from_wing": from_wing, "to_wing": to_wing, "count": count}
            )
        else:
            print(
                f"\n  Dry run: {count:,} drawers would be renamed from '{from_wing}' to '{to_wing}'\n"
            )
        return

    result = col.rename_wing(from_wing=from_wing, to_wing=to_wing, batch_size=batch_size)
    if want_json:
        _emit_json({"success": True, "from_wing": from_wing, "to_wing": to_wing, **result})
    else:
        print(f"\n  Renamed {result['renamed']:,} drawers: '{from_wing}' -> '{to_wing}'")
        if result["errors"]:
            print(f"  Errors: {result['errors']:,}")
        print()


# How many queued sources the plan names per wing before summarising.
_PENDING_PLAN_SAMPLE = 5


def _print_pending_plan(planned: list, *, want_json: bool) -> None:
    """Say what a drain would post, in the caller's chosen shape."""
    if want_json:
        _emit_json({"dry_run": True, "pending": len(planned), "requests": planned})
        return
    n = len(planned)
    print(f"\n  mempalace pending drain — {n} queued mine request(s) would be posted\n")
    by_wing: dict = {}
    for request in planned:
        by_wing.setdefault(request.get("wing") or "(no wing)", []).append(request)
    for wing in sorted(by_wing):
        rows = by_wing[wing]
        print(f"  wing {wing}  ({len(rows)})")
        for request in rows[:_PENDING_PLAN_SAMPLE]:
            source = str(request.get("dir") or "?")
            print(f"    {request.get('mode', 'convos'):<8} {source}")
        if len(rows) > _PENDING_PLAN_SAMPLE:
            print(f"    … and {len(rows) - _PENDING_PLAN_SAMPLE} more")
    print("\n  Nothing has been posted. Re-run with --yes to drain the queue.\n")


def cmd_pending(args):
    """Dispatch the ``pending`` verb group.

    A verb group named without an action is a usage error, the same class
    as argparse's own "invalid choice" — so it exits 2, not 64. 64 means
    exactly one thing: the daemon answered and rejected a well-formed
    request. This guard never reaches the daemon.

    It goes through ``_fail_client`` rather than printing and exiting
    inline, because a hand-rolled ``print(..., file=sys.stderr)`` gives a
    ``--json`` caller prose and no document at all — the wave's recurring
    shape, a report that disagrees with the promised contract.
    """
    action = getattr(args, "pending_action", None)
    if action == "drain":
        cmd_pending_drain(args)
        return
    _fail_client(
        "mempalace pending: choose an action (drain). See `mempalace pending --help`.",
        getattr(args, "json", False),
    )


def cmd_replay(args):
    """Deprecated alias for ``pending drain`` — warns, then defers.

    Kept because scripts and a cron entry call it. The name is the bug
    (#498): "replay" reads as replaying history, while it posts mine jobs.
    It inherits the ``--yes`` guard rather than routing around it, so the
    alias is not a back door to the old behaviour.
    """
    print(
        "mempalace replay: renamed to `mempalace pending drain` — "
        "it drains the pending MINE queue, it does not replay history (#498).",
        file=sys.stderr,
    )
    cmd_pending_drain(args)


def cmd_pending_drain(args):
    """Re-post every queued mine request to the daemon — after saying so.

    ``~/.mempalace/pending/*.jsonl`` accumulates mine requests whenever the
    Stop / PreCompact hooks fire while the daemon or its backend is
    unreachable (the 2026-05-21 power-resilience design). Draining it
    re-posts each one.

    **Defaults to a plan.** The verb this replaced, ``replay``, took no
    arguments, printed no description, and posted everything immediately: a
    2026-09-17 session reading it as "replay history" queued twelve mines
    against production (#498). Nothing is posted without ``--yes``.

    The plan comes from :func:`pending_queue.peek`, which reproduces the
    drain's own filtering rather than counting lines, so the number printed
    is the number that would be posted. A preview that over-counted would be
    the same defect this command was filed for.
    """
    want_json = getattr(args, "json", False)

    if not _daemon_strict():
        msg = "nothing to do (PALACE_DAEMON_URL not set or strict mode off)"
        if want_json:
            _emit_json({"error": msg, "pending": 0})
        else:
            print(f"mempalace pending drain: {msg}", file=sys.stderr)
        sys.exit(0)

    try:
        from .. import pending_queue
    except Exception as e:  # noqa: BLE001 — user-facing CLI guard
        if want_json:
            _emit_json({"error": f"could not import pending_queue: {e}"})
        else:
            print(f"  ERROR: could not import pending_queue: {e}", file=sys.stderr)
        sys.exit(2)

    planned = pending_queue.peek()
    if not planned:
        if want_json:
            _emit_json({"dry_run": not getattr(args, "yes", False), "pending": 0, "requests": []})
        else:
            print("mempalace pending drain: the pending queue is empty.")
        sys.exit(0)

    if not getattr(args, "yes", False):
        _print_pending_plan(planned, want_json=want_json)
        sys.exit(0)

    def post(request: dict) -> bool:
        # ``background=True`` is what makes a drain a drain rather than a
        # crawl: a synchronous /mine waits for a gap in the palace write
        # lock, and under checkpoint load those gaps are minutes apart
        # (measured: 120 s, one request drained, #456). The daemon's own
        # pending-mines queue is durable, so handing off is not a loss of
        # delivery guarantee — it moves the wait to the side that owns the
        # lock.
        return _post_daemon_mine_cli(
            request["dir"],
            request["wing"],
            # `or`, not a .get default: a queued line may carry an explicit
            # `"mode": null` (the hooks write whatever they were given), and
            # `.get("mode", "convos")` returns None for that, not the
            # default. Since #525 the poster REFUSES None, so the difference
            # is a crashed drain versus a drained queue.
            request.get("mode") or "convos",
            background=True,
        )

    report = pending_queue.replay(post)
    if want_json:
        _emit_json(
            {
                "dry_run": False,
                "attempted": report.attempted,
                "succeeded": report.succeeded,
                "failed": report.failed,
                "files_drained": report.files_drained,
            }
        )
    else:
        print(
            f"mempalace pending drain: attempted={report.attempted} "
            f"succeeded={report.succeeded} failed={report.failed} "
            f"files_drained={report.files_drained}"
        )
    # sys.exit, not return: `main` discards a handler's return value, so the
    # old `return 1` never reached the shell and a wholly failed drain still
    # exited 0. Tracked separately; these paths do not wait for it.
    sys.exit(0 if report.failed == 0 else 1)


def cmd_migrate_wings(args):
    """Normalize legacy wing names (strip leading/trailing separators)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    from ..migrate import migrate_wing_names

    migrate_wing_names(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


# Curated docs the `doctor --curated` check inspects, and the ceiling on how
# many. The cap is a budget guard, not a taste call: the check's cost is one
# daemon request regardless, but the enumeration stats every file, and a
# repo with a thousand docs should not turn a health check into a disk walk.
_CURATED_DOCS_MAX = 50
#: How many file names `reconcile-docs --dry-run` lists before summarising.
_RECONCILE_SAMPLE = 10


def _curated_doc_paths(
    project_root: str, *, limit: "int | None" = _CURATED_DOCS_MAX, respect_gitignore: bool = False
) -> tuple:
    """``(paths_to_examine, total_found)`` for the project's curated docs.

    ``CLAUDE.md`` plus ``docs/**/*.md`` — the hand-written layer whose stale
    copy in the palace is invisible, as distinct from transcripts, which are
    append-only and re-mined continuously.

    Sorted for determinism (two runs must agree, or the cap would inspect a
    different subset each time) and capped at :data:`_CURATED_DOCS_MAX`.

    The PRE-CAP total comes back with the list, and that is load-bearing
    rather than convenience. Returning only the capped list made the
    truncated tail unrepresentable: it could never populate stale / never
    indexed / undecidable, so the check printed ✓ "N up to date" and exited 0
    having examined a fraction of the files. Measured in review — 2g holds
    795 curated docs, 745 of them unexamined, i.e. 6.3% coverage able to
    report clean. A cap that hides what it skipped turns this check into the
    one thing the module refuses to be.

    ``CLAUDE.md`` is placed first BY CONSTRUCTION so it always survives the
    cap. Sorting the combined list happened to do that, because ``C`` sorts
    before ``docs/``, but "happens to" is not a property worth relying on —
    a project whose docs directory were capitalised would have lost it.

    ``limit=None`` removes the cap, and ``reconcile-docs`` (#529) passes it:
    a cap is right for a health check that must stay cheap and wrong for a
    reconciler whose entire job is to miss nothing. ``respect_gitignore``
    drops ignored files through the miner's own matchers, so the reconciler
    never queues a mine for a file the miner would refuse to ingest — two
    components disagreeing about what counts as a doc is the defect this
    verb exists to remove, and it would be poor form to introduce it here.
    """
    root = os.path.abspath(os.path.expanduser(project_root))
    if not os.path.isdir(root):
        return [], 0
    head = []
    claude = os.path.join(root, "CLAUDE.md")
    if os.path.isfile(claude):
        head.append(claude)
    docs = []
    docs_root = os.path.join(root, "docs")
    if os.path.isdir(docs_root):
        for dirpath, dirnames, filenames in os.walk(docs_root):
            dirnames.sort()
            for name in sorted(filenames):
                if name.endswith(".md"):
                    docs.append(os.path.join(dirpath, name))
    ordered = head + sorted(docs)
    found = len(ordered)
    if respect_gitignore:
        ordered = [p for p in ordered if not _doc_is_gitignored(root, p)]
    # `found` is the PRE-filter, PRE-cap total on purpose: a caller that only
    # received the survivors could not report how many it skipped, and a count
    # that hides what it dropped is the same under-reporting this module was
    # written to remove (#490).
    return (ordered if limit is None else ordered[:limit]), found


def _doc_is_gitignored(root: str, path: str) -> bool:
    """True when the miner's own .gitignore matchers would skip this file.

    Uses ``miner.load_gitignore_matcher`` / ``is_gitignored`` rather than a
    second implementation: if the reconciler and the miner disagreed about
    what counts as a doc, the reconciler would queue mines the miner drops
    and report them as queued — a report disagreeing with what happened,
    which is the defect family this verb exists to close.
    """
    try:
        from pathlib import Path

        from ..miner import is_gitignored, load_gitignore_matcher
    except Exception:  # noqa: BLE001 — never let an import break enumeration
        return False
    cache: dict = {}
    here = Path(root)
    target = Path(path)
    try:
        rel_parts = target.relative_to(here).parts[:-1]
    except ValueError:
        return False
    # ``load_gitignore_matcher`` answers None for a directory with no
    # .gitignore, and ``is_gitignored`` calls ``.matches`` on every entry it
    # is handed — the miner's walker drops the Nones before that call, and so
    # must this. The first version appended them and swallowed the resulting
    # AttributeError under a broad except, which made every doc read
    # "not ignored": a matcher that had never once matched, reporting clean.
    matchers = [
        m
        for m in (
            load_gitignore_matcher(part, cache)
            for part in (
                here,
                *[here.joinpath(*rel_parts[: i + 1]) for i in range(len(rel_parts))],
            )
        )
        if m is not None
    ]
    return bool(is_gitignored(target, matchers))


def _in_linked_worktree(path: str) -> bool:
    """True when ``path`` is inside a linked git worktree, not a main checkout.

    A linked worktree's ``.git`` is a FILE holding ``gitdir: …``; a main
    checkout's is a directory. Cheap and exact, and worth knowing here: the
    palace recorded the MAIN checkout's absolute paths, so every curated doc
    enumerated from a worktree is legitimately "never indexed" — true, and
    useless as a health signal. An alarming line that means nothing is how a
    check gets ignored (mempalace#454 was a false red that every lane learned
    to deselect), so the check says which situation it is in.
    """
    marker = os.path.join(os.path.abspath(os.path.expanduser(path)), ".git")
    return os.path.isfile(marker)


def _curated_indexed_mtimes(wing: str, palace_path: str) -> tuple:
    """``({source_file: mtime|None}, note)`` — what the palace recorded.

    Returns the same shape from either source, because both already speak it:
    the daemon's ``mempalace_mined`` (one request, ``max_source_mtime`` per
    source — palace-daemon#266) and ``palace.prefetch_mined_set`` (one bulk
    query, written precisely to avoid a per-file cost).

    ``note`` is non-empty when the map cannot be trusted as complete, and the
    caller must then report *undecidable* rather than clean. A doctor that
    turns its own inability to look into a clean bill of health is the
    failure mode this whole check exists to remove.
    """
    if _daemon_url():
        try:
            data = _call_daemon_tool("mempalace_mined", {"wing": wing})
        except DaemonError as exc:
            return {}, f"daemon unreachable ({exc})"
        recorded = {}
        missing_field = False
        for slot in (data.get("sources_by_wing") or {}).values():
            for source in slot.get("sources") or []:
                path = source.get("source_file")
                if not path:
                    continue
                if "max_source_mtime" not in source:
                    missing_field = True
                recorded[path] = source.get("max_source_mtime")
        if missing_field:
            return recorded, "daemon predates max_source_mtime (palace-daemon#266)"
        return recorded, ""
    try:
        from ..palace import get_collection, prefetch_mined_set

        col = get_collection(palace_path, collection_name=None, create=False)  # the configured drawers collection
        return dict(prefetch_mined_set(col)), ""
    except Exception as exc:  # noqa: BLE001 — a doctor never raises
        return {}, f"palace not readable here ({exc})"


def _classify_curated_docs(paths, recorded) -> tuple:
    """``(stale, never, unknown, current)`` for enumerated curated docs.

    One classifier, read by both ``doctor --curated`` and ``reconcile-docs``
    (#529), because the two must agree about what "stale" means or the doctor
    would report a file the reconciler declines to queue — a disagreement
    between two components that both claim to answer the same question, which
    is the failure this verb exists to remove rather than to add.

    Staleness is decided by :func:`mempalace.provenance.source_stale` and
    nowhere else: it owns the 60-second grace window and the honest ``None``.

    ``unknown`` is NEVER folded into ``stale``. Undecidable means the answer
    is not available — an older daemon with no ``max_source_mtime``, a file
    this host cannot stat, a recorded time in the future — and treating it as
    "probably stale" would re-mine a whole wing on the strength of a missing
    field. It is reported and left for a human, on both sides.
    """
    from datetime import datetime

    from .. import provenance

    stale, never, unknown, current = [], [], [], []
    for path in paths:
        if path not in recorded:
            never.append(path)
            continue
        mtime = recorded.get(path)
        if mtime is None:
            unknown.append(path)
            continue
        hit = {
            "source_file": path,
            "indexed_at": datetime.fromtimestamp(float(mtime)).isoformat(),
        }
        verdict = provenance.source_stale(hit)
        if verdict is True:
            stale.append(path)
        elif verdict is None:
            unknown.append(path)
        else:
            current.append(path)
    return stale, never, unknown, current


def _curated_docs_check(wing: str, palace_path: str) -> tuple:
    """``(ok, detail, level)`` for the curated-docs staleness check (#451 E).

    Staleness is decided by :func:`mempalace.provenance.source_stale` and
    nowhere else — it already owns the comparison, the 60-second grace window
    ("a file touched within a minute of its own mine is the mine, not an
    edit") and the honest ``None`` cases. A second notion here would drift.

    Four outcomes per file, and only the first is a ✗:

    * **stale** — on disk, newer than what the palace read.
    * **never indexed** — absent from the source list. Reliable, because that
      list is an enumeration; "absent from a search's top N" would not be.
    * **undecidable** — no recorded mtime, an older daemon, a file this host
      cannot stat, or a recorded time in the future. Reported, never cleaned.
    * **fresh**.

    Note the deliberate disagreement with ``prefetch_mined_set``, whose
    docstring says to treat a ``None`` mtime as *stale*. That is right for
    the miner, where re-mining is cheap and safe. Here it would fabricate
    "you edited this" out of missing bookkeeping, so it reads undecidable.
    """
    paths, total_found = _curated_doc_paths(os.getcwd())
    if not paths:
        return None, "no CLAUDE.md or docs/*.md in this project", "warn"
    unexamined = total_found - len(paths)

    recorded, note = _curated_indexed_mtimes(wing, palace_path)
    stale, never, unknown, _current = _classify_curated_docs(paths, recorded)

    parts = []
    if stale:
        parts.append(
            "{} file(s) modified after indexing — run `mempalace mine {}`".format(
                len(stale), stale[0]
            )
        )
    if never:
        parts.append("{} never indexed".format(len(never)))
    if unknown:
        parts.append("{} cannot be checked".format(len(unknown)))
    if note:
        parts.append(note)
    if unexamined:
        # Disclosed on EVERY outcome, not only the clean one: a ✗ that hides
        # how much it did not look at is still an under-reported answer.
        parts.append(
            "{} examined, {} not examined (cap {})".format(
                len(paths), unexamined, _CURATED_DOCS_MAX
            )
        )
    if never and _in_linked_worktree(os.getcwd()):
        parts.append(
            "run from a linked worktree, whose paths the palace never saw — "
            "check from the main checkout"
        )
    if not parts:
        return True, "{} curated file(s) up to date".format(len(paths)), "ok"
    detail = "; ".join(parts)
    if stale:
        return False, detail, "error"
    # Everything examined was fine but the cap hid the rest: that is an
    # unknown, never a clean bill. A truncated run can only ever be a warn.
    return None, detail, "warn"


def cmd_doctor(args):
    """One-screen health check of the memory workflow (#425).

    Answers, in order: is the MCP bridge on PATH? is the daemon reachable and
    how big is the palace? is this project's wing present? are the save hooks
    firing (fresh hook_state)? is anything queued for replay? Every line is a
    ✓ / ✗ / ! so a broken memory workflow is loud instead of silently wrong —
    the failure mode that let the MCP bridge stay dead fleet-wide for days.
    """
    import shutil
    import time as _time

    want_json = getattr(args, "json", False)
    checks: list[dict] = []

    def add(name, ok, detail, level="ok"):
        # ``ok`` is TRI-state and must stay that way: True / None ("cannot
        # tell") / False. ``bool(ok)`` collapsed None to False, which made
        # every warn render as ✗ (the glyph map's "warn" entry was
        # unreachable) and flip the exit code — against the intent visible
        # two lines down, where ``ok_all`` tests ``is not False`` precisely so
        # a None passes, and against the closing message, which points at the
        # ✗ lines only. It went unnoticed because all five checks are ✓ on a
        # healthy host, so no warn had ever been rendered.
        checks.append(
            {
                "check": name,
                "ok": ok if ok is None else bool(ok),
                "detail": detail,
                "level": level,
            }
        )

    # 1. MCP bridge resolvable on PATH.
    bridge = shutil.which("mempalace-mcp")
    add(
        "mcp_bridge",
        bool(bridge),
        bridge or "mempalace-mcp NOT on PATH",
        "ok" if bridge else "error",
    )

    # 2. Daemon reachability + palace size (fast, no locks).
    total_drawers = None
    n_wings = None
    wing_counts = {}
    if _daemon_url():
        t0 = _time.monotonic()
        try:
            data = _call_daemon_rest("/status/fast")
            ms = int((_time.monotonic() - t0) * 1000)
            if data is None:
                # 404: the route is missing on this daemon. Before #518 this
                # fell straight into `data.get(...)`, and the AttributeError
                # was caught by the blanket handler below and rendered as
                # "unreachable @ <url>: 'NoneType' object has no attribute
                # 'get'" — a Python error shown to an operator as an outage.
                # #518 moved the 401/403 half of that onto DaemonAuthError;
                # this is the other half, which the same change RELOCATED
                # from 401 to 404 rather than removing.
                add(
                    "daemon",
                    False,
                    "reachable @ {} but /status/fast is missing — older daemon ({}ms)".format(
                        _daemon_url(), ms
                    ),
                    "error",
                )
                raise _DoctorHandled
            total_drawers = data.get("total_drawers")
            wing_counts = data.get("wings", {}) or {}
            n_wings = len(wing_counts)
            add(
                "daemon",
                True,
                "reachable @ {} — {} drawers, {} wings ({}ms)".format(
                    _daemon_url(), total_drawers, n_wings, ms
                ),
            )
        except _DoctorHandled:
            pass
        except DaemonAuthError as e:
            # A refused credential is not an outage (#518). Saying
            # "unreachable" here sends the operator to check whether the
            # daemon is running when the daemon is up and answering.
            add(
                "daemon",
                False,
                "reachable @ {} but refused the credential ({}) — check PALACE_API_KEY".format(
                    _daemon_url(), e.status
                ),
                "error",
            )
        except Exception as e:  # noqa: BLE001 — a doctor never raises
            add("daemon", False, "unreachable @ {}: {}".format(_daemon_url(), e), "error")
    else:
        add("daemon", None, "no daemon configured (local-palace mode)", "warn")

    # 3. This project's wing.
    wing = getattr(args, "wing", None)
    if not wing:
        from ..config import normalize_wing_name

        wing = normalize_wing_name(Path(os.getcwd()).name)
    if wing_counts:
        n = wing_counts.get(wing, 0)
        if n:
            ranked = sorted(wing_counts.values(), reverse=True)
            rank = ranked.index(n) + 1 if n in ranked else 0
            add("wing", True, "'{}': {:,} drawers (rank {} of {})".format(wing, n, rank, n_wings))
        else:
            add("wing", None, "'{}': no drawers yet".format(wing), "warn")
    else:
        add("wing", None, "'{}': cannot check (daemon unreachable)".format(wing), "warn")

    # 4. Save hooks firing — freshness of the hook-state marker.
    hook_log = os.path.expanduser("~/.mempalace/hook_state/hook.log")
    if os.path.isfile(hook_log):
        age = _time.time() - os.path.getmtime(hook_log)
        if age < 1800:
            add("save_hooks", True, "hook.log updated {}s ago".format(int(age)))
        else:
            add(
                "save_hooks",
                None,
                "hook.log stale ({}m ago) — no recent saves".format(int(age // 60)),
                "warn",
            )
    else:
        add("save_hooks", None, "no hook_state/hook.log — hooks may not be installed", "warn")

    # 5. Replay backlog.
    pending_dir = os.path.expanduser("~/.mempalace/pending")
    pending = 0
    if os.path.isdir(pending_dir):
        for fn in os.listdir(pending_dir):
            if fn.endswith(".jsonl"):
                try:
                    with open(os.path.join(pending_dir, fn), encoding="utf-8") as f:
                        pending += sum(1 for ln in f if ln.strip())
                except OSError:
                    pass
    if pending == 0:
        add("replay_queue", True, "empty")
    else:
        add(
            "replay_queue",
            None,
            "{} request(s) queued — run `mempalace replay`".format(pending),
            "warn",
        )

    # 6. Curated docs newer than what the palace indexed (#451 item E).
    # Opt-in: the cheapest reliable source of recorded mtimes is one
    # `mempalace_mined` call at ~730ms, and the five checks above run in
    # ~405ms all together, so this would nearly triple an unasked-for
    # health check.
    if getattr(args, "curated", False):
        c_ok, c_detail, c_level = _curated_docs_check(
            wing,
            os.path.expanduser(getattr(args, "palace", None) or MempalaceConfig().palace_path),
        )
        add("curated_docs", c_ok, c_detail, c_level)

    ok_all = all(c["ok"] is not False for c in checks)
    if want_json:
        _emit_json({"ok": ok_all, "checks": checks})
        sys.exit(0 if ok_all else 1)

    glyph = {"ok": "\u2713", "warn": "!", "error": "\u2717"}
    print("\n  MemPalace doctor\n  " + "-" * 40)
    for c in checks:
        lvl = c["level"] if c["ok"] is not False else "error"
        print("  {} {:<12} {}".format(glyph.get(lvl, "?"), c["check"], c["detail"]))
    print()
    if not ok_all:
        print(
            "  \u2717 something is wrong with the memory workflow — see the \u2717 lines above.\n"
        )
    sys.exit(0 if ok_all else 1)


def cmd_status(args):
    want_json = getattr(args, "json", False)
    if _daemon_strict() and not args.palace:
        # --palace overrides routing: an explicit local-path argument
        # means the user wants to inspect THAT palace, not the daemon.
        try:
            data = _call_daemon_rest("/status/fast")
            if data is None:
                data = _call_daemon_tool("mempalace_status", {})
        except DaemonError as e:
            # Delegated (#521): this site hand-rolled the payload and the exit
            # code, so a refusal reached it as an outage — `status` with a 401
            # exited 2 saying "ERROR: <detail>" while `stats` exited 64. The
            # shared helper carries both the option-C shape and the
            # refusal-vs-outage split.
            _fail_daemon(e, want_json)
        if want_json:
            _emit_json(data)
            return
        _print_daemon_status(data)
        return

    from ..miner import status

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if want_json:
        _emit_local_status_json(palace_path)
        return
    status(palace_path=palace_path)


def _emit_local_status_json(palace_path: str) -> None:
    """JSON status from a local palace — mirrors the MCP ``tool_status``
    response shape: ``{total_drawers, wings, rooms}`` plus an ``error``
    key when the palace is unreachable. Used by ``cmd_status --json``
    when daemon routing is off (or ``--palace`` was passed).
    """
    from collections import defaultdict

    from ..miner import _open_collection_or_explain

    # ``_open_collection_or_explain`` prints a human-readable hint to
    # stdout on failure. Capture it so JSON output stays clean.
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        col = _open_collection_or_explain(palace_path)
    if col is None:
        _emit_json(
            {
                "error": "palace_unavailable",
                "hint": buf.getvalue().strip() or f"No palace found at {palace_path}",
                "palace_path": palace_path,
            }
        )
        sys.exit(2)

    total = col.count()
    wings: dict = defaultdict(int)
    rooms: dict = defaultdict(int)
    batch_size = 5000
    offset = 0
    while offset < total:
        r = col.get(limit=batch_size, offset=offset, include=["metadatas"])
        batch = r.get("metadatas") or []
        if not batch:
            break
        for m in batch:
            m = m or {}
            wings[m.get("wing", "unknown")] += 1
            rooms[m.get("room", "unknown")] += 1
        offset += len(batch)

    _emit_json(
        {
            "total_drawers": total,
            "wings": dict(wings),
            "rooms": dict(rooms),
            "palace_path": palace_path,
        }
    )


def _open_drawers_or_refuse(
    palace_path: str, *, explicit_backend=None, want_json: bool = False
) -> tuple:
    """Resolve the backend, gate the local precheck, open the drawers.

    Returns ``(collection, backend_name, target_label)``. On any refusal it
    prints ONE message and exits; it never returns a half-answer.

    One sequence for ``purge``, ``prune`` and ``mined``, which each grew
    their own copy across #418 and #459 (#485). The policy was identical
    every time; only the messages and the exit codes drifted, which is what
    made the same condition report differently depending on which command
    you happened to run.

    **The exit codes are not a taste call.** ``cli.py`` has carried a
    contract since #44 — 0 success, 1 no results, 2 the operation could not
    run, 64 the daemon rejected a well-formed request — and every refusal
    below is "could not run", so every one
    is 2. Two of the three commands were exiting **0** on a missing
    database: a refusal reported as a success, which is the defect #418 and
    #459 removed from the prose, still alive in the exit status.

    ``1`` keeps meaning exactly what the contract says — the operation ran
    and selected nothing. That is ``purge``'s zero-match case and nothing in
    here.

    Resolution precedes the directory test: a service-backed palace has no
    local database file by design, so that test is not a question about
    whether the palace exists (#459).

    ``sync`` is deliberately OUTSIDE this sequence and still exits 0 on a
    missing palace and 1 on a database-less directory, which makes the four
    commands more divergent than before this helper existed (2/2/2/0). Not
    an oversight: its open path runs through ``sync_palace()`` behind a
    daemon route, a job-queue route, an apply-scope rule and a confirmation
    prompt, so adopting this is a decision about where the precheck sits
    relative to daemon routing rather than a substitution. Tracked in #494.
    """
    from ..backends import detect_backend_for_path
    from ..migrate import contains_palace_database
    from ..palace import (
        BackendMismatchError,
        backend_stores_data_locally,
        get_collection,
        resolve_backend_name,
    )

    def _refuse(hint, *, backend_name=None, palace_state=False):
        """Emit one refusal and exit 2.

        ``palace_state`` picks the renderer and is a separate argument on
        purpose: an earlier draft inferred it from whether the backend was
        known, so a connection error — which knows its backend — printed "No
        palace found at <dir>" and hid the real cause. A refusal naming the
        wrong reason is this same defect family in miniature.
        """
        if want_json:
            payload = {
                "error": "palace_unavailable",
                "hint": hint,
                "palace_path": palace_path,
            }
            if backend_name:
                payload["backend"] = backend_name
            _emit_json(payload)
        elif palace_state:
            _print_retired_local_palace_or_default(palace_path)
        else:
            print(f"\n  {hint}")
        sys.exit(2)

    try:
        backend_name = resolve_backend_name(palace_path, explicit=explicit_backend)
    except BackendMismatchError as exc:
        _refuse(f"Backend mismatch at {palace_path}: {exc}")
    except KeyError as exc:
        detail = exc.args[0] if exc.args else exc
        _refuse(f"Unknown backend selected for {palace_path}: {detail}")

    if backend_stores_data_locally(backend_name):
        # ``contains_palace_database`` is chroma-specific (chroma.sqlite3);
        # the artifact probe covers the other local backends, which these
        # commands used to reject for not being chroma.
        has_db = os.path.isdir(palace_path) and (
            contains_palace_database(palace_path)
            or detect_backend_for_path(palace_path) is not None
        )
        if not has_db:
            _refuse(
                f"No palace database at {palace_path}",
                backend_name=backend_name,
                palace_state=True,
            )
        target = f"{palace_path} ({backend_name})"
    else:
        target = f"{backend_name} backend (palace {palace_path})"

    try:
        col = get_collection(
            palace_path,
            collection_name=None,  # the configured drawers collection
            create=False,
            backend=backend_name,
        )
    except Exception as e:  # noqa: BLE001 — backend exceptions vary by backend
        _refuse(f"Error reading palace: {e}", backend_name=backend_name)
    return col, backend_name, target


def _main_checkout_for(worktree_root: str) -> str:
    """The main checkout a linked worktree belongs to, or "" if undeterminable.

    A linked worktree's ``.git`` is a FILE holding
    ``gitdir: /main/.git/worktrees/<name>``, so the main checkout is that
    path with ``/.git/worktrees/<name>`` removed. Returning "" rather than
    guessing matters: the refusal message names a directory the operator is
    about to run a command in, and a wrong one sends them somewhere real and
    unrelated. No answer is better than a confident wrong path.
    """
    marker = os.path.join(os.path.abspath(os.path.expanduser(worktree_root)), ".git")
    try:
        with open(marker, encoding="utf-8") as handle:
            line = handle.read().strip()
    except OSError:
        return ""
    if not line.startswith("gitdir:"):
        return ""
    gitdir = line.split(":", 1)[1].strip()
    marker_seq = os.sep + ".git" + os.sep + "worktrees" + os.sep
    idx = gitdir.find(marker_seq)
    if idx == -1:
        return ""
    return gitdir[:idx]


def _curated_wing_for(project_root: str) -> str:
    """The wing the curated-docs hook would use for this project root.

    Mirrors `palace-memory-sync.sh` exactly — `basename` of the project
    directory, lowercased, with `-` mapped to `_` (`tr 'A-Z-' 'a-z_'`) — so a
    doc reconciled here lands in the same wing the hook would have filed it
    under. Two components choosing different wings for the same file is not a
    cosmetic difference: the palace would hold two copies, and a search
    scoped to one wing would miss the other.
    """
    base = os.path.basename(os.path.abspath(os.path.expanduser(project_root)))
    return base.lower().replace("-", "_")


def cmd_reconcile_docs(args):
    """Queue a mine for every curated doc the palace has not seen (#529).

    The curated-docs hook is event-driven and therefore best-effort. Measured
    on the live hook before this verb was written, it misses a doc in at
    least five distinct ways: a worktree path its Bash matcher excludes; a
    worktree path its shell-glob matcher DOES match and which then dies on
    the palace host, which has no `.claude/worktrees` at all; a doc arriving
    by merge or pull, which produces no tool event; a glob token taken for a
    filename; and a path the command merely mentioned. Every one is invisible
    to a listener and obvious to a comparison against what is on disk.

    So this does not listen. It enumerates `CLAUDE.md` + `docs/**/*.md`,
    asks the palace what it recorded, and queues a background single-file
    projects-mode mine for anything newer or absent — the same payload
    `palace-doc-sync.sh` sends, through the same poster, so there is one
    wire format rather than two that drift.

    Undecidable files are reported and NOT queued. "No recorded mtime" is an
    absence of an answer, not a stale answer, and re-mining a whole wing on
    the strength of a missing field is how a reconciler becomes the thing
    that needs reconciling.
    """
    want_json = getattr(args, "json", False)
    root = os.path.abspath(os.path.expanduser(args.repo_root))

    if not os.path.isdir(root):
        _fail_client(f"reconcile-docs: {args.repo_root} is not a directory", want_json)

    if _in_linked_worktree(root):
        # Refuse rather than queue work that cannot land: the palace host
        # carries the MAIN checkout only (Syncthing does not replicate
        # `.claude/worktrees`), so every mine queued for a worktree path is
        # logged "not present on palace host" and dropped. Queuing them would
        # print a confident `queued=N` for N mines that will never run.
        main = _main_checkout_for(root)
        _fail_client(
            "reconcile-docs: {} is a linked worktree; the palace never saw its "
            "paths and the palace host does not carry them. Run it against the "
            "main checkout{}".format(root, f" ({main})" if main else ""),
            want_json,
        )

    wing = getattr(args, "wing", None) or _curated_wing_for(root)
    paths, found = _curated_doc_paths(root, limit=None, respect_gitignore=True)
    skipped_ignored = found - len(paths)

    if not paths:
        msg = f"reconcile-docs: no CLAUDE.md or docs/**/*.md under {root}"
        if want_json:
            _emit_json(
                {
                    "root": root,
                    "wing": wing,
                    "queued": 0,
                    "current": 0,
                    "skipped_ignored": skipped_ignored,
                    "note": msg,
                }
            )
        else:
            print(msg)
        sys.exit(1)

    if not _daemon_url():
        _fail_daemon(
            DaemonError("reconcile-docs needs the palace-daemon; set PALACE_DAEMON_URL"),
            want_json,
        )

    palace_path = os.path.expanduser(args.palace) if getattr(args, "palace", None) else ""
    recorded, note = _curated_indexed_mtimes(wing, palace_path)
    if note.startswith("daemon unreachable"):
        _fail_daemon(DaemonError(note), want_json)

    stale, never, unknown, current = _classify_curated_docs(paths, recorded)
    to_queue = stale + never

    if not recorded and not getattr(args, "force", False):
        # An empty answer and a genuinely unindexed project are the same
        # picture from here, and they want opposite actions. The likeliest
        # cause is a wing that does not exist — a typo, or a project whose
        # directory name is not its wing — and guessing wrong is not a wasted
        # call: single-file curated mines each pay a derived-graph recompute
        # (measured ~20 min, which is why the hook has a pause switch), so a
        # mistyped wing would queue every doc in the repo.
        #
        # Refuse and name the wing. --dry-run is checked BELOW this, so the
        # plan still prints — showing the operator what it would do is how
        # they discover the wing was wrong.
        _fail_client(
            "reconcile-docs: the palace recorded nothing for wing {!r}, so every "
            "one of the {} enumerated file(s) reads as never-indexed. That is "
            "usually a wrong --wing rather than an empty project. Check with "
            "`mempalace mined --wing {}`, or pass --force if the project really "
            "has never been mined.".format(wing, len(paths), wing),
            want_json,
            wing=wing,
            enumerated=len(paths),
        )

    if getattr(args, "dry_run", False):
        if want_json:
            _emit_json(
                {
                    "root": root,
                    "wing": wing,
                    "dry_run": True,
                    "would_queue": len(to_queue),
                    "current": len(current),
                    "undecidable": len(unknown),
                    "skipped_ignored": skipped_ignored,
                    "files": to_queue,
                    "note": note,
                }
            )
        else:
            print(f"\n  reconcile-docs {root} → wing {wing}\n")
            print(f"    would queue      {len(to_queue)} file(s)")
            for path in to_queue[:_RECONCILE_SAMPLE]:
                print(f"      {os.path.relpath(path, root)}")
            if len(to_queue) > _RECONCILE_SAMPLE:
                print(f"      … and {len(to_queue) - _RECONCILE_SAMPLE} more")
            print(f"    current          {len(current)} file(s)")
            print(f"    undecidable      {len(unknown)} file(s) (reported, never queued)")
            print(f"    skipped-ignored  {skipped_ignored} file(s)")
            if note:
                print(f"    note             {note}")
            print("\n  Nothing has been queued. Re-run without --dry-run.\n")
        sys.exit(0 if to_queue else 1)

    queued, failed = [], []
    # The poster prints a one-line receipt per accepted mine. Under --json
    # that receipt would land in front of the document and every consumer's
    # `json.loads(stdout)` would fail on precisely the runs that queued
    # something — the useful ones. Receipts go to stderr in JSON mode.
    receipts = contextlib.redirect_stdout(sys.stderr) if want_json else contextlib.nullcontext()
    with receipts:
        for path in to_queue:
            if _post_daemon_mine_cli(path, wing=wing, mode="projects", background=True):
                queued.append(path)
            else:
                failed.append(path)

    if want_json:
        _emit_json(
            {
                "root": root,
                "wing": wing,
                "dry_run": False,
                "queued": len(queued),
                "failed": len(failed),
                "current": len(current),
                "undecidable": len(unknown),
                "skipped_ignored": skipped_ignored,
                "files": queued,
                "note": note,
            }
        )
    else:
        print(
            f"reconcile-docs: queued={len(queued)} file(s) failed={len(failed)} "
            f"current={len(current)} file(s) undecidable={len(unknown)} file(s) "
            f"skipped-ignored={skipped_ignored} file(s) wing={wing}"
        )
        if note:
            print(f"  note: {note}", file=sys.stderr)
    if failed:
        sys.exit(2)
    sys.exit(0 if queued else 1)


def cmd_mined(args):
    """List mined source files grouped by wing.

    Companion to ``status`` (which groups by wing × room) — answers "which
    files have I mined into this wing?" so an operator can pick targets
    for ``mempalace purge --source-file <path>``.

    Skips drawers without a ``source_file`` metadata key (typically
    diary entries, kg drawers, manually-added entries).

    Daemon-routes when ``_daemon_strict()`` is on and ``--palace`` was
    not given (#285). palace-daemon's ``mempalace_mined`` tool (PR #96)
    returns the same ``{sources_by_wing, wing_filter, total_wings,
    total_sources}`` shape the JSON path emits locally.
    """
    want_json_early = getattr(args, "json", False)

    if _daemon_strict() and not args.palace:
        try:
            payload: dict = {}
            if getattr(args, "wing", None):
                payload["wing"] = args.wing
            if getattr(args, "limit", None) is not None:
                payload["limit"] = args.limit
            data = _call_daemon_tool("mempalace_mined", payload)
        except DaemonError as e:
            if want_json_early:
                _emit_json({"error": str(e), "source": "daemon"})
            else:
                print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        _print_daemon_mined(data, want_json=want_json_early, limit=args.limit)
        return

    from collections import defaultdict

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )
    col, _backend_name, _target = _open_drawers_or_refuse(
        palace_path, explicit_backend=_backend_arg(args), want_json=want_json_early
    )

    # Wing-by-source aggregation. Pagination mirrors miner.status so
    # palaces with hundreds of thousands of drawers don't trip SQLite's
    # max-variable limit on a single col.get(limit=total).
    #
    # When --wing is given, push the filter into each batch's where=
    # so we never scan unrelated wings. The previous version called
    # col.get(where=..., include=[]) without pagination to size the loop;
    # ChromaDB returns at most ~10K ids on a single get() call, silently
    # truncating beyond that — so the inner loop's `offset < total` would
    # have undercounted on a wing > 10K drawers, missing late entries.
    # Drop the upfront sizing entirely; the inner loop reads until it
    # gets back an empty batch (Copilot finding on jphein/mempalace#7).
    where = {"wing": args.wing} if args.wing else None
    wing_sources: dict = defaultdict(lambda: defaultdict(int))
    batch_size = 5000
    offset = 0
    while True:
        kwargs = {"limit": batch_size, "offset": offset, "include": ["metadatas"]}
        if where is not None:
            kwargs["where"] = where
        r = col.get(**kwargs)
        batch = r.get("metadatas") if isinstance(r, dict) else getattr(r, "metadatas", [])
        if not batch:
            break
        for m in batch:
            m = m or {}
            src = m.get("source_file")
            if not src:
                continue
            wing = m.get("wing", "?")
            wing_sources[wing][src] += 1
        offset += len(batch)
        # Defensive: if the backend returned fewer than batch_size, we're
        # past the last page. Saves one trailing empty col.get on palaces
        # whose wing-count is an exact multiple of batch_size (rare but
        # cheap to handle).
        if len(batch) < batch_size:
            break

    want_json = getattr(args, "json", False)

    if not wing_sources:
        if want_json:
            _emit_json(
                {
                    "sources_by_wing": {},
                    "wing_filter": args.wing,
                    "total_wings": 0,
                    "total_sources": 0,
                }
            )
            sys.exit(1)
        scope = f" in wing={args.wing}" if args.wing else ""
        print(f"\n  No mined source files found{scope}.\n")
        return

    if want_json:
        sources_by_wing: dict = {}
        total_sources = 0
        for wing in sorted(wing_sources):
            sources = sorted(wing_sources[wing].items(), key=lambda x: x[1], reverse=True)
            shown = sources if args.limit == 0 else sources[: args.limit]
            sources_by_wing[wing] = {
                "sources": [{"source_file": src, "drawer_count": count} for src, count in shown],
                "total_sources": len(sources),
                "total_drawers": sum(c for _, c in sources),
                "truncated": bool(args.limit) and len(sources) > args.limit,
            }
            total_sources += len(sources)
        _emit_json(
            {
                "sources_by_wing": sources_by_wing,
                "wing_filter": args.wing,
                "limit": args.limit,
                "total_wings": len(wing_sources),
                "total_sources": total_sources,
            }
        )
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Mined — sources by wing")
    print(f"{'=' * 55}\n")
    for wing in sorted(wing_sources):
        sources = sorted(wing_sources[wing].items(), key=lambda x: x[1], reverse=True)
        print(f"  WING: {wing}  ({len(sources)} sources, {sum(c for _, c in sources)} drawers)")
        shown = sources if args.limit == 0 else sources[: args.limit]
        for src, count in shown:
            print(f"    {count:5}  {src}")
        if args.limit and len(sources) > args.limit:
            print(f"    ... {len(sources) - args.limit} more (use --limit 0 to show all)")
        print()
    print(f"{'=' * 55}\n")


def _count_of(value) -> int:
    """Coerce a wing/room count from ``/stats`` status block into an int.

    The daemon normally returns ``{name: int}``, but a future or
    misbehaving daemon could nest ``{name: {"total": int}}`` or hand back
    a non-numeric value entirely. Accept the int and the ``{"total": ...}``
    shapes; anything else (string, list, None) counts as 0 rather than
    crashing the whole dashboard with an AttributeError.
    """
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        total = value.get("total", 0)
        return total if isinstance(total, int) else 0
    return 0


def _stats_bar(count: int, total: int, width: int = 24) -> str:
    """Render a horizontal bar proportional to ``count`` against ``total``.

    Uses Unicode block-element fills so a row at half the max draws to
    roughly half the bar width. Empty when ``total`` is zero so we never
    divide by zero on a freshly-initialised palace.
    """
    if total <= 0:
        return ""
    ratio = max(0.0, min(1.0, count / total))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


_STATS_VALID_SECTIONS = ("kg", "graph", "status", "all")


def _resolve_stats_format(args) -> str:
    """Pick ``mempalace stats`` output format. ``--format`` wins, then ``--json``."""
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _resolve_stats_sections(args) -> set[str]:
    """Translate ``--section`` into the set of /stats top-level keys to render.

    ``all`` (default) expands to ``{kg, graph, status}``. Anything else
    narrows to a single block. argparse's ``choices=`` guards the value
    before we get here so we don't validate again.
    """
    sec = getattr(args, "section", None) or "all"
    if sec == "all":
        return {"kg", "graph", "status"}
    return {sec}


def _print_stats_dashboard(
    data: dict,
    top: int,
    sections: set[str],
    hide_rels: bool,
    tags_bundle: dict | None = None,
) -> None:
    """Render the /stats envelope as a human-friendly dashboard.

    Mirrors ``_print_daemon_status``'s 55-char rules + two-space indent
    style so ``status`` and ``stats`` look like siblings to a human
    skimming the terminal. ``protocol`` and ``aaak_dialect`` are
    suppressed in table mode — they're text blobs, not analytics. Pass
    them through in ``json`` mode for jq pipelines.
    """
    status = data.get("status") or {}
    total = status.get("total_drawers", 0)
    wings = status.get("wings") or {}

    print(f"\n{'=' * 60}")
    print(f"  MemPalace Stats — {total} drawers")
    print(f"  via palace-daemon @ {_daemon_url()}")
    print(f"{'=' * 60}\n")

    if "status" in sections:
        print("  WINGS")
        print(f"  {'-' * 56}")
        if isinstance(wings, dict) and wings:
            items = sorted(
                ((w, _count_of(c)) for w, c in wings.items()),
                key=lambda kv: kv[1],
                reverse=True,
            )
            max_count = items[0][1] if items else 0
            shown = items[:top] if top else items
            for wing, count in shown:
                bar = _stats_bar(count, max_count)
                print(f"    {wing:<28} {count:>7}  {bar}")
            remaining = len(items) - len(shown)
            if remaining > 0:
                tail = sum(c for _, c in items[len(shown) :])
                print(f"    ... {remaining} more wings ({tail} drawers; --top 0 shows all)")
        else:
            print("    (no wings)")
        print()

        rooms = status.get("rooms") or {}
        print("  ROOMS")
        print(f"  {'-' * 56}")
        if isinstance(rooms, dict) and rooms:
            room_items = sorted(
                ((r, _count_of(c)) for r, c in rooms.items()),
                key=lambda kv: kv[1],
                reverse=True,
            )
            room_max = room_items[0][1] if room_items else 0
            room_shown = room_items[:top] if top else room_items
            for room, count in room_shown:
                bar = _stats_bar(count, room_max)
                print(f"    {room:<28} {count:>7}  {bar}")
            room_remaining = len(room_items) - len(room_shown)
            if room_remaining > 0:
                tail = sum(c for _, c in room_items[len(room_shown) :])
                print(f"    ... {room_remaining} more rooms ({tail} drawers; --top 0 shows all)")
        else:
            print("    (no rooms)")
        print()

    if "kg" in sections:
        kg = data.get("kg") or {}
        print("  KNOWLEDGE GRAPH")
        print(f"  {'-' * 56}")
        if "error" in kg:
            print(f"    (error: {kg.get('error')})")
        else:
            print(f"    entities         : {kg.get('entities', 0):>7}")
            print(f"    triples          : {kg.get('triples', 0):>7}")
            print(f"    current facts    : {kg.get('current_facts', 0):>7}")
            print(f"    expired facts    : {kg.get('expired_facts', 0):>7}")
            rels = kg.get("relationship_types") or []
            if rels and not hide_rels:
                preview = ", ".join(rels[:8])
                suffix = f" (+{len(rels) - 8} more)" if len(rels) > 8 else ""
                print(f"    relations ({len(rels):>2})  : {preview}{suffix}")
            elif rels and hide_rels:
                print(f"    relations        : {len(rels)} types (suppressed)")
        print()

    if "graph" in sections:
        graph = data.get("graph") or {}
        print("  GRAPH")
        print(f"  {'-' * 56}")
        if "error" in graph:
            print(f"    (error: {graph.get('error')})")
        else:
            print(f"    total rooms      : {graph.get('total_rooms', 0):>7}")
            print(
                f"    tunnel rooms     : {graph.get('tunnel_rooms', 0):>7}  "
                "(rooms shared by 2+ wings)"
            )
            print(f"    edges            : {graph.get('total_edges', 0):>7}")
            top_tunnels = graph.get("top_tunnels") or []
            if top_tunnels:
                print("    top tunnels      :")
                for t in top_tunnels[: min(5, top) if top else 5]:
                    wing_list = ", ".join(t.get("wings") or [])
                    print(f"      - {t.get('room', '?'):<22} [{wing_list}]")
        print()

    if tags_bundle is not None:
        tags = tags_bundle or {}
        print("  TAGS")
        print(f"  {'-' * 56}")
        if "_error" in tags:
            print(f"    (unavailable: {tags['_error']})")
        elif "error" in tags:
            print(f"    (error: {tags.get('error')})")
        else:
            items = tags.get("tags") or []
            if not items:
                print("    (no tags)")
            else:
                max_count = items[0].get("count", 0) if items else 0
                shown = items[:top] if top else items
                for entry in shown:
                    tag = entry.get("tag", "?")
                    count = entry.get("count", 0)
                    bar = _stats_bar(count, max_count, width=18)
                    print(f"    {tag:<28} {count:>5}  {bar}")
                remaining = len(items) - len(shown)
                if remaining > 0:
                    print(f"    ... {remaining} more tags (--top 0 shows all)")
        print()

    print(f"{'=' * 60}\n")


def cmd_stats(args):
    """Fast direct-to-daemon palace analytics CLI (#191).

    Wraps ``GET /stats`` on the palace daemon, which returns a unified
    envelope of three blocks: ``kg`` (entities, triples, relationship
    types), ``graph`` (rooms, tunnels, edges), ``status`` (drawer counts,
    wings, rooms, protocol/AAAK text). Replaces the older multi-RPC
    fan-out — one network hit instead of four. ``--section`` narrows the
    table output to a single block; json mode always passes through the
    whole envelope so jq pipelines see the daemon contract unchanged.
    ``--tags`` still triggers an extra ``mempalace_list_tags`` MCP call
    because /stats doesn't include the tag breakdown (tag counts can be
    100K+ entries and don't belong in the fast-path summary). Daemon
    unreachable → exit 1 (matches sibling cmd_list/cmd_graph/cmd_cypher);
    inner-error envelope → exit 2.
    """
    fmt = _resolve_stats_format(args)
    want_json = fmt == "json"
    want_tags = getattr(args, "tags", False)
    hide_rels = getattr(args, "no_relationship_types", False)
    top = max(0, getattr(args, "top", 10) or 0)
    sections = _resolve_stats_sections(args)

    if not _daemon_url():
        msg = (
            "stats requires the palace-daemon. Set PALACE_DAEMON_URL "
            "(or daemon_url in ~/.mempalace/config.json) and retry."
        )
        if want_json:
            _emit_json({"error": "daemon_required", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    try:
        data = _call_daemon_rest("/stats")
    except DaemonRequestError as _req_err:
        # A refusal is not an outage (#499).
        _exit_daemon_request_error(_req_err, want_json=want_json)
    except DaemonError as e:
        _fail_daemon(e, want_json)

    if data is None:
        _fail_daemon(None, want_json, route="/stats")

    if "error" in data and not (data.get("kg") or data.get("graph") or data.get("status")):
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  {data['error']}", file=sys.stderr)
        sys.exit(2)

    tags_bundle: dict | None = None
    if want_tags:
        try:
            tags_bundle = _call_daemon_tool("mempalace_list_tags", {"min_count": 1})
        except DaemonError as e:
            tags_bundle = {"_error": str(e)}

    if want_json:
        # Pass-through preserves the daemon envelope contract — operators
        # piping to jq get every field including protocol/aaak_dialect.
        # --no-relationship-types replaces the list with a count to keep
        # script consumers from drowning in a 1000-entry array.
        out = {
            "kg": dict(data.get("kg") or {}),
            "graph": data.get("graph") or {},
            "status": data.get("status") or {},
        }
        if hide_rels and "relationship_types" in out["kg"]:
            rels = out["kg"].pop("relationship_types") or []
            out["kg"]["relationship_types_count"] = len(rels)
        if tags_bundle is not None:
            out["tags"] = tags_bundle
        _emit_json(out)
        return

    _print_stats_dashboard(
        data,
        top=top,
        sections=sections,
        hide_rels=hide_rels,
        tags_bundle=tags_bundle,
    )


# ── mempalace tags ────────────────────────────────────────────────────
#
# Slice of #191: surface the existing ``mempalace_list_tags`` MCP tool
# as a daemon-strict CLI command. The tool is also reachable through
# ``stats --tags``, but folding it into ``stats`` only renders alongside
# the wings/rooms/kg blocks; ``tags`` as a standalone subcommand keeps
# the output focused (and accepts ``--wing`` / ``--room`` filters that
# ``stats`` doesn't expose).


def _resolve_tags_format(args) -> str:
    """``--format`` wins, then ``--json`` shorthand, default ``table``.

    Matches the sibling fast-path commands.
    """
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _print_tags_table(bundle: dict, top: int) -> None:
    """Aligned tag/count rows with a visual gauge — matches the stats --tags block."""
    items = bundle.get("tags") or []
    if not items:
        print("\n  (no tags match the requested filter)\n")
        return

    max_count = items[0].get("count", 0) if items else 0
    shown = items[:top] if top else items
    filters = bundle.get("filters") or {}
    wing = filters.get("wing")
    room = filters.get("room")
    min_count = filters.get("min_count", 1)
    scope = []
    if wing:
        scope.append(f"wing={wing}")
    if room:
        scope.append(f"room={room}")
    if min_count and min_count > 1:
        scope.append(f"min_count={min_count}")
    scope_label = (" — " + ", ".join(scope)) if scope else ""
    total = bundle.get("total_unique_tags", len(items))

    print(f"\n  TAGS — {total} unique{scope_label}")
    print(f"  {'-' * 56}")
    for entry in shown:
        tag = entry.get("tag", "?")
        count = entry.get("count", 0)
        bar = _stats_bar(count, max_count, width=18)
        print(f"    {tag:<28} {count:>5}  {bar}")
    remaining = len(items) - len(shown)
    if remaining > 0:
        print(f"    ... {remaining} more tags (--top 0 shows all)")
    print()


def cmd_tags(args):
    """Fast direct-to-daemon tag inventory (slice of #191).

    Calls the daemon's ``mempalace_list_tags`` MCP tool — already a
    first-class read path on the daemon, just not previously exposed
    as a CLI verb. Supports ``--wing`` / ``--room`` scoping and a
    ``--min-count`` floor; output formats match the sibling commands
    (``--format=table`` default, ``--json``/``--format=json`` pass-through).

    Daemon unreachable → exit 1; inner-error envelope → exit 2.
    """
    fmt = _resolve_tags_format(args)
    want_json = fmt == "json"

    if not _daemon_url():
        msg = (
            "tags requires the palace-daemon. Set PALACE_DAEMON_URL "
            "(or daemon_url in ~/.mempalace/config.json) and retry."
        )
        if want_json:
            _emit_json({"error": "daemon_required", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    min_count = max(1, int(getattr(args, "min_count", 1) or 1))
    arguments: dict = {"min_count": min_count}
    wing = getattr(args, "wing", None)
    room = getattr(args, "room", None)
    if wing:
        arguments["wing"] = wing
    if room:
        arguments["room"] = room

    try:
        data = _call_daemon_tool("mempalace_list_tags", arguments)
    except DaemonError as e:
        _fail_daemon(e, want_json)

    if isinstance(data, dict) and "error" in data and not data.get("tags"):
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  {data['error']}", file=sys.stderr)
        sys.exit(2)

    if want_json:
        # Pass through the daemon envelope unchanged so jq pipelines see
        # the same shape mempalace_list_tags emits everywhere else.
        _emit_json(data)
        return

    top = max(0, int(getattr(args, "top", 20) or 0))
    _print_tags_table(data, top=top)


# ── mempalace overlap ─────────────────────────────────────────────────
#
# Slice of #191: cross-wing entity finder via a single ``/cypher`` POST.
# Demonstrates KG usage at the CLI and answers "what entities does wing
# A share with wing B?" — the natural follow-on question to ``graph``'s
# tunnel summary, which counts shared rooms but not shared entities.
#
# Inline-substitutes the two wing names as Cypher literals (the daemon's
# /cypher endpoint doesn't accept a separate ``params`` field — see
# _cypher_literal in knowledge_graph_age.py).


def _resolve_overlap_format(args) -> str:
    """``--format`` wins, then ``--json`` shorthand, default ``table``."""
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


_OVERLAP_DEFAULT_LIMIT = 50
_OVERLAP_MAX_LIMIT = 5000


def _build_overlap_cypher(wing_a: str, wing_b: str, limit: int) -> str:
    """Return the inlined Cypher source for the overlap query.

    Two MATCH clauses bound to the same Entity node — one per wing —
    forces AGE to intersect rather than union. ``count(DISTINCT d)``
    on each side keeps the per-wing tally from double-counting drawers
    that mention the same entity twice.
    """
    from ..config import sanitize_kg_value
    from ..knowledge_graph_age import _cypher_literal

    a_lit = _cypher_literal(sanitize_kg_value(wing_a, "wing_a"))
    b_lit = _cypher_literal(sanitize_kg_value(wing_b, "wing_b"))
    return (
        f"MATCH (wa:Wing {{name: {a_lit}}})-[:CONTAINS]->(:Room)"
        "-[:CONTAINS]->(da:Drawer)-[:RELATION]->(e:Entity) "
        f"MATCH (wb:Wing {{name: {b_lit}}})-[:CONTAINS]->(:Room)"
        "-[:CONTAINS]->(db:Drawer)-[:RELATION]->(e) "
        "RETURN e.name AS entity, count(DISTINCT da) AS a_drawers, "
        "count(DISTINCT db) AS b_drawers "
        "ORDER BY a_drawers + b_drawers DESC "
        f"LIMIT {int(limit)}"
    )


def _hallways_maintenance(args) -> bool:
    """Run ``hallways --rebuild`` / ``--prune-spellings`` if asked; True when one ran (upstream #2576).

    Both write the palace's hallway file locally under the repair lock, so they stay on the plural verb rather
    than the daemon-routed ``hallway list`` the read path delegates to.
    """
    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    config = MempalaceConfig(palace_path=palace_path)
    if getattr(args, "rebuild", False):
        from ..hallways import compute_hallways_for_wing
        from ..palace import get_collection
        from ..palace_graph import sqlite_grouped_counts_reader

        col = get_collection(palace_path, create=False, read_only=True)
        wing_filter = getattr(args, "wing", None)
        if wing_filter:
            wings = [wing_filter]
        else:
            reader = sqlite_grouped_counts_reader(config)
            rows = reader(palace_path, config.collection_name) if reader else None
            if rows is None:
                print("  --rebuild without --wing needs a sqlite-readable palace.")
                sys.exit(1)
            wings = sorted({str(r[1]) for r in rows if r[1]})
        # Scan and replace under the palace writer lock, like the other repair
        # commands: the hallway-file lock alone lets a mine add drawers and
        # save a newer snapshot between this scan and its save, which would
        # then be overwritten with stale results.
        total = 0
        with _repair_lock(palace_path):
            for w in wings:
                created = compute_hallways_for_wing(w, col=col, config=config)
                total += len(created)
                print(f"  {w:<36} {len(created):>7} hallways")
        print(f"  Rebuilt {total} hallways across {len(wings)} wing(s).")
        return True
    if getattr(args, "prune_spellings", False):
        from ..hallways import prune_spelling_hallways

        apply = getattr(args, "yes", False)
        report = prune_spelling_hallways(config=config, apply=apply)
        doomed = report["self_links"] + report["duplicates"]
        print(
            f"  {doomed} of {report['total']} hallways are spelling artifacts: "
            f"{report['self_links']} self-links, {report['duplicates']} duplicate spellings."
        )
        for wing, n in list(report["by_wing"].items())[:8]:
            print(f"    {wing}: {n}")
        for label in report["sample"][:5]:
            print(f"    e.g. {label}")
        if not doomed:
            return True
        if apply:
            print(f"  Removed {report['removed']}.")
        else:
            print("  Dry run. Re-run with --yes to remove them.")
        return True
    return False


def cmd_hallways(args):
    """Deprecated alias for ``mempalace hallway list`` (#407).

    The plural verb predates the daemon and carried its own fetch path:
    always local whatever ``PALACE_DAEMON_URL`` said, ``args.wing``
    passed raw to ``list_hallways`` instead of through the sanitizing
    MCP tool, and a co-occurrence sort with no tiebreak, so equal-count
    rows changed places between runs. #438 closed the ``--json`` hole in
    isolation; delegating the whole body closes the remaining rows of
    #407's table with it and leaves one hallway-listing path instead of
    two divergent ones.

    Kept working for scripts that call it — removal is a later release.
    The notice goes to stderr so it cannot corrupt a ``--json`` pipe,
    and is suppressed entirely on the ``--json`` surface, which carries
    a ``deprecated`` marker inside the payload instead (#438's choice,
    kept: nothing then has to parse stderr to notice).
    """
    if _hallways_maintenance(args):
        return
    if _resolve_read_format(args) != "json":
        print(
            "mempalace: `hallways` is deprecated — use `mempalace hallway list` "
            "(daemon-routed, --json, pagination). See techempower-org/mempalace#407.",
            file=sys.stderr,
        )
    cmd_hallway_list(args, _deprecated_alias=True)


def _print_overlap_table(rows: list[dict], wing_a: str, wing_b: str) -> None:
    """Aligned columns: entity | A drawers | B drawers | total."""
    if not rows:
        print(f"\n  No entity overlap between '{wing_a}' and '{wing_b}'.\n")
        return

    name_w = max(len("entity"), max(len(str(r.get("entity") or "")) for r in rows))
    name_w = min(name_w, 48)
    print(f"\n  OVERLAP — {wing_a}  ↔  {wing_b}  ({len(rows)} entities)")
    print(f"  {'-' * (name_w + 26)}")
    print(f"    {'entity':<{name_w}}  {'A':>6}  {'B':>6}  {'total':>6}")
    for row in rows:
        ent = str(row.get("entity") or "")
        if len(ent) > name_w:
            ent = ent[: name_w - 1] + "…"
        a = int(row.get("a_drawers") or 0)
        b = int(row.get("b_drawers") or 0)
        print(f"    {ent:<{name_w}}  {a:>6}  {b:>6}  {a + b:>6}")
    print()


def cmd_overlap(args):
    """Cross-wing entity overlap via a single read-only Cypher hop (slice of #191).

    Answers "what entities appear in both wing A and wing B?". Uses the
    daemon's ``POST /cypher`` (read-only by SQLSTATE 25006); the two
    wing names are sanitized + inlined as Cypher literals because the
    endpoint accepts only ``{cypher, graph}`` — no parameters.

    Daemon unreachable / 401/404/403 → exit 1; 403 read-only write
    attempt cannot fire here (only MATCH/RETURN); inner-error envelope
    or sanitization failure → exit 2.
    """
    fmt = _resolve_overlap_format(args)
    want_json = fmt == "json"

    wing_a = getattr(args, "wing_a", None)
    wing_b = getattr(args, "wing_b", None)
    if not wing_a or not wing_b:
        msg = "overlap requires two positional wing names (WING_A WING_B)"
        if want_json:
            _emit_json({"error": msg, "source": "cli"})
        else:
            print(f"error: {msg}", file=sys.stderr)
        sys.exit(2)

    if wing_a == wing_b:
        msg = "overlap requires two DIFFERENT wing names; got the same value twice"
        if want_json:
            _emit_json({"error": msg, "source": "cli"})
        else:
            print(f"error: {msg}", file=sys.stderr)
        sys.exit(2)

    if not _daemon_url():
        msg = (
            "overlap requires the palace-daemon. Set PALACE_DAEMON_URL "
            "(or daemon_url in ~/.mempalace/config.json) and retry."
        )
        if want_json:
            _emit_json({"error": "daemon_required", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    limit_raw = int(getattr(args, "limit", _OVERLAP_DEFAULT_LIMIT) or _OVERLAP_DEFAULT_LIMIT)
    limit = max(1, min(limit_raw, _OVERLAP_MAX_LIMIT))

    try:
        cypher = _build_overlap_cypher(wing_a, wing_b, limit)
    except ValueError as e:
        # sanitize_kg_value rejection — empty/over-length/null-bytes.
        if want_json:
            _emit_json({"error": str(e), "source": "cli"})
        else:
            print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    graph = getattr(args, "graph", None) or _CYPHER_DEFAULT_GRAPH
    body = {"cypher": cypher, "graph": str(graph)}

    try:
        data, status = _post_cypher(body)
    except DaemonError as e:
        _fail_daemon(e, want_json)

    if status is not None:
        # Any non-2xx from /cypher — older daemon (404), auth (401/403),
        # or 503 if not on postgres backend. Same shape as cmd_cypher's
        # fallthrough — exit 1, scripts treat all daemon-side failures
        # uniformly.
        _fail_daemon(
            f"daemon /cypher returned {status}",
            want_json,
            status=status,
        )

    if data is not None and "error" in data and "rows" not in data and "data" not in data:
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  {data['error']}", file=sys.stderr)
        sys.exit(2)

    rows = _extract_cypher_rows(data or {})

    if want_json:
        out = {
            "rows": rows,
            "count": len(rows),
            "wing_a": wing_a,
            "wing_b": wing_b,
            "graph": graph,
            "limit": limit,
        }
        if isinstance(data, dict):
            for k, v in data.items():
                if k not in ("rows", "data", "count", "graph", "wing_a", "wing_b", "limit"):
                    out[k] = v
        _emit_json(out)
        return

    _print_overlap_table(rows, wing_a=wing_a, wing_b=wing_b)


# ── mempalace why ─────────────────────────────────────────────────────
#
# Slice of #191: explain a drawer — show *why* it would surface.
# Composes three read-only daemon calls (no searcher.py changes):
#   1. mempalace_get_drawer        → location + tags + content snippet
#   2. /cypher (Drawer→Entity)     → top entities the drawer mentions
#   3. mempalace_search            → drawers that surface alongside it
# A debugging lens for "why does this drawer match X?" — answer is the
# combination of where it lives, what entities it links to, and what
# semantically-similar siblings exist.


def _resolve_why_format(args) -> str:
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


_WHY_DEFAULT_NEIGHBORS = 5
_WHY_MAX_NEIGHBORS = 50
_WHY_DEFAULT_ENTITIES = 10
_WHY_MAX_ENTITIES = 100
_WHY_SNIPPET_CHARS = 240


def _why_query_snippet(content: str) -> str:
    """First non-blank paragraph (or first WHY_SNIPPET_CHARS chars), trimmed.

    Used as the self-query for the neighbors hop — pick a representative
    chunk of the drawer's own content so the vector search is anchored
    on the drawer's actual semantics rather than its tag/wing metadata.
    """
    if not content:
        return ""
    for para in content.split("\n\n"):
        stripped = para.strip()
        if stripped:
            return stripped[:_WHY_SNIPPET_CHARS]
    return content.strip()[:_WHY_SNIPPET_CHARS]


def _build_why_entities_cypher(drawer_id: str, limit: int) -> str:
    """Return the inlined Cypher for the drawer→entities MENTIONS hop."""
    from ..config import sanitize_kg_value
    from ..knowledge_graph_age import _cypher_literal

    did = _cypher_literal(sanitize_kg_value(drawer_id, "drawer_id"))
    return (
        f"MATCH (d:Drawer {{id: {did}}})-[m:MENTIONS]->(e:Entity) "
        "RETURN e.name AS entity, m.count AS count, m.etype AS etype "
        "ORDER BY m.count DESC "
        f"LIMIT {int(limit)}"
    )


def _why_extract_neighbors(search_payload: dict, exclude_id: str) -> list[dict]:
    """Pull the neighbor list out of mempalace_search response, dropping the
    drawer itself if it appears (it will — self-similarity is 1.0).
    """
    results = (search_payload.get("results") if isinstance(search_payload, dict) else None) or []
    neighbors = []
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("drawer_id")
        if rid and rid == exclude_id:
            continue
        neighbors.append(
            {
                "id": rid,
                "wing": r.get("wing") or "",
                "room": r.get("room") or "",
                "distance": r.get("distance"),
                "matched_via": r.get("matched_via"),
                "snippet": (r.get("content") or "")[:120],
            }
        )
    return neighbors


def _print_why_report(
    drawer: dict,
    entities: list[dict],
    neighbors: list[dict],
) -> None:
    """Three-block table: WHERE / MENTIONS / NEIGHBORS."""
    did = drawer.get("drawer_id", "?")
    wing = drawer.get("wing") or "(no wing)"
    room = drawer.get("room") or "(no room)"
    tags = drawer.get("tags") or []
    content = drawer.get("content") or ""
    snippet = _why_query_snippet(content)

    print(f"\n  WHY — drawer {did}")
    print(f"  {'-' * 56}")
    print(f"    location:  {wing} / {room}")
    if tags:
        print(f"    tags:      {', '.join(tags)}")
    else:
        print("    tags:      (none)")
    if snippet:
        first_line = snippet.replace("\n", " ")
        if len(first_line) > 76:
            first_line = first_line[:75] + "…"
        print(f"    snippet:   {first_line}")

    print(f"\n  MENTIONS — {len(entities)} entities (top by mention count)")
    if entities:
        name_w = max(6, max(len(str(e.get("entity") or "")) for e in entities))
        name_w = min(name_w, 40)
        for ent in entities:
            name = str(ent.get("entity") or "")
            if len(name) > name_w:
                name = name[: name_w - 1] + "…"
            cnt = int(ent.get("count") or 0)
            etype = ent.get("etype") or ""
            etype_str = f"  ({etype})" if etype and etype != "unknown" else ""
            print(f"    {name:<{name_w}}  ×{cnt}{etype_str}")
    else:
        print("    (no MENTIONS edges — drawer not in KG or KG empty)")

    print(f"\n  NEIGHBORS — {len(neighbors)} semantically similar drawers")
    if neighbors:
        id_w = max(8, max(len(str(n.get("id") or "")) for n in neighbors))
        id_w = min(id_w, 36)
        for n in neighbors:
            nid = str(n.get("id") or "")
            if len(nid) > id_w:
                nid = nid[: id_w - 1] + "…"
            loc = f"{n.get('wing') or '?'}/{n.get('room') or '?'}"
            dist = n.get("distance")
            dist_str = f"d={dist:.3f}" if isinstance(dist, (int, float)) else "d=?"
            snip = (n.get("snippet") or "").replace("\n", " ")
            if len(snip) > 40:
                snip = snip[:39] + "…"
            print(f"    {nid:<{id_w}}  {loc:<24}  {dist_str:<10}  {snip}")
    else:
        print("    (no neighbors returned — corpus may be small or daemon vector-disabled)")
    print()


def cmd_why(args):
    """Explain a drawer — surface the signals that make it findable (slice of #191).

    Composes three read-only daemon calls into one report:
      * ``mempalace_get_drawer`` for wing/room/tags + content
      * read-only Cypher for the drawer's :MENTIONS-Entity edges (top N)
      * ``mempalace_search`` on the drawer's own first paragraph for the
        nearest semantic neighbors (drawer itself filtered out)

    Daemon unreachable → exit 1; missing drawer or inner-error envelope
    → exit 2. No ``searcher.py`` writes; pure orchestration over existing
    read paths.
    """
    fmt = _resolve_why_format(args)
    want_json = fmt == "json"

    drawer_id = getattr(args, "drawer_id", None)
    if not drawer_id or not str(drawer_id).strip():
        msg = "why requires a drawer ID (positional argument)"
        if want_json:
            _emit_json({"error": msg, "source": "cli"})
        else:
            print(f"error: {msg}", file=sys.stderr)
        sys.exit(2)
    drawer_id = str(drawer_id).strip()

    if not _daemon_url():
        msg = (
            "why requires the palace-daemon. Set PALACE_DAEMON_URL "
            "(or daemon_url in ~/.mempalace/config.json) and retry."
        )
        if want_json:
            _emit_json({"error": "daemon_required", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    neighbors_limit = max(
        1,
        min(
            int(getattr(args, "neighbors", _WHY_DEFAULT_NEIGHBORS) or _WHY_DEFAULT_NEIGHBORS),
            _WHY_MAX_NEIGHBORS,
        ),
    )
    entities_limit = max(
        1,
        min(
            int(getattr(args, "entities", _WHY_DEFAULT_ENTITIES) or _WHY_DEFAULT_ENTITIES),
            _WHY_MAX_ENTITIES,
        ),
    )

    try:
        drawer = _call_daemon_tool("mempalace_get_drawer", {"drawer_id": drawer_id})
    except DaemonError as e:
        _fail_daemon(e, want_json)

    if isinstance(drawer, dict) and "error" in drawer and "drawer_id" not in drawer:
        if want_json:
            _emit_json(drawer)
        else:
            print(f"\n  {drawer['error']}", file=sys.stderr)
        sys.exit(2)

    # Entities — best-effort. AGE may not be configured on every daemon
    # (chroma-only backends), so a 404/503 here downgrades to "no entities"
    # rather than failing the whole report.
    entities: list[dict] = []
    graph = getattr(args, "graph", None) or _CYPHER_DEFAULT_GRAPH
    try:
        cypher = _build_why_entities_cypher(drawer_id, entities_limit)
        ent_data, ent_status = _post_cypher({"cypher": cypher, "graph": str(graph)})
        if ent_status is None and ent_data is not None:
            entities = _extract_cypher_rows(ent_data)
    except DaemonError:
        # Daemon went away between calls — surface on the search hop.
        pass
    except ValueError:
        # sanitize_kg_value rejection — already validated drawer_id is
        # non-empty above; this should be unreachable but defensively
        # leaves the entities block empty.
        pass

    # Neighbors — use the drawer's first non-blank paragraph as the query.
    neighbors: list[dict] = []
    snippet = _why_query_snippet(drawer.get("content") or "")
    if snippet:
        # +1 because the drawer itself will land in its own self-search.
        search_args = {"query": snippet, "limit": neighbors_limit + 1}
        try:
            search_payload = _call_daemon_tool("mempalace_search", search_args)
        except DaemonError as e:
            _fail_daemon(e, want_json)
        if (
            isinstance(search_payload, dict)
            and "error" in search_payload
            and "results" not in search_payload
        ):
            if want_json:
                _emit_json(search_payload)
            else:
                print(f"\n  {search_payload['error']}", file=sys.stderr)
            sys.exit(2)
        neighbors = _why_extract_neighbors(search_payload or {}, exclude_id=drawer_id)[
            :neighbors_limit
        ]

    if want_json:
        out = {
            "drawer_id": drawer_id,
            "wing": drawer.get("wing") or "",
            "room": drawer.get("room") or "",
            "tags": drawer.get("tags") or [],
            "snippet": snippet,
            "entities": entities,
            "neighbors": neighbors,
        }
        _emit_json(out)
        return

    _print_why_report(drawer, entities, neighbors)


# ── mempalace tunnels ─────────────────────────────────────────────────
#
# Slice of #191: list cross-wing tunnels via a single
# ``mempalace_list_tunnels`` MCP call. Tunnels are first-class palace
# structure (explicit links wired in ``~/.mempalace/tunnels.json``, plus
# passive overlap inferred from rooms appearing in 2+ wings), but the
# CLI never exposed a way to inventory them. Mirrors ``cmd_tags`` shape.


def _resolve_tunnels_format(args) -> str:
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _tunnels_kind(entry: dict) -> str:
    """Tunnel kind label — ``mempalace_list_tunnels`` tags entries with
    ``kind: 'explicit'|'passive'`` (issue #75). Default 'explicit' for the
    case where the daemon predates the explicit/passive merge.
    """
    return str(entry.get("kind") or "explicit")


def _print_tunnels_table(bundle, scope_wing: str | None) -> None:
    """Aligned tunnel rows — wing_a ↔ wing_b (room) [kind]."""
    # ``mempalace_list_tunnels`` returns a bare list (not a dict envelope)
    # — issue #75 documents the shape. Be defensive against future
    # wrapping in case the daemon ever shifts to ``{"tunnels": [...]}``.
    if isinstance(bundle, dict):
        items = bundle.get("tunnels") or bundle.get("data") or []
    else:
        items = bundle or []

    scope_label = f" — wing={scope_wing}" if scope_wing else ""
    if not items:
        print(f"\n  (no tunnels{scope_label})\n")
        return

    a_w = max(len("wing_a"), max(len(str(t.get("source_wing") or "")) for t in items))
    b_w = max(len("wing_b"), max(len(str(t.get("target_wing") or "")) for t in items))
    room_w = max(
        len("room"), max(len(str(t.get("source_room") or t.get("room") or "")) for t in items)
    )
    a_w = min(a_w, 24)
    b_w = min(b_w, 24)
    room_w = min(room_w, 24)

    print(f"\n  TUNNELS — {len(items)}{scope_label}")
    print(f"  {'-' * (a_w + b_w + room_w + 24)}")
    print(f"    {'wing_a':<{a_w}}  {'wing_b':<{b_w}}  {'room':<{room_w}}  {'kind':<9}")
    for t in items:
        a = str(t.get("source_wing") or "")
        b = str(t.get("target_wing") or "")
        room = str(t.get("source_room") or t.get("room") or "")
        kind = _tunnels_kind(t)
        if len(a) > a_w:
            a = a[: a_w - 1] + "…"
        if len(b) > b_w:
            b = b[: b_w - 1] + "…"
        if len(room) > room_w:
            room = room[: room_w - 1] + "…"
        print(f"    {a:<{a_w}}  {b:<{b_w}}  {room:<{room_w}}  {kind:<9}")
    print()


def _rebuild_derived_graph(wing: str, palace_path=None) -> None:
    """Recompute one wing's derived graph: topic tunnels, hallways, entity tunnels.

    Delegates the three steps to ``miner.recompute_derived_graph``, which is
    the single definition of what "the derived graph" means — the post-mine
    block calls the same function, so a fourth step cannot land in one and be
    forgotten in the other.

    Takes the palace write lock. Hallways are a whole-file load / modify /
    save that carries other wings forward from its OWN load, so a rebuild of
    wing A racing a drain mine of wing B silently drops one side's new
    records — whichever ``os.replace`` lands second wins, with no error.
    """
    from .. import miner as _miner
    from ..config import MempalaceConfig, normalize_wing_name
    from ..palace import MineAlreadyRunning, mine_palace_lock

    # The miner stores a normalized wing (``My-Project`` -> ``my_project``), so
    # a raw slug here matches no drawers and reports a confident +0.
    wing = normalize_wing_name(wing)

    config = MempalaceConfig(palace_path=palace_path) if palace_path else MempalaceConfig()

    print(f"\n  Rebuilding derived graph for wing '{wing}'")
    collection = None
    try:
        # create=False: the default would MATERIALIZE a palace at a typo'd
        # --palace and then report +0/+0/+0 success against the empty thing it
        # just made. A rebuild must never create a palace.
        collection = _miner.get_collection(config.palace_path, create=False)
    except Exception as e:
        # Hallways need the collection; the two tunnel steps do not, so this
        # degrades to a partial rebuild rather than aborting.
        print(f"  WARNING: could not open the palace collection — {e}", file=sys.stderr)

    try:
        with mine_palace_lock(config.palace_path):
            result = _miner.recompute_derived_graph(wing, collection=collection, config=config)
    except MineAlreadyRunning as exc:
        # Another writer holds the palace. Surfacing this beats silently
        # racing it and dropping half the hallways.
        print(f"mempalace: {exc}", file=sys.stderr)
        sys.exit(1)

    for key, noun, label in (
        ("topic_tunnels", "topic tunnel", "Topic tunnels"),
        ("hallways", "hallway", "Hallways"),
        ("entity_tunnels", "entity tunnel", "Entity tunnels"),
    ):
        err = result["errors"].get(key)
        if err is not None:
            print(f"  WARNING: {noun} computation skipped — {err}", file=sys.stderr)
        else:
            print(f"  {label + ':':16}+{result[key]}")


def cmd_tunnels(args):
    """List cross-wing tunnels (slice of #191).

    Wraps the daemon's ``mempalace_list_tunnels`` MCP tool. Default is
    explicit-only (the agent-wired tunnels at ``~/.mempalace/tunnels.json``);
    pass ``--passive`` to also include passive tunnels (rooms appearing
    in 2+ wings, inferred from the palace graph — see issue #75).

    Daemon unreachable → exit 1; inner-error envelope → exit 2.
    """
    if getattr(args, "tunnels_action", None) in {"propose", "prune"}:
        cmd_tunnels_maintenance(args)
        return
    if getattr(args, "rebuild", False):
        # Local operation — deliberately ahead of the daemon check below,
        # which only the listing path needs.
        if _daemon_strict() and not getattr(args, "palace", None):
            # The palace lives on another host. Rebuilding the LOCAL one
            # prints +0/+0/+0 and exits 0, so an operator who ran this
            # BECAUSE the graph was stale is now told it is fresh. A
            # confident wrong answer is worse than a refusal.
            print(
                "mempalace: tunnels --rebuild is a local operation and this client is "
                "daemon-strict, so the palace it would rebuild is not the one you are "
                f"reading from ({_daemon_url() or 'the palace daemon'}).",
                file=sys.stderr,
            )
            print(
                "mempalace: run it on the palace host, or pass --palace <dir> to rebuild "
                "a local palace instead.",
                file=sys.stderr,
            )
            sys.exit(2)
        if getattr(args, "all", False):
            print(
                "mempalace: tunnels --rebuild --all is not available. Rebuilding every "
                "wing pays compute_hallways_for_wing's full load-and-rewrite of "
                "hallways.json per wing — ~44s per wing at its current 1.2 GB, ~37 min "
                "across 50 wings — which recreates the problem this verb exists to fix. "
                "Pass --wing <slug>, or loop in the shell so the cost stays visible. "
                "Tracked on #442 (hallways to a real store).",
                file=sys.stderr,
            )
            sys.exit(2)
        wing = getattr(args, "wing", None)
        if not wing:
            print(
                "mempalace: tunnels --rebuild requires --wing <slug>. There is no "
                "--all: rebuilding every wing pays compute_hallways_for_wing's full "
                "load-and-rewrite of hallways.json per wing (~44s per wing at the "
                "current 1.2 GB, ~37 min across 50 wings), which would recreate the "
                "problem this verb exists to fix. See #442.",
                file=sys.stderr,
            )
            sys.exit(2)
        _rebuild_derived_graph(wing, getattr(args, "palace", None))
        return

    fmt = _resolve_tunnels_format(args)
    want_json = fmt == "json"

    if not _daemon_url():
        msg = (
            "tunnels requires the palace-daemon. Set PALACE_DAEMON_URL "
            "(or daemon_url in ~/.mempalace/config.json) and retry."
        )
        if want_json:
            _emit_json({"error": "daemon_required", "hint": msg})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)

    arguments: dict = {"include_passive": bool(getattr(args, "passive", False))}
    wing = getattr(args, "wing", None)
    if wing:
        arguments["wing"] = wing

    try:
        data = _call_daemon_tool("mempalace_list_tunnels", arguments)
    except DaemonError as e:
        _fail_daemon(e, want_json)

    if isinstance(data, dict) and "error" in data and not (data.get("tunnels") or data.get("data")):
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  {data['error']}", file=sys.stderr)
        sys.exit(2)

    if want_json:
        _emit_json(data)
        return

    _print_tunnels_table(data, scope_wing=wing)


# ── mempalace drawer / mempalace duplicate ────────────────────────────
#
# Single-drawer CRUD (#355) and duplicate detection (#363) — the CLI face
# of five drawer-family MCP tools. `list` browses by wing/room, `purge`
# deletes in bulk and `move` relocates, but until now there was no way to
# get / add / delete / update ONE drawer by ID from the shell.
#
# Routing follows `wake-up` (#285): daemon-strict and no ``--palace``
# sends the call to the daemon's ``/mcp`` endpoint so the daemon stays the
# single writer for the canonical palace; otherwise the same tool handler
# runs in-process against the local palace. No silent fallback between the
# two — that split-brain is what daemon-strict exists to prevent.
#
# Three deliberate departures from the issues' proposed shapes:
#   * ``drawer update`` exposes --wing / --room / --tag but never
#     --content. ``mempalace_update_drawer`` accepts new content; the
#     fork's verbatim-always principle forbids the human CLI from
#     rewriting stored drawer text (same reason `move` has no --content).
#     #355's own proposal only shows tag/wing/room, so the issue and the
#     principle agree. Filing new text is `drawer add`.
#   * ``duplicate`` has no ``scan`` action. #363 sketches one but its
#     "MCP tools covered" section lists only ``mempalace_check_duplicate``
#     — no tool performs a corpus-wide sweep, and inventing one
#     client-side would be a new algorithm, not a CLI binding.
#   * ``duplicate check`` has no ``--wing``. The tool schema takes only
#     content + threshold, and post-filtering its top-5 *palace-wide*
#     candidates by wing would usually render as "not a duplicate" — a
#     false negative, exactly what the tool's own vector_disabled branch
#     refuses to emit.

_DRAWER_LOCAL_HANDLERS = {
    "mempalace_get_drawer": "tool_get_drawer",
    "mempalace_add_drawer": "tool_add_drawer",
    "mempalace_delete_drawer": "tool_delete_drawer",
    "mempalace_update_drawer": "tool_update_drawer",
    "mempalace_check_duplicate": "tool_check_duplicate",
}

_DUPLICATE_DEFAULT_THRESHOLD = 0.9


def _unit_float(value: str) -> float:
    """argparse type for a 0-1 similarity threshold.

    A bare ``type=float`` would accept ``--threshold 90`` and silently
    make every comparison a non-duplicate.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"expected a number between 0 and 1, got {value!r}"
        ) from None
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError(f"--threshold must be between 0 and 1 (got {number})")
    return number


def _add_drawer_format_flag(parser) -> None:
    """Attach the shared ``--format {table,json}`` flag to a leaf parser."""
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help=(
            "Output format: table (default, human summary), "
            "json (tool pass-through; same as --json)"
        ),
    )


def _add_drawer_output_flags(parser) -> None:
    """Attach ``--json`` / ``--quiet`` to a nested leaf parser.

    The bulk propagation loop in :func:`main` deliberately skips parsers
    that own their own subparsers, so two-level commands have to register
    these themselves (cf. ``logstream`` / ``artifact``).
    """
    parser.add_argument(
        "--json",
        "-j",
        dest="json",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--quiet",
        "-q",
        dest="quiet",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )


def _resolve_drawer_format(args) -> str:
    """Pick drawer/duplicate output format. ``--format`` wins, then ``--json``."""
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _import_mcp_server():
    """Import ``mempalace.mcp_server`` without losing the CLI's stdout.

    **The upstream mechanism.** ``mempalace/mcp_server.py`` redirects
    stdout → stderr at *module scope* — the statements run during import,
    before any function is called — at both the Python level
    (``sys.stdout = sys.stderr``) and the file-descriptor level
    (``os.dup2(2, 1)``). That is deliberate and correct for its own
    purpose: chromadb/onnxruntime print banners, sometimes from C, and
    they would corrupt Claude Desktop's JSON parser (#225). The module
    undoes it in ``_restore_stdout()``, called from its own ``main()``
    before entering the protocol loop — a path a CLI process never
    reaches. So a bare ``from . import mcp_server`` inside a CLI command
    silently rewires every later ``print`` to stderr, and a ``--json``
    pipeline reads an empty document.

    **Why ``sys.stdout is sys.stderr`` is the right probe.** It tests for
    the damage rather than for the event that caused it, which buys three
    properties at once. It is *idempotent* — once repaired the condition
    is false, so calling this helper on every command is free and a
    double call cannot double-restore. It is *order-independent* —
    comparing against the stream saved just before the import would see
    no change when some earlier command already imported the module, and
    would then skip a repair that is still needed, because the damage is
    identical regardless of who triggered it. And it is *narrow* — it
    fires only on the exact aliasing the redirect creates, never on a
    legitimately-replaced stdout (a pytest capture object, a pipe).

    **Repair order, and why the caller's stream wins.**
    ``mcp_server._restore_stdout()`` runs first because it is the only
    thing that reverses the fd-level ``dup2`` — a bare reassignment would
    leave fd 1 pointing at stderr. But *which stream* it installs is
    ``_REAL_STDOUT``, a snapshot taken when mcp_server was **first**
    imported in this process, so its age is unbounded: in a long-lived
    process or a pytest session it can be a stream that has since been
    replaced or closed. ``saved_stdout`` — read immediately before the
    import — is by construction the stream this process is writing to
    *now*. So once the fd is repaired we prefer the caller's stream
    whenever it is a real one, rather than second-guessing the snapshot.

    Today the two are the same object on the path that matters:
    ``_REAL_STDOUT = sys.stdout`` is the first statement in mcp_server's
    module body, above its own imports, so a first import captures
    exactly what the caller just saved (verified). The preference is
    therefore currently a no-op — deliberately kept, because that
    equality is a coincidence of statement order in another module, and
    an upstream sync that moves the snapshot down or adds an import above
    it would silently turn it into a real divergence. Correct by
    construction beats correct by coincidence.

    One residual case is *not* closed and cannot be, from here: if
    ``sys.stdout`` is already aliased to stderr on entry, then
    ``saved_stdout`` is stderr too, there is no live stream to prefer,
    and the repair is only as good as ``_REAL_STDOUT``. Reaching that
    state needs a second, non-mcp_server hijack; no CLI path does it.

    Any CLI command routing to a local tool handler must import through
    here rather than calling ``from . import mcp_server`` directly.
    """
    saved_stdout = sys.stdout
    from .. import mcp_server

    if sys.stdout is sys.stderr:
        with contextlib.suppress(Exception):
            mcp_server._restore_stdout()
        if saved_stdout is not sys.stderr:
            sys.stdout = saved_stdout
    return mcp_server


def _drawer_call(tool: str, arguments: dict, args) -> dict:
    """Invoke one drawer-family MCP tool, daemon-first.

    Daemon-strict and no ``--palace`` → the daemon's ``/mcp`` endpoint.
    Otherwise the handler is pulled off ``mempalace.mcp_server`` by name
    (``getattr`` rather than the ``TOOLS`` registry so the module
    attribute stays the single patch point for tests) and called
    in-process. ``DaemonError`` propagates to the caller.
    """
    if _daemon_strict() and not getattr(args, "palace", None):
        return _call_daemon_tool(tool, arguments)

    palace = getattr(args, "palace", None)
    if palace:
        # Mirror cmd_init's env-var handoff so mcp_server's lazily-read
        # ``_config.palace_path`` resolves to the requested palace.
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(os.path.expanduser(palace))

    mcp_server = _import_mcp_server()
    handler = getattr(mcp_server, _DRAWER_LOCAL_HANDLERS[tool])
    return handler(**arguments)


def _drawer_exit_client_error(code: str, message: str, want_json: bool) -> None:
    """Refuse a malformed invocation before any palace call. Exit 2."""
    if want_json:
        _emit_json({"error": code, "hint": message})
    else:
        print(f"\n  ERROR: {message}", file=sys.stderr)
    sys.exit(2)


def _drawer_call_or_exit(tool: str, arguments: dict, args, want_json: bool, fallback: str) -> dict:
    """``_drawer_call`` plus the shared failure mapping.

    Daemon unreachable → exit 1 (sibling parity with cmd_list / cmd_move /
    cmd_tunnels). Palace reachable but the tool refused the operation
    (``error`` key, or ``success is False``) → exit 2.
    """
    try:
        data = _drawer_call(tool, arguments, args)
    except DaemonError as e:
        _fail_daemon(e, want_json)

    if not isinstance(data, dict):
        data = {"result": data}
    if data.get("error") or data.get("success") is False:
        if want_json:
            _emit_json(data)
        else:
            print(f"\n  ERROR: {data.get('error') or fallback}", file=sys.stderr)
        sys.exit(2)
    return data


def _drawer_tag_line(data: dict) -> str:
    tags = data.get("tags") or []
    return ", ".join(str(t) for t in tags) if tags else "—"


def _print_drawer_warnings(data: dict) -> None:
    for warning in data.get("warnings") or []:
        print(f"    warning: {warning}")
    flags = data.get("sanitize_flags") or []
    if flags:
        print(f"    sanitized: {', '.join(str(f) for f in flags)}")


def _print_drawer_get(data: dict) -> None:
    """Metadata header, then the drawer's content verbatim.

    Content is printed byte-exact — this command is the shell's read path
    into the palace and a re-wrapped or truncated body would break the
    verbatim promise. ``--format content`` drops the header entirely for
    piping.
    """
    drawer_id = data.get("drawer_id", "")
    metadata = data.get("metadata") or {}
    print()
    print(f"  DRAWER {drawer_id}")
    print(f"  {'-' * 68}")
    print(f"    wing    {data.get('wing') or '—'}")
    print(f"    room    {data.get('room') or '—'}")
    print(f"    tags    {_drawer_tag_line(data)}")
    filed_at = metadata.get("filed_at")
    if filed_at:
        print(f"    filed   {filed_at}")
    added_by = metadata.get("added_by")
    if added_by:
        print(f"    by      {added_by}")
    source_file = metadata.get("source_file")
    if source_file:
        print(f"    source  {source_file}")
    chunks = data.get("chunks")
    if chunks:
        print(f"    chunks  {chunks}")
    print(f"  {'-' * 68}")
    print()
    _write_stdout_exact(data.get("content") or "")
    print()


def _print_drawer_add(data: dict) -> None:
    drawer_id = data.get("drawer_id", "")
    print()
    if data.get("reason") == "already_exists":
        print(f"  Drawer already filed — {drawer_id}")
        print("    (identical wing/room/content; nothing was written)")
    else:
        print(f"  Filed drawer {drawer_id}")
        print(f"    wing    {data.get('wing') or '—'}")
        print(f"    room    {data.get('room') or '—'}")
        chunks = data.get("chunks")
        if chunks:
            print(f"    chunks  {chunks}")
    print(f"    tags    {_drawer_tag_line(data)}")
    _print_drawer_warnings(data)
    print()


def _print_drawer_delete(data: dict) -> None:
    rows = data.get("chunks_deleted") or len(data.get("deleted_ids") or [])
    print()
    print(f"  Deleted drawer {data.get('drawer_id', '')}")
    print(f"    rows    {rows}")
    print()


def _print_drawer_update(data: dict, requested: set) -> None:
    """Confirm which fields the update touched.

    ``requested`` is the set of field names the user actually asked to
    change. ``mempalace_update_drawer`` echoes the drawer's post-update
    wing/room regardless of what changed, so untouched fields are marked
    rather than presented as a move (same convention as
    ``_print_move_result``).
    """
    print()
    if data.get("noop"):
        print(f"  Drawer {data.get('drawer_id', '')} unchanged — nothing to update")
        print()
        return
    print(f"  Updated drawer {data.get('drawer_id', '')}")
    for field in ("wing", "room"):
        value = data.get(field) or "—"
        marker = "→" if field in requested else " "
        suffix = "" if field in requested else "  (unchanged)"
        print(f"    {field:<7} {marker} {value}{suffix}")
    if "tags" in requested:
        print(f"    tags    → {_drawer_tag_line(data)}")
    else:
        print(f"    tags      {_drawer_tag_line(data)}  (unchanged)")
    _print_drawer_warnings(data)
    print()


def _print_duplicate_report(data: dict, threshold: float) -> None:
    if data.get("vector_disabled"):
        print()
        print("  Duplicate detection is UNAVAILABLE — vector search is disabled.")
        reason = data.get("vector_disabled_reason")
        if reason:
            print(f"    reason  {reason}")
        hint = data.get("hint")
        if hint:
            print(f"    hint    {hint}")
        print()
        return

    matches = data.get("matches") or []
    print()
    if not matches:
        print(f"  No duplicate found at threshold {threshold}")
        print()
        return

    id_w = min(max(len("drawer"), max(len(str(m.get("id") or "")) for m in matches)), 46)
    wing_w = min(max(len("wing"), max(len(str(m.get("wing") or "")) for m in matches)), 20)
    room_w = min(max(len("room"), max(len(str(m.get("room") or "")) for m in matches)), 20)

    print(f"  DUPLICATES — {len(matches)} at threshold {threshold}")
    print(f"  {'-' * (id_w + wing_w + room_w + 16)}")
    print(f"    {'drawer':<{id_w}}  {'wing':<{wing_w}}  {'room':<{room_w}}  {'sim':>5}")
    for match in matches:
        did = str(match.get("id") or "")
        wing = str(match.get("wing") or "")
        room = str(match.get("room") or "")
        if len(did) > id_w:
            did = did[: id_w - 1] + "…"
        if len(wing) > wing_w:
            wing = wing[: wing_w - 1] + "…"
        if len(room) > room_w:
            room = room[: room_w - 1] + "…"
        similarity = match.get("similarity")
        sim = f"{similarity:.3f}" if isinstance(similarity, (int, float)) else "—"
        print(f"    {did:<{id_w}}  {wing:<{wing_w}}  {room:<{room_w}}  {sim:>5}")
    print()


def _drawer_delete_confirm(args, drawer_id: str, want_json: bool) -> bool:
    """Confirmation gate before an irreversible single-drawer delete.

    ``--confirm`` skips the gate. On a TTY (and not JSON) we prompt and
    proceed only on y/yes. Non-interactive or JSON without ``--confirm``
    is *refused* (exit 2) — a pipeline must never delete a drawer it
    wasn't explicitly told to. Returns False on an interactive decline so
    the caller can report "aborted" and exit 0. Mirrors
    ``_bulk_move_confirm``.
    """
    if bool(getattr(args, "confirm", False)):
        return True
    if want_json or not sys.stdin.isatty():
        msg = f"refusing to delete drawer {drawer_id} without --confirm in a non-interactive shell"
        if want_json:
            _emit_json({"error": "confirmation_required", "hint": msg, "drawer_id": drawer_id})
        else:
            print(f"\n  ERROR: {msg}", file=sys.stderr)
        sys.exit(2)
    answer = input(f"Delete drawer {drawer_id}? This is irreversible. [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _drawer_content_or_exit(args, want_json: bool, *, required: bool):
    """Resolve ``--content`` / ``--content-file`` into one string.

    Returns None when neither was supplied and ``required`` is False.
    """
    try:
        content = _read_text_arg(
            getattr(args, "content", None),
            getattr(args, "content_file", None),
            default=None,
        )
    except (ValueError, OSError) as e:
        _drawer_exit_client_error("bad_content", str(e), want_json)
        return None  # pragma: no cover - _drawer_exit_client_error exits
    if content is None and required:
        _drawer_exit_client_error(
            "content_required",
            "pass the text with --content or --content-file (use '-' for stdin)",
            want_json,
        )
    return content


def _drawer_tags_or_exit(args, want_json: bool, *, clear_flag: str):
    """Resolve repeatable ``--tag`` plus the explicit empty-list flag.

    Returns None to leave the tool's own tag handling alone (auto
    extraction on add, untouched on update), ``[]`` to clear, or the
    supplied list.
    """
    tags = getattr(args, "tag", None)
    clear = bool(getattr(args, clear_flag, False))
    if tags and clear:
        _drawer_exit_client_error(
            "tag_conflict",
            f"pass --tag or --{clear_flag.replace('_', '-')}, not both",
            want_json,
        )
    if clear:
        return []
    return list(tags) if tags else None


def cmd_drawer(args):
    """Single-drawer CRUD by ID — get / add / delete / update (#355)."""
    action = getattr(args, "drawer_action", None)
    fmt = _resolve_drawer_format(args)
    want_json = fmt == "json"

    if action == "get":
        data = _drawer_call_or_exit(
            "mempalace_get_drawer",
            {"drawer_id": args.drawer_id},
            args,
            want_json,
            f"drawer not found: {args.drawer_id}",
        )
        if want_json:
            _emit_json(data)
        elif fmt == "content":
            _write_stdout_exact(data.get("content") or "")
        else:
            _print_drawer_get(data)
        return

    if action == "add":
        content = _drawer_content_or_exit(args, want_json, required=True)
        payload = {
            "wing": args.wing,
            "room": args.room,
            "content": content,
            "added_by": args.added_by,
        }
        if getattr(args, "source", None):
            payload["source_file"] = args.source
        tags = _drawer_tags_or_exit(args, want_json, clear_flag="no_tags")
        if tags is not None:
            payload["tags"] = tags
        data = _drawer_call_or_exit("mempalace_add_drawer", payload, args, want_json, "add failed")
        if want_json:
            _emit_json(data)
        else:
            _print_drawer_add(data)
        return

    if action == "delete":
        if not _drawer_delete_confirm(args, args.drawer_id, want_json):
            print("\n  Aborted — nothing deleted.\n")
            return
        data = _drawer_call_or_exit(
            "mempalace_delete_drawer",
            {"drawer_id": args.drawer_id},
            args,
            want_json,
            f"drawer not found: {args.drawer_id}",
        )
        if want_json:
            _emit_json(data)
        else:
            _print_drawer_delete(data)
        return

    # update — metadata only, never content (verbatim-always).
    payload = {"drawer_id": args.drawer_id}
    if getattr(args, "wing", None) is not None:
        payload["wing"] = args.wing
    if getattr(args, "room", None) is not None:
        payload["room"] = args.room
    tags = _drawer_tags_or_exit(args, want_json, clear_flag="clear_tags")
    if tags is not None:
        payload["tags"] = tags
    if len(payload) == 1:
        _drawer_exit_client_error(
            "no_change",
            "update requires at least one of --wing / --room / --tag / --clear-tags",
            want_json,
        )
    data = _drawer_call_or_exit(
        "mempalace_update_drawer", payload, args, want_json, "update failed"
    )
    if want_json:
        _emit_json(data)
    else:
        _print_drawer_update(data, requested=set(payload) - {"drawer_id"})


def cmd_duplicate(args):
    """Check whether content already exists in the palace (#363).

    Wraps ``mempalace_check_duplicate``. A completed check exits 0
    whatever the verdict — ``is_duplicate`` in the payload is the answer,
    and overloading the exit code by default would collide with the
    daemon-unreachable 1 that every sibling command uses.

    ``--fail-on-duplicate`` opts into a scriptable guard: non-zero then
    means "do not file this", which is also what daemon-unreachable and a
    disabled vector index mean, so ``duplicate check --file x
    --fail-on-duplicate && file-it`` fails safe in every branch. A
    disabled vector index cannot answer at all; the tool says so
    explicitly rather than claiming "not a duplicate", and we always exit
    2 on it so a guard never reads silence as novelty.
    """
    fmt = _resolve_drawer_format(args)
    want_json = fmt == "json"

    content = _drawer_content_or_exit(args, want_json, required=True)
    threshold = getattr(args, "threshold", None)
    if threshold is None:
        threshold = _DUPLICATE_DEFAULT_THRESHOLD

    data = _drawer_call_or_exit(
        "mempalace_check_duplicate",
        {"content": content, "threshold": threshold},
        args,
        want_json,
        "duplicate check failed",
    )

    if want_json:
        _emit_json(data)
    else:
        _print_duplicate_report(data, threshold)

    if data.get("vector_disabled"):
        sys.exit(2)
    if data.get("is_duplicate") and getattr(args, "fail_on_duplicate", False):
        sys.exit(1)
