#!/usr/bin/env python3
"""
MemPalace — Give your AI a memory. No API key required.

Three ways to ingest:
  Projects:      mempalace mine ~/projects/my_app                  (code, docs, notes)
  Conversations: mempalace mine <convo-dir> --mode convos          (Claude Code, Claude.ai, ChatGPT, Slack exports)
  Documents:     mempalace mine <docs-dir> --mode extract          (PDF, DOCX, PPTX, XLSX, RTF, EPUB — requires mempalace[extract])
  Adapters:      mempalace mine <source> --source <adapter-name>  (registered source adapters)

Same palace. Same search. Different ingest strategies.

Commands:
    mempalace init <dir>                  Detect rooms from folder structure
    mempalace split <dir>                 Split concatenated mega-files into per-session files
    mempalace mine <dir>                  Mine project files (default)
    mempalace mine <dir> --mode convos    Mine conversation exports
    mempalace mine <dir> --mode extract   Mine binary office documents (PDF/DOCX/etc.)
    mempalace mine <source> --source NAME Mine through a registered source adapter
    mempalace search "query"              Find anything, exact words
    mempalace mcp                         Show MCP setup command
    mempalace task create ...             Create a complete agent handoff
    mempalace task launch ...             Run a stored task headlessly
    mempalace wake-up                     Show L0 + L1 wake-up context
    mempalace wake-up --wing my_app       Wake-up for a specific project
    mempalace status                      Show what's been filed
    mempalace mined                       List mined source files grouped by wing
    mempalace purge --source-file <path>  Remove drawers mined from a specific file

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/.claude/projects/-Users-you-Projects-my_app --mode convos --wing my_app
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

from __future__ import annotations

import json
import argparse
import contextlib
import os
import shlex
import sys
import warnings
from pathlib import Path

from .auto_wake import urlopen_with_wake
from .config import MempalaceConfig
from .corpus_origin import detect_origin_heuristic, detect_origin_llm
from .llm_client import LLMError, get_provider
from .provenance import (
    all_transcript,
    annotate,
    no_curated_source,
    provenance_note,
    source_kind,
)
from .version import __version__


# ==================== AGENT-SHAPED OUTPUT (issue #44) ====================
# ``--json`` / ``-j`` flips command output from prose to a stable JSON
# document on stdout, intended for shell pipelines and non-MCP agents.
# ``--quiet`` / ``-q`` suppresses decorative chrome (headers, divider
# lines, the daemon-routing announcement on stderr). When stdout is not
# a TTY we default to quiet mode so piped output stays clean — explicit
# ``--quiet`` / ``--json`` still override (see ``_resolve_quiet``).
#
# Exit codes (per issue #44):
#   0  success
#   1  no results / search returned empty
#   2  palace unavailable (daemon unreachable, palace missing, etc.)
#   64 bad args (argparse default for parse errors)


def _resolve_quiet(args) -> bool:
    """True when chrome should be suppressed.

    Quiet is on whenever any of these are true:
      * ``--quiet`` / ``-q`` was passed
      * ``--json`` / ``-j`` was passed (JSON output is always machine
        consumption — chrome would corrupt the document)
      * ``sys.stdout`` is not a TTY (piped or redirected)
    """
    if getattr(args, "json", False):
        return True
    if getattr(args, "quiet", False):
        return True
    try:
        return not sys.stdout.isatty()
    except (AttributeError, ValueError):
        # ``sys.stdout`` may be replaced by a non-stream in some test
        # harnesses; treat that as "no TTY" so we err toward clean output.
        return True


def _emit_json(payload: dict) -> None:
    """Write a JSON document to stdout with a trailing newline.

    Centralised so every JSON-emitting command uses the same formatting
    (sort_keys=False to preserve insertion order, indent=2 for human
    readability when the agent prints what it just received).
    """
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


# ==================== DAEMON ROUTING ====================
# When ``PALACE_DAEMON_URL`` is set, palace-daemon is the single writer
# for the canonical palace and high-traffic CLI subcommands route there
# instead of opening a local chromadb client. Mirrors the gate already
# in ``mempalace.hooks_cli`` (mining side) and
# ``mempalace.mcp_server`` (MCP dispatch). Currently routes ``status``,
# ``search``, and ``mine``; the remaining subcommands (``repair``,
# ``export``, ``sweep``, ``init``) still need on-host filesystem access
# and stay local. When the daemon URL is unset, all paths run locally
# unchanged.

_DAEMON_TIMEOUT_DEFAULT = 120  # seconds; tune via PALACE_MCP_TIMEOUT


class DaemonError(RuntimeError):
    """Raised when a daemon HTTP call fails or returns a JSON-RPC error."""


def _print_retired_local_palace_or_default(palace_path: str) -> None:
    """If the user's default palace is missing AND a RETIRED marker
    exists, print the marker's content as the not-found message — so
    agents see "set PALACE_DAEMON_URL" instead of "Run: mempalace init".

    Falls through to the legacy "Run: mempalace init" message in every
    other case (palace literally absent on a fresh install, etc.).
    """
    from .palace import _emit_current_daemon_url

    palace_root = os.path.expanduser("~/.mempalace")
    marker = os.path.join(palace_root, "RETIRED")
    default_path = os.path.join(palace_root, "palace")
    is_default = os.path.abspath(palace_path) == os.path.abspath(default_path)
    if is_default and os.path.exists(marker):
        try:
            with open(marker) as f:
                note = f.read().rstrip()
        except OSError:
            note = "(marker unreadable)"
        print(f"\n  Local palace at {default_path} is RETIRED.\n")
        for line in note.splitlines():
            print(f"  {line}")
        _emit_current_daemon_url(print)
        return
    print(f"\n  No palace found at {palace_path}")
    print("  Run: mempalace init <dir> then mempalace mine <dir>")


def _daemon_strict() -> bool:
    """True when daemon routing is on and strict mode is enabled.

    Resolution: ``MempalaceConfig.daemon_strict`` — env var
    ``PALACE_DAEMON_URL`` wins as the source of the URL, with
    ``~/.mempalace/config.json`` key ``"daemon_url"`` as fallback (see
    issue #49). Set ``PALACE_DAEMON_STRICT=0`` or
    ``"daemon_strict": false`` in config to force the local path.
    """
    # getattr-tolerant: test fakes and minimal configs (upstream #2062
    # fixtures) may not define the fork-side attribute; absent → local.
    return getattr(MempalaceConfig(), "daemon_strict", False)


def _daemon_url() -> str:
    return MempalaceConfig().daemon_url or ""


def _daemon_timeout() -> int:
    raw = os.environ.get("PALACE_MCP_TIMEOUT", str(_DAEMON_TIMEOUT_DEFAULT))
    try:
        return int(raw)
    except ValueError:
        return _DAEMON_TIMEOUT_DEFAULT


def _call_daemon_tool(name: str, arguments: dict) -> dict:
    """JSON-RPC ``tools/call`` against the daemon's ``/mcp`` endpoint.

    Returns the inner tool result already parsed from the JSON text
    payload (the ``content[0].text`` envelope MCP wraps every tool
    response in). Raises :class:`DaemonError` on network failure or a
    JSON-RPC error response — the CLI must surface failures to the
    caller, never silently fall back to local (that would re-introduce
    the split-brain that daemon-strict was created to prevent).
    """
    import urllib.error
    import urllib.request

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {"content-type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(
        f"{_daemon_url()}/mcp",
        data=json.dumps(request).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen_with_wake(req, timeout=_daemon_timeout()) as resp:
            body = resp.read()
        envelope = json.loads(body.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e
    if "error" in envelope:
        err = envelope["error"]
        raise DaemonError(f"daemon error {err.get('code')}: {err.get('message')}")
    content = (envelope.get("result") or {}).get("content") or []
    if not content:
        return {}
    text = content[0].get("text") or ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Non-JSON tool output (rare). Return as-is for the caller to
        # decide what to do.
        return {"_raw": text}


def _call_daemon_rest(path: str, params: dict | None = None) -> dict:
    """GET a daemon REST endpoint directly — no MCP envelope, no AGE locks.

    Falls back to _call_daemon_tool on non-2xx (endpoint might not exist
    on older daemons). Raises DaemonError on network failure.
    """
    import urllib.error
    import urllib.request
    import urllib.parse

    url = f"{_daemon_url()}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urlopen_with_wake(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 401, 403):
            return None  # endpoint missing or auth mismatch — caller falls back to MCP
        raise DaemonError(f"daemon REST {path} failed ({e.code}): {e.reason}") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e


def _post_daemon_rest(path: str, body: dict) -> dict:
    """POST to a daemon REST endpoint — for hybrid/keyword/age-fused search.

    Returns the parsed JSON response. Returns None on 404 (endpoint not
    available on older daemons). Raises DaemonError on network failure.
    """
    import urllib.error
    import urllib.request

    url = f"{_daemon_url()}{path}"
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
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise DaemonError(f"daemon REST {path} failed ({e.code}): {e.reason}") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e


def _patch_daemon_rest(path: str, body: dict) -> dict:
    """PATCH a daemon REST endpoint — for single-drawer metadata moves.

    Mirrors :func:`_post_daemon_rest` (X-API-Key header, JSON body) but uses
    the PATCH verb. Returns the parsed JSON response, or None on 404/401/403
    (endpoint missing on an older daemon, or auth mismatch — the caller maps
    that to the same exit code as an unreachable daemon). Raises DaemonError
    on network failure.
    """
    import urllib.error
    import urllib.request

    url = f"{_daemon_url()}{path}"
    headers = {"content-type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="PATCH",
    )
    try:
        with urlopen_with_wake(req, timeout=_daemon_timeout()) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 401, 403):
            return None
        raise DaemonError(f"daemon REST {path} failed ({e.code}): {e.reason}") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e


def _post_daemon_mine_cli(directory: str, wing: str, mode: str = "convos") -> bool:
    """POST a mine request to the daemon's ``/mine`` endpoint.

    CLI-shaped variant of :func:`mempalace.hooks_cli._post_daemon_mine`:
    on failure, prints to stderr and returns ``False`` so the caller can
    ``sys.exit(1)``. Hooks_cli's version logs to a file and swallows
    silently because a missed-mine isn't worth crashing a hook over;
    here, the user invoked `mempalace mine` and expects to see errors.
    """
    import urllib.error
    import urllib.request

    headers = {"content-type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(
        f"{_daemon_url()}/mine",
        data=json.dumps({"dir": directory, "wing": wing, "mode": mode}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen_with_wake(req, timeout=_daemon_timeout()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        print(f"  Daemon mine accepted: {body[:200]}")
        return True
    except urllib.error.HTTPError as e:
        # The response body carries the daemon's actual reason ("Directory
        # does not exist: …") — a bare "HTTP Error 400" hides the one thing
        # the user needs to act on.
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        try:
            detail = json.loads(raw).get("detail", "") if raw else ""
        except (ValueError, AttributeError):
            detail = raw[:200]
        msg = f"{e}: {detail}" if detail else str(e)
        print(f"  ERROR: daemon mine failed: {msg}", file=sys.stderr)
        if "does not exist" in detail:
            print(
                "  HINT: the daemon may run on another host and can only mine"
                " paths that exist there (synced or PALACE_DAEMON_PATH_MAP-"
                "mapped). ~/.claude/projects/*/scratch is excluded from sync"
                " by default — stage files in a synced location (e.g."
                " ~/.claude/projects/<proj>/palace-inbox/) and mine that path.",
                file=sys.stderr,
            )
        return False
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        print(f"  ERROR: daemon mine failed: {e}", file=sys.stderr)
        return False


def _print_daemon_status(data: dict) -> None:
    """Format ``mempalace_status`` JSON for human reading.

    The daemon's status tool returns a flat ``wings: {name: count}``
    dict (not the wing×room nested shape that ``miner.status`` builds
    from raw metadata). We print the daemon's shape rather than
    over-fetching to reconstruct the local layout — the daemon URL is
    visible in the header so the reader knows which view they're
    looking at.
    """
    import json as _json

    total = data.get("total_drawers", 0)
    wings = data.get("wings") or {}
    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {total} drawers")
    print(f"  via palace-daemon @ {_daemon_url()}")
    print(f"{'=' * 55}\n")
    if isinstance(wings, dict) and wings:
        for wing, count in sorted(wings.items(), key=lambda kv: kv[1], reverse=True):
            if isinstance(count, dict):
                # Defensive: if a future daemon returns wing×room nested,
                # still render something useful.
                inner = count.get("total", sum((count.get("rooms") or {}).values()))
                print(f"  WING: {wing:30} {inner:>6} drawers")
            else:
                print(f"  WING: {wing:30} {count:>6} drawers")
    elif "error" in data:
        # Render daemon errors with the same shape as _print_daemon_search:
        # error → message → hint. Important for "palace.backend_unreachable"
        # so the user sees "start the postgres container" instead of the
        # legacy "Run: mempalace init <dir>" hint that was actively misleading
        # during the 2026-05-17 power-event diagnosis.
        print(f"  daemon reported error: {data['error']}")
        if "message" in data:
            print(f"  {data['message']}")
        if "hint" in data:
            print(f"  {data['hint']}")
    else:
        # Surface unexpected shapes verbatim.
        print(_json.dumps(data, indent=2))
    print(f"\n{'=' * 55}\n")


def _print_daemon_mined(data: dict, *, want_json: bool, limit: int) -> None:
    """Render ``mempalace_mined`` daemon tool output (palace-daemon #96).

    Same payload shape as ``cmd_mined``'s local JSON path:
    ``{sources_by_wing, wing_filter, total_wings, total_sources}`` with
    each wing carrying ``{sources: [...], total_sources, total_drawers,
    truncated}``. ``want_json`` pass-through emits the daemon's payload
    verbatim; otherwise renders the same per-wing block layout the
    local pretty-printer below produces.
    """
    if want_json:
        _emit_json(data)
        return

    sources_by_wing = data.get("sources_by_wing") or {}
    wing_filter = data.get("wing_filter")

    if not sources_by_wing:
        scope = f" in wing={wing_filter}" if wing_filter else ""
        print(f"\n  No mined source files found{scope}.\n")
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Mined — sources by wing")
    print(f"  via palace-daemon @ {_daemon_url()}")
    print(f"{'=' * 55}\n")
    for wing in sorted(sources_by_wing):
        block = sources_by_wing[wing] or {}
        sources = block.get("sources") or []
        total_sources = block.get("total_sources", len(sources))
        total_drawers = block.get("total_drawers", sum(s.get("drawer_count", 0) for s in sources))
        print(f"  WING: {wing}  ({total_sources} sources, {total_drawers} drawers)")
        for entry in sources:
            src = entry.get("source_file", "?")
            count = entry.get("drawer_count", 0)
            print(f"    {count:5}  {src}")
        if block.get("truncated") and limit:
            hidden = total_sources - len(sources)
            if hidden > 0:
                print(f"    ... {hidden} more (use --limit 0 to show all)")
        print()
    print(f"{'=' * 55}\n")


# ── enhanced search output (#191) ──────────────────────────────────────
# Three human-facing formats share a single hit shape from the daemon
# (or from ``searcher.search`` on local fallback): ``wing``, ``room``,
# ``source_file``, ``similarity``, ``bm25_score``, ``matched_via``,
# ``text``, ``created_at``, optional ``tags``. ``table`` is the default
# enhanced view (multi-line per hit with metadata + a relevance bar
# proportional to cosine similarity). ``compact`` collapses each hit to
# a single line for fast scanning. ``full`` is identical to ``table``
# but skips content truncation so long drawers render in full.
#
# ANSI colour is opt-in via ``_search_use_color``: only when stdout is a
# TTY, ``--quiet`` / ``--json`` is off, and the ``NO_COLOR`` env var is
# unset (per https://no-color.org). Honouring NO_COLOR means CI logs
# and accessibility users get plain text without extra flags.

_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2m"
_ANSI_BOLD = "\033[1m"
_ANSI_CYAN = "\033[36m"
_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_MAGENTA = "\033[35m"

# Default content truncation budget for ``table`` format. Long drawers
# get the first N lines with a ``... +M more lines`` marker so the
# terminal stays readable on a 5-result page. ``full`` skips truncation.
_SEARCH_TABLE_MAX_LINES = 12


def _search_use_color(quiet: bool) -> bool:
    """True when ANSI colour codes are safe to emit.

    Suppressed for ``--quiet`` / ``--json`` callers (machine consumption),
    when stdout is not a TTY (piped/redirected output), and when the
    ``NO_COLOR`` env var is set (https://no-color.org convention).
    """
    if quiet:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _color(text: str, code: str, use_color: bool) -> str:
    """Wrap ``text`` in an ANSI colour code only when ``use_color`` is True."""
    if not use_color or not text:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def _relevance_bar(similarity, width: int = 16) -> str:
    """Render a horizontal bar proportional to cosine ``similarity``.

    Mirrors ``_stats_bar``'s Unicode block fill so search and stats
    look like siblings. Returns an empty string when similarity is
    unavailable (e.g. BM25-only hit) so the caller can fall back to
    a numeric BM25 line instead of an empty bar.
    """
    if similarity is None:
        return ""
    try:
        ratio = max(0.0, min(1.0, float(similarity)))
    except (TypeError, ValueError):
        return ""
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _format_hit_metadata(hit: dict) -> str:
    """Inline metadata line — source file + creation timestamp.

    Both fields are best-effort. ``created_at`` is the ``filed_at``
    timestamp the daemon attaches at write time (see #184 fork-changes
    entry on ``filed_at``); missing values render as ``unknown`` rather
    than an empty placeholder so the column alignment stays readable.
    """
    source = hit.get("source_file") or "?"
    created = hit.get("created_at") or "unknown"
    return f"{source}  ·  {created}"


def _truncate_content(text: str, max_lines: int) -> tuple:
    """Return (shown_lines, truncated_count) for ``table`` format.

    Preserves newlines (we never collapse multi-line drawers) and
    appends a ``+N more lines`` marker when truncated so the reader
    knows there's more behind the ellipsis. ``--format full`` skips
    this helper entirely.
    """
    if not text:
        return [], 0
    lines = text.strip().split("\n")
    if max_lines <= 0 or len(lines) <= max_lines:
        return lines, 0
    return lines[:max_lines], len(lines) - max_lines


def _print_search_header(
    query: str, data: dict, wing, room, warnings, quiet: bool, use_color: bool
) -> None:
    """Header chrome for ``table`` / ``full`` formats. Suppressed under --quiet."""
    if quiet:
        return
    print(f"\n{'=' * 60}")
    print(f'  Results for: "{_color(query, _ANSI_BOLD, use_color)}"')
    if wing:
        print(f"  Wing: {_color(wing, _ANSI_CYAN, use_color)}")
    if room:
        print(f"  Room: {_color(room, _ANSI_MAGENTA, use_color)}")
    if data.get("available_in_scope") is not None:
        print(f"  Scope has: {data['available_in_scope']} drawers matching filter")
    for w in warnings:
        print(f"  ! {w}")
    # Nothing curated matched means nothing in this result set can carry a
    # later correction — every hit is a quoted copy of what was said at the
    # time (techempower-org/mempalace#451). Say which of the two shapes it is
    # rather than overclaiming "all transcripts" over a mixed set.
    hits = data.get("results") or []
    if all_transcript(hits):
        print(
            f"  ! all {len(hits)} hits are session-transcript copies — no curated "
            "document matched; the source may be newer than its indexed copy"
        )
    elif no_curated_source(hits):
        print(
            f"  ! none of the {len(hits)} hits came from a curated document — they "
            "are session-transcript and palace-diary copies; the source may be "
            "newer than its indexed copy"
        )
    if data.get("fallback"):
        print(f"  ! fallback: {data['fallback']}")
    print(f"  via palace-daemon @ {_daemon_url()}")
    print(f"{'=' * 60}\n")


def _print_hit_table(index: int, hit: dict, *, full: bool, use_color: bool) -> None:
    """Render a single hit in ``table`` (default) or ``full`` format."""
    wing = _color(hit.get("wing", "?"), _ANSI_CYAN, use_color)
    room = _color(hit.get("room", "?"), _ANSI_MAGENTA, use_color)
    sim = hit.get("similarity")
    bm25 = hit.get("bm25_score")

    print(f"  [{index}] {wing} / {room}")

    bar = _relevance_bar(sim)
    if bar:
        # Cosine bar is the primary signal; show BM25 inline when both
        # exist so hybrid hits don't lose their second score.
        sim_str = f"{sim:.3f}" if isinstance(sim, (int, float)) else str(sim)
        bm25_suffix = f"  bm25={bm25}" if bm25 is not None else ""
        print(f"      {_color(bar, _ANSI_GREEN, use_color)}  cosine={sim_str}{bm25_suffix}")
    elif bm25 is not None:
        # BM25-only hit (rare; vector store missing or filter forced it).
        matched_via = hit.get("matched_via", "drawer")
        print(f"      BM25: {bm25}  (matched_via: {matched_via})")

    meta = _format_hit_metadata(hit)
    print(f"      {_color(meta, _ANSI_DIM, use_color)}")

    note = provenance_note(hit)
    if note:
        print(f"      {_color('⚠ ' + note, _ANSI_DIM, use_color)}")

    tags = hit.get("tags")
    if tags:
        tag_str = ", ".join(str(t) for t in tags)
        print(f"      {_color('tags:', _ANSI_DIM, use_color)} {tag_str}")

    print()
    max_lines = 0 if full else _SEARCH_TABLE_MAX_LINES
    shown, hidden = _truncate_content(hit.get("text") or "", max_lines)
    for line in shown:
        print(f"      {line}")
    if hidden:
        marker = f"... +{hidden} more lines (use --format full to see all)"
        print(f"      {_color(marker, _ANSI_DIM, use_color)}")
    print()
    print(f"  {'─' * 56}")


def _provenance_tag(hit: dict) -> str:
    """Short source-shape tag for ``--format compact`` (empty when unremarkable).

    ``⟨transcript⟩`` — a quoted copy from a session transcript.
    ``⟨stale⟩`` — the file on disk has been modified since it was indexed.
    Never both: a growing session transcript is expected, so ``⟨stale⟩`` is
    suppressed for transcripts exactly as ``provenance_note`` suppresses the
    matching sentence.

    The kind is derived when the hit was not annotated, mirroring
    ``provenance_note``'s fallback so ``compact`` is never the one renderer
    that silently drops the caveat. Staleness is read only from the annotated
    field: deciding it costs an ``os.stat`` per hit, and every path that
    produces hits already stamps it.
    """
    kind = hit.get("source_kind") or source_kind(hit)
    flags = []
    if kind == "transcript":
        flags.append("transcript")
    elif hit.get("source_stale") is True:
        flags.append("stale")
    return f" ⟨{','.join(flags)}⟩" if flags else ""


def _print_hit_compact(index: int, hit: dict, *, use_color: bool) -> None:
    """One-line-per-hit rendering for ``--format compact``.

    Format: ``[N] wing/room  ▰▰▰▰░░░░░░░░  0.812  source  preview``.
    Preview is the first non-empty line of the drawer, truncated to
    fit a reasonable terminal width (~110 cols). Newlines collapse
    into a single space so the preview never wraps.
    """
    wing = _color(hit.get("wing", "?"), _ANSI_CYAN, use_color)
    room = _color(hit.get("room", "?"), _ANSI_MAGENTA, use_color)
    sim = hit.get("similarity")
    bar = _relevance_bar(sim, width=12)
    sim_str = f"{sim:.3f}" if isinstance(sim, (int, float)) else "  -  "
    bar_part = _color(bar, _ANSI_GREEN, use_color) if bar else "  -  "
    text = (hit.get("text") or "").strip()
    first_line = next((ln for ln in text.split("\n") if ln.strip()), "")
    if len(first_line) > 70:
        first_line = first_line[:67] + "…"
    source = hit.get("source_file") or "?"
    if len(source) > 28:
        source = "…" + source[-27:]
    source = f"{source}{_provenance_tag(hit)}"
    print(
        f"  [{index}] {wing}/{room}  {bar_part}  {sim_str}  "
        f"{_color(source, _ANSI_DIM, use_color)}  {first_line}"
    )


def _print_daemon_search(
    query: str,
    data: dict,
    wing: str = None,
    room: str = None,
    *,
    fmt: str = "table",
    quiet: bool = False,
) -> None:
    """Format ``mempalace_search`` JSON; mirrors ``searcher.search`` output.

    ``fmt`` selects ``table`` (default — multi-line, metadata + relevance
    bar + truncated content), ``compact`` (one line per hit), or ``full``
    (table layout with no content truncation). ``quiet`` suppresses the
    header chrome and ANSI colour, intended for piped output and
    ``--quiet``/``--json`` callers.
    """
    if "error" in data and not data.get("results"):
        print(f"\n  {data['error']}")
        if "hint" in data:
            print(f"  {data['hint']}")
        return

    hits = data.get("results") or []
    warnings = data.get("warnings") or []

    if not hits:
        print(f'\n  No results found for: "{query}"')
        for w in warnings:
            print(f"  ! {w}")
        return

    use_color = _search_use_color(quiet)
    _print_search_header(query, data, wing, room, warnings, quiet, use_color)

    if fmt == "compact":
        for i, hit in enumerate(hits, 1):
            _print_hit_compact(i, hit, use_color=use_color)
        if not quiet:
            print()
        return

    full = fmt == "full"
    for i, hit in enumerate(hits, 1):
        _print_hit_table(i, hit, full=full, use_color=use_color)
    if not quiet:
        print()


_MEMPALACE_PROJECT_FILES = ("mempalace.yaml", "entities.json")

# Pass 0 corpus-origin sampling caps. Tier 1 reads FULL file content (no
# front-bias sampling) but bounds total memory on enormous corpora. Tier 2
# trims to a smaller view because LLM context windows are finite.
_PASS_ZERO_MAX_FILES = 30
_PASS_ZERO_PER_FILE_CAP = 100_000  # 100KB per file is generous for prose
_PASS_ZERO_TOTAL_CAP = 5_000_000  # 5MB total ceiling — bounds memory
_PASS_ZERO_LLM_PER_SAMPLE = 2_000  # for Tier 2 LLM call only
_PASS_ZERO_LLM_MAX_SAMPLES = 20  # caps the LLM-tier sample count
_EXPLICIT_BACKEND_ENV = "MEMPALACE_BACKEND_EXPLICIT"

# Keep parser construction lightweight for --version and hook commands.
# This mirrors miner.MAX_CHUNKS_PER_FILE without importing miner here;
# importing miner pulls in Chroma dependencies before argparse can handle
# lightweight exits such as --version.
_CLI_MAX_CHUNKS_PER_FILE_DEFAULT = 50_000


def _backend_arg(args):
    """Return a CLI-selected backend from subcommand or global flags."""
    return getattr(args, "backend", None) or getattr(args, "global_backend", None)


def _apply_backend_arg(args) -> None:
    backend = _backend_arg(args)
    if not backend:
        return
    backend = str(backend).strip().lower()
    from .backends import get_backend_class

    get_backend_class(backend)
    os.environ[_EXPLICIT_BACKEND_ENV] = backend
    os.environ["MEMPALACE_BACKEND"] = backend


def _selected_backend_for_palace(palace_path: str) -> str:
    from .palace import resolve_backend_name

    return resolve_backend_name(palace_path, explicit=os.environ.get(_EXPLICIT_BACKEND_ENV))


def _maintenance_requires_chroma(palace_path: str, command_name: str) -> bool:
    try:
        backend_name = _selected_backend_for_palace(palace_path)
    except Exception as exc:  # noqa: BLE001 - user-facing guard before maintenance imports
        print(f"\n  {command_name} cannot resolve the palace backend: {exc}", file=sys.stderr)
        return False
    if backend_name == "chroma":
        return True
    print(
        f"\n  {command_name} is Chroma-only in this release (selected backend: {backend_name}).",
        file=sys.stderr,
    )
    return False


def _gather_origin_samples(project_dir) -> list:
    """Collect Tier-1 samples for corpus-origin detection.

    Reads FULL file content (capped at ``_PASS_ZERO_PER_FILE_CAP`` per file
    and ``_PASS_ZERO_TOTAL_CAP`` overall). No front-bias sampling — AI
    signal that lives past the first N chars of a file must still trip
    detection, so we read the whole file up to the cap.

    Skips mempalace's own per-project artifacts (``entities.json``,
    ``mempalace.yaml``) so a re-run of ``mempalace init`` produces the
    same classification result it did on the first run. Without this
    filter, the first run writes entities.json into the corpus, the
    second run picks it up as a sample, and the Tier-1 density math
    drifts (different total_chars). That makes init non-idempotent.

    Returns a list of strings (one per readable file). Empty list when
    the project has no readable text.
    """
    from .entity_detector import scan_for_detection

    files = scan_for_detection(project_dir, max_files=_PASS_ZERO_MAX_FILES)
    samples: list = []
    total_chars = 0
    for filepath in files:
        if filepath.name in _MEMPALACE_PROJECT_FILES:
            continue
        if total_chars >= _PASS_ZERO_TOTAL_CAP:
            break
        try:
            # ``scan_for_detection`` picks candidates by extension, so a FIFO
            # named ``notes.md`` reaches this loop; opening one for reading
            # blocks until a writer appears. ``is_file()`` stats instead.
            # It belongs inside the try: it raises PermissionError on an
            # unreadable directory, which the open below used to absorb.
            if not filepath.is_file():
                continue
            with open(filepath, encoding="utf-8", errors="replace") as f:
                content = f.read(_PASS_ZERO_PER_FILE_CAP)
        except OSError:
            continue
        if not content:
            continue
        samples.append(content)
        total_chars += len(content)
    return samples


def _trim_samples_for_llm(samples: list) -> list:
    """Reduce Tier-1 full-content samples to LLM-friendly size.

    Tier 2 hits an LLM with a finite context window — we trim each sample
    to ``_PASS_ZERO_LLM_PER_SAMPLE`` chars and cap the overall sample
    count at ``_PASS_ZERO_LLM_MAX_SAMPLES``.
    """
    return [s[:_PASS_ZERO_LLM_PER_SAMPLE] for s in samples[:_PASS_ZERO_LLM_MAX_SAMPLES]]


def _run_pass_zero(project_dir, palace_dir, llm_provider) -> dict:
    """Pass 0: detect whether the corpus is AI-dialogue and persist the
    result to ``<palace>/.mempalace/origin.json``.

    Returns the wrapped result dict (same shape as origin.json) on success,
    or ``None`` when there are no readable samples to detect from. The
    return value is what cmd_init forwards to ``discover_entities`` via
    the ``corpus_origin`` kwarg.

    File-write failures (e.g. read-only palace) are caught and reported on
    stderr; init never blocks on them.
    """
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    samples = _gather_origin_samples(project_dir)
    if not samples:
        print("  Skipping corpus-origin detection -- no readable samples.")
        return None

    # Tier 1 — always runs. Cheap regex grep, no API.
    result = detect_origin_heuristic(samples)

    # Tier 2 — runs only when an LLM provider is available. The provider
    # contract is best-effort: corpus_origin internally falls back to a
    # conservative default on transport/parse failure, so we don't need a
    # try/except here, but we still keep one for any unforeseen exception.
    #
    # MERGE-FIELDS, NOT REPLACE: Tier 2's persona/user/platform extraction
    # is the whole reason to run it, but a weak local model (e.g. Ollama
    # gemma4:e4b) can return a wrong likely_ai_dialogue/confidence call
    # that overrides a confident heuristic answer. Per @igorls's review of
    # PR #1211: keep the heuristic's likely_ai_dialogue + confidence
    # (don't let a weak LLM flip a confident regex answer), and merge in
    # LLM's persona-related fields + combined evidence.
    if llm_provider is not None:
        try:
            llm_result = detect_origin_llm(_trim_samples_for_llm(samples), llm_provider)
            # Heuristic owns: likely_ai_dialogue, confidence (do NOT touch).
            # LLM contributes: primary_platform, user_name, agent_persona_names
            # (heuristic doesn't extract any of these).
            if llm_result.primary_platform:
                result.primary_platform = llm_result.primary_platform
            if llm_result.user_name:
                result.user_name = llm_result.user_name
            if llm_result.agent_persona_names:
                result.agent_persona_names = list(llm_result.agent_persona_names)
            # Combine evidence — keep both signal trails for the audit record,
            # prefixed so the on-disk origin.json says which tier produced
            # each entry. Idempotent: re-prefixing an already-tagged entry
            # is a no-op.
            tier1_prefix = "Tier-1 heuristic: "
            tier2_prefix = "Tier-2 LLM: "
            heuristic_evidence = [
                s if s.startswith(tier1_prefix) else f"{tier1_prefix}{s}"
                for s in (str(e) for e in result.evidence)
            ]
            llm_evidence = [
                s if s.startswith(tier2_prefix) else f"{tier2_prefix}{s}"
                for s in (str(e) for e in llm_result.evidence)
            ]
            result.evidence = heuristic_evidence + llm_evidence
        except Exception as exc:  # noqa: BLE001 — never block init on LLM failure
            print(f"  LLM corpus-origin tier failed ({exc}); using heuristic only.")

    wrapped = {
        "schema_version": 1,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "result": result.to_dict(),
    }

    origin_path = Path(palace_dir).expanduser() / ".mempalace" / "origin.json"
    try:
        origin_path.parent.mkdir(parents=True, exist_ok=True)
        with open(origin_path, "w", encoding="utf-8") as f:
            json.dump(wrapped, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"  Could not write {origin_path}: {exc}", file=sys.stderr)
        # Return the wrapped dict anyway so the in-memory pipeline still
        # benefits from the detection result this run.
        return wrapped

    # Banner — one line, two-space indent matching existing init style.
    res = result
    if res.likely_ai_dialogue:
        platform = res.primary_platform or "AI dialogue (platform unidentified)"
        user = res.user_name or "—"
        agents = ", ".join(res.agent_persona_names) if res.agent_persona_names else "—"
        print(f"  Detected: {platform} (user: {user}, agents: {agents})")
    else:
        print(f"  Corpus origin: not AI-dialogue (confidence: {res.confidence:.2f})")

    return wrapped


def _ensure_mempalace_files_gitignored(project_dir) -> bool:
    """If project_dir is a git repo, ensure MemPalace's per-project files
    are listed in .gitignore so they don't get committed by accident.

    Returns True if .gitignore was updated, False otherwise. Issue #185:
    `mempalace init` writes mempalace.yaml + entities.json into the
    project root, where they previously had no protection against being
    staged into git.
    """
    from pathlib import Path

    project_path = Path(project_dir).expanduser().resolve()
    if not (project_path / ".git").exists():
        return False
    gitignore = project_path / ".gitignore"
    # ``exists()`` is true for a FIFO, and both the read below and the append
    # at the end of this function would block in the kernel on one. Decide by
    # type instead: an absent file still yields "" as before, a regular one
    # is read, and anything else is left untouched.
    if gitignore.exists() and not gitignore.is_file():
        return False
    # Force UTF-8: Windows defaults to GBK and chokes on non-ASCII .gitignore
    # comments, killing auto-init even though the file is valid UTF-8.
    existing = (
        gitignore.read_text(encoding="utf-8", errors="replace") if gitignore.is_file() else ""
    )
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing = [p for p in _MEMPALACE_PROJECT_FILES if p not in existing_lines]
    if not missing:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = prefix + "\n# MemPalace per-project files (issue #185)\n" + "\n".join(missing) + "\n"
    with open(gitignore, "a", encoding="utf-8") as f:
        f.write(block)
    print(f"  Added {', '.join(missing)} to {gitignore.name}")
    return True


def cmd_init(args):
    import json
    from pathlib import Path
    from .entity_detector import confirm_entities
    from .project_scanner import discover_entities
    from .room_detector_local import detect_rooms_local

    # Honor --palace (issue #1313): without this, init silently ignored the
    # flag and always used ~/.mempalace. Mirror the env-var pattern used by
    # mcp_server.py so every downstream read of ``cfg.palace_path`` (Pass 0,
    # cfg.init(), the post-init mine) routes to the user-specified location.
    if getattr(args, "palace", None):
        os.environ["MEMPALACE_PALACE_PATH"] = os.path.abspath(os.path.expanduser(args.palace))

    cfg = MempalaceConfig()

    # Resolve entity-detection languages: --lang overrides config.
    lang_arg = getattr(args, "lang", None)
    if lang_arg:
        languages = [s.strip() for s in lang_arg.split(",") if s.strip()] or ["en"]
        cfg.set_entity_languages(languages)
    else:
        languages = cfg.entity_languages
    languages_tuple = tuple(languages)

    # --llm is ON by default. --no-llm is the explicit opt-out. Provider
    # precedence is unchanged (Ollama localhost first, then openai-compat,
    # then anthropic). Never block init on a missing LLM: when no provider
    # responds, print a one-line message pointing at --no-llm and fall
    # through to heuristics-only.
    llm_provider = None
    if not getattr(args, "no_llm", False):
        provider_name = getattr(args, "llm_provider", "ollama") or "ollama"
        provider_model = getattr(args, "llm_model", "gemma4:e4b") or "gemma4:e4b"
        try:
            candidate = get_provider(
                name=provider_name,
                model=provider_model,
                endpoint=getattr(args, "llm_endpoint", None),
                api_key=getattr(args, "llm_api_key", None),
            )
            if (
                provider_name == "openai-compat"
                and getattr(candidate, "api_key_source", None) == "env"
                and candidate.is_external_service
            ):
                ok = False
                msg = "external openai-compat init requires explicit --llm-api-key"
                print(f"  LLM skipped: {msg}")
            else:
                ok, msg = candidate.check_available()
            if ok:
                llm_provider = candidate
                print(f"  LLM enabled: {provider_name}/{provider_model}")
                # Privacy warning (issue #24): if the configured endpoint
                # sends data off the user's machine/network, surface that
                # before init proceeds. URL-based — Ollama on localhost,
                # LM Studio on LAN, etc. won't trigger; Anthropic /
                # cloud OpenAI-compat / any non-local endpoint will.
                if candidate.is_external_service:
                    print(
                        f"  ⚠ {provider_name} is an EXTERNAL API. Your folder "
                        f"content will be sent to the provider during init. "
                        f"MemPalace does not control how the provider logs, "
                        f"retains, or uses your data. Pass --no-llm to keep "
                        f"init fully local."
                    )
                    # Consent gate (issue #26): block init when the api_key
                    # was acquired via env-fallback (stray credential in
                    # shell env). Explicit --llm-api-key (api_key_source ==
                    # "flag") means the user already opted in.
                    # --accept-external-llm bypasses for CI / non-interactive.
                    api_key_source = getattr(candidate, "api_key_source", None)
                    accept_flag = getattr(args, "accept_external_llm", False)
                    if api_key_source == "env" and not accept_flag:
                        try:
                            answer = (
                                input(
                                    "  Your API key was loaded from the environment "
                                    "(not passed via --llm-api-key). Continue with "
                                    "external LLM? [y/N] "
                                )
                                .strip()
                                .lower()
                            )
                        except EOFError:
                            answer = ""
                        if answer != "y":
                            print(
                                "  Declined — falling back to heuristics-only. "
                                "Pass --llm-api-key explicitly or "
                                "--accept-external-llm to skip this prompt."
                            )
                            llm_provider = None
            else:
                print(
                    f"  No LLM provider reachable ({msg}). "
                    f"Running heuristics-only — pass --no-llm to silence this."
                )
        except LLMError as e:
            print(
                f"  LLM init failed ({e}). Running heuristics-only — pass --no-llm to silence this."
            )

    # Pass 0: detect whether the corpus is AI-dialogue. Writes
    # <palace>/.mempalace/origin.json and supplies corpus context to the
    # entity classifier so it can correctly handle agent persona names
    # (e.g. "Echo", "Sparrow") without misclassifying them as people.
    corpus_origin = _run_pass_zero(
        project_dir=args.dir,
        palace_dir=cfg.palace_path,
        llm_provider=llm_provider,
    )

    # Pass 1: discover entities — manifests + git authors first, prose detection
    # as supplement for names mentioned only in docs/notes. Optional phase-2
    # LLM refinement runs inside discover_entities when llm_provider is given.
    print(f"\n  Scanning for entities in: {args.dir}")
    if languages_tuple != ("en",):
        print(f"  Languages: {', '.join(languages_tuple)}")
    detected = discover_entities(
        args.dir,
        languages=languages_tuple,
        llm_provider=llm_provider,
        corpus_origin=corpus_origin,
    )
    total = (
        len(detected["people"])
        + len(detected["projects"])
        + len(detected.get("topics", []))
        + len(detected["uncertain"])
    )
    if total > 0:
        confirmed = confirm_entities(detected, yes=getattr(args, "yes", False))
        # Save confirmed entities to <project>/entities.json (per-project
        # audit trail — user can inspect or hand-edit) AND merge into the
        # global registry the miner reads at mine time. Topics are kept
        # separately so the miner can later compute cross-wing tunnels
        # from shared topics (see palace_graph.compute_topic_tunnels).
        if confirmed["people"] or confirmed["projects"] or confirmed.get("topics"):
            project_path = Path(args.dir).expanduser().resolve()
            entities_path = project_path / "entities.json"
            # Opening a pre-existing FIFO for writing blocks in the kernel
            # until a reader appears. Only a regular file is a valid target
            # for the per-project audit trail; the global registry merge
            # below is unaffected either way.
            if entities_path.exists() and not entities_path.is_file():
                print(
                    f"  ! Not writing entities: {entities_path} is not a regular file",
                    file=sys.stderr,
                )
            else:
                with open(entities_path, "w", encoding="utf-8") as f:
                    json.dump(confirmed, f, indent=2, ensure_ascii=False)
                print(f"  Entities saved: {entities_path}")

            from .config import normalize_wing_name
            from .miner import add_to_known_entities

            # Match the slug ``room_detector_local`` writes into
            # ``mempalace.yaml`` so the miner's tunnel lookup hits the
            # same key in ``topics_by_wing`` at mine time (issue #1194 —
            # without this, hyphenated dirnames silently lose tunnels).
            wing = normalize_wing_name(project_path.name)
            registry_path = add_to_known_entities(confirmed, wing=wing)
            print(f"  Registry updated: {registry_path}")
    else:
        print("  No entities detected -- proceeding with directory-based rooms.")

    # Pass 2: detect rooms from folder structure
    try:
        detect_rooms_local(project_dir=args.dir, yes=getattr(args, "yes", False))
    except OSError as exc:
        # Writing mempalace.yaml is the point of init; a target it cannot
        # write (a pre-existing pipe, a full disk) is a hard failure, and a
        # message beats the traceback this used to produce.
        print(f"\n  ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    cfg.init()
    backend = _backend_arg(args)
    if backend:
        cfg.set_backend(backend)

    # Pass 3: protect git repos from accidentally committing per-project files
    _ensure_mempalace_files_gitignored(args.dir)

    # Pass 4: offer to run mine immediately. The directory just had its
    # rooms + entities set up, so 99% of users will mine next anyway —
    # asking here removes the "remember to type the next command" friction.
    # `--auto-mine` skips the prompt and mines automatically; `--yes` is
    # SCOPED to entity auto-accept and does NOT imply mining.
    _maybe_run_mine_after_init(args, cfg)


def _format_size_mb(num_bytes: int) -> str:
    """Render a byte count as a human-readable size for the mine estimate.

    < 1 MB rounds up to ``<1 MB`` so users never see a misleading ``0 MB``
    on small projects. Otherwise reports an integer megabyte count.
    """
    if num_bytes <= 0:
        return "<1 MB"
    mb = num_bytes / (1024 * 1024)
    if mb < 1:
        return "<1 MB"
    return f"{mb:.0f} MB"


def _maybe_run_mine_after_init(args, cfg) -> None:
    """Prompt the user to mine the directory just initialised, or auto-mine
    when ``--auto-mine`` was passed. Extracted so the prompt path is
    unit-testable.

    Behaviour matrix:

    - default (no flags) — prompt, default Yes, mine in-process if accepted
    - ``--yes`` — entity auto-accept only; STILL prompts for the mine step
    - ``--auto-mine`` — skip the mine prompt and mine directly
    - ``--yes --auto-mine`` — fully non-interactive

    Mine errors are surfaced (not swallowed): a failing mine exits with a
    non-zero status via :func:`sys.exit` so downstream scripts can see it.
    The pre-scan that produces the file-count estimate is reused as the
    mine input so we never walk the corpus twice.
    """
    from .miner import mine, scan_project

    project_dir = args.dir
    auto_mine = bool(getattr(args, "auto_mine", False))

    # Single corpus walk: this scan feeds BOTH the "what would be mined"
    # estimate the user sees in the prompt AND the file list mine() will
    # process. We pass the result into mine() via the `files` kwarg so it
    # doesn't re-walk the tree.
    try:
        scanned_files = scan_project(project_dir)
        file_count = len(scanned_files)
        total_bytes = 0
        for fp in scanned_files:
            try:
                total_bytes += fp.stat().st_size
            except OSError:
                # Skip files that vanished between scan and stat — mine()
                # will skip them too.
                continue
        size_str = _format_size_mb(total_bytes)
    except Exception:
        scanned_files = None
        file_count = None
        size_str = None

    # Show the scope estimate BEFORE the prompt so the user knows what
    # they are agreeing to. On a real corpus mine takes minutes; hitting
    # Enter on a default-Y prompt with no size cue is a footgun.
    if isinstance(file_count, int):
        if size_str:
            print(f"  ~{file_count} files (~{size_str}) would be mined into this palace.\n")
        else:
            print(f"  ~{file_count} files would be mined into this palace.\n")

    if not auto_mine:
        try:
            answer = input("  Mine this directory now? [Y/n] ").strip().lower()
        except EOFError:
            # Non-interactive stdin (e.g. piped) — treat like decline so
            # we don't block. User can re-run with --auto-mine to opt in.
            answer = "n"
        if answer not in ("", "y", "yes"):
            print(f"\n  Skipped. Run `mempalace mine {shlex.quote(project_dir)}` when ready.")
            return

    palace_path = cfg.palace_path
    try:
        mine(
            project_dir=project_dir,
            palace_path=palace_path,
            files=scanned_files,
        )
    except KeyboardInterrupt:
        # mine() handles its own SIGINT summary + sys.exit(130); re-raise
        # any KeyboardInterrupt that escapes (shouldn't happen) so the
        # shell still sees a clean interrupt rather than a swallowed one.
        raise
    except Exception as e:
        print(f"\n  ERROR: mine failed: {e}", file=sys.stderr)
        sys.exit(1)


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
    from .config import sanitize_name, strip_lone_surrogates
    from .query_sanitizer import SAFE_QUERY_LENGTH

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

    from . import server_registry

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

    Flags the tool has no parameters for (kg-extract, gitignore handling,
    chunking overrides, origin redetection, explicit backend) keep the
    direct path — where a held writer lease still surfaces as the existing
    MineAlreadyRunning error rather than being silently dropped.
    """
    if getattr(args, "kg_extract", False) or getattr(args, "redetect_origin", False):
        return False
    if args.no_gitignore or include_ignored:
        return False
    if getattr(args, "max_chunks_per_file", None) is not None:
        return False
    if _backend_arg(args):
        return False
    return True


def _forward_mine_to_hub(args, palace_path: str) -> bool:
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

    from . import server_registry

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

    arguments = {
        "source": os.path.abspath(os.path.expanduser(args.dir)),
        "mode": args.mode,
        "agent": args.agent,
        "limit": args.limit or 0,
        "dry_run": bool(args.dry_run),
        "extract": args.extract,
    }
    if args.wing:
        arguments["wing"] = args.wing
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
    from .config import normalize_wing_name
    from .sources import (
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
    from .knowledge_graph import KnowledgeGraph
    from .palace import get_collection

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
    from .config import normalize_wing_name

    wing_source = Path(directory)
    if wing_source.is_file():
        from .miner import resolve_project_root

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
        from .miner import READABLE_EXTENSIONS
    except Exception:  # pragma: no cover — miner import is not optional in practice
        return bool(path.suffix)
    return path.suffix.lower() in READABLE_EXTENSIONS


def cmd_mine(args):
    from .palace import MineAlreadyRunning, MineValidationError

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

    # Explicit --daemon submits to the opt-in local job-queue daemon
    # (upstream #1783 family). This is a deliberate user request and takes
    # precedence over the ambient PALACE_DAEMON_URL HTTP routing below — the
    # two are different daemons; the flag names the one the user asked for.
    if getattr(args, "background", False) and not getattr(args, "daemon", False):
        print("mempalace: --background requires --daemon", file=sys.stderr)
        sys.exit(2)
    if getattr(args, "daemon", False):
        include_ignored = []
        for raw in args.include_ignored or []:
            include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())
        payload = {
            "source": args.dir,
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
        _submit_daemon_cli_job("mine", payload, args, background=getattr(args, "background", False))
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
        if ignored_flags:
            print(
                f"  WARN: daemon-strict mode ignores these local-only flags: {', '.join(ignored_flags)}",
                file=sys.stderr,
            )

        directory = os.path.abspath(os.path.expanduser(args.dir))
        wing = args.wing or _derive_daemon_mine_wing(directory, args.dir)
        ok = _post_daemon_mine_cli(directory, wing=wing, mode=args.mode)
        sys.exit(0 if ok else 1)

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    include_ignored = []
    for raw in args.include_ignored or []:
        include_ignored.extend(part.strip() for part in raw.split(",") if part.strip())

    # A live HTTP hub for this palace holds the MCP writer lease, so a
    # direct mine here would be refused. Hand the job to the hub instead —
    # this is how the save hooks keep capturing transcripts on a machine
    # that runs `mempalace serve`.
    if _mine_args_forwardable(args, include_ignored) and _forward_mine_to_hub(args, palace_path):
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
            from .convo_miner import mine_sessions

            mine_sessions(
                convo_dir=args.dir,
                palace_path=palace_path,
                wing=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        elif mode == "convos":
            from .convo_miner import mine_convos

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
            from .format_miner import mine_formats

            mine_formats(
                format_dir=args.dir,
                palace_path=palace_path,
                wing=args.wing,
                agent=args.agent,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        else:
            from .miner import mine

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
        from .repair import print_sqlite_integrity_abort

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
        from .backends import QueryResult

        query_input = kwargs.get("query_texts", kwargs.get("query_embeddings"))
        num_queries = len(query_input) if isinstance(query_input, (list, tuple)) else 1
        include = kwargs.get("include") or []
        return QueryResult.empty(
            num_queries=num_queries,
            embeddings_requested="embeddings" in include,
        )

    def get(self, **kwargs):
        from .backends import GetResult

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
    from .knowledge_graph import KnowledgeGraph
    from .palace import get_collection, mine_palace_lock
    from .sources import (
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
    from .sweeper import sweep, sweep_directory

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    target = os.path.expanduser(args.target)

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
    from .migrate import confirm_destructive_action

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
        from .sync import validate_apply_scope

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


def cmd_sync(args):
    """Prune drawers whose source files are gitignored, deleted, or moved (#1252).

    Where it runs (#418): ``--daemon`` submits to the local job queue;
    daemon-strict with no ``--palace`` routes to the palace daemon; otherwise
    the backend is resolved *before* the palace-directory precheck, and that
    precheck is skipped for a service-backed store, which has no local
    database file by design.
    """
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    if getattr(args, "background", False) and not getattr(args, "daemon", False):
        print("mempalace: --background requires --daemon", file=sys.stderr)
        sys.exit(2)

    if getattr(args, "daemon", False):
        payload = {
            "dir": args.dir,
            "root": list(args.root or []),
            "wing": args.wing,
            "dry_run": args.dry_run,
        }
        _submit_daemon_cli_job("sync", payload, args, background=getattr(args, "background", False))
        return

    if _daemon_strict() and not getattr(args, "palace", None):
        _sync_via_daemon(args)
        return

    from .palace import MineAlreadyRunning
    from .wal import _wal_log
    from .backends import detect_backend_for_path
    from .palace import (
        _backend_artifact_label,
        backend_stores_data_locally,
        resolve_backend_name,
    )
    from .sync import sync_palace

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
        from .sync import validate_apply_scope

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


def _daemon_search_fast(query: str, n_results: int, wing: str = None) -> dict | None:
    """BM25 fast path via GET /search/fast. Returns normalised data dict or None."""
    rest_params = {"q": query, "limit": n_results}
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
    annotate(hits)
    return {"results": hits, "query": query, "source": "bm25-fast"}


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
    annotate(data.get("results"))
    return data


def _submit_daemon_cli_job(kind: str, payload: dict, args, *, background: bool) -> None:
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    backend = _backend_arg(args)
    from .daemon import DaemonError, submit_job

    try:
        job = submit_job(
            kind,
            payload,
            palace_path=palace_path,
            backend=backend,
            wait=not background,
            auto_start=True,
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

    from .daemon import job_deferred_by_lock

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
    from .service import print_job_result

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
    from .daemon import (
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
            from .service import print_job_result

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
    except DaemonError:
        fb = None
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
                annotate(data.get("results") if isinstance(data, dict) else None)
        except DaemonError as e:
            if want_json:
                _emit_json({"error": str(e), "source": "daemon", "query": args.query})
            else:
                print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(2)
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

    from .searcher import SearchError, search, search_memories

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
    except DaemonError as e:
        # Match cmd_status's daemon-down fallback (line 2230) and the
        # graceful 401/403 + unreachable handling added in 850e08c. On
        # JSON output, emit a structured error so machine callers see
        # the same shape as other failure paths.
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

    if data is None:
        # _call_daemon_rest returns None on 404/401/403 — endpoint
        # missing on an older daemon, or auth mismatch. Same exit code
        # as the unreachable case so scripts can treat "no daemon list"
        # uniformly without parsing the message.
        if want_json:
            _emit_json({"error": "daemon /list unavailable", "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                "see mempalace status for diagnostics",
                file=sys.stderr,
            )
        sys.exit(1)

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
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

    if data is None:
        # _patch_daemon_rest returns None on 404/401/403 — route missing on
        # an older daemon, or auth mismatch. Exit 1 matches the unreachable
        # case so scripts treat "no daemon move" uniformly.
        if want_json:
            _emit_json({"error": "daemon PATCH /memory unavailable", "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                "see mempalace status for diagnostics",
                file=sys.stderr,
            )
        sys.exit(1)

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
        except DaemonError as e:
            if want_json:
                _emit_json({"error": str(e), "source": "daemon"})
            else:
                print(
                    f"palace daemon unreachable at {_daemon_url()} — "
                    f"see mempalace status for diagnostics ({e})",
                    file=sys.stderr,
                )
            sys.exit(1)

        if data is None:
            if want_json:
                _emit_json({"error": "daemon /list unavailable", "source": "daemon"})
            else:
                print(
                    f"palace daemon unreachable at {_daemon_url()} — "
                    "see mempalace status for diagnostics",
                    file=sys.stderr,
                )
            sys.exit(1)

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
    except DaemonError as e:
        # Match cmd_list / cmd_status daemon-down fallback. JSON callers
        # get a structured error on stdout; humans get the standard
        # "daemon unreachable" line on stderr.
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

    if data is None:
        # _call_daemon_rest returns None on 404/401/403 — endpoint
        # missing on an older daemon, or auth mismatch. Treat the same
        # as unreachable so scripts get one failure shape.
        if want_json:
            _emit_json({"error": "daemon /graph unavailable", "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                "see mempalace status for diagnostics",
                file=sys.stderr,
            )
        sys.exit(1)

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
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

    if status == 403:
        # Server-enforced read-only: SQLSTATE 25006 surfaces as HTTP 403.
        # Don't dump traceback noise — give the operator a one-line hint
        # that maps to the next action.
        hint = (
            "daemon /cypher returned 403 — this endpoint is read-only; "
            "rewrite as MATCH / RETURN, or use the mempalace_kg_* MCP tools to mutate"
        )
        if want_json:
            _emit_json({"error": hint, "source": "daemon", "status": 403})
        else:
            print(hint, file=sys.stderr)
        sys.exit(2)

    if status is not None:
        # 401/404/503 etc — endpoint missing on an older daemon, auth
        # mismatch, or non-postgres backend. Treat the same as
        # unreachable so scripts get one failure shape.
        if want_json:
            _emit_json(
                {
                    "error": f"daemon /cypher returned {status}",
                    "source": "daemon",
                    "status": status,
                }
            )
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"/cypher returned {status} (see mempalace status for diagnostics)",
                file=sys.stderr,
            )
        sys.exit(1)

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

    from .layers import MemoryStack

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    stack = MemoryStack(palace_path=palace_path)

    text = stack.wake_up(wing=args.wing)
    tokens = len(text) // 4
    print(f"Wake-up text (~{tokens} tokens):")
    print("=" * 50)
    print(text)


def cmd_split(args):
    """Split concatenated transcript mega-files into per-session files."""
    from .split_mega_files import main as split_main
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
    from .exporter import export_palace

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
    from .migrate import migrate

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
    from .migrate_to_postgres import run_migration

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
    from .migrate import confirm_destructive_action

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
    from .backends import detect_backend_for_path
    from .migrate import confirm_destructive_action, contains_palace_database
    from .palace import (
        BackendMismatchError,
        backend_stores_data_locally,
        get_collection,
        resolve_backend_name,
    )

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

    try:
        backend_name = resolve_backend_name(palace_path, explicit=_backend_arg(args))
    except BackendMismatchError as exc:
        print(f"\n  Backend mismatch at {palace_path}: {exc}")
        print("  Select the matching backend or use a fresh palace directory.")
        sys.exit(2)
    except KeyError as exc:
        print(f"\n  Unknown backend selected for {palace_path}: {exc.args[0] if exc.args else exc}")
        print("  Set --backend or MEMPALACE_BACKEND to a registered backend.")
        sys.exit(2)

    stores_locally = backend_stores_data_locally(backend_name)
    if stores_locally:
        # ``contains_palace_database`` is chroma-specific (chroma.sqlite3);
        # the artifact probe covers the other local backends, which this
        # command used to reject outright for not being chroma.
        has_db = os.path.isdir(palace_path) and (
            contains_palace_database(palace_path)
            or detect_backend_for_path(palace_path) is not None
        )
        if not has_db:
            _print_retired_local_palace_or_default(palace_path)
            return
        target = f"{palace_path} ({backend_name})"
    else:
        target = f"{backend_name} backend (palace {palace_path})"

    try:
        col = get_collection(
            palace_path,
            collection_name="mempalace_drawers",
            create=False,
            backend=backend_name,
        )
    except Exception as e:
        # Exit non-zero: an unreachable backend is a purge that did not
        # happen, and #418 is precisely about those looking like success.
        print(f"\n  Error reading palace: {e}")
        sys.exit(1)

    # Probe match count via a `where=`-filtered get with no payload.
    try:
        matched = col.get(where=where, include=[])
    except Exception as e:
        print(f"\n  Error querying drawers: {e}")
        sys.exit(1)

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
        sys.exit(1)

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

    from .backends.base import PalaceRef
    from .backends.chroma import ChromaBackend
    from .migrate import contains_palace_database
    from .recency import age_days

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

    if not os.path.isdir(palace_path) or not contains_palace_database(palace_path):
        if want_json:
            print(json.dumps({"error": "no palace database", "palace": palace_path}))
        else:
            _print_retired_local_palace_or_default(palace_path)
        return

    # Optional wing/room scope — without it, prune spans the whole palace.
    clauses = []
    if args.wing:
        clauses.append({"wing": args.wing})
    if args.room:
        clauses.append({"room": args.room})
    where = None
    if clauses:
        where = clauses[0] if len(clauses) == 1 else {"$and": clauses}

    backend = ChromaBackend()
    try:
        col = backend.get_collection(
            palace=PalaceRef(id=palace_path, local_path=palace_path),
            collection_name="mempalace_drawers",
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}) if want_json else f"\n  Error reading palace: {e}")
        return

    # Pull ids + metadata for the scope; age is decided in Python.
    try:
        got = (
            col.get(where=where, include=["metadatas"]) if where else col.get(include=["metadatas"])
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}) if want_json else f"\n  Error querying drawers: {e}")
        return

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
        print(json.dumps({"error": str(e)}) if want_json else f"\n  Delete failed: {e}\n")
        return

    remaining = col.count()
    if want_json:
        print(
            json.dumps(
                {
                    "stale_days": stale_days,
                    "scope": scope,
                    "deleted": len(stale_ids),
                    "remaining": remaining,
                }
            )
        )
    else:
        print(f"\n  Pruned {len(stale_ids):,} drawers. Remaining: {remaining:,}\n")


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

    from .backends.chroma import ChromaBackend
    from .backends.base import PalaceRef

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


def cmd_replay(args):
    """Drain ``~/.mempalace/pending/*.jsonl`` by re-issuing each request to the daemon.

    Pending requests accumulate when the Stop / PreCompact hooks fire while
    the daemon (or its backend) is unreachable — see the 2026-05-21
    power-resilience design. Drain semantics:

    * Each line is one ``{"dir", "wing", "mode", "ts"}`` mine request.
    * On 2xx daemon response the line is consumed; on failure the line
      stays in the file for the next attempt.
    * Duplicate ``(dir, wing, mode)`` tuples are deduped before transmit
      so a long outage doesn't replay the same target N times.
    """
    if not _daemon_strict():
        print(
            "mempalace replay: nothing to do (PALACE_DAEMON_URL not set or strict mode off).",
            file=sys.stderr,
        )
        return 0

    try:
        from . import pending_queue
    except Exception as e:
        print(f"  ERROR: could not import pending_queue: {e}", file=sys.stderr)
        return 1

    def post(request: dict) -> bool:
        # _post_daemon_mine_cli doesn't share the hook's pending-queue
        # re-enqueue path, so skip_queue isn't applicable here; the
        # CLI variant prints to stderr and returns bool unconditionally.
        return _post_daemon_mine_cli(request["dir"], request["wing"], request.get("mode", "convos"))

    report = pending_queue.replay(post)
    if report.is_empty:
        print("mempalace replay: pending queue is empty.")
        return 0

    print(
        f"mempalace replay: attempted={report.attempted} "
        f"succeeded={report.succeeded} failed={report.failed} "
        f"files_drained={report.files_drained}"
    )
    return 0 if report.failed == 0 else 1


def cmd_migrate_wings(args):
    """Normalize legacy wing names (strip leading/trailing separators)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    from .migrate import migrate_wing_names

    migrate_wing_names(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


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
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "level": level})

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
        except Exception as e:  # noqa: BLE001 — a doctor never raises
            add("daemon", False, "unreachable @ {}: {}".format(_daemon_url(), e), "error")
    else:
        add("daemon", None, "no daemon configured (local-palace mode)", "warn")

    # 3. This project's wing.
    wing = getattr(args, "wing", None)
    if not wing:
        from .config import normalize_wing_name

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
            if want_json:
                _emit_json({"error": str(e), "source": "daemon"})
            else:
                print(f"\n  ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        if want_json:
            _emit_json(data)
            return
        _print_daemon_status(data)
        return

    from .miner import status

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

    from .miner import _open_collection_or_explain

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

    from .backends.chroma import ChromaBackend
    from .migrate import contains_palace_database

    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    )

    if not os.path.isdir(palace_path) or not contains_palace_database(palace_path):
        if want_json_early:
            _emit_json(
                {
                    "error": "palace_unavailable",
                    "hint": f"No palace database at {palace_path}",
                    "palace_path": palace_path,
                }
            )
            sys.exit(2)
        _print_retired_local_palace_or_default(palace_path)
        return

    backend = ChromaBackend()
    from .backends.base import PalaceRef

    try:
        col = backend.get_collection(
            palace=PalaceRef(id=palace_path, local_path=palace_path),
            collection_name="mempalace_drawers",
        )
    except Exception as e:
        if want_json_early:
            _emit_json(
                {
                    "error": "palace_unavailable",
                    "hint": f"Error reading palace: {e}",
                    "palace_path": palace_path,
                }
            )
            sys.exit(2)
        print(f"\n  Error reading palace: {e}")
        return

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
    except DaemonError as e:
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

    if data is None:
        if want_json:
            _emit_json({"error": "daemon /stats unavailable", "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                "see mempalace status for diagnostics",
                file=sys.stderr,
            )
        sys.exit(1)

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
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

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
    from .config import sanitize_kg_value
    from .knowledge_graph_age import _cypher_literal

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


def cmd_hallways(args):
    """List within-wing entity hallways (the auto-built associative graph).

    DEPRECATED (#407): ``mempalace hallway list`` is the first-class verb —
    daemon-routed, paginated, with ``--json``. This legacy verb stays for
    scripts that call it, prints a one-line notice on stderr, and now honours
    ``--json`` instead of silently ignoring it (a ``jq`` pipeline used to get
    human text and exit 0).
    """
    from .hallways import list_hallways

    want_json = _resolve_read_format(args) == "json"
    if not want_json:
        print(
            "mempalace: `hallways` is deprecated — use `mempalace hallway list` "
            "(daemon-routed, --json, pagination). See techempower-org/mempalace#407.",
            file=sys.stderr,
        )

    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    rows = list_hallways(
        getattr(args, "wing", None),
        config=MempalaceConfig(palace_path=palace_path),
    )
    rows.sort(key=lambda h: h.get("co_occurrence_count", 0), reverse=True)
    limit = max(0, getattr(args, "limit", 50))
    if want_json:
        _emit_json(
            {
                "hallways": rows[:limit],
                "wing_filter": getattr(args, "wing", None),
                "total": len(rows),
                "deprecated": "use `mempalace hallway list`",
            }
        )
        return
    if not rows:
        print("No hallways yet -- they are built from drawer entities when you mine.")
        return
    print(f"  {len(rows)} hallway(s):")
    for h in rows[:limit]:
        label = h.get("label") or f"{h.get('entity_a', '?')} <-> {h.get('entity_b', '?')}"
        print(f"    {label}")


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
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

    if status is not None:
        # Any non-2xx from /cypher — older daemon (404), auth (401/403),
        # or 503 if not on postgres backend. Same shape as cmd_cypher's
        # fallthrough — exit 1, scripts treat all daemon-side failures
        # uniformly.
        if want_json:
            _emit_json(
                {
                    "error": f"daemon /cypher returned {status}",
                    "source": "daemon",
                    "status": status,
                }
            )
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"/cypher returned {status} (see mempalace status for diagnostics)",
                file=sys.stderr,
            )
        sys.exit(1)

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
    from .config import sanitize_kg_value
    from .knowledge_graph_age import _cypher_literal

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
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

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
            if want_json:
                _emit_json({"error": str(e), "source": "daemon"})
            else:
                print(
                    f"palace daemon unreachable at {_daemon_url()} — "
                    f"see mempalace status for diagnostics ({e})",
                    file=sys.stderr,
                )
            sys.exit(1)
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


def cmd_tunnels(args):
    """List cross-wing tunnels (slice of #191).

    Wraps the daemon's ``mempalace_list_tunnels`` MCP tool. Default is
    explicit-only (the agent-wired tunnels at ``~/.mempalace/tunnels.json``);
    pass ``--passive`` to also include passive tunnels (rooms appearing
    in 2+ wings, inferred from the palace graph — see issue #75).

    Daemon unreachable → exit 1; inner-error envelope → exit 2.
    """
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
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

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
    from . import mcp_server

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
        if want_json:
            _emit_json({"error": str(e), "source": "daemon"})
        else:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({e})",
                file=sys.stderr,
            )
        sys.exit(1)

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


def cmd_update(args):
    """Configure, check, or prepare updates without installing automatically."""
    import json

    from .update_awareness import check_updates, configure_updates, prepare_upgrade

    try:
        if args.update_action == "configure":
            result = configure_updates(
                enabled=args.enabled,
                interval_days=args.interval_days,
                installer=args.installer,
            )
        elif args.update_action == "check":
            result = check_updates(force=True)
        elif args.update_action == "plan":
            result = prepare_upgrade(installer=args.installer)
        else:
            raise ValueError("choose update configure, check, or plan")
    except (OSError, ValueError) as exc:
        print(f"mempalace update: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ── Logstream (RFC 003 agent coordination) ────────────────────────────────


def _open_logstream(args):
    """Open the palace logstream database for a CLI command.

    Direct SQLite access is safe alongside a running hub: logstream.sqlite3
    is WAL-mode and independent of Chroma, so CLI writes are immediately
    visible to hub readers without the mine-style forwarding Chroma needs.
    """
    from .logstream import LOGSTREAM_DB_FILENAME, Logstream

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
    from .logstream import normalize_watch_values, sanitize_watch_spec

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

    from .logstream import (
        WATCH_STATE_ABSENT,
        WATCH_STATE_CORRUPT,
        read_watch_state,
        write_watch_cursor,
    )

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


def _fail_daemon(err, want_json: bool) -> None:
    """Daemon call failed → exit 1 (matches cmd_why / cmd_tags / cmd_graph).

    ``DaemonError`` covers two different situations and the distinction
    matters to whoever reads the line: a transport failure (the daemon is
    asleep, wrong port, no route) versus a JSON-RPC error the daemon
    itself returned (the tool raised server-side — e.g. a dropped
    postgres connection under an AGE query). Reporting the second as
    "unreachable" sends the reader after the wrong problem, so keep the
    sibling commands' wording for transport and say what actually
    happened otherwise.
    """
    text = str(err)
    if want_json:
        _emit_json({"error": text, "source": "daemon"})
    elif text.startswith("daemon error"):
        print(
            f"palace daemon at {_daemon_url()} rejected the call — {text}",
            file=sys.stderr,
        )
    else:
        print(
            f"palace daemon unreachable at {_daemon_url()} — "
            f"see mempalace status for diagnostics ({err})",
            file=sys.stderr,
        )
    sys.exit(1)


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


def _print_diary_entries(agent: str, entries: list, total=None) -> None:
    """Newest-first entry list — header line per entry, then its content."""
    if not entries:
        print(f"\n  No diary entries for '{agent}'.\n")
        return
    suffix = f" of {total}" if total is not None else ""
    print(
        f"\n  DIARY — {agent}  ({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}{suffix})"
    )
    for entry in entries:
        stamp = entry.get("timestamp") or entry.get("date") or "?"
        topic = entry.get("topic") or "general"
        print(f"\n  [{stamp}]  {topic}  ({entry.get('drawer_id') or '?'})")
        for line in str(entry.get("content") or "").splitlines():
            print(f"    {line}")
    print()


def cmd_diary(args):
    """``mempalace diary write|read`` — the agent diary at the CLI (#354).

    ``write`` wraps ``mempalace_diary_write``; ``read`` wraps
    ``mempalace_diary_read``. Both require an agent name (``--agent`` or
    ``MEMPALACE_AGENT_NAME``) because the diary is per-agent in the tool
    contract. ``read``'s ``--topic`` / ``--since`` filters are applied
    client-side — the tool has no such parameters.
    """
    fmt = _resolve_tool_format(args)
    want_json = fmt == "json"
    action = getattr(args, "diary_action", None)

    agent = _resolve_agent_name(args)
    if not agent:
        _fail_client(
            "diary requires an agent name — pass --agent NAME or set MEMPALACE_AGENT_NAME",
            want_json,
        )

    if action == "write":
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
        return

    # read
    topic = getattr(args, "topic", None)
    since = getattr(args, "since", None)
    limit = max(1, min(int(getattr(args, "limit", None) or _DIARY_DEFAULT_LIMIT), _DIARY_MAX_LIMIT))
    # With a client-side filter in play, ask for the whole page the tool
    # will give us (capped at 100) and narrow afterwards — otherwise
    # ``--topic X --limit 5`` could return nothing while matching entries
    # sit just outside the requested window.
    fetch_n = _DIARY_MAX_LIMIT if (topic or since) else limit
    payload = {"agent_name": agent, "last_n": fetch_n}
    if getattr(args, "wing", None):
        payload["wing"] = args.wing
    try:
        data = _call_tool_routed(args, "mempalace_diary_read", "tool_diary_read", payload)
    except DaemonError as e:
        _fail_daemon(e, want_json)
    if _tool_failed(data):
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
        return
    _print_diary_entries(data.get("agent") or agent, entries, total=data.get("total"))


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
            from .logsync import load_peers, sync_all, sync_with_peer

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

    from .tasks import create_task, task_handoff, validate_task_request

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


def cmd_palace_set_embedder(args):
    """Record (or force-override) a palace's embedder identity (RFC 001).

    Resolves the ``unknown`` state for a legacy palace, or records a specific
    model with ``--model``. It records identity on the palace only; it does not
    change the configured model — when the two differ it prints how to align
    ``MEMPALACE_EMBEDDING_MODEL``. ``--force`` overwrites an existing,
    differently-named identity.
    """
    from .backends.base import EmbedderIdentityMismatchError
    from .palace import set_palace_embedder_identity

    config = MempalaceConfig()
    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else config.palace_path
    )
    model = getattr(args, "model", None)
    try:
        old, new = set_palace_embedder_identity(
            palace_path,
            model=model,
            force=getattr(args, "force", False),
            backend=_backend_arg(args),
        )
    except EmbedderIdentityMismatchError as exc:
        print(f"  ✗ {exc}")
        raise SystemExit(2) from exc
    if old is None:
        print(f"  ✓ recorded embedder identity: {new.model_name} (dim={new.dimension})")
    elif old.model_name == new.model_name:
        print(f"  ✓ embedder identity unchanged: {new.model_name} (dim={new.dimension})")
    else:
        print(
            f"  ✓ embedder identity changed: {old.model_name} → {new.model_name} "
            f"(dim={new.dimension})"
        )
    # set-embedder records the palace's identity; it does not change the
    # configured model. If they differ, the next normal open would mismatch —
    # tell the user how to align them.
    configured = config.embedding_model
    if new.model_name and configured and new.model_name != configured:
        print(
            f"  ⚠ configured model is {configured!r}; set MEMPALACE_EMBEDDING_MODEL="
            f"{new.model_name} (or run onboarding) so normal opens of this palace match."
        )


def cmd_repair_status(args):
    """Read-only HNSW capacity health check (#1222)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if not _maintenance_requires_chroma(palace_path, "repair-status"):
        raise SystemExit(2)
    from .repair import status as repair_status

    repair_status(palace_path=palace_path)


def cmd_repair(args):
    """Repair palace state.

    Default mode is full HNSW rebuild via extract + re-upsert
    (``--mode rebuild`` / ``--mode legacy``, synonyms). Also handles
    ``--mode max-seq-id`` for un-poisoning ``max_seq_id`` rows
    corrupted by the legacy 0.6.x → 1.5.x chromadb migration shim
    (#1208 / #1288 family). The earlier ``reorganize`` mode was
    retired alongside the recovery collection (PR #8 / row 32).

    Closes Copilot finding on jphein/mempalace#8: docstring claimed
    only "rebuild" while the function continued to dispatch
    ``max-seq-id`` based on ``args.mode``.

    On a successful rebuild the palace SQLite file is VACUUMed and the
    FTS5 index is rebuilt, so the next repair's integrity preflight reads
    a consistent database (#1747).
    """
    config = MempalaceConfig()
    collection_name = config.collection_name
    palace_path = os.path.abspath(
        os.path.expanduser(args.palace) if args.palace else config.palace_path
    )
    if not _maintenance_requires_chroma(palace_path, "repair"):
        raise SystemExit(2)

    import shutil
    from .backends.chroma import ChromaBackend
    from .backups import copy_palace_dir
    from .migrate import confirm_destructive_action, contains_palace_database
    from .repair import (
        RebuildCollectionError,
        TruncationDetected,
        _close_chroma_handles,
        _extract_drawers,
        _post_rebuild_cleanup,
        _preview_legacy_repair,
        _promote_temp_collection,
        _rebuild_collection_via_temp,
        check_extraction_safety,
        index_read_recovery_guidance,
        maybe_repair_poisoned_max_seq_id_before_rebuild,
        print_sqlite_integrity_abort,
        resolve_repair_preflight_errors,
        sqlite_integrity_errors,
    )

    if getattr(args, "repair_action", None) == "rebuild-index":
        args.mode = "from-sqlite"
        args.archive_existing = True

    if getattr(args, "mode", "legacy") == "max-seq-id":
        from .repair import repair_max_seq_id

        repair_max_seq_id(
            palace_path,
            segment=getattr(args, "segment", None),
            from_sidecar=getattr(args, "from_sidecar", None),
            backup=getattr(args, "backup", True),
            dry_run=getattr(args, "dry_run", False),
            assume_yes=getattr(args, "yes", False),
        )
        return

    if getattr(args, "mode", "legacy") == "from-sqlite":
        from .migrate import confirm_destructive_action
        from .repair import RebuildCleanupError, RebuildPartialError, rebuild_from_sqlite

        source_path = getattr(args, "source", None)
        source_path = (
            os.path.abspath(os.path.expanduser(source_path)) if source_path else palace_path
        )
        archive_existing = getattr(args, "archive_existing", False)

        # Gate any path that touches the user's existing palace dir
        # behind confirm_destructive_action. The legacy mode already
        # gates; from-sqlite needs the same protection because:
        # (a) --archive-existing renames the existing palace,
        # (b) --source PATH writes into --palace dir which the user
        #     may not realize is also a palace.
        # No prompt when source != dest AND dest does not exist (pure
        # extract-into-fresh-dir case is non-destructive to existing
        # palaces).
        # A --dry-run only reads the source SQLite and prints a plan — it
        # never archives, creates, or writes — so it must not trip the
        # destructive-action confirmation (#2095, #2133).
        dry_run = getattr(args, "dry_run", False)
        is_destructive_to_dest = source_path == palace_path or os.path.exists(palace_path)
        if (
            not dry_run
            and is_destructive_to_dest
            and not confirm_destructive_action(
                "Rebuild from SQLite", palace_path, assume_yes=getattr(args, "yes", False)
            )
        ):
            return

        try:
            counts = rebuild_from_sqlite(
                source_palace=source_path,
                dest_palace=palace_path,
                archive_existing_dest=archive_existing,
                dry_run=dry_run,
            )
        except RebuildPartialError as exc:
            # The error itself was already printed by rebuild_from_sqlite
            # with recovery instructions; surface a non-zero exit so
            # scripts and CI gates see the failure.
            print(
                "\n  Rebuild partial — see message above. "
                f"Failed in collection: {exc.failed_collection}"
            )
            sys.exit(1)
        except RebuildCleanupError:
            # All rows may have landed, but rebuild_from_sqlite deliberately
            # withholds success until FTS5 rebuild, VACUUM, and quick_check are
            # clean. Its exception already includes the retained destination
            # and archive/source recovery paths.
            print("\n  Rebuild cleanup failed -- see recovery details above.")
            sys.exit(1)
        # An empty counts dict is rebuild_from_sqlite's documented signal
        # for a validation refusal (missing source, existing dest,
        # in-place without --archive-existing). The library already
        # printed an actionable message; exit non-zero so unattended
        # scripts/CI distinguish "invalid inputs" from a successful
        # rebuild that legitimately found zero rows (which still returns
        # a populated dict with 0-valued counts).
        if not counts:
            sys.exit(1)
        return

    db_path = os.path.join(palace_path, "chroma.sqlite3")

    if not os.path.isdir(palace_path):
        _print_retired_local_palace_or_default(palace_path)
        return
    if not contains_palace_database(palace_path):
        print(f"\n No palace database found at {db_path}")
        return

    # Run the SQLite integrity preflight before any chromadb client open.
    # ChromaDB's rust binding raises pyo3_runtime.PanicException on a
    # malformed page, which is not a regular Exception subclass and
    # propagates past the try/except below — the user gets a 30-line
    # stack trace instead of the friendly abort message. Run quick_check
    # here so we can surface the clear recovery instructions and exit
    # cleanly before chromadb's compactor touches the disk.
    dry_run = getattr(args, "dry_run", False)
    # The FTS5 autoheal inside this call is a write, so a --dry-run predicts
    # its outcome instead of performing it (#1596 is auto-healable and must
    # not surface as an abort in a preview).
    sqlite_errors = resolve_repair_preflight_errors(
        palace_path, sqlite_integrity_errors(palace_path), dry_run=dry_run
    )
    if sqlite_errors:
        print_sqlite_integrity_abort(palace_path, sqlite_errors)
        sys.exit(1)

    preflight = maybe_repair_poisoned_max_seq_id_before_rebuild(
        palace_path,
        backup=getattr(args, "backup", True),
        dry_run=dry_run,
        assume_yes=getattr(args, "yes", False),
    )
    if preflight is not None:
        return

    print(f"\n{'=' * 55}")
    print(" MemPalace Repair")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    if dry_run:
        # Return before the backend is used at all: the chromadb client this
        # path opens is itself a write to chroma.sqlite3 (measured — the file
        # hash changes on get_collection alone, before count()), so a preview
        # that reached it could not be inert. Staying off the chromadb layer
        # also keeps a dry run clear of the layer repair is separately reported
        # to segfault in on a large palace (#2113). Exit non-zero on an
        # unreadable count for parity with the from-sqlite preview above, so
        # `--dry-run && repair --yes` cannot walk into the destructive run
        # after a failed preview (#2095, #2133).
        if not _preview_legacy_repair(
            palace_path=palace_path,
            collection_name=collection_name,
            confirm_truncation_ok=getattr(args, "confirm_truncation_ok", False),
        ):
            sys.exit(1)
        return

    backend = ChromaBackend()
    from .backends.base import PalaceRef

    # Try to read existing drawers
    try:
        col = backend.get_collection(
            palace=PalaceRef(id=palace_path, local_path=palace_path),
            collection_name=collection_name,
        )
        total = col.count()
        print(f"  Drawers found: {total}")
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print(index_read_recovery_guidance())
        return

    if total == 0:
        print("  Nothing to repair.")
        return

    if not confirm_destructive_action(
        "Repair", palace_path, assume_yes=getattr(args, "yes", False)
    ):
        return

    # Extract all drawers in batches
    print("\n  Extracting drawers...")
    batch_size = 5000
    all_ids, all_docs, all_metas, all_embeddings = _extract_drawers(col, total, batch_size)
    emb_status = (
        "with stored embeddings"
        if all_embeddings is not None
        else "without embeddings (will recompute)"
    )
    print(f"  Extracted {len(all_ids)} drawers ({emb_status})")

    # ── #1208 guard ──────────────────────────────────────────────────
    # Cross-check against the SQLite ground truth before doing anything
    # destructive. Catches the user-reported case where chromadb's
    # collection-layer get() silently caps at 10,000 rows even on much
    # larger palaces (e.g. after manual HNSW quarantine). Override with
    # --confirm-truncation-ok only after independently verifying the
    # extraction count is real.
    try:
        check_extraction_safety(
            palace_path,
            len(all_ids),
            confirm_truncation_ok=getattr(args, "confirm_truncation_ok", False),
            collection_name=collection_name,
        )
    except TruncationDetected as e:
        print(e.message)
        return

    palace_path = os.path.normpath(palace_path)
    backup_path = palace_path + ".backup"
    if os.path.exists(backup_path):
        if not contains_palace_database(backup_path):
            print(
                "  Backup validation failed: backup path exists but does not contain chroma.sqlite3. "
                f"Please remove or rename: {backup_path}"
            )
            return
        shutil.rmtree(backup_path)
    print(f"  Backing up to {backup_path}...")
    copy_palace_dir(palace_path, backup_path, log=print)

    try:
        filed = _rebuild_collection_via_temp(
            backend,
            palace_path,
            all_ids,
            all_docs,
            all_metas,
            batch_size,
            collection_name=collection_name,
            progress=print,
            all_embeddings=all_embeddings,
        )
    except RebuildCollectionError as e:
        print(f"  Repair failed: {e}")
        if getattr(e, "live_replaced", False):
            temp_name = f"{collection_name}__repair_tmp"
            print(f"  Attempting recovery: promoting verified copy from '{temp_name}'...")
            try:
                _close_chroma_handles(palace_path, backend=backend)
                _promote_temp_collection(
                    backend,
                    palace_path,
                    temp_name,
                    collection_name,
                    len(all_ids),
                    batch_size,
                    progress=print,
                )
                print("  Recovery succeeded: live collection restored from the verified temp copy.")
            except Exception as promote_error:
                print(f"  Automatic recovery failed: {promote_error}")
                print(
                    f"  The verified pre-swap copy still survives under '{temp_name}' -- do NOT "
                    f"delete it. Recover manually by promoting it, or restore the full-directory "
                    f"backup at: {backup_path}"
                )
        sys.exit(1)

    # The bulk delete + re-upsert cycle above leaves the FTS5 inverted index
    # inconsistent, which fails the next repair's integrity preflight (#1747).
    _post_rebuild_cleanup(palace_path, backend=backend, progress=print)

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print(f"  Backup saved at {backup_path}")
    print(f"\n{'=' * 55}\n")


def cmd_hook(args):
    """Run hook logic: reads JSON from stdin, outputs JSON to stdout."""
    from .hooks_cli import run_hook

    run_hook(hook_name=args.hook, harness=args.harness)


def cmd_instructions(args):
    """Output skill instructions to stdout."""
    from .instructions_cli import run_instructions

    run_instructions(name=args.name)


def cmd_rules(args):
    """Output the shared-brain agent rules block for a given agent identity."""
    from .instructions_cli import run_rules

    run_rules(agent_id=args.agent)


def cmd_mcp(args):
    """Show how to wire MemPalace into MCP-capable hosts."""
    base_server_cmd = "mempalace-mcp"
    cmd_parts = [base_server_cmd]

    if args.palace:
        resolved_palace = str(Path(args.palace).expanduser())
        cmd_parts.extend(["--palace", shlex.quote(resolved_palace)])
    backend = _backend_arg(args)
    if backend:
        cmd_parts.extend(["--backend", shlex.quote(str(backend).strip().lower())])
    server_cmd = " ".join(cmd_parts)

    print("MemPalace MCP quick setup:")
    print(f"  claude mcp add mempalace -- {server_cmd}")
    print(f"  codex mcp add mempalace -- {server_cmd}")
    print("\nRun the server directly:")
    print(f"  {server_cmd}")

    if not args.palace:
        print("\nOptional custom palace:")
        print(f"  claude mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print(f"  codex mcp add mempalace -- {base_server_cmd} --palace /path/to/palace")
        print(f"  {base_server_cmd} --palace /path/to/palace")


_SERVER_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_SERVER_BIND_ALL_HOSTS = {"0.0.0.0", "::", "[::]"}


def _server_is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in _SERVER_LOOPBACK_HOSTS


def _server_token_path(palace_path: str) -> Path:
    """Per-palace location for the auto-generated server bearer token.

    Distinct from the daemon's token dir; keyed by the canonical palace path so
    one server per palace reuses a stable token across restarts. Delegates to
    ``server_registry`` so the token and the hub serverinfo record share one
    directory convention.
    """
    from .server_registry import server_token_path

    return server_token_path(palace_path)


def _load_or_create_server_token(palace_path: str) -> tuple[str, bool]:
    """Return (token, created). Reuse an existing 0600 token or mint a new one."""
    import secrets

    token_path = _server_token_path(palace_path)
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing, False
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(token_path.parent), 0o700)
    except OSError:
        pass
    # O_CREAT with 0600 so the token is never briefly world-readable on disk.
    fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token, True


def cmd_serve(args):
    """Run a secure remote HTTP MCP server for a team to share one palace (#1877).

    A turnkey wrapper over ``mempalace-mcp --transport http``: it resolves a
    bearer token (auto-generating a strong one for non-loopback binds), prints a
    ready-to-paste client config, then execs the real server in the foreground so
    Docker/systemd own the process lifecycle. The token is passed via the
    environment, never argv, so it can't leak through ``ps``.
    """
    host = args.host
    port = int(args.port)
    loopback = _server_is_loopback(host)
    palace_path = (
        os.path.abspath(os.path.expanduser(args.palace))
        if args.palace
        else MempalaceConfig().palace_path
    )
    backend = _backend_arg(args)

    tls_cert = os.path.expanduser(args.tls_cert) if args.tls_cert else None
    tls_key = os.path.expanduser(args.tls_key) if args.tls_key else None
    if bool(tls_cert) != bool(tls_key):
        print("mempalace: --tls-cert and --tls-key must be given together", file=sys.stderr)
        sys.exit(2)
    for label, path in (("--tls-cert", tls_cert), ("--tls-key", tls_key)):
        if path and not os.path.isfile(path):
            print(f"mempalace: {label} file not found: {path}", file=sys.stderr)
            sys.exit(2)
    scheme = "https" if tls_cert else "http"

    # Token resolution. Explicit flag > existing env > (non-loopback) auto-generated.
    token = (args.token or os.environ.get("MEMPALACE_MCP_HTTP_TOKEN", "")).strip()
    token_created = False
    if not token and not loopback and not args.allow_insecure:
        token, token_created = _load_or_create_server_token(palace_path)

    # Build the child environment. Token rides in the env (never argv) so it
    # stays out of the process table.
    env = dict(os.environ)
    env["MEMPALACE_PALACE_PATH"] = palace_path
    if backend:
        env["MEMPALACE_BACKEND"] = str(backend).strip().lower()
    if token:
        env["MEMPALACE_MCP_HTTP_TOKEN"] = token
    if args.allow_insecure:
        env["MEMPALACE_MCP_HTTP_ALLOW_INSECURE_NO_TOKEN"] = "1"

    child = [
        sys.executable,
        "-m",
        "mempalace.mcp_server",
        "--transport",
        "http",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if backend:
        child += ["--backend", str(backend).strip().lower()]
    child += ["--palace", palace_path]
    if tls_cert:
        child += ["--tls-cert", tls_cert, "--tls-key", tls_key]
    if args.read_only:
        child.append("--read-only")

    # Client-facing address: 0.0.0.0/:: means "all interfaces" — clients dial a
    # real reachable host, so show a placeholder rather than the bind wildcard.
    client_host = "YOUR_SERVER_HOST" if host.strip().lower() in _SERVER_BIND_ALL_HOSTS else host
    url = f"{scheme}://{client_host}:{port}/mcp"

    print("Starting MemPalace remote MCP server")
    print(f"  palace   : {palace_path}")
    print(f"  backend  : {(backend or 'default').strip().lower() if backend else 'default'}")
    print(f"  bind     : {host}:{port}  ({'loopback' if loopback else 'network-exposed'})")
    print(f"  tls      : {'on' if tls_cert else 'off (plaintext -- terminate TLS at a proxy)'}")
    print(f"  read-only: {'yes' if args.read_only else 'no'}")
    if token_created:
        print("\n  A new bearer token was generated and stored 0600 at:")
        print(f"    {_server_token_path(palace_path)}")
        print("  Store it securely -- clients need it to connect:")
        print(f"    {token}")
    print("\nConnect a client:")
    if token:
        print(
            f"  claude mcp add --transport http mempalace {url} "
            f'--header "Authorization: Bearer {token if token_created else "$MEMPALACE_MCP_HTTP_TOKEN"}"'
        )
    else:
        print(f"  claude mcp add --transport http mempalace {url}")
    print(f"  curl {scheme}://{client_host}:{port}/healthz   # liveness (no auth)\n")
    sys.stdout.flush()

    # Foreground: hand the process to the real server so signals (SIGTERM from
    # Docker/systemd) reach it directly. exec on POSIX; subprocess on Windows
    # (no exec semantics) propagating the exit code.
    if os.name == "posix":
        os.execve(sys.executable, child, env)
    else:
        import subprocess

        completed = subprocess.run(child, env=env)
        sys.exit(completed.returncode)


def cmd_compress(args):
    """Compress drawers in a wing using AAAK Dialect."""
    from .dialect import Dialect
    from .palace import get_closets_collection

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    # Load dialect (with optional entity config)
    config_path = args.config
    if not config_path:
        # ``isfile`` rather than ``exists``: the latter is true for a FIFO,
        # and ``Dialect.from_config`` opens whatever it is handed, which
        # blocks in the kernel on a pipe named entities.json in the cwd.
        for candidate in ["entities.json", os.path.join(palace_path, "entities.json")]:
            if os.path.isfile(candidate):
                config_path = candidate
                break

    if config_path and os.path.isfile(config_path):
        dialect = Dialect.from_config(config_path)
        print(f"  Loaded entity config: {config_path}")
    else:
        dialect = Dialect()

    # State-aware open: distinguish "no palace" from "initialized but empty"
    # from "corrupt" via the shared helper (#1498). MCP and library callers
    # catch the backend exceptions directly; CLI gets the friendly print.
    from .palace import _open_collection_or_explain

    col = _open_collection_or_explain(palace_path, collection_name="mempalace_drawers")
    if col is None:
        sys.exit(1)

    # Query drawers in batches to avoid SQLite variable limit (~999)
    where = {"wing": args.wing} if args.wing else None
    _BATCH = 500
    docs, metas, ids = [], [], []
    offset = 0
    while True:
        try:
            kwargs = {
                "include": ["documents", "metadatas"],
                "limit": _BATCH,
                "offset": offset,
            }
            if where:
                kwargs["where"] = where
            batch = col.get(**kwargs)
        except Exception as e:
            if not docs:
                print(f"\n  Error reading drawers: {e}")
                sys.exit(1)
            break
        batch_docs = batch.get("documents", [])
        if not batch_docs:
            break
        docs.extend(batch_docs)
        metas.extend(batch.get("metadatas", []))
        ids.extend(batch.get("ids", []))
        offset += len(batch_docs)
        if len(batch_docs) < _BATCH:
            break

    if not docs:
        wing_label = f" in wing '{args.wing}'" if args.wing else ""
        print(f"\n  No drawers found{wing_label}.")
        return

    print(
        f"\n  Compressing {len(docs)} drawers"
        + (f" in wing '{args.wing}'" if args.wing else "")
        + "..."
    )
    print()

    total_original = 0
    total_compressed = 0
    compressed_entries = []

    for doc, meta, doc_id in zip(docs, metas, ids):
        compressed = dialect.compress(doc, metadata=meta)
        stats = dialect.compression_stats(doc, compressed)

        total_original += stats["original_chars"]
        total_compressed += stats["summary_chars"]

        compressed_entries.append((doc_id, compressed, meta, stats))

        if args.dry_run:
            wing_name = meta.get("wing", "?")
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "?")).name
            print(f"  [{wing_name}/{room_name}] {source}")
            print(
                f"    {stats['original_tokens_est']}t -> {stats['summary_tokens_est']}t ({stats['size_ratio']:.1f}x)"
            )
            print(f"    {compressed}")
            print()

    # Store compressed versions (unless dry-run)
    if not args.dry_run:
        try:
            # Route through palace.get_closets_collection so the shared
            # chroma backend (via get_backend("chroma")) is reused — avoids
            # a redundant ChromaBackend instance and its potential WAL-lock
            # contention on Windows.
            comp_col = get_closets_collection(palace_path, create=True)
            for doc_id, compressed, meta, stats in compressed_entries:
                comp_meta = dict(meta)
                comp_meta["compression_ratio"] = round(stats["size_ratio"], 1)
                comp_meta["original_tokens"] = stats["original_tokens_est"]
                comp_col.upsert(
                    ids=[doc_id],
                    documents=[compressed],
                    metadatas=[comp_meta],
                )
            print(
                f"  Stored {len(compressed_entries)} compressed drawers in 'mempalace_closets' collection."
            )
        except Exception as e:
            print(f"  Error storing compressed drawers: {e}")
            sys.exit(1)

    # Summary
    ratio = total_original / max(total_compressed, 1)
    # Estimate tokens from char count (~3.8 chars/token for English text)
    orig_tokens = max(1, int(total_original / 3.8))
    comp_tokens = max(1, int(total_compressed / 3.8))
    print(f"  Total: {orig_tokens:,}t -> {comp_tokens:,}t ({ratio:.1f}x compression)")
    if args.dry_run:
        print("  (dry run -- nothing stored)")


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
    from ._stdio import reconfigure_stdio_utf8_on_windows

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

    Splits the two failure modes ``DaemonError`` conflates, because the
    operator response differs and the sibling commands' single
    "unreachable" message is actively wrong for the second:

    - transport failure (message starts ``daemon unreachable``) → exit 1
    - JSON-RPC error from a daemon that answered (``daemon error -32001:
      ... exceeded PALACE_MCP_TOOL_TIMEOUT_SECONDS``) → exit 2, same
      class as an inner ``{"error": ...}`` envelope

    Never falls back to local: a silent fallback is exactly the
    split-brain daemon-strict exists to prevent.
    """
    try:
        return _call_daemon_tool(name, arguments)
    except DaemonError as e:
        detail = str(e)
        unreachable = detail.startswith("daemon unreachable")
        if want_json:
            _emit_json(
                {
                    "error": detail,
                    "source": "daemon",
                    "tool": name,
                    "reachable": not unreachable,
                }
            )
        elif unreachable:
            print(
                f"palace daemon unreachable at {_daemon_url()} — "
                f"see mempalace status for diagnostics ({detail})",
                file=sys.stderr,
            )
        else:
            print(
                f"\n  ERROR: daemon rejected {name}: {detail}",
                file=sys.stderr,
            )
        sys.exit(1 if unreachable else 2)


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
    want_json = _resolve_read_format(args) == "json"
    sort = getattr(args, "sort", "name") or "name"
    limit = max(0, int(getattr(args, "limit", 0) or 0))

    if _daemon_strict() and not getattr(args, "palace", None):
        try:
            fast = _call_daemon_rest("/status/fast")
        except DaemonError as e:
            _read_family_fail(
                f"palace daemon unreachable at {_daemon_url()} ({e})", want_json, 1, "daemon"
            )
            return
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

    shown = rows[:limit] if limit else rows
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


def cmd_hallway_list(args):
    """List within-wing entity hallways (slice of #191, issue #358).

    Wraps ``mempalace_list_hallways``. The pre-existing ``mempalace
    hallways`` verb stays as a back-compatible local-only alias; this is
    the daemon-routed superset with ``--json`` and stable ordering.
    """
    want_json = _resolve_read_format(args) == "json"
    wing = getattr(args, "wing", None)
    limit = max(0, int(getattr(args, "limit", _HALLWAY_DEFAULT_LIMIT) or 0))

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
        _emit_json({"hallways": rows, "wing_filter": wing, "total": len(rows)})
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
        help="Where the palace lives (default: from ~/.mempalace/config.json or ~/.mempalace/palace)",
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
    p_mine.add_argument(
        "--daemon",
        action="store_true",
        help="Submit this mine to the opt-in local daemon queue",
    )
    p_mine.add_argument(
        "--background",
        action="store_true",
        help="With --daemon, return a job id immediately instead of waiting",
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
    p_sync.add_argument(
        "--daemon",
        action="store_true",
        help="Submit this sync to the opt-in local daemon queue",
    )
    p_sync.add_argument(
        "--background",
        action="store_true",
        help="With --daemon, return a job id immediately instead of waiting",
    )

    # search
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
        choices=["claude-code", "codex"],
        help="Harness type (determines stdin JSON format)",
    )

    # instructions
    p_instructions = sub.add_parser(
        "instructions",
        help="Output skill instructions to stdout",
    )
    instructions_sub = p_instructions.add_subparsers(dest="instructions_name")
    for instr_name in ["init", "search", "mine", "help", "status"]:
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
        "--agent",
        required=True,
        help="Stable agent identity to render into the rules, e.g. mac-claude",
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
        help="Persist the cursor here so a restart resumes exactly where it stopped",
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

    sub.add_parser(
        "replay",
        help="Drain ~/.mempalace/pending/ by re-issuing queued mine requests to the daemon",
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
        help="Agent name whose diary to read (default: $MEMPALACE_AGENT_NAME)",
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
        help="Read from one wing only (default: every wing this agent wrote to)",
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

    # ── Propagate --json/--quiet to every subparser (issue #44) ─────
    # argparse parses pre-subcommand flags into ``args.json`` /
    # ``args.quiet`` only if they appear BEFORE the subcommand. To let
    # users write the natural ``mempalace status --json``, attach the
    # same flags to each subparser so post-subcommand usage parses too.
    # Help is suppressed on the per-subparser copies to keep ``--help``
    # output uncluttered — the canonical docs live on the top-level parser.
    for _sp in sub.choices.values():
        # Skip the two-level parents (hook, instructions, rooms) — their
        # actions are owned by nested sub-parsers and the leaf parsers
        # don't need JSON output (hook/instructions write their own JSON;
        # rooms is operator-only). Detect them by the presence of a
        # registered subparsers action.
        has_nested = any(isinstance(a, argparse._SubParsersAction) for a in _sp._actions)
        if has_nested:
            continue
        _sp.add_argument(
            "--json",
            "-j",
            dest="json",
            action="store_true",
            default=False,
            help=argparse.SUPPRESS,
        )
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
        "status": cmd_status,
        "doctor": cmd_doctor,
        "update": cmd_update,
        "stats": cmd_stats,
        "tags": cmd_tags,
        "overlap": cmd_overlap,
        "why": cmd_why,
        "tunnels": cmd_tunnels,
        "mined": cmd_mined,
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

    dispatch[args.command](args)


if __name__ == "__main__":
    main()
