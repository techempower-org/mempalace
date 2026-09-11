"""Auto-query runner — chains signal extraction, routing, and formatting.

This module is the single entry point for the auto-query pipeline.
It is called from the UserPromptSubmit hook (via ``__main__.py``) or
directly from tests.

Pipeline::

    load config → check enabled/mode → extract signals → pick tool
    → execute MCP call → format injection → log decision

The MCP call is made via ``_call_mcp()``, which delegates to the
palace-daemon HTTP proxy (``/mcp`` endpoint).  When the daemon is not
reachable, the call is skipped and a dry-run decision is logged.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from mempalace.auto_query import Decision, SessionState
from mempalace.auto_query.decisions import append_decision, rotate_log
from mempalace.auto_query.depth_cache import (
    cache_key,
    load_cached_injection,
    store_injection,
)
from mempalace.auto_query.formatter import format_injection
from mempalace.auto_query.router import DEPTH_KEEP, THRESHOLDS, pick_tool
from mempalace.auto_query.signals import extract_signals
from mempalace.config import MempalaceConfig
from mempalace.provenance import source_kind


# Daemon round-trip ceiling. Hybrid search on an 85K-drawer wing measured
# 5.3s (2026-09-03) — the old 5s ceiling silently dropped exactly the big
# wings where recall matters most. The depth path tries the fast BM25 route
# first (see _call_fast_search) so this ceiling is rarely reached.
_MCP_TIMEOUT_S = 8


class AutoQueryResult:
    """Result of a single auto-query pipeline run."""

    __slots__ = ("injection", "decision", "tool_call", "mcp_result", "receipt")

    def __init__(
        self,
        injection=None,  # type: Optional[str]
        decision=None,  # type: Optional[Decision]
        tool_call=None,  # type: Optional[MCPCall]
        mcp_result=None,  # type: Optional[dict]
        receipt=None,  # type: Optional[dict]
    ):
        self.injection = injection
        self.decision = decision
        self.tool_call = tool_call
        self.mcp_result = mcp_result
        # One-line accounting of a fired query for the visible terminal
        # receipt — emitted even when nothing survives filtering, so a
        # query that found nothing is still SEEN (JP, 2026-09-03).
        self.receipt = receipt


def run_auto_query(
    prompt,  # type: str
    session_id,  # type: str
    turn,  # type: int
    project_wing="",  # type: str
    known_wings=None,  # type: Optional[set]
    known_entities=None,  # type: Optional[set]
    has_recent_drawers=False,  # type: bool
    config=None,  # type: Optional[MempalaceConfig]
    queried_entities=None,  # type: Optional[set]
    log_dir=None,  # type: Optional[str]
    wing_counts=None,  # type: Optional[dict]
):
    # type: (...) -> AutoQueryResult
    """Run the auto-query pipeline for a single user turn.

    Returns an ``AutoQueryResult`` with the injection text (or None),
    the decision record, and the raw MCP result.
    """
    if config is None:
        config = MempalaceConfig()

    if not config.auto_query_enabled:
        return AutoQueryResult()

    mode = config.auto_query_mode
    if mode == "off":
        return AutoQueryResult()

    if known_wings is None:
        known_wings = set()

    session_state = SessionState(
        turn_index=turn,
        queried_entities=queried_entities or set(),
        session_id=session_id,
    )

    signals = extract_signals(
        text=prompt,
        session_state=session_state,
        project_wing=project_wing,
        known_wings=known_wings,
        known_entities=known_entities,
        has_recent_drawers=has_recent_drawers,
    )

    tool_call = pick_tool(signals, mode, session_state)

    threshold = THRESHOLDS.get(mode, float("inf"))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if tool_call is None:
        decision = Decision(
            ts=ts,
            session_id=session_id,
            turn=turn,
            signals=_serialize_signals(signals),
            score=signals.total_score,
            threshold=int(threshold) if threshold != float("inf") else 999,
            mode=mode,
            decision="skip",
            reason="below threshold or no signal pattern",
        )
        _safe_log(decision, log_dir)
        return AutoQueryResult(decision=decision)

    if mode == "dry-run":
        decision = Decision(
            ts=ts,
            session_id=session_id,
            turn=turn,
            signals=_serialize_signals(signals),
            score=signals.total_score,
            threshold=int(threshold),
            mode=mode,
            decision="dry-run-skip",
            reason="dry-run mode — would fire",
            tool=tool_call.tool,
            args=tool_call.args,
        )
        _safe_log(decision, log_dir)
        return AutoQueryResult(decision=decision, tool_call=tool_call)

    # Depth fires re-issue a deterministic query whose results barely change
    # within a session, while the daemon round-trip costs most of a second —
    # over the hook latency budget. Serve repeats from the TTL cache; only
    # the first fire per window pays the daemon call.
    depth_key = None
    if signals.depth_fire and tool_call.tool == "mempalace_search":
        depth_key = cache_key(
            tool_call.args.get("query", ""),
            tool_call.args.get("wing", ""),
            tool_call.args.get("limit", 0),
        )
        cached = load_cached_injection(depth_key, config.auto_query_depth_cache_ttl)
        if cached is not None:
            decision = Decision(
                ts=ts,
                session_id=session_id,
                turn=turn,
                signals=_serialize_signals(signals),
                score=signals.total_score,
                threshold=int(threshold),
                mode=mode,
                decision="fire",
                reason="results formatted (depth cache)",
                tool=tool_call.tool,
                args=tool_call.args,
                latency_ms=0,
                injection_tokens=len(cached) // 4,
            )
            _safe_log(decision, log_dir)
            # The cached block is still a palace query the human should see.
            m = re.search(r"^results \((\d+)\):", cached, re.MULTILINE)
            receipt = {
                "query": tool_call.args.get("query", ""),
                "wing": tool_call.args.get("wing", ""),
                "tool": tool_call.tool,
                "hits": int(m.group(1)) if m else 0,
                "raw_hits": int(m.group(1)) if m else 0,
                "best": None,
                "floor": None,
                "latency_ms": 0,
                "cached": True,
            }
            return AutoQueryResult(
                injection=cached, decision=decision, tool_call=tool_call, receipt=receipt
            )

    # Live mode. Identifier-bearing queries try the daemon's BM25 fast route
    # first (~25ms vs 3-5s hybrid on an 85K-drawer wing, 2026-09-03): exact
    # terms are what pay for ``NSAPI`` / ``EF_FPLMN`` / ``0x807`` shapes.
    # Anything else — or a fast miss — goes to the hybrid MCP call.
    t0 = time.monotonic()
    mcp_result = None
    fast_used = False
    ident_terms = [sig.name for sig in getattr(signals, "identifier", []) or []][:3]
    if ident_terms and tool_call.tool == "mempalace_search":
        # One fast call per identifier (BM25-fast ANDs the terms of a single
        # query, so "NSAPI IP_VERSION_MISMATCH RAB" misses drawers that hold
        # only one of them), merged by drawer id, best rank first. ~25ms each.
        fast = _fast_search_many(
            ident_terms,
            tool_call.args.get("wing", ""),
            tool_call.args.get("limit", DEPTH_KEEP),
            config,
        )
        if fast is not None:
            probe = _filter_search_results(fast, session_id, config)
            if probe.get("results"):
                mcp_result = fast
                fast_used = True
    if mcp_result is None:
        if signals.depth_fire and _big_wing(tool_call.args.get("wing", ""), wing_counts):
            # Hybrid (vector + BM25 + graph) measured 5.3s on an 85K-drawer
            # wing vs 3.3s vector-only (2026-09-03). Depth refresh is a
            # semantic re-anchor; identifiers already went through the fast
            # route above, so the BM25/graph arms buy little here.
            tool_call.args.setdefault("candidate_strategy", "vector")
        mcp_result = _call_mcp(tool_call, config)
    latency_ms = int((time.monotonic() - t0) * 1000)

    if mcp_result is None:
        decision = Decision(
            ts=ts,
            session_id=session_id,
            turn=turn,
            signals=_serialize_signals(signals),
            score=signals.total_score,
            threshold=int(threshold),
            mode=mode,
            decision="skip",
            reason="daemon unreachable",
            tool=tool_call.tool,
            args=tool_call.args,
            latency_ms=latency_ms,
        )
        _safe_log(decision, log_dir)
        # A query that timed out or found no daemon is still a query the
        # human should see — silent failures are how "the palace is broken"
        # goes unnoticed for weeks (fleet check-in, 2026-09-03).
        receipt = {
            "query": tool_call.args.get("query") or tool_call.args.get("entity") or "",
            "wing": tool_call.args.get("wing", ""),
            "tool": tool_call.tool,
            "hits": 0,
            "raw_hits": 0,
            "best": None,
            "floor": None,
            "latency_ms": latency_ms,
            "error": "daemon unreachable or timed out",
        }
        return AutoQueryResult(decision=decision, tool_call=tool_call, receipt=receipt)

    # Quality gate (fleet check-in, 2026-09-03): drop session manifests,
    # diary AUTO-SAVE checkpoints, transcript exhaust, below-floor hits and
    # anything already injected this session; keep the top DEPTH_KEEP.
    raw_count = _count_results(mcp_result)
    filtered = _filter_search_results(mcp_result, session_id, config)
    keep = DEPTH_KEEP if signals.depth_fire else max(DEPTH_KEEP, 5)
    mcp_result = _apply_filtered(mcp_result, filtered, keep)

    header_lines = None
    if signals.depth_fire and signals.project_wing and wing_counts:
        header_lines = _wing_inventory_lines(signals.project_wing, wing_counts)

    injection = format_injection(
        tool_call, mcp_result, signals, latency_ms, header_lines=header_lines
    )

    if injection and depth_key is not None:
        store_injection(depth_key, injection)

    result_count = _count_results(mcp_result)
    if injection:
        _remember_injected(session_id, mcp_result)
    injection_tokens = len(injection) // 4 if injection else 0

    decision_str = "fire" if injection else "skip"
    if injection:
        reason = "results formatted"
    elif raw_count and not result_count:
        reason = "all results filtered (manifest/diary/floor/seen)"
    else:
        reason = "no results from MCP"

    receipt = {
        "query": tool_call.args.get("query") or tool_call.args.get("entity") or "",
        "wing": tool_call.args.get("wing", ""),
        "tool": tool_call.tool,
        "hits": result_count,
        "raw_hits": raw_count,
        "best": filtered.get("best_score"),
        "floor": filtered.get("floor"),
        "latency_ms": latency_ms,
        "route": "bm25-fast" if fast_used else "hybrid",
    }

    decision = Decision(
        ts=ts,
        session_id=session_id,
        turn=turn,
        signals=_serialize_signals(signals),
        score=signals.total_score,
        threshold=int(threshold),
        mode=mode,
        decision=decision_str,
        reason=reason,
        tool=tool_call.tool,
        args=tool_call.args,
        latency_ms=latency_ms,
        result_drawers=result_count,
        injection_tokens=injection_tokens,
    )
    _safe_log(decision, log_dir)

    # Mark entities as queried so they don't fire again this session.
    for sig in signals.entity:
        session_state.queried_entities.add(sig.name)

    return AutoQueryResult(
        injection=injection,
        decision=decision,
        tool_call=tool_call,
        mcp_result=mcp_result,
        receipt=receipt,
    )


# Rooms and shapes that are the palace's own bookkeeping, not knowledge.
# They dominated auto-query results fleet-wide ("my own exhaust").
_EXHAUST_ROOMS = frozenset({"sessions", "diary", "checkpoint"})
_EXHAUST_PREFIXES = (
    "AUTO-SAVE:",
    "Session manifest",
    "> This session is being continued",
    # Prompt-echo drawers — the harness's own instruction text mirrored back
    # ("Investigate per the method in your instructions", "Read the RFC …"),
    # not knowledge (fleet finding, gnome-speaks-46, 2026-09-03).
    "Investigate per the method",
    "Get started. Read",
    "You are working ",
    "You are triaging ",
)


def _is_exhaust(item):
    # type: (dict) -> bool
    room = str(item.get("room", "") or "")
    if room in _EXHAUST_ROOMS:
        return True
    drawer_id = str(item.get("drawer_id") or item.get("id") or "")
    if drawer_id.startswith("diary_"):
        return True
    text = str(item.get("text", "") or "").lstrip()
    return text.startswith(_EXHAUST_PREFIXES)


def _item_id(item):
    # type: (dict) -> str
    """Drawer id under either key the search arms use (``drawer_id`` / ``id``)."""
    return str(item.get("drawer_id") or item.get("id") or "")


# Curated one-fact-per-file memory drawers are worth more than a transcript
# chunk at the same score (fleet finding, openwrt-a0, 2026-09-03: a correct
# 0.486 memory-file hit sat just under the flat 0.50 floor and would have been
# suppressed — "flat thresholds punish exactly the corpus you just added").
# A curated hit clears a floor lowered by this margin.
_CURATED_FLOOR_MARGIN = 0.10


def _above_floor(item, min_sim, min_bm25):
    # type: (dict, float, float) -> bool
    curated = _is_curated(item)
    sim = item.get("similarity")
    if isinstance(sim, (int, float)):
        floor = (min_sim - _CURATED_FLOOR_MARGIN) if curated else min_sim
        return sim >= floor
    bm25 = item.get("bm25_score")
    if isinstance(bm25, (int, float)):
        floor = (min_bm25 - _CURATED_FLOOR_MARGIN) if curated else min_bm25
        return bm25 >= floor
    # No score at all (KG / diary shapes) — let the caller's shape decide.
    return True


def _filter_search_results(mcp_result, session_id, config):
    # type: (dict, str, MempalaceConfig) -> dict
    """Return {"results": [...kept...], "best_score": float|None, "floor": float}."""
    results = mcp_result.get("results") if isinstance(mcp_result, dict) else None
    min_sim = getattr(config, "auto_query_min_similarity", 0.50)
    min_bm25 = getattr(config, "auto_query_min_bm25", 1.5)
    if not isinstance(results, list):
        return {"results": None, "best_score": None, "floor": min_sim}
    seen_ids = _load_injected(session_id)
    best = None
    kept = []
    for item in results:
        if not isinstance(item, dict):
            continue
        sim = item.get("similarity")
        if isinstance(sim, (int, float)) and (best is None or sim > best):
            best = sim
        if _is_exhaust(item):
            continue
        if not _above_floor(item, min_sim, min_bm25):
            continue
        if _item_id(item) in seen_ids:
            continue
        kept.append(item)
    return {"results": kept, "best_score": best, "floor": min_sim}


def _is_curated(item):
    # type: (dict) -> bool
    """A drawer mined from a per-project auto-memory file (one fact per file).

    Those files are the memory agents actually reach for (fleet check-in,
    2026-09-03); in the palace they are recognizable by their source path.

    Delegates to :func:`mempalace.provenance.source_kind` so the "what shape
    of source is this?" predicate has exactly one definition — the ranking
    here and the caveat the CLI renders must never disagree about which hits
    are curated (techempower-org/mempalace#451).
    """
    return source_kind(item) == "memory"


def _apply_filtered(mcp_result, filtered, keep):
    # type: (dict, dict, int) -> dict
    kept = filtered.get("results")
    if kept is None:
        return mcp_result
    # Curated (fact-shaped) drawers first, transcript drawers after — stable
    # within each group so the retrieval ranking is otherwise preserved.
    ordered = sorted(kept, key=lambda it: 0 if _is_curated(it) else 1)
    out = dict(mcp_result)
    out["results"] = ordered[:keep]
    return out


def _wing_inventory_lines(wing, wing_counts):
    # type: (str, dict) -> list
    """One line saying what the palace holds for this project."""
    try:
        n = int(wing_counts.get(wing, 0))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return ["palace: wing '%s' has no drawers yet" % wing]
    ranked = sorted(wing_counts.items(), key=lambda kv: -int(kv[1] or 0))
    rank = next((i + 1 for i, (w, _) in enumerate(ranked) if w == wing), 0)
    line = "palace: wing '%s' holds %s drawers (rank %d of %d wings)" % (
        wing,
        "{:,}".format(n),
        rank,
        len(ranked),
    )
    related = [
        w
        for w, _ in ranked
        if w != wing and (w.startswith(wing[:4]) or wing.startswith(w[:4])) and len(w) >= 4
    ][:3]
    if related:
        line += " · related wings: " + ", ".join(related)
    return [line]


def _injected_path(session_id):
    # type: (str) -> str
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_")[:80] or "cli"
    return os.path.join(os.path.expanduser("~/.mempalace/auto_query/injected"), safe + ".json")


def _load_injected(session_id):
    # type: (str) -> set
    try:
        with open(_injected_path(session_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(str(x) for x in data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _remember_injected(session_id, mcp_result):
    # type: (str, dict) -> None
    """Never inject the same drawer twice in one session (fail-open on I/O)."""
    results = mcp_result.get("results") if isinstance(mcp_result, dict) else None
    if not isinstance(results, list):
        return
    ids = _load_injected(session_id)
    ids.update(
        str(r.get("drawer_id")) for r in results if isinstance(r, dict) and r.get("drawer_id")
    )
    path = _injected_path(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(ids)[-500:], f)
    except OSError:
        pass


_DAEMON_ENV_FILE = os.path.expanduser("~/.config/palace-daemon/env")


def _api_key():
    # type: () -> str
    """PALACE_API_KEY from the environment, else from the daemon env file.

    Hook processes are spawned without the user's shell rc, so the key that
    lives in ``~/.config/palace-daemon/env`` (mode 600) is not in os.environ.
    Routes that require the key would otherwise fail open to slower paths —
    or silently, which is worse.
    """
    key = os.environ.get("PALACE_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(_DAEMON_ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if line.startswith("PALACE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


_BIG_WING_DRAWERS = 50_000


def _big_wing(wing, wing_counts):
    # type: (str, Optional[dict]) -> bool
    if not wing or not wing_counts:
        return False
    try:
        return int(wing_counts.get(wing, 0) or 0) >= _BIG_WING_DRAWERS
    except (TypeError, ValueError):
        return False


def _fast_search_many(terms, wing, limit, config):
    # type: (list, str, int, MempalaceConfig) -> Optional[dict]
    """Fast-route each term, merge by drawer id, order by rank desc."""
    merged = {}  # type: dict
    any_ok = False
    for term in terms:
        got = _call_fast_search(term, wing, limit, config)
        if got is None:
            continue
        any_ok = True
        for item in got.get("results", []):
            key = _item_id(item) or id(item)
            prev = merged.get(key)
            if prev is None or (item.get("rank") or 0) > (prev.get("rank") or 0):
                merged[key] = item
    if not any_ok:
        return None
    results = sorted(merged.values(), key=lambda it: -(it.get("rank") or 0))
    return {"results": results[: max(int(limit or DEPTH_KEEP), 1) * 2], "source": "bm25-fast"}


def _call_fast_search(query, wing, limit, config):
    # type: (str, str, int, MempalaceConfig) -> Optional[dict]
    """GET the daemon's ``/search/fast`` BM25 route; normalize to the search shape.

    The route returns a bare list of ``{id, wing, room, rank, snippet,
    source_file, tags}``. Returns ``{"results": [...]}`` with ``drawer_id`` /
    ``text`` filled in so the exhaust filter, dedupe and formatter treat the
    items exactly like hybrid results, or None on any failure (fail open to
    the hybrid call).
    """
    daemon_url = config.daemon_url
    if not daemon_url or not query:
        return None
    params = {"q": query, "limit": str(int(limit or DEPTH_KEEP))}
    if wing:
        params["wing"] = wing
    url = "{}/search/fast?{}".format(daemon_url.rstrip("/"), urllib.parse.urlencode(params))
    headers = {}
    api_key = _api_key()
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("results", [])
    if not isinstance(data, list):
        return None
    results = []
    for item in data:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "drawer_id": item.get("id") or item.get("drawer_id") or "",
                "wing": item.get("wing", ""),
                "room": item.get("room", ""),
                "text": item.get("snippet") or item.get("text") or "",
                "created_at": item.get("created_at", ""),
                "source_file": item.get("source_file"),
                "similarity": None,
                "distance": None,
                "bm25_score": None,
                "rank": item.get("rank"),
                "matched_via": "bm25-fast",
            }
        )
    return {"results": results, "source": "bm25-fast"}


def _call_mcp(tool_call, config):
    # type: (MCPCall, MempalaceConfig) -> Optional[dict]
    """Call palace-daemon's /mcp HTTP proxy.

    Returns the parsed JSON result dict, or None if the daemon is
    unreachable or returns an error.
    """
    daemon_url = config.daemon_url
    if not daemon_url:
        return None

    url = "{}/mcp".format(daemon_url.rstrip("/"))
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": tool_call.tool,
                "arguments": tool_call.args,
            },
            "id": 1,
        }
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_MCP_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8")
            rpc_response = json.loads(body)
            return _extract_mcp_result(rpc_response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def _extract_mcp_result(rpc_response):
    # type: (dict) -> Optional[dict]
    """Extract the tool result from a JSON-RPC MCP response.

    The daemon returns ``{"jsonrpc":"2.0","result":{"content":[{"type":"text",
    "text":"..."}]}}``.  The ``text`` field is itself a JSON string containing
    the actual result dict (search results, KG facts, diary entries, etc.).
    """
    if not isinstance(rpc_response, dict):
        return None
    result = rpc_response.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content", [])
    if not content:
        return None
    text = content[0].get("text", "") if isinstance(content[0], dict) else ""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _serialize_signals(signals):
    # type: (SignalSet) -> dict
    """Serialize a SignalSet for the decision log."""
    return {
        "entity": [{"name": s.name, "score": s.score, "wing": s.wing} for s in signals.entity],
        "temporal": [{"name": s.name, "phrase": s.phrase} for s in signals.temporal],
        "resumption": signals.resumption,
        "explicit": signals.explicit,
        "depth_fire": signals.depth_fire,
        "total_score": signals.total_score,
        "project_wing": signals.project_wing,
    }


def _count_results(mcp_result):
    # type: (dict) -> int
    """Count result items from an MCP response."""
    for key in ("results", "entries", "nodes"):
        items = mcp_result.get(key)
        if isinstance(items, list):
            return len(items)
    outgoing = mcp_result.get("outgoing", [])
    incoming = mcp_result.get("incoming", [])
    return len(outgoing) + len(incoming)


def _safe_log(decision, log_dir):
    # type: (Decision, Optional[str]) -> None
    """Log a decision, swallowing any I/O errors."""
    try:
        append_decision(decision, log_dir=log_dir)
        rotate_log(log_dir=log_dir)
    except OSError:
        pass
