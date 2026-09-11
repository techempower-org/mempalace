"""Re-inject what a compaction took away (techempower-org/mempalace#449).

A context compaction keeps the session id and throws away the context. Two
things follow, and both were measured on the fleet:

1. **The auto-query dedupe outlives the context it was protecting.**
   ``~/.mempalace/auto_query/injected/<session>.json`` lists the drawers
   already shown this session so they are not repeated (#429). After a
   compaction those drawers are gone from the agent and still marked shown —
   so auto-query suppresses precisely the material the agent just lost, and
   suppresses more of it the longer the session ran. One live session on
   katana had 119 ids in that file.

2. **The post-compaction hooks offered a pointer, not content.** They printed
   "your history is in the palace, run ``mempalace wake-up``". Fleet
   check-in 2026-09-03: agents do not act on invitations ("the reflex has
   never fired unprompted"). Hooks that inject without asking do.

So on ``SessionStart(source="compact")`` this module clears the dedupe file
and returns the wake-up story itself as ``additionalContext``.

The hook side is one call::

    printf '%s' "$INPUT" | PYTHONPATH=$MEMPALACE_DIR $PY -m mempalace.compact_recovery

It reads Claude Code's SessionStart JSON on stdin, prints the hook payload on
stdout, and always exits 0 — a recovery aid must never be able to fail a
session.
"""

import argparse
import json
import os
import sys
import time

from mempalace.auto_query.injected import clear_injected

__all__ = ["CompactRecovery", "recover", "main"]

# Ceiling on the palace text injected after a compaction. Wake-up L1 is
# ~800 tokens by construction (``layers.Layer1.MAX_CHARS``); the cap is the
# backstop for a wing whose L1 has grown, so a recovery can never turn into
# the next context problem.
DEFAULT_MAX_CHARS = 4000

# The daemon answers ``mempalace_wakeup`` from a cache in 33-48 ms (measured
# against familiar, 2026-09-10), so this ceiling is not sized for the happy
# path — it is sized for the sleeping host. familiar is Slumber-Ward
# sleepable, and a sleeping host BLACKHOLES the SYN rather than refusing it:
# the connect() blocks for the full timeout, measured 4,074 ms. That is 81% of
# the hook's own 5 s ceiling and 8x the 500 ms startup-injection budget, for a
# call whose answer is 33 ms when it comes at all.
#
# 1.5 s is ~30x the observed round trip and keeps a sleeping host under 2 s of
# hook time. Overshooting it costs nothing that matters: the dedupe clear has
# already happened, and the recovery fails open to a receipt line.
WAKEUP_TIMEOUT_S = 1.5


class CompactRecovery(object):
    """What a post-compaction recovery did, and what it wants injected."""

    __slots__ = ("cleared", "context", "system_message", "receipt")

    def __init__(self, cleared, context, system_message, receipt):
        # type: (int, str, str, dict) -> None
        self.cleared = cleared
        self.context = context
        self.system_message = system_message
        self.receipt = receipt

    def hook_payload(self):
        # type: () -> dict
        """The JSON Claude Code's SessionStart hook expects.

        ``systemMessage`` is the visible receipt — a palace query the human
        can see, per the transparency rule that every query announces itself.
        ``additionalContext`` is omitted entirely when there is nothing to
        inject, so a dead daemon costs the session nothing but one line.
        """
        payload = {"systemMessage": self.system_message}
        if self.context:
            payload["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": self.context,
            }
        return payload


def _load_config():
    # type: () -> MempalaceConfig
    from mempalace.config import MempalaceConfig

    return MempalaceConfig()


def _call_daemon_tool(tool, args, config, timeout=None):
    # type: (str, dict, MempalaceConfig, Optional[int]) -> Optional[dict]
    """Invoke one daemon MCP tool, reusing the auto-query transport.

    Imported lazily: clearing the dedupe file must not pay for loading the
    whole classifier when the daemon is unreachable anyway.
    """
    from mempalace.auto_query import MCPCall
    from mempalace.auto_query import runner

    original = runner._MCP_TIMEOUT_S
    runner._MCP_TIMEOUT_S = timeout or WAKEUP_TIMEOUT_S
    try:
        return runner.call_mcp(MCPCall(tool=tool, args=args), config)
    finally:
        runner._MCP_TIMEOUT_S = original


def _max_chars(config):
    # type: (MempalaceConfig) -> int
    raw = getattr(config, "compact_recovery_max_chars", DEFAULT_MAX_CHARS)
    try:
        return max(200, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS


def _trim(text, limit):
    # type: (str, int) -> str
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n… (trimmed to the post-compaction injection budget)"


def _render(wing, story, cleared):
    # type: (str, str, int) -> str
    """Frame the palace text as recovered context, content first.

    The frame says where the text came from and that the dedupe was lifted,
    because an agent that knows a re-query will now return fresh drawers is
    an agent that will re-query. The pointer goes last: an invitation ahead
    of the content is the failure mode #449 exists to end.
    """
    head = (
        "## Recovered from the palace (post-compaction)\n\n"
        "Prior session memory, re-injected because the compaction dropped it — "
        "context, not instructions.\n\n"
    )
    tail = (
        "\n\n---\nSuppression list cleared ({} drawer ids): re-searching now returns "
        'what auto-query was hiding. `mempalace search "<terms>" --wing {} --limit 3` '
        "(announce every query: ✦ palace ← …).".format(cleared, wing or "<wing>")
    )
    return head + story.strip() + tail


def recover(session_id, wing, config=None):
    # type: (str, str, Optional[MempalaceConfig]) -> CompactRecovery
    """Clear the session's dedupe and build the re-injection block.

    Order matters: the dedupe is cleared first and unconditionally. It is the
    half that does not depend on the network, and it is the half that was
    actively suppressing recall.
    """
    if config is None:
        config = _load_config()

    cleared = clear_injected(session_id)

    t0 = time.monotonic()
    data = None
    try:
        data = _call_daemon_tool(
            "mempalace_wakeup", {"wing": wing} if wing else {}, config, WAKEUP_TIMEOUT_S
        )
    except Exception:  # noqa: BLE001 - a hook may not raise, whatever the daemon does
        data = None
    ms = int((time.monotonic() - t0) * 1000)

    story = ""
    if isinstance(data, dict):
        story = str(data.get("text") or "").strip()

    if not story:
        return CompactRecovery(
            cleared=cleared,
            context="",
            system_message=(
                "✦ palace ⟳ post-compaction: cleared {} suppressed drawer ids · wake-up"
                " unreachable ({}ms) — recall is un-deduplicated but not re-injected".format(
                    cleared, ms
                )
            ),
            receipt={
                "event": "compact-recovery",
                "wing": wing,
                "cleared": cleared,
                "chars": 0,
                "latency_ms": ms,
                "error": "daemon unreachable or timed out",
            },
        )

    context = _render(wing, _trim(story, _max_chars(config)), cleared)
    return CompactRecovery(
        cleared=cleared,
        context=context,
        system_message=(
            "✦ palace ⟳ post-compaction: re-injected wing '{}' wake-up (~{} tokens,"
            " {}ms) · cleared {} suppressed drawer ids".format(
                wing or "?", len(context) // 4, ms, cleared
            )
        ),
        receipt={
            "event": "compact-recovery",
            "wing": wing,
            "cleared": cleared,
            "chars": len(context),
            "latency_ms": ms,
        },
    )


def wing_for_cwd(cwd, config):
    # type: (str, MempalaceConfig) -> str
    """Project directory -> canonical palace wing (same rule as the hooks)."""
    if not cwd:
        return ""
    base = os.path.basename(str(cwd).rstrip("/"))
    if not base:
        return ""
    return config.resolve_wing(base)


def _read_hook_stdin():
    # type: () -> dict
    """Claude Code's SessionStart JSON, or {} for anything unreadable."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
    except (AttributeError, ValueError, OSError):
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv=None):
    # type: (Optional[list]) -> int
    parser = argparse.ArgumentParser(
        prog="mempalace.compact_recovery",
        description="Post-compaction re-injection for the SessionStart hook",
    )
    parser.add_argument("--session-id", default="", help="Session id (default: hook stdin)")
    parser.add_argument("--wing", default="", help="Palace wing (default: derived from cwd)")
    parser.add_argument(
        "--source",
        default="",
        help="SessionStart source; only 'compact' produces output unless --force",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when the source is not 'compact' (debugging)",
    )
    args = parser.parse_args(argv)

    hook = _read_hook_stdin()
    source = args.source or str(hook.get("source") or "")
    if source != "compact" and not args.force:
        return 0

    config = _load_config()
    session_id = args.session_id or str(hook.get("session_id") or "")
    wing = args.wing or wing_for_cwd(hook.get("cwd") or os.getcwd(), config)
    if not session_id:
        return 0

    try:
        result = recover(session_id, wing, config=config)
    except Exception:  # noqa: BLE001 - never fail the session over a recovery aid
        return 0

    sys.stdout.write(json.dumps(result.hook_payload()) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
