"""CLI entry point for auto-query integration.

Invoked by the UserPromptSubmit hook::

    $MEMPALACE_PYTHON -m mempalace.auto_query \\
        --prompt "remind me about the metadata bug?" \\
        --wing memorypalace --session-id "sess-abc" --turn 1

Outputs the injection block to stdout (if any), suitable for
``hookSpecificOutput.additionalContext`` in Claude Code hooks.
Exits 0 on success (with or without output), 1 on error.

Wing list is fetched from the daemon's fast ``/status/fast`` route at
startup and cached on disk; if the daemon is unreachable the cached
set is used, so wing scoring keeps working while the host sleeps.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from mempalace.auto_query.runner import _api_key, run_auto_query
from mempalace.config import MempalaceConfig


# Wing → drawer-count map from the last successful /status/fast fetch, used
# for the depth-refresh inventory line. Module-level so the existing wing
# fetch/cache contract stays untouched.
_LAST_WING_COUNTS = {}  # type: dict


def _default_wings_cache_path():
    # type: () -> str
    """Path to the on-disk wing cache."""
    return os.path.join(os.path.expanduser("~/.mempalace/auto_query"), "wings.json")


def _read_wings_cache(cache_path):
    # type: (str) -> set
    """Read the cached wing set; empty set if missing/unreadable."""
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def _write_wings_cache(wings, cache_path):
    # type: (set, str) -> None
    """Write the wing set to the cache, swallowing I/O errors."""
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(sorted(wings), f)
    except OSError:
        pass


def _fetch_wings_from_daemon(config):
    # type: (MempalaceConfig) -> set
    """Fetch wing names from the daemon's fast status route.

    Uses ``/status/fast`` (a cached counts endpoint that returns in ~ms)
    rather than ``mempalace_list_wings`` over ``/mcp``, which scans the full
    drawer set per call, reliably exceeds the hook's latency budget, and
    silently returns empty — disabling all wing scoring.
    """
    daemon_url = config.daemon_url
    if not daemon_url:
        return set()
    url = "{}/status/fast".format(daemon_url.rstrip("/"))
    headers = {}
    api_key = _api_key()  # env, else ~/.config/palace-daemon/env — hooks lack shell rc
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    from mempalace.auto_query.runner import deadline_timeout

    timeout = deadline_timeout(2)
    if timeout is None:
        return set()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return set()
    wings = data.get("wings", {})
    if isinstance(wings, dict):
        _LAST_WING_COUNTS.clear()
        _LAST_WING_COUNTS.update({str(k): v for k, v in wings.items()})
        return {str(name) for name in wings.keys()}
    if isinstance(wings, list):
        return {w.get("name", w) if isinstance(w, dict) else str(w) for w in wings}
    return set()


def _fetch_wings(config, cache_path=None):
    # type: (MempalaceConfig, Optional[str]) -> set
    """Fetch wing names, refreshing an on-disk cache.

    On a successful daemon fetch the cache is refreshed and the fresh set
    returned. On failure (daemon asleep/unreachable) the last cached set is
    used, so wing scoring keeps working while ``familiar`` naps.
    """
    if cache_path is None:
        cache_path = _default_wings_cache_path()
    wings = _fetch_wings_from_daemon(config)
    if wings:
        _write_wings_cache(wings, cache_path)
        return wings
    return _read_wings_cache(cache_path)


def _load_known_entities(path=None):
    # type: (Optional[str]) -> Optional[set]
    """Load the known-entity registry if present, else None.

    Accepts a JSON list of names or a dict keyed by name. Absent/unreadable
    file yields None (Pass-2 entity matching simply stays inactive).
    """
    if path is None:
        path = os.path.expanduser("~/.mempalace/known_entities.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, list):
        return {str(x) for x in data}
    if isinstance(data, dict):
        return {str(k) for k in data.keys()}
    return None


def main(argv=None):
    # type: (list) -> int
    parser = argparse.ArgumentParser(
        prog="mempalace.auto_query",
        description="Auto-query classifier for MemPalace",
    )
    parser.add_argument("--prompt", required=True, help="User message text")
    parser.add_argument("--wing", default="", help="Project wing name")
    parser.add_argument("--session-id", default="cli", help="Session identifier")
    parser.add_argument("--turn", type=int, default=1, help="Turn index (1-based)")
    parser.add_argument(
        "--recent-drawers",
        action="store_true",
        default=False,
        help="Flag: project wing has recent drawers",
    )

    args = parser.parse_args(argv)
    config = MempalaceConfig()

    project_wing = config.resolve_wing(args.wing) if args.wing else ""

    known_wings = _fetch_wings(config)
    known_entities = _load_known_entities()

    result = run_auto_query(
        prompt=args.prompt,
        session_id=args.session_id,
        turn=args.turn,
        project_wing=project_wing,
        known_wings=known_wings,
        known_entities=known_entities,
        has_recent_drawers=args.recent_drawers,
        config=config,
        wing_counts=dict(_LAST_WING_COUNTS) or None,
    )

    if result.injection:
        sys.stdout.write(result.injection)
        sys.stdout.write("\n")

    # Receipt on stderr (stdout is the injection channel): the hook turns
    # this into the visible `✦ palace ← …` terminal line — including for
    # queries that found nothing, so a silent miss can't hide.
    if result.receipt:
        sys.stderr.write("RECEIPT: " + json.dumps(result.receipt) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
