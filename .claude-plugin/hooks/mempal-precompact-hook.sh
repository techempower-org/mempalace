#!/bin/bash
# MemPalace PreCompact Hook — thin wrapper delegating to palace-daemon's hook.py.
#
# See companion mempal-stop-hook.sh for the rationale. Post-2026-05-11
# we route all hook traffic through palace-daemon so a single canonical
# palace is the source of truth. This script is the back-compat shim
# for stale Claude Code sessions still pointing at the .sh wrapper.
#
# Override with PALACE_DAEMON_HOOK_PY=/path/to/hook.py — required on
# hosts where palace-daemon lives somewhere other than the default.
HOOK_PY="${PALACE_DAEMON_HOOK_PY:-/home/jp/Projects/palace-daemon/clients/hook.py}"
if [ -x "$(command -v python3)" ] && [ -f "$HOOK_PY" ]; then
    exec python3 "$HOOK_PY" --hook precompact --harness claude-code "$@"
fi
exit 0
