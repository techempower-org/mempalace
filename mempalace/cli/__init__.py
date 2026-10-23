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
    mempalace audit                       Score how well organized the palace is

Examples:
    mempalace init ~/projects/my_app
    mempalace mine ~/projects/my_app
    mempalace mine ~/.claude/projects/-Users-you-Projects-my_app --mode convos --wing my_app
    mempalace search "why did we switch to GraphQL"
    mempalace search "pricing discussion" --wing my_app --room costs
"""

from __future__ import annotations

# The public import path remains ``mempalace.cli``. Implementation is split
# across this package so PRs can target a command file instead of one
# 4 000-line module. Fragment files are executed into this package's globals
# (the same namespace as the historical module), which preserves
# ``from mempalace.cli import main, cmd_mine, ...`` and test monkeypatches.

import json
import argparse
import contextlib
import os
import shlex
import sys
import warnings
from pathlib import Path

from typing import NamedTuple

from ..auto_wake import urlopen_with_wake
from ..config import MempalaceConfig
from ..cli_write_routing import (
    add_cli_write_routing_flags,
    resolve_cli_write_routing,
)
from ..write_routing import WriteRoutingError
from ..corpus_origin import detect_origin_heuristic, detect_origin_llm
from ..llm_client import LLMError, get_provider
from ..provenance import (
    annotate,
    no_curated_source,
    provenance_note,
    source_kind,
)
from ..result_ordering import collapse_identical_text, prefer_curated
from ..version import __version__


# ==================== AGENT-SHAPED OUTPUT (issue #44) ====================
# ``--json`` / ``-j`` flips command output from prose to a stable JSON
# document on stdout, intended for shell pipelines and non-MCP agents.
# ``--quiet`` / ``-q`` suppresses decorative chrome (headers, divider
# lines, the daemon-routing announcement on stderr). When stdout is not
# a TTY we default to quiet mode so piped output stays clean — explicit
# ``--quiet`` / ``--json`` still override (see ``_resolve_quiet``).
#
# Exit codes (per issue #44, revised #523/#514):
#   0  success
#   1  no results — the operation RAN and selected nothing
#   2  the operation could not run: palace unavailable (daemon
#      unreachable, a server-side error from a daemon that answered,
#      palace missing) OR a usage error (argparse parse failures, a verb
#      group named without an action)
#   64 the daemon answered and REJECTED a well-formed request (a 4xx —
#      an unknown room filter, a refused credential, a read-only /cypher
#      write). Nothing else uses 64.
#
# 2 vs 64 on a 4xx is decided by WHO could not proceed, not by the status
# number (#518):
#
#   401 / 403  64 — the daemon is up, it understood the request, and it
#              declined. A wrong PALACE_API_KEY is one line of config, and
#              reporting it as "palace unavailable" sends the operator to
#              check whether the daemon is running. `/cypher`'s 403 is a
#              read-only refusal rather than an auth one — same class,
#              same code.
#   404        2  — the route does not exist on this daemon. That is
#              unavailability FOR THAT VERB, not a user error, and it is
#              also the signal six call sites use to fall back to MCP.
#   5xx        2  — the daemon answered but could not serve it.
#
# The transport carries the status to make that decision possible:
# `_call_daemon_rest` and `_patch_daemon_rest` returned None for 404, 401
# and 403 alike, so a refused key and an old daemon were the same event to
# every caller. Measured on 7ed80f03 before the change: `stats` and
# `wings`, 401/403/404, all exited 2 with "palace daemon unreachable".
#
# Machine-readable failures (#521). Every daemon-failure JSON carries:
#
#   error   the prose — what went wrong, in English
#   code    a branchable key: daemon_unreachable | daemon_error |
#           bad_request | auth_failed | read_only | route_missing |
#           daemon_unavailable | daemon_required | daemon_busy
#   source  "daemon" (or "cli" for a client-input error)
#   status  the HTTP status, when an HTTP exchange produced the failure
#
# `error` holds PROSE in every writer, so the 20+ existing readers that
# print `.error` keep working; `code` is additive. Before this, `error`
# held the key in some helpers and the prose in others, and which one a
# caller got depended on which helper happened to handle the failure.
# The window/source family also still emits `message` (a duplicate of
# `error`), DEPRECATED and kept for one release.
#
# 1 vs 2 is the load-bearing line: 1 means the palace was reachable and
# had nothing to say; 2 means the question never got asked. A command
# that reports "no results" for an outage sends the reader looking for
# missing data instead of a missing daemon.
#
# argparse's own parse errors exit 2 and always have — mempalace does not
# subclass ArgumentParser and does not override error(). The previous
# revision of this block claimed 64 was "argparse default for parse
# errors", which was never true of this CLI (#514).
#
# How the daemon-strict read verbs ``window`` / ``source`` (#500, #502)
# resolve against that contract — written down because the interesting rows
# are the ones that look alike from the outside:
#   0   drawers returned
#   1   the window ran and matched nothing (a real answer, not a failure)
#   2   daemon lacks /window or /source (404) — names the minimum daemon
#       version, because a missing route is a deployment fact
#   2   credentials rejected (401/403) — names PALACE_API_KEY, deliberately
#       NOT the "deploy a newer daemon" message. ``_get_daemon_rest``
#       collapses 404/401/403 into None, so reusing it would report a key
#       mismatch as a version problem: a refusal naming the wrong reason.
#   2   transport failure, timeout, or 5xx
#   64  the daemon rejected a well-formed request (400/422: bad ISO bound,
#       unknown room, corrupt cursor) — carrying the daemon's own message,
#       since the daemon is the thing that knows which bound failed


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


class _DoctorHandled(Exception):
    """Internal: `cmd_doctor` already recorded this check, stop the happy path.

    A doctor never raises to the caller; this only unwinds to its own handler
    so one branch can report and the rest can be skipped without an
    `if/else` ladder around every subsequent line.
    """


class DaemonError(RuntimeError):
    """Raised when a daemon HTTP call fails or returns a JSON-RPC error."""


class DaemonBusyError(DaemonError):
    """The daemon answered that it is BUSY — JSON-RPC ``-32003``, or a message
    saying so — and the request was not run (#526, PR 4).

    A subclass so every ``except DaemonError`` keeps working. Its own class
    because the reader's next move differs: an outage means check the daemon,
    a refusal means fix the request, busy means retry shortly — and because it
    was measured being rendered as **0 hits, exit 1**: the REST transports
    returned the 200 error body verbatim and ``.get("results") or []`` turned
    a saturated daemon into an empty corpus. Rendered by ``_fail_daemon`` as
    ``code: "daemon_busy"``, exit 2.
    """

    def __init__(self, message: str, *, detail: str = "", route: str | None = None):
        super().__init__(message)
        self.detail = detail or message
        self.route = route


class DaemonRequestError(DaemonError):
    """The daemon answered and REFUSED the request — a 4xx, not an outage.

    A subclass so every existing `except DaemonError` keeps working; call
    sites that can tell the operator something useful catch this first.

    The distinction is the whole of #499: one exception type meant a client
    could not tell "the palace is down" from "your argument is wrong", and it
    guessed the former. `mempalace list --room diary` reported "palace daemon
    unreachable … see mempalace status" while the daemon was up and had
    answered with, verbatim:

        {"detail": {"error": "room 'diary' is not in the canonical set",
                    "valid_rooms": [...]}}

    The message the operator needed had already arrived. `detail` carries it
    forward so the client stops substituting a guess for it.
    """

    def __init__(self, message: str, *, status: int, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class DaemonAuthError(DaemonRequestError):
    """The daemon answered and refused the CREDENTIAL — 401 or 403 (#518).

    A subclass of ``DaemonRequestError`` because it is the same event class: the
    daemon is up, it understood the request, and it declined. Under the #44
    contract as restated by #476/#523 that is 64 ("the daemon answered and
    rejected a well-formed request"), not 2 ("the operation could not run").
    Reporting a wrong ``PALACE_API_KEY`` as *palace unavailable* sends the
    operator to check whether the daemon is running when the fix is one line of
    config — the same misattribution #499 removed for 400.

    404 deliberately does NOT raise this. A missing route means "this daemon
    cannot serve the verb", which is unavailability for that verb and stays 2;
    it also stays ``None`` from ``_call_daemon_rest`` so the MCP fallback that
    six call sites depend on is untouched.
    """

    code = "auth_failed"


def _print_retired_local_palace_or_default(palace_path: str) -> None:
    """If the user's default palace is missing AND a RETIRED marker
    exists, print the marker's content as the not-found message — so
    agents see "set PALACE_DAEMON_URL" instead of "Run: mempalace init".

    Falls through to the legacy "Run: mempalace init" message in every
    other case (palace literally absent on a fresh install, etc.).
    """
    from ..palace import _emit_current_daemon_url

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


def _raise_if_daemon_error_object(body, route: str):
    """Turn a 200 body that is an ERROR OBJECT into an exception; pass results through.

    Keyed on the PAYLOAD, never on which transport produced it: a dict with an
    ``error`` and neither ``results`` nor ``result`` is a daemon that answered
    "I did not run this". Before this, ``_call_daemon_rest`` and
    ``_post_daemon_rest`` returned such a body verbatim and the search helpers
    read ``.get("results") or []`` — so a saturated daemon (JSON-RPC ``-32003``,
    "daemon busy: 8 MCP tool call(s) in flight") rendered as **0 hits, exit 1**,
    and #526's depth banner would then have said no curated document was in the
    top N. A statement about the store that was really about the daemon.

    ``-32003`` or a message containing "busy" → :class:`DaemonBusyError`.
    Anything else → ``DaemonError`` with the ``"daemon error"`` prefix
    ``_fail_daemon`` keys its reachable/unreachable line on. A body that carries
    ``results`` beside an ``error`` is a result — the caller's own
    ``"error" in data`` handling still runs on it.
    """
    if not isinstance(body, dict) or "error" not in body:
        return body
    if "results" in body or "result" in body:
        return body
    err = body["error"]
    if isinstance(err, dict):
        code = err.get("code")
        message = str(err.get("message") or err.get("error") or err)
    else:
        code = None
        message = str(err)
    if code == -32003 or "busy" in message.lower():
        raise DaemonBusyError(f"daemon busy on {route}: {message}", detail=message, route=route)
    code_str = f" {code}" if code is not None else ""
    raise DaemonError(f"daemon error{code_str} on {route}: {message}")


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
        # Every error envelope raises here — DaemonBusyError for -32003/"busy",
        # DaemonError("daemon error <code> on /mcp <tool>: <message>") otherwise.
        # The route names the tool, which the message this replaced did not.
        _raise_if_daemon_error_object(envelope, f"/mcp {name}")
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


def _daemon_error_detail(exc) -> str:
    """The daemon's own explanation for a refusal, as plain text.

    FastAPI returns ``{"detail": ...}`` where detail is a string or a dict;
    the room validator uses a dict with ``error`` and ``valid_rooms``, which
    is exactly the guidance the operator needs. Anything unparseable falls
    back to the raw body, and an unreadable body to "" — this runs while
    already reporting a failure and must not raise a second one.
    """
    try:
        raw = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 — the body is best-effort
        return ""
    if not raw:
        return ""
    try:
        detail = json.loads(raw).get("detail", raw)
    except (ValueError, AttributeError):
        return raw[:300]
    if isinstance(detail, dict):
        text = str(detail.get("error") or detail)
        options = detail.get("valid_rooms") or detail.get("valid")
        if options:
            text = f"{text} (valid: {', '.join(map(str, options))})"
        return text[:300]
    return str(detail)[:300]


def _exit_daemon_request_error(e, *, want_json: bool, source: str = "daemon"):
    """Report a daemon REFUSAL and exit 64 (bad args), never as an outage.

    One renderer for every REST call site, because there were five of them
    and every one reported the same wrong thing. Fixing only the one named
    in #499 is the shape that produced #459 three weeks after #418: two of
    four call sites corrected, the rest found later by an audit.

    Exit 64 is `cli.py`'s documented code for bad args; each site keeps its
    own (differing) code for a genuine outage, which this deliberately does
    not touch.
    """
    if want_json:
        # Option C (#521): prose in `error` so the 20+ existing readers that
        # print `.error` keep working, the branchable key in `code`. Before
        # this, `error` held the key "bad_request" here and the prose in
        # `detail`, while `_fail_daemon` put the prose in `error` — two
        # conventions a caller had to tell apart by which helper happened to
        # run.
        _emit_json(
            {
                "error": e.detail or str(e),
                "code": getattr(e, "code", "bad_request"),
                "status": e.status,
                "detail": e.detail or str(e),
                "source": source,
            }
        )
    else:
        verb = (
            "refused the credential" if isinstance(e, DaemonAuthError) else "rejected the request"
        )
        print(f"daemon {verb} ({e.status}): {e}", file=sys.stderr)
    sys.exit(64)


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
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Route missing on an older daemon. Still None, deliberately: six
            # call sites read None as "fall back to MCP", and a missing route
            # IS unavailability for that verb (#518 keeps 404 at 2).
            return None
        if e.code in (401, 403):
            # NOT None. Collapsing auth onto the 404 path is what made a wrong
            # PALACE_API_KEY indistinguishable from an old daemon — both
            # rendered as "palace daemon unreachable" while the daemon was up
            # and answering (#518). Measured on 7ed80f03: `stats`, `wings`,
            # 401/403/404 all exited 2 with the same sentence.
            detail = _daemon_error_detail(e)
            raise DaemonAuthError(
                detail or e.reason or f"HTTP {e.code}", status=e.code, detail=detail
            ) from e
        if 400 <= e.code < 500:
            # The daemon answered and refused. Its body already says WHY —
            # FastAPI puts it in `detail` — and discarding it is what made
            # `list --room diary` report the daemon unreachable while it was
            # up and explaining itself (#499).
            detail = _daemon_error_detail(e)
            # The message is the daemon's EXPLANATION; the status is a field.
            # Keeping the code out of the string means a renderer composes it
            # once from `status` rather than depending on how the string was
            # built — a test that constructed this directly caught exactly
            # that coupling.
            raise DaemonRequestError(
                detail or e.reason or f"HTTP {e.code}", status=e.code, detail=detail
            ) from e
        raise DaemonError(f"daemon REST {path} failed ({e.code}): {e.reason}") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e
    return _raise_if_daemon_error_object(body, path)


class _DaemonHTTPResult(NamedTuple):
    """One daemon HTTP answer, with the status kept rather than collapsed.

    ``_call_daemon_rest`` returns ``None`` for 404, 401 and 403 alike, which
    is right for a caller that falls back to MCP but wrong for one that has
    to tell the user WHY. A missing route means "deploy a newer daemon"; a
    401 means "your key is wrong". Reporting the second as the first sends
    the reader after the wrong problem — the defect family
    ``_resolve_palace_or_refuse`` documents in its own ``_refuse`` helper.

    ``code`` is 0 only when no HTTP exchange happened (transport failure),
    and that case raises ``DaemonError`` instead of returning.

    Relationship to #509, which landed between this branch's review and its
    rebase: that PR added ``DaemonRequestError(status=..., detail=...)`` and
    ``_daemon_error_detail``, solving the "a 4xx must carry the daemon's own
    message" half of this problem in the shared layer — and this helper now
    calls ``_daemon_error_detail`` rather than re-parsing the body. What
    #509 deliberately kept is the 404/401/403 collapse to ``None``, because
    its callers fall back to MCP. These two verbs cannot: there is no local
    path, so they must tell the operator which of the three happened. That
    residual difference is the only reason this helper still exists, and it
    is the thing to check before consolidating the two.
    """

    code: int
    payload: "dict | None"
    detail: str


def _window_daemon_get(path: str, params: dict | None = None) -> _DaemonHTTPResult:
    """GET a daemon route, preserving the status code and the error detail.

    FastAPI puts its message in the JSON body's ``detail``, not in the HTTP
    reason phrase, so a 4xx surfaced from ``e.reason`` alone would say
    "Bad Request" where the daemon said "since must be an ISO date string".
    The ``window``/``source`` verbs promise the daemon's own message, so the
    body is read.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{_daemon_url()}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urlopen_with_wake(req, timeout=_daemon_timeout()) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            return _DaemonHTTPResult(code=resp.status, payload=body, detail="")
    except urllib.error.HTTPError as e:
        # Reuse #509's extractor rather than parsing the body again. It
        # handles FastAPI's dict-shaped detail — the room validator answers
        # {"error": ..., "valid_rooms": [...]} — and renders the options
        # inline. An inline json.loads here would stringify that dict and
        # throw away exactly the guidance the operator needs, which is the
        # defect #499 was filed about.
        detail = _daemon_error_detail(e) or str(e.reason)
        return _DaemonHTTPResult(code=e.code, payload=None, detail=detail)
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
            answer = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise DaemonError(f"daemon REST {path} failed ({e.code}): {e.reason}") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e
    return _raise_if_daemon_error_object(answer, path)


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
        if e.code == 404:
            return None  # route missing on an older daemon -> MCP fallback (#518)
        if e.code in (401, 403):
            # Same split as _call_daemon_rest (#518): a refused credential is
            # not a missing route. This transport collapsed all three to None
            # too, so `move` reported a bad key as an outage.
            detail = _daemon_error_detail(e)
            raise DaemonAuthError(
                detail or e.reason or f"HTTP {e.code}", status=e.code, detail=detail
            ) from e
        raise DaemonError(f"daemon REST {path} failed ({e.code}): {e.reason}") from e
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        raise DaemonError(f"daemon unreachable at {_daemon_url()}: {e}") from e


def _post_daemon_mine_cli(
    directory: str, wing: str, mode: str = "convos", *, background: bool = False
) -> bool:
    """POST a mine request to the daemon's ``/mine`` endpoint.

    CLI-shaped variant of :func:`mempalace.hooks_cli._post_daemon_mine`:
    on failure, prints to stderr and returns ``False`` so the caller can
    ``sys.exit(1)``. Hooks_cli's version logs to a file and swallows
    silently because a missed-mine isn't worth crashing a hook over;
    here, the user invoked `mempalace mine` and expects to see errors.

    ``background`` asks the daemon to queue the mine and answer 202 at once
    instead of running it inline. The hook poster has sent it since #433;
    this copy never did, so everything routed through here waited on the
    palace write lock — ``mempalace replay`` drained ONE request in 120 s
    while the daemon's own drainer held the flock (#456). The key is always
    on the wire, never merely omitted, so a later reader can see which mode
    was asked for and its absence cannot creep back in. A daemon predating
    the field ignores it and blocks as before.

    ⚠️ ``mode`` is annotated ``str`` and defaults to ``"convos"``, and
    **neither is a guard against an explicit ``None``**: a default applies
    only when the argument is OMITTED, and an annotation is not enforced at
    runtime. A caller passing ``mode=None`` therefore put ``"mode": null``
    on the wire and the daemon's ``MineBody`` answered 422 for every
    daemon-strict mine (#525). That is why this function now raises rather
    than quietly substituting the default — substituting would trade a loud
    422 for a silently wrong corpus.

    Note what "success" means with ``background=True``: the daemon has taken
    the request into its durable pending-mines queue, not that the mine has
    finished. That is the right contract for a replay — the queue's job is a
    reliable hand-off — but a caller that needs the drawers to exist when the
    call returns must leave it off.
    """
    import urllib.error
    import urllib.request

    if mode is None:
        # Refuse rather than substitute the default. A caller that reaches
        # here with None has skipped its own normalization (#525 was exactly
        # that), and quietly mining in "convos" when the caller meant
        # "projects" would trade a loud 422 for a silently wrong corpus —
        # the worse half of the same trade.
        raise ValueError(
            "_post_daemon_mine_cli: mode must be a string, not None — "
            'resolve it at the call site (`args.mode or "projects"`)'
        )

    headers = {"content-type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    # Compact separators, so the bytes match what ``palace-doc-sync.sh``
    # puts on the same wire (``printf '{"dir":"%s","wing":"%s",…}'``). The
    # daemon parses either; the point is ONE wire format that a byte
    # comparison can pin (#529), rather than two that are "the same" only
    # after each reader normalises them.
    req = urllib.request.Request(
        f"{_daemon_url()}/mine",
        data=json.dumps(
            {"dir": directory, "wing": wing, "mode": mode, "background": background},
            separators=(",", ":"),
        ).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        # A 202 is not an error to urlopen (it only raises on >= 400), so the
        # queued answer already lands here; what it needs is to be *read* —
        # the daemon puts a human sentence in ``systemMessage`` and raw JSON
        # in front of the user is not it.
        with urlopen_with_wake(req, timeout=_daemon_timeout()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        note = payload.get("systemMessage") if isinstance(payload, dict) else None
        queued = isinstance(payload, dict) and payload.get("queued") is True
        verb = "queued" if queued else "accepted"
        print(f"  Daemon mine {verb}: {note or body[:200]}")
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
    # #526: "no curated document matched" was read as a statement about the
    # STORE when it is a statement about the DEPTH — the requested limit sizes
    # the hybrid fusion candidate pool, so at --limit 3 a curated document that
    # exists is often never a candidate. The banner therefore names the depth,
    # and the rank the curated layer actually begins at, and tells the reader to
    # widen the POOL rather than to scroll further down a list.
    promoted = next(
        (h for h in hits if isinstance(h, dict) and h.get("promoted")),
        None,
    )
    curated_rank = data.get("curated_first_rank")
    if promoted is not None:
        print(
            f"  ! 1 curated hit promoted from rank {promoted.get('promoted_from_rank')}; "
            "the curated layer in this wing begins there — widen the pool with --limit 30"
        )
    elif no_curated_source(hits):
        # The transcript/diary split the old banner drew is carried per hit by
        # the ⟨transcript⟩ / ⟨diary⟩ tags; at this level the useful statement is
        # the depth. Never "matched" — that is the sentence #526 was filed about.
        # #549: name the depth actually EXAMINED (30 after a deep fetch), not
        # the truncated list length, which is only the limit.
        depth = data.get("depth_examined") or len(hits)
        line = f"  ! no curated document in the top {depth}"
        if curated_rank:
            line += f" — first curated hit at rank {curated_rank}; widen the pool with --limit 30"
        print(line)
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

    # The reserved slot is marked in every prose renderer, not only ``compact``
    # (#526, review finding on #534): the default view shipping an unmarked promotion is
    # the silent-promotion failure this feature exists to prevent.
    print(f"  [{index}] {wing} / {room}{_promotion_tag(hit)}{_collapse_tag(hit)}")

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


def _promotion_tag(hit: dict) -> str:
    """``⟨curated, promoted from rank K⟩`` for a hit reserved into its slot, else ``""``.

    One producer for the prose marker, called by every renderer (``table`` and
    ``full`` via :func:`_print_hit_table`, ``compact`` via :func:`_provenance_tag`)
    so no renderer can be the one that shows a promotion as an ordinary rank (#526).
    """
    if hit.get("promoted"):
        return f" ⟨curated, promoted from rank {hit.get('promoted_from_rank')}⟩"
    return ""


def _collapse_tag(hit: dict) -> str:
    """``⟨N identical copies collapsed⟩`` for a hit that absorbed duplicates, else ``""``.

    One producer for every prose renderer (``table``/``full`` via
    :func:`_print_hit_table`, ``compact`` via :func:`_provenance_tag`): a slot
    recovered from a second path is said out loud, never silently (#526).
    """
    n = hit.get("duplicates_collapsed") if isinstance(hit, dict) else None
    if not isinstance(n, int) or n <= 0:
        return ""
    noun = "copy" if n == 1 else "copies"
    return f" ⟨{n} identical {noun} collapsed⟩"


def _provenance_tag(hit: dict) -> str:
    """Short source-shape tag for ``--format compact`` (empty when unremarkable).

    ``⟨transcript⟩`` — a quoted copy from a session transcript.
    ``⟨diary⟩`` — a palace-written summary with no file behind it.
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
    promoted = _promotion_tag(hit)
    if promoted:
        # Reserved into this slot rather than ranked into it (#526).
        return promoted + _collapse_tag(hit)
    kind = hit.get("source_kind") or source_kind(hit)
    flags = []
    if kind == "transcript":
        flags.append("transcript")
    elif kind == "diary":
        flags.append("diary")
    elif hit.get("source_stale") is True:
        flags.append("stale")
    tag = f" ⟨{','.join(flags)}⟩" if flags else ""
    return tag + _collapse_tag(hit)


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

_FRAGMENT_DIR = Path(__file__).resolve().parent
# Load order is a dependency sequence, not a catalog: later files use names
# defined earlier (_common before commands, commands before parser). Do not
# reorder this tuple.
_FRAGMENTS = (
    "_common.py",
    "cmd_init.py",
    "_hub.py",
    "cmd_mine.py",
    "cmd_sync.py",
    "cmd_query.py",
    "cmd_audit.py",
    "cmd_rooms.py",
    "cmd_wings.py",
    "cmd_kg.py",
    "cmd_tunnels.py",
    "cmd_update.py",
    "cmd_coord.py",
    "cmd_repair.py",
    "cmd_serve.py",
    "parser.py",
)


def _exec_fragment(filename: str) -> None:
    # Load through SourceFileLoader so the fragment's bytecode is cached in __pycache__ like any module's.
    # exec(compile(read_text())) recompiled every fragment on every process start: ~74 ms for cli alone
    # (fork, 2026-10-22), on the Stop/PreCompact hook path and its 100 ms startup budget.
    from importlib.machinery import SourceFileLoader

    path = _FRAGMENT_DIR / filename
    name = f"{__name__}.{filename[:-3]}"
    exec(SourceFileLoader(name, str(path)).get_code(name), globals())


for _fragment in _FRAGMENTS:
    _exec_fragment(_fragment)
del _fragment
