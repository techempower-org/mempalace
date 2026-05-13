#!/usr/bin/env bash
# MemPalace Codex Hook — routes through palace-daemon's hook.py
# (post-2026-05-13 split-brain fix; matches the claude-code path).
#
# Previously this shelled to `mempalace hook run` which goes through
# upstream's mempalace/hooks_cli.py. After the 2026-05-11 cascade we
# split-brain'd claude-code to route through palace-daemon's stdlib-only
# clients/hook.py; this script does the same for codex so feature work
# in palace-daemon (project wings, slug labels, mine dedup, etc.) reaches
# both harnesses.
#
# If palace-daemon/clients/hook.py is missing, exit 0 (don't fail the
# session) — same pattern as the claude-code shim.
set -euo pipefail
HOOK_NAME="${1:?Usage: mempal-hook.sh <hook-name>}"
HOOK_PY=/home/jp/Projects/palace-daemon/clients/hook.py
if [[ ! -f "$HOOK_PY" ]]; then
    echo "{}"
    exit 0
fi
exec python3 "$HOOK_PY" --hook "$HOOK_NAME" --harness codex
