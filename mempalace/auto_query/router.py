"""Tool router for auto-query integration.

Pure function: maps a SignalSet to an MCPCall (or None).  No I/O, no MCP
calls, no side effects.  The router is stateless; rate limiting and
deduplication are the caller's responsibility.

Priority order (from spec section 2):
  0.5 Resumption ask  -> mempalace_search      (the user asked to resume)
  1. Task resumption  -> mempalace_diary_read  (highest precision)
  2. Explicit hint     -> mempalace_search      (user is asking directly)
  3. Entity + temporal -> mempalace_kg_query    (entity-scoped history)
  4. Entity only       -> mempalace_search      (entity-scoped)
  5. Temporal only     -> mempalace_search      (project-scoped recent)
"""

import re
from typing import Optional

from mempalace.auto_query import MCPCall, SessionState, SignalSet

# Minimum total_score required for each mode before the router fires.
# "off" uses infinity so the threshold is never reached.
THRESHOLDS = {
    "off": float("inf"),
    "dry-run": 4,
    "conservative": 6,
    "balanced": 4,
    "aggressive": 2,
}

# Maximum length for search query strings passed to MCP tools.
_MAX_QUERY_LEN = 200

# Over-fetch sizes: the runner filters manifests / diary / below-floor /
# already-injected drawers after the call, then keeps DEPTH_KEEP.
DEPTH_FETCH = 8
DEPTH_KEEP = 3
IDENT_FETCH = 6


def pick_tool(
    signals: SignalSet,
    mode: str,
    session_state: SessionState,
) -> Optional[MCPCall]:
    """Select the MCP tool to invoke based on extracted signals.

    Returns None if the score is below the threshold for the given mode,
    or if no meaningful signal pattern is detected.

    The router does NOT call ``query_sanitizer.sanitize_query()`` itself --
    it performs lightweight truncation only.  The runner (harness shim)
    is responsible for full sanitization before calling the router.
    """
    threshold = THRESHOLDS.get(mode, float("inf"))
    if signals.total_score < threshold:
        return None

    return _select_tool(signals)


def _select_tool(signals: SignalSet) -> Optional[MCPCall]:
    """Route signals to the best MCP tool.

    Returns None if no signal pattern matches (edge case: score above
    threshold but no individual signal flags are set).
    """
    # Priority 0.5: the user asked, in words, to be picked back up (#364) —
    # "where were we", "catch me up", "which PRs are open". This outranks
    # both the positional turn-1 resumption and the periodic depth refresh,
    # because an explicit ask is a better query than either.
    #
    # It is deliberately NOT routed to the diary: diary entries are the
    # palace's own AUTO-SAVE bookkeeping, which the quality gate already
    # classifies as exhaust. A resumption wants the wing's content.
    if signals.resumption_phrase:
        args = {"query": _resumption_query(signals), "limit": DEPTH_FETCH}
        if signals.project_wing:
            args["wing"] = signals.project_wing
        return MCPCall(tool="mempalace_search", args=args)

    # Priority 1: Task resumption
    if signals.resumption:
        return MCPCall(
            tool="mempalace_diary_read",
            args={
                "agent_name": "claude-code",
                "wing": signals.project_wing,
                "last_n": 3,
            },
        )

    # Priority 1.5: Periodic depth refresh — a project-scoped pull to
    # re-anchor context mid-session. Sits just below task resumption and above
    # every content-derived signal (explicit/entity/temporal).
    #
    # The query is the USER'S OWN WORDS, not a fixed string. The old
    # "session context <wing>" query matched session manifests forever and
    # returned the same three drawers every turn — the single largest reason
    # agents learned to ignore the palace (fleet check-in, 2026-09-03). When
    # the turn is too short to carry meaning, fall back to the wing's
    # decision-shaped content instead of its manifests. The runner
    # over-fetches so post-filtering (manifests, diary, floor, already-seen)
    # still leaves DEPTH_KEEP results.
    if signals.depth_fire:
        words = _sanitize_for_search(signals.query_text).split()
        if len(words) >= 3:
            query = " ".join(words)
        else:
            query = "decisions problems findings {}".format(signals.project_wing).strip()
        args = {"query": query, "limit": DEPTH_FETCH}
        if signals.project_wing:
            args["wing"] = signals.project_wing
        return MCPCall(tool="mempalace_search", args=args)

    # Priority 2: Explicit recall hint
    if signals.explicit:
        query = _sanitize_for_search(signals.query_text)
        return MCPCall(
            tool="mempalace_search",
            args={"query": query, "limit": 10},
        )

    # Priority 2.5: Identifier-shaped tokens — exact-match retrieval. Union
    # candidate strategy adds the BM25 lexical arm, which is what pays for
    # ``NSAPI`` / ``EF_FPLMN`` / ``0x807`` shaped queries.
    if signals.identifier and not any(sig.score >= 2 for sig in signals.entity):
        names = [sig.name for sig in signals.identifier[:3]]
        args = {"query": " ".join(names), "limit": IDENT_FETCH, "candidate_strategy": "union"}
        if signals.project_wing:
            args["wing"] = signals.project_wing
        return MCPCall(tool="mempalace_search", args=args)

    # Priority 3: Entity + temporal compound
    if signals.entity and signals.temporal:
        best = max(signals.entity, key=lambda s: s.score)
        return MCPCall(
            tool="mempalace_kg_query",
            args={"entity": best.name, "direction": "both"},
        )

    # Priority 4: Entity only
    if signals.entity:
        best = max(signals.entity, key=lambda s: s.score)
        args = {"query": best.name, "limit": 5}
        if best.wing:
            args["wing"] = best.wing
        return MCPCall(tool="mempalace_search", args=args)

    # Priority 5: Temporal only
    if signals.temporal:
        query = _sanitize_for_search(signals.query_text)
        args = {"query": query, "limit": 5}
        if signals.project_wing:
            args["wing"] = signals.project_wing
        return MCPCall(tool="mempalace_search", args=args)

    # No signal pattern matched despite score >= threshold
    return None


# Words that carry no retrieval weight once the resumption phrase itself is
# removed. Everything left is the topic the user actually named.
_RESUMPTION_FILLER = frozenset(
    """a about again an and any are as at be been did do for from had has have
    here i in is it its just me my now of on or our please re so still that the
    their them then there these they this to up us was we were what when where
    which who why with you your""".split()
)


def _resumption_query(signals: SignalSet) -> str:
    """Search text for a resumption ask: the topic, never the question.

    "where were we on the pgvector cutover" searches the cutover. A bare
    "where were we" names no topic, so fall back to the wing's
    decision-shaped content rather than searching the question itself —
    searching the question is how the old depth query matched session
    manifests forever (fleet check-in, 2026-09-03).

    ONE surviving word is a topic, not noise. The names people resume on are
    usually single tokens — a wing (``2g``), an issue (``#449``), a component
    (``pgvector``) — and requiring two discarded exactly those, sending
    "catch me up on 2g" to the generic depth query instead of to the wing the
    user just named. Only an empty remainder falls back.
    """
    text = signals.query_text or ""
    phrase = signals.resumption_phrase
    if phrase:
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)
    words = [w for w in re.findall(r"[\w.#/-]+", text) if w.lower() not in _RESUMPTION_FILLER]
    if words:
        return _sanitize_for_search(" ".join(words))
    return "decisions problems findings {}".format(signals.project_wing).strip()


def _sanitize_for_search(text: str) -> str:
    """Lightweight sanitization for search queries.

    Strips leading/trailing whitespace and truncates to ``_MAX_QUERY_LEN``
    characters.  For full sanitization, the runner should use
    ``query_sanitizer.sanitize_query()`` before calling the router.
    """
    return text.strip()[:_MAX_QUERY_LEN]
