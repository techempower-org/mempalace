#!/bin/bash
# MemPalace Stop Hook — thin wrapper delegating to palace-daemon's hook.py.
#
# Post-2026-05-11 split-brain fix: all hook traffic routes through the
# palace-daemon HTTP gateway so a single canonical palace is the source
# of truth. The hooks.json in this plugin already declares
# palace-daemon's hook.py directly, but Claude Code sessions that loaded
# the old hook config (which called this script) keep firing it until
# they restart. Making this script a thin pass-through means those
# stale sessions still route through the daemon instead of erroring.
#
# Override the hook.py location with PALACE_DAEMON_HOOK_PY=/path/to/hook.py
# — required on hosts where palace-daemon lives somewhere other than the
# default below (e.g. /home/jp/.local/share/palace-daemon/ on disks, or
# CI fixtures).
#
# If palace-daemon's hook.py is missing on this machine, we exit 0 (not
# a hard error) so a Stop event from a host without palace-daemon
# doesn't gum up the harness.
HOOK_PY="${PALACE_DAEMON_HOOK_PY:-/home/jp/Projects/palace-daemon/clients/hook.py}"
if [ -x "$(command -v python3)" ] && [ -f "$HOOK_PY" ]; then
    exec python3 "$HOOK_PY" --hook stop --harness claude-code "$@"
fi
exit 0
