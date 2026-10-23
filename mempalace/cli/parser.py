# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def _reconfigure_stdio_utf8_on_windows():
    """Decode stdio as UTF-8 on Windows for the primary `mempalace` CLI.

    Thin wrapper around the shared helper in ``mempalace._stdio``. The CLI
    overrides stdout/stderr to ``replace`` because ``mempalace search``
    prints verbatim drawer text that may carry surrogate halves
    round-tripped from filenames -- ``strict`` would crash mid-print and
    lose the rest of the search result block. stdin keeps the default
    ``surrogateescape`` so a redirected non-UTF-8 file does not kill the
    read on the first bad byte.
    """
    from .._stdio import reconfigure_stdio_utf8_on_windows

    reconfigure_stdio_utf8_on_windows(stdout_errors="replace", stderr_errors="replace")


# ── read-family commands: wings / hallway / checkpoint / taxonomy / aaak ──
#
# Slices of #191 (issues #356, #358, #360, #362). Each command wraps an
# MCP tool that the daemon already serves but that had no CLI verb.
#
# Routing follows the ``cmd_mined`` / ``cmd_wakeup`` dual-path shape
# (#285): daemon when ``_daemon_strict()`` is on and ``--palace`` was not
# given, local otherwise. ``--palace`` is an explicit "inspect THAT
# palace" request and always wins over daemon routing.
#
# Exit codes match the sibling read commands (``tags``, ``tunnels``,
# ``overlap``, ``why``): 0 ok, 1 daemon unreachable, 2 inner-error
# envelope or client-side validation failure.

_WINGS_SORT_CHOICES = ("name", "count")
_HALLWAY_DEFAULT_LIMIT = 50
_CHECKPOINT_DEFAULT_THRESHOLD = 0.9


def _resolve_read_format(args) -> str:
    """``--format`` wins; ``--json`` is the shorthand; table is default.

    Same contract as ``_resolve_tags_format`` / ``_resolve_tunnels_format``
    — kept as one shared helper for the read family rather than a fourth
    near-identical copy.
    """
    fmt = getattr(args, "format", None)
    if fmt:
        return fmt
    if getattr(args, "json", False):
        return "json"
    return "table"


def _read_family_fail(message: str, want_json: bool, code: int, source: str = "cli") -> None:
    """Emit one error in the requested shape and exit with ``code``."""
    if want_json:
        _emit_json({"error": message, "source": source})
    else:
        print(f"\n  ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def _daemon_tool_or_fail(name: str, arguments: dict, want_json: bool) -> dict:
    """``_call_daemon_tool`` with the family's uniform failure handling.

    This wrapper stays, and so do its six callers; what moved is its
    duplicated failure MESSAGE, which now delegates to ``_fail_daemon`` with
    a ``tool=`` kwarg. It kept its own copy for as long as it drew a
    distinction ``_fail_daemon`` did not: transport failure → exit 1,
    JSON-RPC error from a daemon that answered → exit 2. That is why the
    message consolidation deliberately left it alone — merging then would
    have forced one exit code onto two situations in six commands, as a
    "cleanup".

    The two **converge** rather than become identical: the transport branch
    moves 1 → 2, the JSON-RPC branch is unchanged at 2, and the distinction
    between them survives in the PROSE only — plus the ``reachable`` flag on
    the JSON, which means exactly what it meant before.

    Never falls back to local: a silent fallback is exactly the
    split-brain daemon-strict exists to prevent.
    """
    try:
        return _call_daemon_tool(name, arguments)
    except DaemonRequestError as e:
        # The daemon answered and refused a well-formed request — 64, not
        # an outage (#499).
        _exit_daemon_request_error(e, want_json=want_json)
    except DaemonError as e:
        _fail_daemon(e, want_json, tool=name)


def _fail_on_error_envelope(data, want_json: bool, populated_key: str | None = None) -> None:
    """Exit 2 when the tool returned an ``{"error": ...}`` envelope.

    ``populated_key`` names the payload key that, when non-empty, means
    the response is a usable partial rather than a hard failure (the
    ``partial: true`` case ``tool_list_wings`` / ``tool_get_taxonomy``
    can emit when a metadata facet read fails midway).
    """
    if not isinstance(data, dict) or "error" not in data:
        return
    if populated_key and data.get(populated_key):
        return
    if want_json:
        _emit_json(data)
    else:
        print(f"\n  {data['error']}", file=sys.stderr)
    sys.exit(2)


@contextlib.contextmanager
def _local_mcp_server(palace: str | None):
    """Yield ``mempalace.mcp_server`` scoped to ``palace`` for one call.

    Two concerns, both delegated to the mechanisms the drawer family
    established in #355 rather than reimplemented here:

    - **stdout** — ``_import_mcp_server()`` is the single canonical fix
      for the import-time stdio hijack (#225). Its
      ``sys.stdout is sys.stderr`` probe tests for the *damage* rather
      than for the import that caused it, which makes it idempotent and
      order-independent in a way that comparing against a pre-import
      snapshot is not.
    - **``--palace``** — ``MempalaceConfig.palace_path`` is a property
      that re-reads ``MEMPALACE_PALACE_PATH`` on every access, so setting
      the env var redirects the tool handlers even when ``mcp_server``
      was imported long ago against a different config object. Rebinding
      ``mcp_server._config`` would not: an already-imported module keeps
      whatever singleton it was given.

    What this adds over a bare env assignment is *scope*. ``os.environ``
    is process-global and the tool handlers read it lazily, so a value
    left behind would silently redirect a later command that passed no
    ``--palace`` at all. The previous value is restored in a ``finally``,
    including the "was not set" case, so a raising tool cannot leak the
    override either.

    The snapshot is taken **before** the import, not after, because
    importing ``mcp_server`` is itself an environment mutation.
    ``mcp_server`` runs ``parse_known_args()`` at *module scope*
    (``_args = _parse_args()``) against whatever is in ``sys.argv`` — the
    importing process's own command line — and then sets
    ``MEMPALACE_PALACE_PATH`` from any ``--palace`` it finds there. So a
    snapshot taken after the import would capture the value the import
    just wrote and "restore" that, leaving the override behind. Reading
    it first restores the environment the command actually started with.
    (``--backend`` is mutated by the same block and is deliberately not
    unwound here: the drawer family's ``--backend`` support depends on
    it, and quietly reverting another lane's mechanism from this helper
    would be worse than the narrow leak.)
    """
    key = "MEMPALACE_PALACE_PATH"
    sentinel = object()
    previous = os.environ.get(key, sentinel)

    def _restore() -> None:
        if previous is sentinel:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous

    try:
        mcp_server = _import_mcp_server()
        if palace:
            os.environ[key] = os.path.abspath(os.path.expanduser(palace))
        yield mcp_server
    finally:
        _restore()


# ── mempalace wings (issue #356) ──────────────────────────────────────


def _sorted_wing_rows(wings: dict, sort: str) -> list[tuple[str, int]]:
    """Wing → count pairs ordered by name (default) or count descending.

    Count order breaks ties by name so repeated runs are byte-identical
    — a table that reshuffles between invocations is useless in a diff.
    """
    rows = [(str(name), int(count or 0)) for name, count in (wings or {}).items()]
    if sort == "count":
        rows.sort(key=lambda r: (-r[1], r[0]))
    else:
        rows.sort(key=lambda r: r[0])
    return rows


def _print_wings_table(data, sort: str, limit: int) -> None:
    """Aligned wing rows — name, drawer count, share of the palace."""
    wings = data.get("wings") if isinstance(data, dict) else None
    rows = _sorted_wing_rows(wings or {}, sort)
    if not rows:
        print("\n  (no wings)\n")
        return

    total = sum(count for _, count in rows)
    shown = rows[:limit] if limit else rows

    name_w = max(len("wing"), max(len(name) for name, _ in shown))
    name_w = min(name_w, 40)
    count_w = max(len("drawers"), max(len(f"{c:,}") for _, c in shown))

    print(f"\n  WINGS — {len(rows)} ({total:,} drawers)")
    print(f"  {'-' * (name_w + count_w + 14)}")
    print(f"    {'wing':<{name_w}}  {'drawers':>{count_w}}  {'share':>6}")
    for name, count in shown:
        label = name if len(name) <= name_w else name[: name_w - 1] + "…"
        share = (count / total * 100) if total else 0.0
        print(f"    {label:<{name_w}}  {count:>{count_w},}  {share:>5.1f}%")
    if len(shown) < len(rows):
        print(f"    … {len(rows) - len(shown):,} more (raise --limit to see them)")
    print()


def cmd_wings(args):
    """List every wing with its drawer count (slice of #191, issue #356).

    Wraps ``mempalace_list_wings``. On the daemon path the counts come
    from ``GET /status/fast`` first — that endpoint already aggregates
    wing counts from the metadata index and answers in well under a
    second, whereas ``mempalace_list_wings`` over ``/mcp`` walks the
    facet path and does not return in any usable time on a palace of a
    few hundred thousand drawers (measured against the production
    daemon, 2026-08-20). ``mempalace_list_wings`` stays as the fallback
    for daemons that predate ``/status/fast``. Both shapes are reduced
    to the tool's ``{"wings": {...}}`` envelope so ``--json`` consumers
    see one contract regardless of which path served the request.

    The issue also asked for a "last updated" column; ``tool_list_wings``
    returns counts only and no timestamp is available anywhere on the
    read path, so the table carries a share-of-palace percentage instead.
    """
    if getattr(args, "wings_action", None) == "split":
        cmd_wings_maintenance(args)
        return
    want_json = _resolve_read_format(args) == "json"
    sort = getattr(args, "sort", "name") or "name"
    limit = max(0, int(getattr(args, "limit", 0) or 0))

    if _daemon_strict() and not getattr(args, "palace", None):
        try:
            fast = _call_daemon_rest("/status/fast")
        except DaemonRequestError as _req_err:
            # A refusal is not an outage (#499).
            _exit_daemon_request_error(_req_err, want_json=want_json)
        except DaemonError as e:
            # Normalised onto the shared helper (#523). This site had
            # drifted furthest: a leading blank line, an "  ERROR: "
            # prefix, no "see mempalace status" clause, and a JSON error
            # holding a pre-rendered sentence rather than the exception.
            # All four are now the same as the other nineteen.
            _fail_daemon(e, want_json)
        if isinstance(fast, dict) and "wings" in fast:
            data = {"wings": fast.get("wings") or {}}
        else:
            data = _daemon_tool_or_fail("mempalace_list_wings", {}, want_json)
    else:
        with _local_mcp_server(getattr(args, "palace", None)) as mcp_server:
            data = mcp_server.tool_list_wings()

    _fail_on_error_envelope(data, want_json, populated_key="wings")

    if want_json:
        _emit_json(data)
        return
    _print_wings_table(data, sort=sort, limit=limit)


# ── mempalace taxonomy (issue #362) ───────────────────────────────────


def _print_taxonomy_tree(data, wing_filter: str | None) -> None:
    """Wing → room → count tree, wings ordered by total drawers desc."""
    taxonomy = data.get("taxonomy") if isinstance(data, dict) else None
    taxonomy = taxonomy or {}
    if not taxonomy:
        scope = f" for wing={wing_filter}" if wing_filter else ""
        print(f"\n  (no taxonomy{scope})\n")
        return

    wing_totals = {w: sum(int(n or 0) for n in rooms.values()) for w, rooms in taxonomy.items()}
    total = sum(wing_totals.values())
    print(f"\n  TAXONOMY — {len(taxonomy)} wing(s), {total:,} drawers")
    for wing in sorted(taxonomy, key=lambda w: (-wing_totals[w], w)):
        rooms = taxonomy[wing] or {}
        print(f"\n    {wing}  ({wing_totals[wing]:,})")
        for room in sorted(rooms, key=lambda r: (-int(rooms[r] or 0), r)):
            print(f"      {room:<28} {int(rooms[room] or 0):>10,}")
    print()


def cmd_taxonomy(args):
    """Print the wing → room → drawer-count tree (slice of #191, issue #362).

    Wraps ``mempalace_get_taxonomy``. ``--wing`` narrows the output to
    one wing; the underlying tool takes no arguments, so the filter is
    applied client-side after the tree comes back (documented here
    because it does not reduce the work the daemon does).
    """
    want_json = _resolve_read_format(args) == "json"
    wing = getattr(args, "wing", None)

    if _daemon_strict() and not getattr(args, "palace", None):
        data = _daemon_tool_or_fail("mempalace_get_taxonomy", {}, want_json)
    else:
        with _local_mcp_server(getattr(args, "palace", None)) as mcp_server:
            data = mcp_server.tool_get_taxonomy()

    _fail_on_error_envelope(data, want_json, populated_key="taxonomy")

    if wing and isinstance(data, dict) and isinstance(data.get("taxonomy"), dict):
        tree = data["taxonomy"]
        data = dict(data)
        data["taxonomy"] = {wing: tree[wing]} if wing in tree else {}
        data["wing_filter"] = wing

    if want_json:
        _emit_json(data)
        return
    _print_taxonomy_tree(data, wing_filter=wing)


# ── mempalace aaak spec (issue #362) ──────────────────────────────────


def cmd_aaak(args):
    """Print the AAAK dialect specification (slice of #191, issue #362).

    Wraps ``mempalace_get_aaak_spec``. Routed rather than read straight
    out of the local package on purpose: when a daemon is configured,
    the spec that matters is the one the daemon is actually filing
    against, which can differ from the locally installed version.
    """
    want_json = _resolve_read_format(args) == "json"

    if _daemon_strict() and not getattr(args, "palace", None):
        data = _daemon_tool_or_fail("mempalace_get_aaak_spec", {}, want_json)
    else:
        with _local_mcp_server(getattr(args, "palace", None)) as mcp_server:
            data = mcp_server.tool_get_aaak_spec()

    _fail_on_error_envelope(data, want_json, populated_key="aaak_spec")

    if want_json:
        _emit_json(data)
        return

    spec = (data or {}).get("aaak_spec") if isinstance(data, dict) else None
    if not spec:
        print("\n  (no AAAK spec returned)\n", file=sys.stderr)
        sys.exit(2)
    print(spec if spec.endswith("\n") else spec + "\n", end="")


# ── mempalace hallway list|delete (issue #358) ────────────────────────


def _hallway_limit(args) -> int:
    """The requested row cap, uncoerced beyond ``int``.

    Deliberately does *not* clamp with ``max(0, ...)``: ``0`` is the
    documented "show everything" sentinel, so clamping folded a negative
    limit into it (#407).
    """
    raw = getattr(args, "limit", _HALLWAY_DEFAULT_LIMIT)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return _HALLWAY_DEFAULT_LIMIT


def _hallway_shown(rows: list[dict], limit: int) -> list[dict]:
    """The rows a ``--limit`` actually displays, on every surface.

    ``0`` means all. A *negative* limit means *none* -- it used to mean
    all, because ``max(0, limit)`` folded it into the ``0`` sentinel and
    ``--limit -1`` printed the entire graph. Slicing with the raw value
    is no better: ``rows[:-1]`` is "all but the last", which is neither
    of the two things a caller could have meant.
    """
    if limit == 0:
        return list(rows)
    return list(rows[:limit]) if limit > 0 else []


def _hallway_rows(data) -> list[dict]:
    """Normalise the tool response to a list of hallway records.

    ``mempalace_list_hallways`` returns a bare list; tolerate a future
    ``{"hallways": [...]}`` wrapping the way ``_print_tunnels_table``
    does for its sibling tool.
    """
    if isinstance(data, dict):
        return list(data.get("hallways") or data.get("data") or [])
    return list(data or [])


def _print_hallways_table(rows: list[dict], scope_wing: str | None, limit: int) -> None:
    """Aligned hallway rows — id, wing, entity_a ↔ entity_b, co-occurrences."""
    scope_label = f" — wing={scope_wing}" if scope_wing else ""
    if not rows:
        print(
            f"\n  (no hallways{scope_label}) — they are built from drawer entities when you mine."
        )
        print()
        return

    shown = _hallway_shown(rows, limit)
    if not shown:
        # A limit can legitimately hide every row; say so rather than
        # crashing on max() over an empty sequence.
        print(f"\n  HALLWAYS — {len(rows)}{scope_label}")
        print(f"    (no rows shown at --limit {limit})")
        print()
        return
    id_w = min(max(len("id"), max(len(str(h.get("id") or "")) for h in shown)), 34)
    wing_w = min(max(len("wing"), max(len(str(h.get("wing") or "")) for h in shown)), 22)

    def _pair(h: dict) -> str:
        return f"{h.get('entity_a', '?')} ↔ {h.get('entity_b', '?')}"

    pair_w = min(max(len("entities"), max(len(_pair(h)) for h in shown)), 44)

    print(f"\n  HALLWAYS — {len(rows)}{scope_label}")
    print(f"  {'-' * (id_w + wing_w + pair_w + 16)}")
    print(f"    {'id':<{id_w}}  {'wing':<{wing_w}}  {'entities':<{pair_w}}  {'count':>6}")
    for h in shown:
        hid = _truncate_cell(str(h.get("id") or ""), id_w)
        wing = _truncate_cell(str(h.get("wing") or ""), wing_w)
        pair = _truncate_cell(_pair(h), pair_w)
        count = int(h.get("co_occurrence_count") or 0)
        print(f"    {hid:<{id_w}}  {wing:<{wing_w}}  {pair:<{pair_w}}  {count:>6,}")
    if len(shown) < len(rows):
        print(f"    … {len(rows) - len(shown):,} more (raise --limit to see them)")
    print()


def _truncate_cell(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def cmd_hallway_list(args, _deprecated_alias: bool = False):
    """List within-wing entity hallways (slice of #191, issue #358).

    Wraps ``mempalace_list_hallways``. The pre-existing ``mempalace
    hallways`` verb is a deprecated alias that delegates here (#407), so
    both spellings share one fetch path, one sort and one limit rule;
    ``_deprecated_alias`` only adds the ``deprecated`` marker to the
    JSON payload when the caller arrived by the old name.
    """
    want_json = _resolve_read_format(args) == "json"
    wing = getattr(args, "wing", None)
    limit = _hallway_limit(args)

    if _daemon_strict() and not getattr(args, "palace", None):
        arguments = {"wing": wing} if wing else {}
        data = _daemon_tool_or_fail("mempalace_list_hallways", arguments, want_json)
    else:
        with _local_mcp_server(getattr(args, "palace", None)) as mcp_server:
            data = mcp_server.tool_list_hallways(wing)

    _fail_on_error_envelope(data, want_json)

    rows = _hallway_rows(data)
    # Deterministic order: strongest link first, id as the tiebreak.
    rows.sort(key=lambda h: (-int(h.get("co_occurrence_count") or 0), str(h.get("id") or "")))

    if want_json:
        # ``total`` stays the unsliced count: a scripted caller needs to
        # know a limit truncated the list. ``--limit`` used to be
        # accepted and ignored here -- the same silently-dropped-flag
        # defect #407 was filed about, one verb over.
        payload = {
            "hallways": _hallway_shown(rows, limit),
            "wing_filter": wing,
            "total": len(rows),
        }
        if _deprecated_alias:
            payload["deprecated"] = "use `mempalace hallway list`"
        _emit_json(payload)
        return
    _print_hallways_table(rows, scope_wing=wing, limit=limit)


def cmd_hallway_delete(args):
    """Delete one hallway record by id (slice of #191, issue #358).

    Destructive, so it refuses to act without ``--confirm``. On an
    interactive terminal the flag can be supplied by answering the
    prompt; with no TTY there is nobody to ask, so the command exits 2
    rather than deleting on an implied yes.
    """
    want_json = _resolve_read_format(args) == "json"
    hallway_id = getattr(args, "hallway_id", None)
    if not hallway_id or not str(hallway_id).strip():
        _read_family_fail("hallway delete requires a hallway id", want_json, 2)
        return
    hallway_id = str(hallway_id).strip()

    if not getattr(args, "confirm", False):
        if want_json or not _stdin_is_tty():
            _read_family_fail(
                f"refusing to delete hallway {hallway_id} without --confirm "
                "(no interactive terminal to ask on)",
                want_json,
                2,
            )
            return
        answer = input(f"  Delete hallway {hallway_id}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("  Aborted — nothing deleted.\n")
            return

    if _daemon_strict() and not getattr(args, "palace", None):
        data = _daemon_tool_or_fail(
            "mempalace_delete_hallway", {"hallway_id": hallway_id}, want_json
        )
    else:
        with _local_mcp_server(getattr(args, "palace", None)) as mcp_server:
            data = mcp_server.tool_delete_hallway(hallway_id)

    _fail_on_error_envelope(data, want_json)

    if want_json:
        _emit_json(data)
    elif (data or {}).get("deleted"):
        print(f"\n  Deleted hallway {hallway_id}.\n")
    else:
        print(f"\n  No hallway with id {hallway_id} — nothing deleted.\n")
    # A miss is not an error: delete is idempotent and the report above
    # already distinguishes the two outcomes.


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def cmd_hallway(args):
    """Dispatch the two-level ``hallway`` verb."""
    action = getattr(args, "hallway_action", None)
    if action == "list":
        cmd_hallway_list(args)
    elif action == "delete":
        cmd_hallway_delete(args)


# ── mempalace checkpoint (issue #360) ─────────────────────────────────
#
# The issue proposed ``--session <id>`` plus a ``checkpoint list``
# subcommand. Neither exists in the tool: ``mempalace_checkpoint`` takes
# ``{items:[{wing,room,content}], diary?, dedup_threshold?, added_by?}``
# with ``items`` required, and it is write-only — there is no session
# identifier and nothing to list. Per the wave brief the schema wins, so
# the CLI mirrors the real payload.


def _checkpoint_items(args) -> list[dict]:
    """Build the ``items`` payload from ``--items-file`` or the single-item flags.

    Raises ``ValueError`` with a caller-facing message on any malformed
    input. Validation is deliberately duplicated here even though
    ``tool_checkpoint`` guards its own inputs: a typo should cost a
    non-zero exit, not a partially-filed batch reported as errors.
    """
    items_file = getattr(args, "items_file", None)
    wing = getattr(args, "wing", None)
    room = getattr(args, "room", None)
    content_inline = getattr(args, "content", None)
    content_file = getattr(args, "content_file", None)
    single = any(v is not None for v in (wing, room, content_inline, content_file))

    if items_file and single:
        raise ValueError("pass --items-file or the single-item flags (--wing/--room/--content)")
    if not items_file and not single:
        raise ValueError(
            "checkpoint needs items: --items-file PATH (or -) or --wing W --room R --content TEXT"
        )

    if items_file:
        # _read_text_arg already maps "-" to a byte-exact stdin read.
        raw = _read_text_arg(None, items_file)
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"--items-file is not valid JSON: {e}") from None
        if isinstance(parsed, dict):
            parsed = parsed.get("items", parsed)
        if not isinstance(parsed, list):
            raise ValueError("--items-file must contain a JSON array of {wing, room, content}")
        items = parsed
    else:
        content = _read_text_arg(content_inline, content_file, default=None)
        items = [{"wing": wing, "room": room, "content": content}]

    if not items:
        raise ValueError("no items to file")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"item {index} is not an object")
        for field in ("wing", "room", "content"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"item {index}: {field} must be a non-empty string")
    return items


def _checkpoint_diary(args) -> dict | None:
    """Build the optional ``diary`` payload, or ``None`` when unrequested."""
    entry = _read_text_arg(
        getattr(args, "diary_entry", None), getattr(args, "diary_entry_file", None), default=None
    )
    agent = getattr(args, "diary_agent", None)
    topic = getattr(args, "diary_topic", None)
    wing = getattr(args, "diary_wing", None)
    if entry is None:
        if any(v for v in (agent, topic, wing)):
            raise ValueError("--diary-agent/--diary-topic/--diary-wing need --diary-entry too")
        return None
    if not entry.strip():
        raise ValueError("--diary-entry must not be blank")
    diary: dict = {"entry": entry}
    if agent:
        diary["agent_name"] = agent
    if topic:
        diary["topic"] = topic
    if wing:
        diary["wing"] = wing
    return diary


def _print_checkpoint_report(data) -> None:
    added = (data or {}).get("added") or []
    duplicates = (data or {}).get("duplicates") or []
    errors = (data or {}).get("errors") or []
    diary = (data or {}).get("diary")
    print(f"\n  CHECKPOINT — {len(added)} filed, {len(duplicates)} duplicate, {len(errors)} error")
    for entry in added:
        print(f"    + {entry.get('wing', '?')}/{entry.get('room', '?')}  {entry.get('id', '')}")
    for dup in duplicates:
        print(f"    = {dup.get('room', '?')} (already filed)")
    for err in errors:
        print(f"    ! {err.get('error', err)}")
    if diary:
        print(f"    diary: {diary.get('id') or diary.get('error') or 'written'}")
    print()


def cmd_checkpoint(args):
    """Batch-file a session in one call (slice of #191, issue #360).

    Wraps ``mempalace_checkpoint``: semantic-dedups each item, files the
    non-duplicates as drawers, then writes one optional diary entry.

    This writes to the palace, so ``--dry-run`` prints the exact payload
    that would be sent and exits without calling the tool — the cheap
    way to check a generated ``--items-file`` before it lands.
    """
    want_json = _resolve_read_format(args) == "json"

    try:
        items = _checkpoint_items(args)
        diary = _checkpoint_diary(args)
    except (ValueError, OSError) as e:
        _read_family_fail(str(e), want_json, 2)
        return

    try:
        threshold = float(getattr(args, "dedup_threshold", _CHECKPOINT_DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        _read_family_fail("--dedup-threshold must be a number", want_json, 2)
        return
    if not 0.0 <= threshold <= 1.0:
        _read_family_fail("--dedup-threshold must be between 0 and 1", want_json, 2)
        return

    arguments: dict = {"items": items, "dedup_threshold": threshold}
    if diary is not None:
        arguments["diary"] = diary
    added_by = getattr(args, "added_by", None)
    if added_by:
        arguments["added_by"] = added_by

    if getattr(args, "dry_run", False):
        preview = {"dry_run": True, "would_send": arguments}
        if want_json:
            _emit_json(preview)
        else:
            print(f"\n  DRY RUN — would file {len(items)} item(s), nothing written.")
            for item in items:
                print(f"    {item['wing']}/{item['room']}  ({len(item['content']):,} chars)")
            if diary is not None:
                print(f"    diary by {diary.get('agent_name', '(default)')}")
            print()
        return

    if _daemon_strict() and not getattr(args, "palace", None):
        data = _daemon_tool_or_fail("mempalace_checkpoint", arguments, want_json)
    else:
        with _local_mcp_server(getattr(args, "palace", None)) as mcp_server:
            data = mcp_server.tool_checkpoint(**arguments)

    _fail_on_error_envelope(data, want_json, populated_key="added")

    if want_json:
        _emit_json(data)
        return
    _print_checkpoint_report(data)
    # Exit 2 when nothing landed and the batch reported errors — a
    # scripted caller must not read "no output" as success.
    if not (data or {}).get("added") and (data or {}).get("errors"):
        sys.exit(2)


def main():  # noqa: C901 — merged fork daemon-routing + upstream hub-forward dispatch; see #383 sync
    """CLI entry point for the ``mempalace`` console script.

    Side effect: pops ``PYTHONPATH`` from ``os.environ`` (see #1423) so
    any subprocess this CLI spawns inherits a clean env. Host applications
    that call ``main()`` programmatically should be aware that the parent
    process loses ``PYTHONPATH`` as well. Library imports
    (``import mempalace.searcher`` from a host app) do NOT trigger this
    side effect; only the CLI/MCP entry points pop the env var.
    """
    # Drop leaked PYTHONPATH so any subprocess the CLI spawns (mine workers,
    # repair tooling) starts with a clean env. The sys.path filter in
    # mempalace/__init__.py already protects this process from the same
    # ABI mismatch; here we extend the protection to children.
    os.environ.pop("PYTHONPATH", None)

    _reconfigure_stdio_utf8_on_windows()

    version_label = f"MemPalace {__version__}"
    parser = argparse.ArgumentParser(
        description="MemPalace — Give your AI a memory. No API key required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{version_label}\n\n{__doc__}",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=version_label,
        help="Show version and exit",
    )
    parser.add_argument(
        "--palace",
        default=None,
        help="Where the palace lives (default: palace_path from config.json, resolved via XDG — see mempalace.config)",
    )
    # ── Agent-shaped output (issue #44) ──────────────────────────────
    # Both flags are global so any subcommand can opt in. They're also
    # registered on each subparser below so users can write either
    # ``mempalace --json status`` or the more natural
    # ``mempalace status --json``.
    parser.add_argument(
        "--json",
        "-j",
        dest="json",
        action="store_true",
        default=False,
        help="Emit JSON to stdout (implies --quiet; suitable for shell pipelines)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        dest="quiet",
        action="store_true",
        default=False,
        help="Suppress decorative output (headers, progress, routing announcement)",
    )
    parser.add_argument(
        "--backend",
        dest="global_backend",
        default=None,
        help="Storage backend to use for this command (default: config/env/detected/chroma)",
    )

    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Detect rooms from your folder structure")
    p_init.add_argument("dir", help="Project directory to set up")
    p_init.add_argument(
        "--palace",
        default=argparse.SUPPRESS,
        help="Where the palace lives (default: from ~/.mempalace/config.json or ~/.mempalace/palace); "
        "may be passed before or after the subcommand — both positions are accepted (#2366)",
    )
    p_init.add_argument(
        "--backend",
        default=None,
        help="Storage backend to persist for this palace (default: chroma)",
    )
    p_init.add_argument(
        "--yes",
        action="store_true",
        help="Auto-accept all detected entities (non-interactive)",
    )
    p_init.add_argument(
        "--auto-mine",
        action="store_true",
        help=(
            "Skip the post-init mine prompt and run mine automatically. "
            "Combine with --yes for a fully non-interactive setup."
        ),
    )
    p_init.add_argument(
        "--lang",
        default=None,
        help=(
            "Comma-separated language codes for entity detection "
            "(e.g. 'en' or 'en,pt-br'). Defaults to value from config "
            "(MEMPALACE_ENTITY_LANGUAGES env var or config.json), or 'en'. "
            "When given, the value is also persisted to config.json."
        ),
    )
    p_init.add_argument(
        "--llm",
        action="store_true",
        help=(
            "DEPRECATED — LLM-assisted entity refinement is now ON by default. "
            "This flag is preserved for backward compatibility; pass --no-llm "
            "to opt out instead."
        ),
    )
    p_init.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Disable LLM-assisted entity refinement. Run init in heuristics-only "
            "mode (no provider acquisition, no LLM calls). Use when running "
            "without a local LLM and you don't want the graceful-fallback message."
        ),
    )
    p_init.add_argument(
        "--llm-provider",
        default="ollama",
        choices=["ollama", "openai-compat", "anthropic"],
        help="LLM provider (default: ollama). Pass --no-llm to disable LLM-assisted refinement entirely.",
    )
    p_init.add_argument(
        "--llm-model",
        default="gemma4:e4b",
        help="Model name for the chosen provider (default: gemma4:e4b for Ollama).",
    )
    p_init.add_argument(
        "--llm-endpoint",
        default=None,
        help=(
            "Provider endpoint URL. Default for Ollama: http://localhost:11434. "
            "Required for openai-compat."
        ),
    )
    p_init.add_argument(
        "--llm-api-key",
        default=None,
        help=(
            "API key for the provider. For anthropic, defaults to $ANTHROPIC_API_KEY; "
            "for openai-compat, defaults to $OPENAI_API_KEY."
        ),
    )
    p_init.add_argument(
        "--accept-external-llm",
        action="store_true",
        help=(
            "Bypass the interactive consent prompt that fires when an external "
            "LLM is configured via an environment-variable API key (issue #26). "
            "Use this in CI / non-interactive runs where you've already decided "
            "the external send is acceptable."
        ),
    )

    # mine
    add_cli_write_routing_flags(
        p_init,
        allow_background=False,
    )

    p_mine = sub.add_parser("mine", help="Mine files into the palace")
    p_mine.add_argument(
        "dir",
        help=(
            "Directory to mine, one file (projects mode mines just that file — a "
            "targeted re-index that replaces its existing drawers; against a daemon "
            "this needs one carrying palace-daemon#258, older daemons reject a "
            "non-.jsonl file with a 400), or one conversation file with --mode convos"
        ),
    )
    p_mine.add_argument(
        "--backend",
        default=None,
        help="Storage backend to use for this mine (default: config/env/detected/chroma)",
    )
    mine_source_group = p_mine.add_mutually_exclusive_group()
    mine_source_group.add_argument(
        "--mode",
        choices=["projects", "convos", "session", "extract"],
        default=None,
        help=(
            "Ingest mode: 'projects' for code/docs (default), 'convos' for chat "
            "exports (one drawer per exchange), 'session' for one addressable "
            "manifest drawer per session file (fork-only; anchor for 'did session X "
            "exist?' queries), 'extract' for office documents (PDF/DOCX/RTF/etc., "
            "requires mempalace[extract])"
        ),
    )
    mine_source_group.add_argument(
        "--source",
        default=None,
        metavar="ADAPTER",
        help=(
            "Route through a registered source adapter instead of the built-in "
            "mine pipeline. Cannot be combined with --mode; no --source preserves "
            "legacy projects-mode mining. Adapters are discovered from the "
            "'mempalace.sources' entry-point group (e.g. filesystem, conversations, "
            "opencode, codex, gemini, aider). Use 'mempalace mine --source list' "
            "to see installed adapters."
        ),
    )
    p_mine.add_argument("--wing", default=None, help="Wing name (default: directory name)")
    p_mine.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Don't respect .gitignore files when scanning project files",
    )
    p_mine.add_argument(
        "--include-ignored",
        action="append",
        default=[],
        help="Always scan these project-relative paths even if ignored; repeat or pass comma-separated paths",
    )
    p_mine.add_argument(
        "--agent",
        default="mempalace",
        help="Your name — recorded on every drawer (default: mempalace)",
    )
    p_mine.add_argument("--limit", type=int, default=0, help="Max files to process (0 = all)")
    p_mine.add_argument(
        "--no-tunnels",
        action="store_true",
        help=(
            "Skip the post-mine derived-graph rebuild: cross-wing topic tunnels, "
            "within-wing hallways, and cross-wing entity tunnels. Those cost "
            "O(wing), not O(change), so a small targeted mine otherwise pays for "
            "the whole wing. The drawers filed are identical either way; only the "
            "derived graph is left un-refreshed until the next full mine. Note "
            "the hallway rebuild is skipped too — entity tunnels are derived "
            "from it, so the two cannot be separated"
        ),
    )
    p_mine.add_argument(
        "--redetect-origin",
        action="store_true",
        help=(
            "Re-run corpus_origin detection on this directory and overwrite "
            "<palace>/.mempalace/origin.json. Useful when the corpus has grown "
            "since `mempalace init` and the stored origin may be stale. "
            "Heuristic-only (no LLM call) — re-run `mempalace init --llm` for "
            "Tier 2 refinement."
        ),
    )
    p_mine.add_argument(
        "--dry-run", action="store_true", help="Show what would be filed without filing"
    )
    add_cli_write_routing_flags(p_mine)
    # The fork's palace-daemon route (PALACE_DAEMON_URL, daemon-strict) also takes --background (#456).
    for _action in p_mine._actions:
        if _action.dest == "background":
            _action.help = (
                "Do not wait for the mine to finish: on a local-daemon route (--daemon or a prefer/require "
                "policy), return a job id immediately; on the palace daemon, queue it and return at once"
            )
    p_mine.add_argument(
        "--extract",
        choices=["exchange", "general"],
        default="exchange",
        help="Extraction strategy for convos mode: 'exchange' (default) or 'general' (5 memory types)",
    )
    p_mine.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Parallel workers for file read/chunk/route; writes stay serialized "
            "so embedding is single-threaded. Default 1 (sequential); higher "
            "parallelizes prep on multi-core cold mines."
        ),
    )
    p_mine.add_argument(
        "--max-chunks-per-file",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"Per-file chunk cap; files producing more chunks are skipped with a "
            f"summary counter. Default {_CLI_MAX_CHUNKS_PER_FILE_DEFAULT} "
            f"(or MEMPALACE_MAX_CHUNKS_PER_FILE). Set 0 to disable. Lower this on "
            f"Windows if you hit ONNX bad_alloc (#1455)."
        ),
    )
    p_mine.add_argument(
        "--include-subagents",
        action="store_true",
        default=False,
        help=(
            "Also mine Claude Code subagent transcripts (subagents/ dirs). "
            "Excluded by default: these are short ephemeral exchanges "
            "(Explore/Plan/Grep agents) already summarized in the parent "
            "session, and on typical workspaces they dominate file counts."
        ),
    )

    # sweep
    p_sweep = sub.add_parser(
        "sweep",
        help="Tandem miner: catch anything the primary miner missed "
        "(message-level, timestamp-coordinated, idempotent)",
    )
    p_sweep.add_argument(
        "target",
        help="A .jsonl transcript file, or a directory to scan recursively",
    )

    # sync
    add_cli_write_routing_flags(p_sweep)
    p_sync = sub.add_parser(
        "sync",
        help="Prune drawers whose source files are gitignored, deleted, or moved (#1252)",
    )
    p_sync.add_argument(
        "dir",
        nargs="?",
        default=None,
        help="Project root to sync (optional; auto-detects from drawer metadata)",
    )
    p_sync.add_argument("--wing", default=None, help="Limit to one wing")
    p_sync.add_argument(
        "--root",
        action="append",
        default=[],
        help="Additional project root (repeatable)",
    )
    p_sync.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Preview only (default)",
    )
    p_sync.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Actually delete drawers (overrides --dry-run; requires --wing or a project root)",
    )
    p_sync.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt --apply shows before deleting",
    )

    # search
    add_cli_write_routing_flags(p_sync)
    p_search = sub.add_parser("search", help="Find anything, exact words")
    p_search.add_argument("query", help="What to search for")
    p_search.add_argument(
        "--backend",
        default=None,
        help="Storage backend to use for this search (default: config/env/detected/chroma)",
    )
    p_search.add_argument("--wing", default=None, help="Limit to one project")
    p_search.add_argument("--room", default=None, help="Limit to one room")
    p_search.add_argument(
        "--tag",
        action="append",
        dest="tags",
        default=None,
        metavar="TAG",
        help=(
            "Only return drawers carrying this tag. May be repeated; "
            "multiple --tag flags AND together (drawer must have ALL of them)."
        ),
    )
    p_search.add_argument("--results", type=int, default=15, help="Number of results")
    p_search.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Alias of --results (overrides --results when both given)",
    )
    p_search.add_argument(
        "--mode",
        choices=("auto", "fast", "hybrid"),
        default=None,
        help=(
            "Search mode: auto (default — BM25 when possible, MCP fallback), "
            "fast (BM25 only, ~100ms), hybrid (vector + BM25 + AGE graph, ~500ms)"
        ),
    )
    p_search.add_argument(
        "--format",
        choices=("table", "compact", "full", "json"),
        default=None,
        help=(
            "Output format: table (default, multi-line + relevance bar), "
            "compact (one line per hit), full (table layout, no content "
            "truncation), json (machine-readable; same as --json)"
        ),
    )

    # list — fast direct-to-daemon drawer browser (#191)
    p_list = sub.add_parser(
        "list",
        help="Browse drawers by wing/room metadata (no ranking, no embedding)",
    )
    p_list.add_argument("--wing", default=None, help="Limit to one wing")
    p_list.add_argument("--room", default=None, help="Limit to one room")
    p_list.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max drawers to return (default: 20, max: 1000)",
    )
    p_list.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Pagination offset (default: 0)",
    )
    p_list.add_argument(
        "--format",
        choices=("table", "compact", "full", "json"),
        default=None,
        help=(
            "Output format: table (default, multi-line preview), "
            "compact (one line per drawer), full (labelled sections, "
            "no truncation), json (machine-readable; same as --json)"
        ),
    )

    # move — single-drawer metadata relocation (complement to rename-wing)
    p_move = sub.add_parser(
        "move",
        help="Relocate one drawer to a different wing/room (metadata only)",
    )
    p_move.add_argument("drawer_id", help="ID of the drawer to move")
    p_move.add_argument("--wing", default=None, help="New wing (omit to leave unchanged)")
    p_move.add_argument("--room", default=None, help="New room (omit to leave unchanged)")
    p_move.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help=(
            "Output format: table (default, old→new confirmation), "
            "json (daemon pass-through; same as --json)"
        ),
    )

    # bulk-move — multi-drawer metadata relocation by source wing/room
    p_bulk_move = sub.add_parser(
        "bulk-move",
        help="Relocate many drawers (by source wing/room) to a target wing/room (metadata only)",
    )
    p_bulk_move.add_argument(
        "--wing",
        default=None,
        help="Source: select drawers in this wing (at least one of --wing/--room required)",
    )
    p_bulk_move.add_argument(
        "--room",
        default=None,
        help="Source: select drawers in this room (at least one of --wing/--room required)",
    )
    p_bulk_move.add_argument(
        "--to-wing",
        dest="to_wing",
        default=None,
        help="Target wing (at least one of --to-wing/--to-room required)",
    )
    p_bulk_move.add_argument(
        "--to-room",
        dest="to_room",
        default=None,
        help="Target room (at least one of --to-wing/--to-room required)",
    )
    p_bulk_move.add_argument(
        "--apply",
        action="store_true",
        help="Actually move the drawers. Without this, bulk-move only previews (dry run).",
    )
    p_bulk_move.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt (required to --apply in a non-interactive shell)",
    )
    p_bulk_move.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help=(
            "Output format: table (default, preview/summary), "
            "json (machine-readable; same as --json)"
        ),
    )

    # graph
    p_graph = sub.add_parser(
        "graph",
        help="KG + palace structural snapshot (wings, rooms, tunnels, kg_stats)",
    )
    p_graph.add_argument(
        "--limit",
        type=int,
        default=500,
        help=(
            "Cap on KG entity count (and 2x this for MENTIONS triples). "
            "Default: 500, max: 50000 — matches the daemon's hard ceiling."
        ),
    )
    p_graph.add_argument(
        "--format",
        choices=("table", "full", "json"),
        default=None,
        help=(
            "Output format: table (default, summary + top wings + sample), "
            "full (every wing, every sampled triple, no truncation), "
            "json (machine-readable; same as --json)"
        ),
    )

    # cypher — read-only Cypher query against the AGE knowledge graph
    p_cypher = sub.add_parser(
        "cypher",
        help="Run a read-only Cypher query against the AGE knowledge graph",
    )
    p_cypher.add_argument(
        "query",
        help="Cypher query string (MATCH / RETURN; write verbs are server-rejected)",
    )
    p_cypher.add_argument(
        "--graph",
        default=_CYPHER_DEFAULT_GRAPH,
        help=f"AGE graph name (default: {_CYPHER_DEFAULT_GRAPH})",
    )
    p_cypher.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default=None,
        help=(
            "Output format: table (default, aligned columns), "
            "json (pass-through daemon envelope; same as --json), "
            "csv (pipe-friendly, no decoration)"
        ),
    )
    p_cypher.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Advisory cap. The daemon's statement_timeout (PR #228) is the real "
            "ceiling — pass LIMIT in the query itself for a hard cutoff."
        ),
    )
    p_search.add_argument(
        "--since",
        default=None,
        help=(
            "Only drawers filed on/after this ISO date/datetime (inclusive), "
            "e.g. 2026-04-01. Drawers without a filed_at are excluded while "
            "a date bound is set"
        ),
    )
    p_search.add_argument(
        "--before",
        default=None,
        help="Only drawers filed strictly before this ISO date/datetime (exclusive)",
    )

    # compress
    p_compress = sub.add_parser(
        "compress", help="Compress drawers using AAAK Dialect (~30x reduction)"
    )
    p_compress.add_argument("--wing", default=None, help="Wing to compress (default: all wings)")
    p_compress.add_argument(
        "--dry-run", action="store_true", help="Preview compression without storing"
    )
    p_compress.add_argument(
        "--config", default=None, help="Entity config JSON (e.g. entities.json)"
    )

    # export
    p_export = sub.add_parser("export", help="Export palace as browsable markdown files")
    p_export.add_argument(
        "--output",
        "-o",
        default="./palace-export",
        help="Output directory (default: ./palace-export)",
    )

    # wake-up
    p_wakeup = sub.add_parser("wake-up", help="Show L0 + L1 wake-up context (~600-900 tokens)")
    p_wakeup.add_argument("--wing", default=None, help="Wake-up for a specific project/wing")

    # split
    p_split = sub.add_parser(
        "split",
        help="Split concatenated transcript mega-files into per-session files (run before mine)",
    )
    p_split.add_argument("dir", help="Directory containing transcript files")
    p_split.add_argument(
        "--output-dir",
        default=None,
        help="Write split files here (default: same directory as source files)",
    )
    p_split.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be split without writing files",
    )
    p_split.add_argument(
        "--min-sessions",
        type=int,
        default=2,
        help="Only split files containing at least N sessions (default: 2)",
    )

    # hook
    p_hook = sub.add_parser(
        "hook",
        help="Run hook logic (reads JSON from stdin, outputs JSON to stdout)",
    )
    hook_sub = p_hook.add_subparsers(dest="hook_action")
    p_hook_run = hook_sub.add_parser("run", help="Execute a hook")
    p_hook_run.add_argument(
        "--hook",
        required=True,
        choices=["session-start", "stop", "session-end", "precompact"],
        help="Hook name to run",
    )
    p_hook_run.add_argument(
        "--harness",
        required=True,
        choices=["claude-code", "codex", "dsh"],
        help="Harness type (determines stdin JSON format)",
    )

    # instructions
    p_instructions = sub.add_parser(
        "instructions",
        help="Output skill instructions to stdout",
    )
    instructions_sub = p_instructions.add_subparsers(dest="instructions_name")
    for instr_name in ["init", "search", "mine", "help", "status", "audit"]:
        instructions_sub.add_parser(instr_name, help=f"Output {instr_name} instructions")

    # rules
    p_rules = sub.add_parser(
        "rules",
        help=(
            "Output the canonical shared-brain agent rules block for a system "
            "prompt (CLAUDE.md, GEMINI.md, AGENTS.md, ...)"
        ),
    )
    p_rules.add_argument(
        "--host",
        required=True,
        help="Stable machine label (lowercase), e.g. windows, mac, blade",
    )
    p_rules.add_argument(
        "--harness",
        required=True,
        help="Runtime family (lowercase), e.g. claude, codex, grok, antigravity",
    )
    p_rules.add_argument(
        "--project",
        required=True,
        help=(
            "Example workspace/repo name (lowercase). The rendered block tells "
            "the agent to compose host:harness:<project> from the current workspace"
        ),
    )
    p_rules.add_argument(
        "--mcp",
        choices=["full", "light"],
        default="full",
        help=(
            "Tool names in the block: 'full' (default, the 47-tool mempalace-mcp) or "
            "'light' (palace_query / palace_exec / palace_coordinate)"
        ),
    )

    # repair
    p_repair = sub.add_parser(
        "repair",
        help=(
            "Rebuild palace vector index (legacy mode) or un-poison max_seq_id rows "
            "(--mode max-seq-id)"
        ),
    )
    p_repair.add_argument(
        "--yes", action="store_true", help="Skip confirmation for destructive changes"
    )
    p_repair.add_argument(
        "repair_action",
        nargs="?",
        choices=["rebuild-index"],
        help=(
            "Re-embed the palace from SQLite using the current embedding model "
            "(alias for --mode from-sqlite --archive-existing)."
        ),
    )
    p_repair.add_argument(
        "--confirm-truncation-ok",
        action="store_true",
        help=(
            "Override the #1208 safety guard. Required when chromadb's collection-layer "
            "extraction returns exactly 10,000 drawers and the SQLite ground-truth check "
            "either matches or can't be read. Use only after independently confirming "
            "the palace really contains that count."
        ),
    )
    p_repair.add_argument(
        "--mode",
        choices=["rebuild", "legacy", "max-seq-id", "from-sqlite"],
        default="legacy",
        help=(
            "rebuild/legacy: full-palace HNSW rebuild via extract + re-upsert (default; "
            "rebuild and legacy are synonyms). "
            "max-seq-id: un-poison max_seq_id rows corrupted by the legacy 0.6.x shim. "
            "from-sqlite: rebuild by reading rows directly from chroma.sqlite3, "
            "bypassing the chromadb client. Use when legacy mode bails because the "
            "chromadb client cannot open the collection."
        ),
    )
    p_repair.add_argument(
        "--source",
        default=None,
        help=(
            "Source palace path for --mode from-sqlite (defaults to --palace). "
            "Use when extracting from an archived corrupt palace into a new location."
        ),
    )
    p_repair.add_argument(
        "--archive-existing",
        action="store_true",
        help=(
            "For --mode from-sqlite when --source equals --palace: rename the "
            "existing palace to <palace>.pre-rebuild-<timestamp> before "
            "rebuilding so the corrupt copy is preserved."
        ),
    )
    p_repair.add_argument(
        "--segment",
        default=None,
        help="Segment UUID filter for --mode max-seq-id (repairs only that segment).",
    )
    p_repair.add_argument(
        "--from-sidecar",
        default=None,
        help=(
            "Path to a pre-corruption chroma.sqlite3 sidecar (for --mode max-seq-id); "
            "clean values are copied from its max_seq_id table verbatim."
        ),
    )
    p_repair.add_argument(
        "--backup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Back up SQLite before mutation (default: on)",
    )
    p_repair.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what the repair would do and exit without modifying the palace",
    )

    # repair-status — read-only HNSW capacity health check (#1222)
    sub.add_parser(
        "repair-status",
        help="Compare sqlite vs HNSW element counts (read-only; never opens a chromadb client)",
    )

    # daemon
    p_daemon = sub.add_parser("daemon", help="Manage the opt-in long-lived daemon")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_action")
    p_daemon_start = daemon_sub.add_parser("start", help="Start the daemon")
    p_daemon_start.add_argument(
        "--foreground",
        action="store_true",
        help="Run in the foreground for debugging or process supervisors",
    )
    p_daemon_start.add_argument(
        "--backend",
        default=None,
        help="Storage backend for this daemon (default: config/env/detected/chroma)",
    )
    daemon_sub.add_parser("stop", help="Stop the daemon")
    daemon_sub.add_parser("status", help="Show daemon status")
    p_daemon_jobs = daemon_sub.add_parser("jobs", help="List recent daemon jobs")
    p_daemon_jobs.add_argument("--limit", type=int, default=20, help="Max jobs to show")
    p_daemon_wait = daemon_sub.add_parser("wait", help="Wait for a daemon job")
    p_daemon_wait.add_argument("job_id", help="Job id returned by --background")

    # mcp
    p_mcp = sub.add_parser(
        "mcp",
        help="Show MCP setup command for connecting MemPalace to your AI client",
    )
    p_mcp.add_argument(
        "--backend",
        default=None,
        help="Storage backend to include in the MCP startup command",
    )

    # serve — turnkey remote HTTP MCP server (#1877)
    p_serve = sub.add_parser(
        "serve",
        help="Run a secure remote HTTP MCP server for a team to share one palace",
    )
    p_serve.add_argument(
        "--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for remote clients)"
    )
    p_serve.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    p_serve.add_argument(
        "--backend", default=None, help="Storage backend (default: config/env/detected)"
    )
    p_serve.add_argument("--palace", default=None, help="Palace path (overrides config/env)")
    p_serve.add_argument(
        "--token",
        default=None,
        help="Bearer token clients must present. Default: reuse/auto-generate one for "
        "non-loopback binds (stored 0600 under ~/.mempalace/server/).",
    )
    p_serve.add_argument("--tls-cert", default=None, help="PEM certificate to enable TLS")
    p_serve.add_argument("--tls-key", default=None, help="PEM private key matching --tls-cert")
    p_serve.add_argument(
        "--read-only",
        action="store_true",
        help="Expose recall only: tools that change state are hidden and refused",
    )
    p_serve.add_argument(
        "--allow-insecure",
        action="store_true",
        help="Permit a non-loopback bind with no token (only behind a trusted proxy)",
    )

    # status
    # migrate
    p_migrate = sub.add_parser(
        "migrate",
        help="Migrate palace from a different ChromaDB version (fixes 3.0.0 → 3.1.0 upgrade)",
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without changing anything",
    )
    p_migrate.add_argument(
        "--yes", action="store_true", help="Skip confirmation for destructive changes"
    )

    p_mig_pg = sub.add_parser(
        "migrate-to-postgres",
        help="Migrate a ChromaDB palace to Postgres (pgvector + AGE)",
    )
    p_mig_pg.add_argument(
        "--from",
        dest="from_palace",
        required=True,
        help="Path to source ChromaDB palace directory",
    )
    p_mig_pg.add_argument(
        "--to",
        dest="to_dsn",
        required=True,
        help="Postgres DSN of target (e.g. postgresql://user:pass@host/db)",
    )
    p_mig_pg.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Drawer batch size for the drawer-copy step (default 1000)",
    )
    p_mig_pg.add_argument(
        "--dry-run",
        action="store_true",
        help="Run preflight only; no writes to the target",
    )

    p_purge = sub.add_parser(
        "purge",
        help=(
            "Delete drawers by wing, room, and/or source-file "
            "(filtered delete via the resolved backend)"
        ),
    )
    p_purge.add_argument("--wing", help="Wing to purge")
    p_purge.add_argument("--room", help="Room to purge (without --wing, purges across ALL wings)")
    p_purge.add_argument(
        "--source-file",
        help="Source-file path to purge (matches metadata.source_file exactly)",
    )
    p_purge.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    p_prune = sub.add_parser(
        "prune",
        help="Delete drawers older than --stale-days N (dry-run unless --confirm)",
    )
    p_prune.add_argument(
        "--stale-days",
        type=int,
        required=True,
        help="Prune drawers whose filed_at is older than this many days",
    )
    p_prune.add_argument("--wing", help="Limit prune to this wing")
    p_prune.add_argument("--room", help="Limit prune to this room")
    p_prune.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete (without this flag, prune only reports a dry-run count)",
    )

    p_rename_wing = sub.add_parser(
        "rename-wing",
        help="Rename all drawers from one wing to another (atomic on postgres)",
    )
    p_rename_wing.add_argument("--from", dest="from_wing", required=True, help="Source wing name")
    p_rename_wing.add_argument("--to", dest="to_wing", required=True, help="Target wing name")
    p_rename_wing.add_argument(
        "--dry-run", action="store_true", help="Count matching drawers without renaming"
    )
    p_rename_wing.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Batch size for non-postgres backends (default: 500)",
    )

    # ── rooms — manage the canonical room set (hybrid-search-taxonomy follow-up) ────
    p_rooms = sub.add_parser(
        "rooms",
        help="Manage the canonical room set (mempalace_canonical_rooms postgres table)",
    )
    rooms_sub = p_rooms.add_subparsers(dest="rooms_cmd", required=True)
    # ── window / source: chronological and by-file listings (#500, #502) ──
    p_window = sub.add_parser(
        "window",
        help="Drawers in filed order between two timestamps (unranked, paged)",
        description=(
            "Walk a wing chronologically: an unranked listing in filed order. "
            "Unlike `search --since`, which filters a RANKED search and so returns "
            "whatever scores highest inside the window, this returns the sequence. "
            "--from is inclusive, --to exclusive, wall-clock -- the same contract as "
            "`list --since`, evaluated in SQL instead of after fetching every row. "
            "Pages with --cursor. Requires palace-daemon >= " + _WINDOW_MIN_DAEMON + "."
        ),
    )
    p_window.add_argument("--wing", help="Wing to walk")
    p_window.add_argument("--room", help="Restrict to one room")
    p_window.add_argument("--from", dest="since", help="Inclusive ISO start (e.g. 2026-09-01)")
    p_window.add_argument("--to", dest="before", help="Exclusive ISO end")
    p_window.add_argument("--source-file", dest="source_file", help="Restrict to one source file")
    p_window.add_argument("--limit", type=int, default=100, help="Page size (max 1000)")
    p_window.add_argument("--cursor", help="Continue from a previous page's cursor")
    p_window.add_argument(
        "--format",
        dest="json",
        action="store_const",
        const=True,
        default=False,
        help="Machine output (use --format json)",
    )

    p_source = sub.add_parser(
        "source",
        help="Every drawer from one source file, in chunk order",
        description=(
            "Search hits carry a source_file; this lists that file's drawers in "
            "order -- chunk_index numerically (so chunk 10 follows chunk 2), then "
            "filed time. Requires palace-daemon >= " + _WINDOW_MIN_DAEMON + "."
        ),
    )
    p_source.add_argument("--file", required=False, help="Transcript path or basename")
    p_source.add_argument("--wing", help="Restrict to one wing")
    p_source.add_argument("--limit", type=int, default=1000, help="Max drawers (max 1000)")
    p_source.add_argument(
        "--format",
        dest="json",
        action="store_const",
        const=True,
        default=False,
        help="Machine output (use --format json)",
    )

    rooms_sub.add_parser("list", help="List all canonical rooms with descriptions")
    p_rooms_add = rooms_sub.add_parser("add", help="Add a new canonical room")
    p_rooms_add.add_argument("name", help="Room slug (lowercase snake_case)")
    p_rooms_add.add_argument(
        "--description",
        default="",
        help="Human-readable description",
    )
    p_rooms_rename = rooms_sub.add_parser(
        "rename", help="Rename a canonical room (cascades to all drawers via ON UPDATE CASCADE)"
    )
    p_rooms_rename.add_argument("old", help="Current room name")
    p_rooms_rename.add_argument("new", help="New room name")
    p_rooms_remove = rooms_sub.add_parser(
        "remove", help="Remove a canonical room (fails if any drawers still in it)"
    )
    p_rooms_remove.add_argument("name", help="Room slug to remove")

    # migrate-wings
    p_migrate_wings = sub.add_parser(
        "migrate-wings",
        help="Normalize legacy wing names (strip leading/trailing separators) so pre-#1675 palaces stay discoverable",
    )
    p_migrate_wings.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying the palace",
    )
    p_migrate_wings.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    p_hallways = sub.add_parser("hallways", help="List entity hallways (associative graph)")
    p_hallways.add_argument("--wing", default=None, help="Filter to one wing")
    p_hallways.add_argument("--limit", type=int, default=50, help="Max hallways to show")
    p_hallways.add_argument(
        "--prune-spellings",
        action="store_true",
        help="Remove hallways older mines wrote per entity spelling: self-links "
        "(main.zig / src/main.zig) and duplicate spellings of one association; "
        "dry run unless --yes",
    )
    p_hallways.add_argument("--yes", action="store_true", help="Apply --prune-spellings")
    p_hallways.add_argument(
        "--rebuild",
        action="store_true",
        help="Recompute hallways from drawer entities for --wing (or every wing); use after "
        "`wings split`, which drops the split wing's records",
    )
    p_audit = sub.add_parser(
        "audit",
        help="Score how well organized the palace is (rooms, naming, tunnels, hallways, KG)",
    )
    p_audit.add_argument("--json", action="store_true", help="Emit the full report as JSON")
    p_audit.add_argument(
        "--quiet", action="store_true", help="Suppress the per-step progress lines on stderr"
    )
    p_audit.add_argument(
        "--fail-under",
        type=int,
        default=None,
        metavar="SCORE",
        help="Exit 2 when the overall score is below SCORE (for CI or cron checks)",
    )
    p_status = sub.add_parser("status", help="Show what's been filed")
    p_status.add_argument(
        "--backend",
        default=None,
        help="Storage backend to use for status (default: config/env/detected/chroma)",
    )
    p_doctor = sub.add_parser(
        "doctor", help="Health-check the memory workflow (bridge, daemon, wing, hooks, replay)"
    )
    p_doctor.add_argument("--wing", default=None, help="Wing to check (default: cwd basename)")
    p_doctor.add_argument(
        "--curated",
        action="store_true",
        help=(
            "Also check whether this project's CLAUDE.md / docs/*.md are newer "
            "than what the palace indexed (one extra daemon request)"
        ),
    )
    p_update = sub.add_parser("update", help="Opt-in release checks and upgrade planning")
    update_sub = p_update.add_subparsers(dest="update_action")
    p_update_configure = update_sub.add_parser("configure", help="Configure periodic checks")
    update_consent = p_update_configure.add_mutually_exclusive_group(required=True)
    update_consent.add_argument("--enable", dest="enabled", action="store_true")
    update_consent.add_argument("--disable", dest="enabled", action="store_false")
    p_update_configure.add_argument("--interval-days", type=int, default=7)
    p_update_configure.add_argument("--installer", choices=("uv-tool", "pipx", "pip"))
    update_sub.add_parser("check", help="Explicitly check the latest stable release")
    p_update_plan = update_sub.add_parser(
        "plan", help="Show the exact upgrade plan without applying it"
    )
    p_update_plan.add_argument("--installer", choices=("uv-tool", "pipx", "pip"))

    # logstream (RFC 003 agent coordination)
    p_logstream = sub.add_parser(
        "logstream",
        help="Agent coordination events — delegate work, wait for replies (RFC 003)",
    )
    logstream_sub = p_logstream.add_subparsers(dest="logstream_action")

    def _add_logstream_filters(p):
        p.add_argument("--stream", default=None, help="Stream, e.g. project/mempalace")
        p.add_argument("--room", default=None, help="Room, e.g. delegation, patches")
        p.add_argument("--topic", default=None, help="Topic, e.g. auth-v2")
        p.add_argument("--type", default=None, help="Event type, e.g. task.request")
        p.add_argument("--to-agent", default=None, help="Target agent (also matches '*')")
        p.add_argument("--from-agent", default=None, help="Writer agent")
        p.add_argument("--correlation-id", default=None, help="Task/conversation id")
        p.add_argument(
            "--status",
            default=None,
            help="open|claimed|ready|applied|blocked|failed|superseded",
        )
        p.add_argument("--since-event-id", default=None, help="Only events strictly after this id")
        p.add_argument(
            "--since-created-at",
            default=None,
            help="Only events at/after this time (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)",
        )

    p_ls_append = logstream_sub.add_parser("append", help="Append a coordination event")
    p_ls_append.add_argument("--type", required=True, help="Event type, e.g. task.request")
    p_ls_append.add_argument("--stream", required=True, help="Stream, e.g. project/mempalace")
    p_ls_append.add_argument("--room", required=True, help="Room, e.g. delegation")
    p_ls_append.add_argument("--topic", default=None, help="Topic name, e.g. auth-v2")
    p_ls_append.add_argument("--from-agent", required=True, help="Writer agent identity")
    p_ls_append.add_argument("--to-agent", default=None, help="Target agent or '*'")
    p_ls_append.add_argument("--correlation-id", default=None, help="Task/conversation id")
    p_ls_append.add_argument("--branch", default=None, help="Git branch")
    p_ls_append.add_argument("--base-commit", default=None, help="Git commit work started from")
    p_ls_append.add_argument(
        "--status",
        default=None,
        help="open|claimed|ready|applied|blocked|failed|superseded",
    )
    p_ls_append.add_argument("--body", default=None, help="Verbatim body text")
    p_ls_append.add_argument(
        "--body-file", default=None, help="Read body from file ('-' for stdin)"
    )
    p_ls_append.add_argument("--metadata", default=None, help="Extra fields as a JSON object")
    p_ls_append.add_argument(
        "--artifact-id",
        action="append",
        default=None,
        help="Reference an already-stored artifact (repeatable)",
    )
    p_ls_append.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_list = logstream_sub.add_parser("list", help="List events")
    _add_logstream_filters(p_ls_list)
    p_ls_list.add_argument(
        "--before-event-id",
        default=None,
        help="Only events strictly before this id in append order",
    )
    p_ls_list.add_argument(
        "--order",
        choices=["asc", "desc"],
        default="asc",
        help="asc (oldest first, default) or desc (newest first)",
    )
    p_ls_list.add_argument("--limit", type=int, default=50, help="Max events (default 50)")
    p_ls_list.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_wait = logstream_sub.add_parser(
        "wait", help="Block until a matching event exists (exit 2 on timeout)"
    )
    _add_logstream_filters(p_ls_wait)
    p_ls_wait.add_argument(
        "--timeout-ms",
        type=int,
        default=60000,
        help="How long to wait in ms (default 60000, max 300000)",
    )
    p_ls_wait.add_argument(
        "--limit", type=int, default=50, help="Max events to return on match (default 50)"
    )
    p_ls_wait.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_watch = logstream_sub.add_parser(
        "watch",
        help="Background watcher: block until interesting events arrive, then wake (exit 2 on idle)",
    )
    p_ls_watch.add_argument(
        "--agent",
        default=None,
        help=(
            "Your identity. Shorthand for --to-agent <id> --exclude-from-agent <id>: "
            "wake for what is addressed to you (broadcasts included) but never for "
            "your own events"
        ),
    )
    p_ls_watch.add_argument(
        "--stream", action="append", default=None, help="Stream (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--room", action="append", default=None, help="Room (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--topic", action="append", default=None, help="Topic (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--type", action="append", default=None, help="Event type (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--status", action="append", default=None, help="Status (repeatable; matches any)"
    )
    p_ls_watch.add_argument(
        "--to-agent", action="append", default=None, help="Target agent (repeatable; '*' matches)"
    )
    p_ls_watch.add_argument(
        "--from-agent", action="append", default=None, help="Writer agent (repeatable)"
    )
    p_ls_watch.add_argument(
        "--exclude-from-agent",
        action="append",
        default=None,
        help="Never wake for events written by this agent (repeatable)",
    )
    p_ls_watch.add_argument(
        "--correlation-id", action="append", default=None, help="Correlation id (repeatable)"
    )
    p_ls_watch.add_argument(
        "--since-event-id",
        default=None,
        help="Start strictly after this event id (overrides --state-file)",
    )
    p_ls_watch.add_argument(
        "--state-file",
        default=None,
        help=(
            "Persist the cursor here so a restart resumes exactly where it stopped. "
            "When omitted with --agent, defaults to "
            "~/.mempalace/watch/<agent>.json with '_' doubled and ':' sanitized to '_'"
        ),
    )
    p_ls_watch.add_argument(
        "--from-start",
        action="store_true",
        help=(
            "Replay the log from the beginning when there is no cursor "
            "(default: start at the tip, like the SSE live-tail)"
        ),
    )
    p_ls_watch.add_argument(
        "--follow",
        action="store_true",
        help="Keep watching after a match instead of exiting on the first one",
    )
    p_ls_watch.add_argument(
        "--idle-exit-ms",
        type=int,
        default=0,
        help="Give up after this long with no match (0 = wait forever)",
    )
    p_ls_watch.add_argument(
        "--poll-timeout-ms",
        type=int,
        default=300000,
        help="Long-poll length per iteration (default 300000, the server maximum)",
    )
    p_ls_watch.add_argument(
        "--limit", type=int, default=50, help="Max events per poll (default 50)"
    )
    p_ls_watch.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_ack = logstream_sub.add_parser(
        "ack", help="Acknowledge an event (appends event.ack, never mutates)"
    )
    p_ls_ack.add_argument("event_id", help="Event id to acknowledge")
    p_ls_ack.add_argument("--from-agent", required=True, help="Acknowledging agent identity")
    p_ls_ack.add_argument(
        "--status",
        default=None,
        help="open|claimed|ready|applied|blocked|failed|superseded",
    )
    p_ls_ack.add_argument(
        "--topic", default=None, help="Topic override (defaults to target event's topic)"
    )
    p_ls_ack.add_argument("--body", default=None, help="Verbatim ack notes")
    p_ls_ack.add_argument("--json", action="store_true", help="Machine-readable output")

    p_ls_sync = logstream_sub.add_parser(
        "sync", help="Pull missing events/artifacts from peer replicas (RFC 004)"
    )
    p_ls_sync.add_argument(
        "--peer", default=None, help="Peer base URL (default: all peers in peers.json)"
    )
    p_ls_sync.add_argument("--token", default=None, help="Bearer token for --peer")
    p_ls_sync.add_argument("--json", action="store_true", help="Machine-readable output")

    # task — guided logstream task lifecycle
    p_task = sub.add_parser("task", help="Create or run complete agent tasks over the logstream")
    task_sub = p_task.add_subparsers(dest="task_action")

    p_task_create = task_sub.add_parser(
        "create", help="Create a canonical task.request and print a pasteable handoff"
    )
    p_task_create.add_argument("--project", required=True, help="Project name for routing")
    p_task_create.add_argument("--from-agent", required=True, help="Requesting agent identity")
    p_task_create.add_argument("--to-agent", required=True, help="Worker agent identity")
    task_goal = p_task_create.add_mutually_exclusive_group(required=True)
    task_goal.add_argument("--goal", default=None, help="Verbatim task goal")
    task_goal.add_argument(
        "--goal-file", default=None, help="Read the task goal from a file ('-' for stdin)"
    )
    p_task_create.add_argument("--branch", required=True, help="Git branch for the work")
    p_task_create.add_argument(
        "--base-commit",
        required=True,
        help="Immutable hexadecimal commit id the worker must start from (not a branch or tag)",
    )
    task_done = p_task_create.add_mutually_exclusive_group(required=True)
    task_done.add_argument("--done", default=None, help="Verbatim definition of done")
    task_done.add_argument(
        "--done-file",
        default=None,
        help="Read the definition of done from a file ('-' for stdin)",
    )
    p_task_create.add_argument("--json", action="store_true", help="Machine-readable output")

    p_task_launch = task_sub.add_parser(
        "launch", help="Run an existing task through a supported headless coding agent"
    )
    task_source = p_task_launch.add_mutually_exclusive_group(required=True)
    task_source.add_argument("correlation_id", nargs="?", help="Task correlation id")
    task_source.add_argument(
        "--task-file",
        default=None,
        help="Exact task.request event JSON fetched through remote MCP",
    )
    p_task_launch.add_argument(
        "--runner",
        required=True,
        choices=tuple(_TASK_RUNNER_ADAPTERS),
        help="Headless agent runner",
    )
    p_task_launch.add_argument("--workspace", required=True, help="Trusted workspace directory")
    p_task_launch.add_argument(
        "--agent",
        default=None,
        help="Worker identity (required for broadcasts; must match addressed tasks)",
    )
    p_task_launch.add_argument("--json", action="store_true", help="Machine-readable errors")

    # artifact (RFC 003 exact content exchange)
    p_artifact = sub.add_parser(
        "artifact", help="Exact artifact exchange for agent handoffs (RFC 003)"
    )
    artifact_sub = p_artifact.add_subparsers(dest="artifact_action")

    p_art_put = artifact_sub.add_parser("put", help="Store exact artifact content")
    p_art_put.add_argument("--kind", required=True, help="patch|file|log|json|note")
    p_art_put.add_argument("--created-by", required=True, help="Writer agent identity")
    p_art_put.add_argument("--content", default=None, help="Inline content")
    p_art_put.add_argument(
        "--file", default=None, help="Read content from file ('-' for stdin; default stdin)"
    )
    p_art_put.add_argument("--metadata", default=None, help="Extra fields as a JSON object")
    p_art_put.add_argument("--json", action="store_true", help="Machine-readable output")

    p_art_get = artifact_sub.add_parser(
        "get", help="Fetch exact artifact content (stdout pipes into git apply)"
    )
    p_art_get.add_argument("artifact_id", help="Artifact id")
    p_art_get.add_argument("--out", default=None, help="Write content to this file instead")
    p_art_get.add_argument(
        "--json", action="store_true", help="Metadata as JSON (content omitted with --out)"
    )

    p_palace = sub.add_parser("palace", help="Palace maintenance commands")
    palace_sub = p_palace.add_subparsers(dest="palace_action")
    p_set_embedder = palace_sub.add_parser(
        "set-embedder",
        help="Record/override the palace's embedder identity (resolve 'unknown', or switch models)",
    )
    p_set_embedder.add_argument(
        "--model",
        default=None,
        help="Embedder model to record (default: current configured model). "
        "Records identity on the palace only; does not change the configured "
        "model (prints how to align MEMPALACE_EMBEDDING_MODEL if they differ).",
    )
    p_set_embedder.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing identity that names a different model "
        "(only if you know the stored vectors are compatible)",
    )
    p_set_embedder.add_argument(
        "--backend",
        default=None,
        help="Storage backend (default: config/env/detected/chroma)",
    )

    # `pending drain` is the primary name; `replay` stays as a warning alias
    # because scripts and a cron entry call it. The old name read as
    # "replay history" and the command posts mine jobs (#498).
    _PENDING_DESC = (
        "Drain the pending MINE queue: re-post each request in "
        "~/.mempalace/pending/ to the daemon as a mine job. Prints a plan and "
        "posts nothing unless --yes is given."
    )
    p_pending = sub.add_parser(
        "pending",
        help="Inspect or drain the pending mine queue (~/.mempalace/pending/)",
        description=_PENDING_DESC,
    )
    pending_sub = p_pending.add_subparsers(dest="pending_action")
    p_pending_drain = pending_sub.add_parser(
        "drain",
        help="Re-post queued mine requests to the daemon (plan only without --yes)",
        description=_PENDING_DESC,
    )
    p_pending_drain.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Actually post the queued requests (without this, only a plan is printed)",
    )

    p_replay = sub.add_parser(
        "replay",
        help="Deprecated alias for `pending drain` — drains the pending MINE queue",
        description=_PENDING_DESC + " Deprecated alias for `mempalace pending drain`.",
    )
    p_replay.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Actually post the queued requests (without this, only a plan is printed)",
    )

    p_reconcile = sub.add_parser(
        "reconcile-docs",
        help="Queue a mine for every curated doc the palace has not seen",
        description=(
            "Compare a repository's curated docs (CLAUDE.md, docs/**/*.md) with "
            "what the palace recorded, and queue a background single-file "
            "projects-mode mine for anything newer or absent. The curated-docs "
            "hook is event-driven and misses docs that arrive by merge or pull, "
            "that are written in a linked worktree, or whose write it cannot "
            "attribute; this enumerates the filesystem instead, so a doc missed "
            "by any of those routes is caught on the next run."
        ),
    )
    p_reconcile.add_argument(
        "repo_root",
        help="The project's MAIN checkout (a linked worktree is refused: the "
        "palace host does not carry one)",
    )
    p_reconcile.add_argument(
        "--wing",
        help="Override the wing (default: the project directory name, as the hook derives it)",
    )
    p_reconcile.add_argument(
        "--force",
        action="store_true",
        help="Queue even when the palace recorded nothing for the wing (normally "
        "refused: it usually means a wrong --wing)",
    )
    p_reconcile.add_argument(
        "--dry-run",
        action="store_true",
        help="Say what would be queued and queue nothing",
    )

    p_mined = sub.add_parser(
        "mined",
        help="List mined source files grouped by wing (companion to status, which groups by room)",
    )
    p_mined.add_argument("--wing", help="Show only this wing")

    def _nonneg_int(value: str) -> int:
        # Reject negative --limit values; argparse's bare type=int would
        # silently accept e.g. -1 and produce nonsensical "... -2 more"
        # output (Copilot finding on jphein/mempalace#4).
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"expected non-negative integer, got {value!r}")
        if n < 0:
            raise argparse.ArgumentTypeError(f"--limit must be >= 0 (got {n})")
        return n

    p_mined.add_argument(
        "--limit",
        type=_nonneg_int,
        default=50,
        help="Show at most this many sources per wing (default 50; 0 means show all)",
    )

    # stats — palace analytics dashboard (#191)
    p_stats = sub.add_parser(
        "stats",
        help="Palace analytics dashboard (wings, rooms, knowledge graph, tunnels, tags)",
    )
    p_stats.add_argument(
        "--top",
        type=_nonneg_int,
        default=10,
        help="Show at most this many rows per section (default 10; 0 means show all)",
    )
    p_stats.add_argument(
        "--tags",
        action="store_true",
        help="Include the tag-count breakdown (extra daemon call)",
    )
    p_stats.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )
    p_stats.add_argument(
        "--section",
        choices=_STATS_VALID_SECTIONS,
        default="all",
        help=(
            "Limit table output to one block: kg (entities/triples), "
            "graph (rooms/tunnels/edges), status (wings/rooms/drawer counts), "
            "or all (default). json mode always passes through every block."
        ),
    )
    p_stats.add_argument(
        "--no-relationship-types",
        action="store_true",
        dest="no_relationship_types",
        help=(
            "Suppress the relationship_types list (can be 1000+ entries). "
            "Table mode shows the count only; json replaces the list with "
            "{'relationship_types_count': N}."
        ),
    )

    # tags — direct-to-daemon wrapper around mempalace_list_tags (slice of #191)
    p_tags = sub.add_parser(
        "tags",
        help="List tags with drawer counts (daemon mempalace_list_tags fast-path)",
    )
    p_tags.add_argument("--wing", default=None, help="Scope tag counts to one wing")
    p_tags.add_argument("--room", default=None, help="Scope tag counts to one room")
    p_tags.add_argument(
        "--min-count",
        dest="min_count",
        type=_nonneg_int,
        default=1,
        help="Drop tags below this drawer-count floor (default: 1)",
    )
    p_tags.add_argument(
        "--top",
        type=_nonneg_int,
        default=20,
        help="Show at most this many rows (default 20; 0 means show all)",
    )
    p_tags.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help=(
            "Output format: table (default, count + gauge), "
            "json (daemon pass-through; same as --json)"
        ),
    )

    # overlap — cross-wing entity overlap via /cypher (slice of #191)
    p_overlap = sub.add_parser(
        "overlap",
        help="Find entities that appear in BOTH of two wings (KG cross-wing query)",
    )
    p_overlap.add_argument("wing_a", help="First wing")
    p_overlap.add_argument("wing_b", help="Second wing")
    p_overlap.add_argument(
        "--limit",
        type=_nonneg_int,
        default=_OVERLAP_DEFAULT_LIMIT,
        help=(
            f"Cap on returned entity count (default: {_OVERLAP_DEFAULT_LIMIT}, "
            f"max: {_OVERLAP_MAX_LIMIT}). Daemon statement_timeout is the real ceiling."
        ),
    )
    p_overlap.add_argument(
        "--graph",
        default=_CYPHER_DEFAULT_GRAPH,
        help=f"AGE graph name (default: {_CYPHER_DEFAULT_GRAPH})",
    )
    p_overlap.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help=(
            "Output format: table (default, aligned columns), "
            "json (daemon envelope pass-through; same as --json)"
        ),
    )

    # why — explain a drawer (slice of #191)
    p_why = sub.add_parser(
        "why",
        help="Explain why a drawer would surface — location, tags, entities, neighbors",
    )
    p_why.add_argument("drawer_id", help="Drawer ID to explain")
    p_why.add_argument(
        "--neighbors",
        type=_nonneg_int,
        default=_WHY_DEFAULT_NEIGHBORS,
        help=(
            f"Semantic neighbor count (default {_WHY_DEFAULT_NEIGHBORS}, max {_WHY_MAX_NEIGHBORS})"
        ),
    )
    p_why.add_argument(
        "--entities",
        type=_nonneg_int,
        default=_WHY_DEFAULT_ENTITIES,
        help=(
            f"Top-N entities by mention count (default {_WHY_DEFAULT_ENTITIES}, "
            f"max {_WHY_MAX_ENTITIES})"
        ),
    )
    p_why.add_argument(
        "--graph",
        default=_CYPHER_DEFAULT_GRAPH,
        help=f"AGE graph name for the MENTIONS query (default: {_CYPHER_DEFAULT_GRAPH})",
    )
    p_why.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help=(
            "Output format: table (default, three-block report), "
            "json (full structured payload; same as --json)"
        ),
    )

    # tunnels — list cross-wing tunnels (slice of #191)
    p_tunnels = sub.add_parser(
        "tunnels",
        help="List cross-wing tunnels (daemon mempalace_list_tunnels fast-path)",
    )
    p_tunnels.add_argument("--wing", default=None, help="Filter to tunnels touching one wing")
    p_tunnels.add_argument(
        "--all",
        action="store_true",
        help=(
            "With --rebuild: refuse, with the measured reason. Accepted rather than "
            "rejected by argparse so the answer explains itself (see #442)"
        ),
    )
    p_tunnels.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Recompute one wing's derived graph (topic tunnels, hallways, entity "
            "tunnels) instead of listing. Requires --wing; runs locally, no daemon "
            "needed. This is THE way to refresh the graph on purpose — since #474 "
            "it otherwise refreshes only as a side effect of a full-directory mine "
            "that files drawers. No --all by design: a whole-palace sweep rewrites "
            "hallways.json once per wing (~37 min across 50 wings at its current "
            "size), which is the problem this verb exists to fix"
        ),
    )
    p_tunnels.add_argument(
        "--passive",
        action="store_true",
        default=False,
        help="Include passive tunnels (rooms appearing in 2+ wings) — default off",
    )
    p_tunnels.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help=(
            "Output format: table (default, aligned columns), "
            "json (daemon pass-through; same as --json)"
        ),
    )

    # drawer — single-drawer CRUD by ID (#355)
    p_drawer = sub.add_parser(
        "drawer",
        help="Get / add / delete / update ONE drawer by ID",
    )
    drawer_sub = p_drawer.add_subparsers(dest="drawer_action")

    p_drawer_get = drawer_sub.add_parser("get", help="Fetch one drawer verbatim by ID")
    p_drawer_get.add_argument("drawer_id", help="ID of the drawer to fetch")
    p_drawer_get.add_argument(
        "--format",
        choices=("table", "json", "content"),
        default=None,
        help=(
            "Output format: table (default, metadata header + verbatim body), "
            "json (tool pass-through; same as --json), "
            "content (body only, byte-exact — for piping)"
        ),
    )
    _add_drawer_output_flags(p_drawer_get)

    p_drawer_add = drawer_sub.add_parser("add", help="File verbatim content as a new drawer")
    p_drawer_add.add_argument("--wing", required=True, help="Wing to file under")
    p_drawer_add.add_argument("--room", required=True, help="Room to file under")
    p_drawer_add.add_argument("--content", default=None, help="Verbatim content to store")
    p_drawer_add.add_argument(
        "--content-file",
        dest="content_file",
        default=None,
        help="Read content from a file ('-' for stdin) — byte-exact",
    )
    p_drawer_add.add_argument(
        "--source", default=None, help="Where this came from (stored as source_file)"
    )
    p_drawer_add.add_argument(
        "--added-by",
        dest="added_by",
        default="cli",
        help="Who is filing this (default: cli)",
    )
    p_drawer_add.add_argument(
        "--tag",
        action="append",
        default=None,
        help="Tag for the drawer (repeatable). Omit to let the palace auto-extract tags.",
    )
    p_drawer_add.add_argument(
        "--no-tags",
        dest="no_tags",
        action="store_true",
        default=False,
        help="File with an empty tag list, suppressing auto-extraction",
    )
    _add_drawer_format_flag(p_drawer_add)
    _add_drawer_output_flags(p_drawer_add)

    p_drawer_delete = drawer_sub.add_parser("delete", help="Delete one drawer by ID (irreversible)")
    p_drawer_delete.add_argument("drawer_id", help="ID of the drawer to delete")
    p_drawer_delete.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Skip the confirmation prompt (required to delete in a non-interactive shell)",
    )
    _add_drawer_format_flag(p_drawer_delete)
    _add_drawer_output_flags(p_drawer_delete)

    p_drawer_update = drawer_sub.add_parser(
        "update",
        help="Update one drawer's wing / room / tags (metadata only — never content)",
    )
    p_drawer_update.add_argument("drawer_id", help="ID of the drawer to update")
    p_drawer_update.add_argument("--wing", default=None, help="New wing (omit to leave unchanged)")
    p_drawer_update.add_argument("--room", default=None, help="New room (omit to leave unchanged)")
    p_drawer_update.add_argument(
        "--tag",
        action="append",
        default=None,
        help="Replace the drawer's tags with these (repeatable)",
    )
    p_drawer_update.add_argument(
        "--clear-tags",
        dest="clear_tags",
        action="store_true",
        default=False,
        help="Remove every tag from the drawer",
    )
    _add_drawer_format_flag(p_drawer_update)
    _add_drawer_output_flags(p_drawer_update)

    # duplicate — pre-ingest duplicate detection (#363)
    p_duplicate = sub.add_parser(
        "duplicate",
        help="Check whether content is already filed in the palace",
    )
    duplicate_sub = p_duplicate.add_subparsers(dest="duplicate_action")

    p_dup_check = duplicate_sub.add_parser("check", help="Semantic duplicate check for content")
    p_dup_check.add_argument("--content", default=None, help="Content to check")
    p_dup_check.add_argument(
        # #363 proposes --file; --content-file is accepted too so muscle
        # memory from `drawer add` (#355's spelling) works here as well.
        "--file",
        "--content-file",
        dest="content_file",
        default=None,
        help="Read the content to check from a file ('-' for stdin)",
    )
    p_dup_check.add_argument(
        "--threshold",
        type=_unit_float,
        default=None,
        help=(
            "Similarity threshold, 0-1 "
            f"(default {_DUPLICATE_DEFAULT_THRESHOLD}); higher is stricter"
        ),
    )
    p_dup_check.add_argument(
        "--fail-on-duplicate",
        dest="fail_on_duplicate",
        action="store_true",
        default=False,
        help="Exit 1 when a duplicate is found, for use as a pre-ingest guard",
    )
    _add_drawer_format_flag(p_dup_check)
    _add_drawer_output_flags(p_dup_check)

    # wings — list every wing with drawer counts (slice of #191, issue #356)
    p_wings = sub.add_parser(
        "wings",
        help="List all wings with drawer counts (mempalace_list_wings)",
    )
    p_wings.add_argument(
        "--sort",
        choices=_WINGS_SORT_CHOICES,
        default="name",
        help="Row order: name (default, alphabetical) or count (drawers descending)",
    )
    p_wings.add_argument(
        "--limit",
        type=_nonneg_int,
        default=0,
        help="Show at most N wings (0 = all, the default). Most useful with --sort count.",
    )
    p_wings.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help=(
            "Output format: table (default, wing/drawers/share columns), "
            'json ({"wings": {...}} tool envelope; same as --json)'
        ),
    )

    # hallway — list/delete within-wing entity hallways (slice of #191, issue #358)
    p_hallway = sub.add_parser(
        "hallway",
        help=(
            "Inspect or delete entity hallways (daemon-routed superset of the "
            "legacy local-only `hallways` verb)"
        ),
    )
    sub_hallway = p_hallway.add_subparsers(dest="hallway_action")
    p_hallway_list = sub_hallway.add_parser("list", help="List hallways (mempalace_list_hallways)")
    p_hallway_list.add_argument("--wing", default=None, help="Filter to hallways in one wing")
    p_hallway_list.add_argument(
        "--limit",
        type=_nonneg_int,
        default=_HALLWAY_DEFAULT_LIMIT,
        help=f"Rows to print (0 = all; default {_HALLWAY_DEFAULT_LIMIT})",
    )
    p_hallway_delete = sub_hallway.add_parser(
        "delete", help="Delete one hallway by id (mempalace_delete_hallway)"
    )
    p_hallway_delete.add_argument("hallway_id", help="Hallway ID to delete")
    p_hallway_delete.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Required to actually delete; without it the command prompts (TTY) or refuses",
    )
    # The --json/--quiet propagation loop below skips two-level parents,
    # so the leaf parsers declare the output flags themselves.
    for _leaf in (p_hallway_list, p_hallway_delete):
        _leaf.add_argument(
            "--format",
            choices=("table", "json"),
            default=None,
            help="Output format: table (default) or json (same as --json)",
        )
        _leaf.add_argument("--json", "-j", dest="json", action="store_true", default=False)
        _leaf.add_argument("--quiet", "-q", dest="quiet", action="store_true", default=False)

    # checkpoint — batch session save (slice of #191, issue #360)
    p_checkpoint = sub.add_parser(
        "checkpoint",
        help="File a batch of drawers plus one diary entry in a single call",
        description=(
            "Wraps mempalace_checkpoint. The tool takes "
            "{items:[{wing,room,content}], diary?, dedup_threshold?, added_by?} — "
            "there is no session id and nothing to list, so issue #360's proposed "
            "--session / `checkpoint list` shapes are not implementable."
        ),
    )
    p_checkpoint.add_argument(
        "--items-file",
        dest="items_file",
        default=None,
        help="JSON array of {wing, room, content} objects; '-' reads stdin",
    )
    p_checkpoint.add_argument("--wing", default=None, help="Single-item form: target wing")
    p_checkpoint.add_argument("--room", default=None, help="Single-item form: target room")
    p_checkpoint.add_argument(
        "--content", default=None, help="Single-item form: verbatim content to file"
    )
    p_checkpoint.add_argument(
        "--content-file",
        dest="content_file",
        default=None,
        help="Single-item form: read content from a file ('-' reads stdin)",
    )
    p_checkpoint.add_argument(
        "--diary-agent", dest="diary_agent", default=None, help="Diary entry author"
    )
    p_checkpoint.add_argument(
        "--diary-entry", dest="diary_entry", default=None, help="Diary entry text (AAAK format)"
    )
    p_checkpoint.add_argument(
        "--diary-entry-file",
        dest="diary_entry_file",
        default=None,
        help="Read the diary entry from a file ('-' reads stdin)",
    )
    p_checkpoint.add_argument(
        "--diary-topic", dest="diary_topic", default=None, help="Diary topic tag"
    )
    p_checkpoint.add_argument(
        "--diary-wing", dest="diary_wing", default=None, help="Diary target wing"
    )
    p_checkpoint.add_argument(
        "--dedup-threshold",
        dest="dedup_threshold",
        type=float,
        default=_CHECKPOINT_DEFAULT_THRESHOLD,
        help=(
            "Similarity threshold 0-1 for the per-item duplicate check "
            f"(default {_CHECKPOINT_DEFAULT_THRESHOLD})"
        ),
    )
    p_checkpoint.add_argument(
        "--added-by",
        dest="added_by",
        default=None,
        help="Attribution for the filed drawers (falls back to the diary agent, then 'checkpoint')",
    )
    p_checkpoint.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Validate and print the payload that would be sent; write nothing",
    )
    p_checkpoint.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format: table (default, per-item report) or json (same as --json)",
    )

    # taxonomy / aaak — reference reads (slice of #191, issue #362)
    p_taxonomy = sub.add_parser(
        "taxonomy",
        help="Print the wing → room → drawer-count tree (mempalace_get_taxonomy)",
    )
    p_taxonomy.add_argument(
        "--wing",
        default=None,
        help="Show only this wing (applied client-side; the tool takes no arguments)",
    )
    p_taxonomy.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format: table (default, indented tree) or json (same as --json)",
    )

    p_aaak = sub.add_parser(
        "aaak",
        help="AAAK dialect reference (mempalace_get_aaak_spec)",
    )
    sub_aaak = p_aaak.add_subparsers(dest="aaak_action")
    p_aaak_spec = sub_aaak.add_parser("spec", help="Print the AAAK dialect specification")
    p_aaak_spec.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format: table (default, raw spec text) or json (same as --json)",
    )
    p_aaak_spec.add_argument("--json", "-j", dest="json", action="store_true", default=False)
    p_aaak_spec.add_argument("--quiet", "-q", dest="quiet", action="store_true", default=False)

    # diary — agent diary write/read (slice of #191, issue #354)
    p_diary = sub.add_parser(
        "diary",
        help="Write or read agent diary entries (mempalace_diary_write / _read)",
    )
    diary_sub = p_diary.add_subparsers(dest="diary_action")
    p_diary_write = diary_sub.add_parser("write", help="File a diary entry")
    p_diary_write.add_argument(
        "entry",
        nargs="?",
        default=None,
        help="Entry text (AAAK encouraged). Omit or pass '-' to read stdin.",
    )
    p_diary_write.add_argument(
        "--agent",
        default=None,
        help="Agent name whose diary this is (default: $MEMPALACE_AGENT_NAME)",
    )
    p_diary_write.add_argument("--topic", default=None, help="Topic tag (default: general)")
    p_diary_write.add_argument(
        "--wing",
        default=None,
        help="Target wing (default: wing_<agent>)",
    )
    p_diary_write.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        help="Session id to stamp on the entry (optional)",
    )
    p_diary_write.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )
    p_diary_write.add_argument(
        "--json", "-j", dest="json", action="store_true", default=False, help=argparse.SUPPRESS
    )
    p_diary_read = diary_sub.add_parser("read", help="Read recent diary entries")
    p_diary_read.add_argument(
        "--agent",
        default=None,
        help=(
            "Agent whose diary to read (default: $MEMPALACE_AGENT_NAME). "
            "Optional — omit it to read every agent's entries; "
            "'mempalace diary agents' lists the names present."
        ),
    )
    p_diary_read.add_argument(
        "--limit",
        type=int,
        default=_DIARY_DEFAULT_LIMIT,
        help=f"Entries to show (default {_DIARY_DEFAULT_LIMIT}, max {_DIARY_MAX_LIMIT})",
    )
    p_diary_read.add_argument(
        "--wing",
        default=None,
        help="Read from one wing only (default: every wing in scope)",
    )
    p_diary_read.add_argument(
        "--topic",
        default=None,
        help="Only entries with this topic (filtered client-side — the tool has no topic filter)",
    )
    p_diary_read.add_argument(
        "--since",
        default=None,
        help="Only entries at or after this date (YYYY-MM-DD, filtered client-side)",
    )
    p_diary_read.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )
    p_diary_read.add_argument(
        "--json", "-j", dest="json", action="store_true", default=False, help=argparse.SUPPRESS
    )
    # agents — which names actually have entries (#501). The hook keys diary
    # drawers on the harness name, so this is the only reliable way to learn
    # what to pass to --agent.
    p_diary_agents = diary_sub.add_parser(
        "agents",
        help="List the agents that have diary entries, with counts",
    )
    p_diary_agents.add_argument(
        "--wing",
        default=None,
        help="Limit to one wing (default: every wing)",
    )
    p_diary_agents.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )
    p_diary_agents.add_argument(
        "--json", "-j", dest="json", action="store_true", default=False, help=argparse.SUPPRESS
    )

    # kg — knowledge-graph writes + temporal read (slice of #191, issue #357)
    p_kg = sub.add_parser(
        "kg",
        help="Knowledge-graph facts: add, invalidate, timeline (see also: stats --section kg)",
    )
    kg_sub = p_kg.add_subparsers(dest="kg_action")
    p_kg_add = kg_sub.add_parser("add", help="Add a fact (subject → predicate → object)")
    p_kg_add.add_argument("--subject", required=True, help="Entity doing/being something")
    p_kg_add.add_argument("--predicate", required=True, help="Relationship type (e.g. maintains)")
    p_kg_add.add_argument("--object", required=True, help="Entity being connected to")
    p_kg_add.add_argument(
        "--valid-from",
        dest="valid_from",
        default=None,
        help="When it became true (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)",
    )
    p_kg_add.add_argument(
        "--valid-to",
        dest="valid_to",
        default=None,
        help="When it stopped being true — backfills an already-ended fact",
    )
    p_kg_add.add_argument(
        "--source-closet", dest="source_closet", default=None, help="Closet id provenance"
    )
    p_kg_add.add_argument(
        "--source-file", dest="source_file", default=None, help="Source file provenance"
    )
    p_kg_add.add_argument(
        "--source-drawer-id",
        dest="source_drawer_id",
        default=None,
        help="Drawer id the fact was extracted from",
    )
    p_kg_add.add_argument(
        "--context",
        default=None,
        help="SPOC anchor — where the fact was witnessed (e.g. drawer:abc123)",
    )
    p_kg_add.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )
    p_kg_add.add_argument(
        "--json", "-j", dest="json", action="store_true", default=False, help=argparse.SUPPRESS
    )
    p_kg_inv = kg_sub.add_parser(
        "invalidate",
        help="Mark a fact as no longer true (requires --confirm)",
    )
    p_kg_inv.add_argument("--subject", required=True, help="Entity")
    p_kg_inv.add_argument("--predicate", required=True, help="Relationship type")
    p_kg_inv.add_argument("--object", required=True, help="Connected entity")
    p_kg_inv.add_argument(
        "--ended",
        default=None,
        help="When it stopped being true (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ, default: today)",
    )
    p_kg_inv.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Required outside an interactive TTY — retracting a fact rewrites graph history",
    )
    p_kg_inv.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )
    p_kg_inv.add_argument(
        "--json", "-j", dest="json", action="store_true", default=False, help=argparse.SUPPRESS
    )
    p_kg_tl = kg_sub.add_parser("timeline", help="Chronological facts for an entity (or all)")
    p_kg_tl.add_argument(
        "entity",
        nargs="?",
        default=None,
        help="Entity to time-line (omit for the full timeline)",
    )
    p_kg_tl.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Only facts valid at this date/datetime (AGE backend only)",
    )
    p_kg_tl.add_argument(
        "--limit",
        type=int,
        default=_KG_TIMELINE_DEFAULT_LIMIT,
        help=(
            f"Facts to show (default {_KG_TIMELINE_DEFAULT_LIMIT}); truncates client-side "
            "because mempalace_kg_timeline takes no limit — and the backend itself "
            f"returns at most {_KG_TIMELINE_TOOL_CAP} facts, so a larger value cannot "
            "widen the window"
        ),
    )
    p_kg_tl.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )
    p_kg_tl.add_argument(
        "--json", "-j", dest="json", action="store_true", default=False, help=argparse.SUPPRESS
    )

    # walk — palace graph traversal (slice of #191, issue #359)
    p_walk = sub.add_parser(
        "walk",
        help="Walk the palace graph from a wing/room/entity (mempalace_walk_palace)",
    )
    p_walk.add_argument("--wing", default=None, help="Walk from this wing")
    p_walk.add_argument("--room", default=None, help="Walk from this room")
    p_walk.add_argument("--entity", default=None, help="Inverse walk from this entity")
    p_walk.add_argument(
        "--depth",
        type=int,
        default=_WALK_DEFAULT_DEPTH,
        help=f"Walk depth 1..5 (default {_WALK_DEFAULT_DEPTH}); hop budget under --follow tunnels",
    )
    p_walk.add_argument(
        "--limit",
        type=int,
        default=_WALK_DEFAULT_LIMIT,
        help=f"Max rows 1..500 (default {_WALK_DEFAULT_LIMIT})",
    )
    p_walk.add_argument(
        "--follow",
        choices=("palace", "tunnels"),
        default="palace",
        help=(
            "palace (default) walks wings/rooms/drawers/entities; "
            "tunnels follows cross-wing connections from --room"
        ),
    )
    p_walk.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )

    # rate — record whether a drawer was useful (slice of #191, issue #361)
    p_rate = sub.add_parser(
        "rate",
        help="Record drawer feedback that nudges search ranking (mempalace_rate_memory)",
    )
    p_rate.add_argument("drawer_id", help="Drawer being rated")
    rate_group = p_rate.add_mutually_exclusive_group(required=True)
    rate_group.add_argument(
        "--useful",
        dest="useful",
        action="store_true",
        default=None,
        help="The drawer was helpful",
    )
    rate_group.add_argument(
        "--not-useful",
        dest="useful",
        action="store_false",
        default=None,
        help="The drawer was noise",
    )
    p_rate.add_argument(
        "--format",
        choices=("table", "json"),
        default=None,
        help="Output format (default table; --json is shorthand for --format json)",
    )

    # ── upstream #2576 maintenance verbs, attached to the fork's own rooms / wings / kg / tunnels commands ──
    # Upstream added these as new top-level commands; the fork already had a command of each name (canonical
    # rooms, the wing listing, kg facts, the tunnel listing), and argparse refuses a second subparser of one
    # name. Each fork dispatcher hands these actions to upstream's handler (cmd_<name>_maintenance).
    p_rooms_propose = rooms_sub.add_parser(
        "propose", help="Sample a wing and ask the LLM for a closed room set (saved for review)"
    )
    p_rooms_propose.add_argument("--wing", required=True, help="Wing to design rooms for")
    p_rooms_propose.add_argument(
        "--sample", type=int, default=60, help="Drawers to sample (default 60)"
    )
    p_rooms_propose.add_argument("--seed", type=int, default=0, help="Sampling seed (default 0)")
    p_rooms_propose.add_argument(
        "--max-rooms", type=int, default=12, help="Upper bound on proposed rooms (default 12)"
    )
    p_rooms_propose.add_argument(
        "--llm-provider", default="ollama", choices=["ollama", "openai-compat", "anthropic"]
    )
    p_rooms_propose.add_argument("--llm-model", default="gemma4:e4b")
    p_rooms_propose.add_argument("--llm-endpoint", default=None)
    p_rooms_propose.add_argument("--llm-api-key", default=None)
    p_rooms_propose.add_argument(
        "--llm-timeout",
        type=int,
        default=600,
        help="Seconds to wait for the LLM; a local model labelling 60 excerpts can take minutes "
        "(default 600)",
    )
    p_rooms_propose.add_argument(
        "--accept-external-llm",
        action="store_true",
        help="Allow the sampled excerpts to be sent to a non-local LLM endpoint",
    )
    p_rooms_apply = rooms_sub.add_parser(
        "apply", help="Assign every drawer in the wing to its nearest room (dry run unless --yes)"
    )
    p_rooms_apply.add_argument("--wing", required=True, help="Wing whose room set to apply")
    p_rooms_apply.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Minimum cosine similarity to move a drawer; below it the drawer keeps its room "
        "(default 0.30)",
    )
    p_rooms_apply.add_argument(
        "--from",
        dest="from_rooms",
        default=None,
        metavar="ROOMS",
        help="Comma-separated rooms whose drawers may move (default: the miner's generic rooms "
        "general,technical,architecture,planning,problems); 'all' reclassifies every drawer",
    )
    p_rooms_apply.add_argument(
        "--show",
        type=int,
        default=0,
        metavar="N",
        help="On a dry run, print N example drawers per destination room",
    )
    p_rooms_apply.add_argument("--yes", action="store_true", help="Write the room changes")
    p_rooms_propose.set_defaults(rooms_action="propose")
    p_rooms_apply.set_defaults(rooms_action="apply")
    wings_sub = p_wings.add_subparsers(dest="wings_action")
    p_wings_split = wings_sub.add_parser(
        "split",
        help="Re-key a machine-level transcript wing into one wing per source project "
        "(plan file first; --yes applies)",
    )
    p_wings_split.add_argument("--wing", required=True, help="Wing to split")
    p_wings_split.add_argument(
        "--yes", action="store_true", help="Apply the saved plan (<palace>/wings/split-<wing>.json)"
    )
    p_kg_norm = kg_sub.add_parser(
        "normalize",
        help="Map one-off predicates onto a closed vocabulary with the LLM (plan first; "
        "--yes applies, keeping history)",
    )
    p_kg_norm.add_argument(
        "--vocabulary",
        default=None,
        help="Comma-separated allowed predicates (default: works_on,owns,depends_on,uses,"
        "decided,status,located_in,measured)",
    )
    p_kg_norm.add_argument("--yes", action="store_true", help="Apply <palace>/kg/normalize.json")
    p_kg_norm.add_argument(
        "--llm-provider", default="ollama", choices=["ollama", "openai-compat", "anthropic"]
    )
    p_kg_norm.add_argument("--llm-model", default="gemma4:e4b")
    p_kg_norm.add_argument("--llm-endpoint", default=None)
    p_kg_norm.add_argument("--llm-api-key", default=None)
    p_kg_norm.add_argument("--llm-timeout", type=int, default=600)
    p_kg_norm.add_argument("--accept-external-llm", action="store_true")
    tunnels_sub = p_tunnels.add_subparsers(dest="tunnels_action")
    p_tunnels_propose = tunnels_sub.add_parser(
        "propose",
        help="Rank shared entities across wings into a reviewable tunnel plan (--yes creates them)",
    )
    p_tunnels_propose.add_argument("--max", type=int, default=60, help="Cap on proposed tunnels")
    p_tunnels_propose.add_argument("--yes", action="store_true", help="Create the saved plan")
    p_tunnels_prune = tunnels_sub.add_parser(
        "prune",
        help="Remove tunnels on generic tokens, missing wings or duplicate spellings (dry run "
        "unless --yes)",
    )
    p_tunnels_prune.add_argument("--yes", action="store_true", help="Apply the prune")

    # ── Propagate --json/--quiet to every subparser (issue #44) ─────
    # argparse parses pre-subcommand flags into ``args.json`` /
    # ``args.quiet`` only if they appear BEFORE the subcommand. To let
    # users write the natural ``mempalace status --json``, attach the
    # same flags to each subparser so post-subcommand usage parses too.
    # Help is suppressed on the per-subparser copies to keep ``--help``
    # output uncluttered — the canonical docs live on the top-level parser.
    for _sp_name, _sp in sub.choices.items():
        # Skip the two-level parents (hook, instructions, rooms) — their
        # actions are owned by nested sub-parsers and the leaf parsers
        # don't need JSON output (hook/instructions write their own JSON;
        # rooms is operator-only). Detect them by the presence of a
        # registered subparsers action.
        has_nested = any(isinstance(a, argparse._SubParsersAction) for a in _sp._actions)
        # wings and tunnels are listings first; their optional maintenance subcommands (upstream #2576) must not
        # cost the listing its --json.
        if has_nested and _sp_name not in ("wings", "tunnels"):
            continue
        # A subparser that declares one of these itself keeps its own (upstream's ``audit --json``).
        _own = {o for a in _sp._actions for o in a.option_strings}
        if "--json" not in _own:
            _sp.add_argument(
                "--json",
                "-j",
                dest="json",
                action="store_true",
                default=False,
                help=argparse.SUPPRESS,
            )
        if "--quiet" not in _own:
            _sp.add_argument(
                "--quiet",
                "-q",
                dest="quiet",
                action="store_true",
                default=False,
                help=argparse.SUPPRESS,
            )

    args = parser.parse_args()
    _apply_backend_arg(args)

    # When ``--json`` / ``--quiet`` was passed at top level, the
    # subparser-side default would clobber it (argparse stores per-action
    # defaults). Restore the top-level value if the subparser-side flag
    # wasn't explicitly set. Cheapest way: check ``sys.argv`` for the
    # token — explicit flag in argv means the user asked for it.
    _argv_after_command = sys.argv[1:]
    if any(t in ("--json", "-j") for t in _argv_after_command):
        args.json = True
    if any(t in ("--quiet", "-q") for t in _argv_after_command):
        args.quiet = True

    if not args.command:
        parser.print_help()
        return

    # Handle two-level subcommands
    if args.command == "hook":
        if not getattr(args, "hook_action", None):
            p_hook.print_help()
            return
        cmd_hook(args)
        return

    if args.command == "instructions":
        name = getattr(args, "instructions_name", None)
        if not name:
            p_instructions.print_help()
            return
        args.name = name
        cmd_instructions(args)
        return

    if args.command == "palace":
        if getattr(args, "palace_action", None) == "set-embedder":
            cmd_palace_set_embedder(args)
        else:
            p_palace.print_help()
        return

    if args.command == "logstream":
        if not getattr(args, "logstream_action", None):
            p_logstream.print_help()
            return
        cmd_logstream(args)
        return

    if args.command == "task":
        if not getattr(args, "task_action", None):
            p_task.print_help()
            return
        cmd_task(args)
        return

    if args.command == "artifact":
        if not getattr(args, "artifact_action", None):
            p_artifact.print_help()
            return
        cmd_artifact(args)
        return

    if args.command == "daemon":
        if not getattr(args, "daemon_action", None):
            p_daemon.print_help()
            return
        cmd_daemon(args)
        return

    if args.command == "hallway":
        if not getattr(args, "hallway_action", None):
            p_hallway.print_help()
            return
        cmd_hallway(args)
        return

    if args.command == "aaak":
        if getattr(args, "aaak_action", None) != "spec":
            p_aaak.print_help()
            return
        cmd_aaak(args)
        return

    if args.command == "diary":
        if not getattr(args, "diary_action", None):
            p_diary.print_help()
            return
        cmd_diary(args)
        return

    if args.command == "kg":
        if not getattr(args, "kg_action", None):
            p_kg.print_help()
            return
        cmd_kg(args)
        return

    dispatch = {
        "init": cmd_init,
        "rules": cmd_rules,
        "mine": cmd_mine,
        "split": cmd_split,
        "search": cmd_search,
        "list": cmd_list,
        "window": cmd_window,
        "source": cmd_source,
        "move": cmd_move,
        "bulk-move": cmd_bulk_move,
        "graph": cmd_graph,
        "cypher": cmd_cypher,
        "export": cmd_export,
        "sweep": cmd_sweep,
        "sync": cmd_sync,
        "mcp": cmd_mcp,
        "serve": cmd_serve,
        "compress": cmd_compress,
        "wake-up": cmd_wakeup,
        "repair": cmd_repair,
        "repair-status": cmd_repair_status,
        "migrate": cmd_migrate,
        "migrate-to-postgres": cmd_migrate_to_postgres,
        "purge": cmd_purge,
        "prune": cmd_prune,
        "rename-wing": cmd_rename_wing,
        "rooms": cmd_rooms,
        "migrate-wings": cmd_migrate_wings,
        "hallways": cmd_hallways,
        "audit": cmd_audit,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "update": cmd_update,
        "stats": cmd_stats,
        "tags": cmd_tags,
        "overlap": cmd_overlap,
        "why": cmd_why,
        "tunnels": cmd_tunnels,
        "mined": cmd_mined,
        "reconcile-docs": cmd_reconcile_docs,
        "pending": cmd_pending,
        "replay": cmd_replay,
        # read family — slices of #191 (issues #356, #360, #362)
        "wings": cmd_wings,
        "checkpoint": cmd_checkpoint,
        "taxonomy": cmd_taxonomy,
        # graph + diary family — slices of #191 (issues #359, #361)
        "walk": cmd_walk,
        "rate": cmd_rate,
    }

    # Issue #49: announce the routing decision to stderr when daemon_url is
    # set, regardless of strict mode. Silent on the pure-local default (no
    # URL configured anywhere) since that's upstream's expected behavior
    # and announcing it on every CLI invocation would be noise. Surfaces:
    #   - daemon-strict on   → "routing → daemon @ URL (source: env|config)"
    #   - daemon-strict off  → "routing → local (PALACE_DAEMON_STRICT=0 overrides
    #                            daemon_url=URL)"
    # Diagnoses the silent split-brain failure mode the issue documents.
    try:
        _cfg = MempalaceConfig()
        # Suppress the routing chrome when --json / --quiet is on, or
        # when stdout isn't a TTY (piped). The announcement is for
        # interactive humans; agents capturing both streams expect a
        # clean surface (issue #44).
        _suppress_routing = (
            getattr(args, "json", False) or getattr(args, "quiet", False) or _resolve_quiet(args)
        )
        if _cfg.daemon_url and args.command not in (None, "--help") and not _suppress_routing:
            _src = "env" if os.environ.get("PALACE_DAEMON_URL", "").strip() else "config"
            if _cfg.daemon_strict:
                print(
                    f"mempalace: routing → daemon @ {_cfg.daemon_url} (source: {_src})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"mempalace: routing → local (PALACE_DAEMON_STRICT=0 overrides "
                    f"daemon_url={_cfg.daemon_url} from {_src})",
                    file=sys.stderr,
                )
    except Exception:
        # Never let routing-announce crash a CLI invocation.
        pass

    # Two-level commands that daemon-route (#355, #363) are dispatched
    # here rather than beside `hook`/`artifact` above, so the #49 routing
    # announcement still fires for them — knowing whether `drawer delete`
    # hit the daemon or a local palace is the whole point of that line.
    if args.command == "drawer":
        if not getattr(args, "drawer_action", None):
            p_drawer.print_help()
            return
        cmd_drawer(args)
        return

    if args.command == "duplicate":
        if getattr(args, "duplicate_action", None) != "check":
            p_duplicate.print_help()
            return
        cmd_duplicate(args)
        return

    # #508: propagate a handler's exit code instead of discarding it.
    #
    # `isinstance(rc, int)` rather than `sys.exit(rc)` is load-bearing and was
    # measured, not guessed: `cmd_sync` returns a `SyncReport`, and
    # `sys.exit(<non-int>)` exits **1** after printing repr() to stderr — so
    # the obvious one-liner would turn a SUCCESSFUL `mempalace sync` into a
    # failure with the report dumped on stderr.
    #
    # `True` is an int in Python, so a handler returning a bare truthy flag
    # would exit 1; none does today (AST over the dispatch table: exactly one
    # handler returns non-None, and it is cmd_sync's report object), and the
    # guard below keeps a bool from being read as a status by accident.
    rc = dispatch[args.command](args)
    if isinstance(rc, int) and not isinstance(rc, bool):
        sys.exit(rc)
