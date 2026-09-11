#!/usr/bin/env bash
# SessionStart — one visible line about the palace: alive?, how big, what this
# project's wing holds, MCP bridge resolvable? (techempower-org/mempalace#423)
#
# Fleet check-in 2026-09-03: agents didn't know what the palace held ("had no
# idea a 2g wing with 84K drawers existed") or whether it was alive (two silent
# breakages in three weeks). Budget <500ms: one GET to the daemon's cached
# /status/fast route; falls back to the auto-query wing cache when the host is
# asleep. Never blocks the session.
set -u
INPUT=$(cat 2>/dev/null || true)
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty' 2>/dev/null); [ -n "$CWD" ] || CWD=$PWD
WING=$(basename "$CWD" | tr 'A-Z-' 'a-z_')
case "$WING" in jp|home) WING=jp;; esac

# Post-compaction re-injection (techempower-org/mempalace#449). A compaction
# keeps the session id and throws away the context, so auto-query's
# per-session suppression list is now hiding exactly the drawers the agent
# just lost — and the old branch here offered a pointer ("run mempalace
# wake-up"), which fleet evidence says agents never act on. Clear the list and
# inject the story itself. All of that lives in mempalace so this stays one
# call: it prints the whole SessionStart payload, or nothing, and exits 0.
if [ "$(printf '%s' "$INPUT" | jq -r '.source // empty' 2>/dev/null)" = "compact" ]; then
  MP_DIR="${MEMPALACE_DIR:-/home/jp/Projects/memorypalace}"
  RECOVERY=$(printf '%s' "$INPUT" | PYTHONPATH="$MP_DIR" \
    "${MEMPALACE_PYTHON:-$MP_DIR/.venv/bin/python3}" -m mempalace.compact_recovery 2>/dev/null \
    || true)
  if [ -n "$RECOVERY" ]; then printf '%s\n' "$RECOVERY"; exit 0; fi
fi
KEY="${PALACE_API_KEY:-}"
if [ -z "$KEY" ] && [ -r ~/.config/palace-daemon/env ]; then
  KEY=$(sed -n 's/^\(export \)\?PALACE_API_KEY=//p' ~/.config/palace-daemon/env | head -1 | tr -d "\"'")
fi
URL="${PALACE_DAEMON_URL:-http://familiar:8085}"
MCP_OK="✓"; command -v mempalace-mcp >/dev/null 2>&1 || MCP_OK="✗ mempalace-mcp not on PATH"
T0=$(date +%s%N)
JSON=$(curl -s -m 2 -H "X-API-Key: $KEY" "$URL/status/fast" 2>/dev/null || true)
MS=$(( ($(date +%s%N) - T0) / 1000000 ))
if printf '%s' "$JSON" | jq -e '.total_drawers' >/dev/null 2>&1; then
  # total, wing count, this wing's drawers, this wing's rank (0 = absent)
  IFS=$'\t' read -r TOTAL NWINGS N RANK < <(printf '%s' "$JSON" | jq -r --arg w "$WING" '
    (.wings | to_entries | sort_by(-.value)) as $r
    | [ .total_drawers, ($r|length), (.wings[$w] // 0), ((($r|map(.key)|index($w)) // -1) + 1) ] | @tsv')
  g() { numfmt --grouping "$1" 2>/dev/null || printf '%s' "$1"; }
  if [ "${N:-0}" -gt 0 ]; then
    WPART="wing $WING: $(g "$N") drawers (rank $RANK of $NWINGS)"
  else
    WPART="wing $WING: none yet — run mempalace wings --sort count to find related wings"
  fi
  LINE="✦ palace ✓ $(g "$TOTAL") drawers · $NWINGS wings · ${MS}ms · mcp $MCP_OK │ $WPART"
  CTX="palace: wing '$WING' — run \`mempalace wake-up --wing $WING\` for the L1 story; \`mempalace search \"<terms>\" --wing $WING --limit 3\` for facts; announce every query (✦ palace ← …)."
  printf '{"systemMessage":%s,"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' \
    "$(printf '%s' "$LINE" | jq -Rs .)" "$(printf '%s' "$CTX" | jq -Rs .)"
else
  LINE="✦ palace ✗ daemon unreachable at $URL (${MS}ms) · mcp $MCP_OK — searches will fail until the palace host is awake (realm wol wake familiar)"
  printf '{"systemMessage":%s}\n' "$(printf '%s' "$LINE" | jq -Rs .)"
fi
exit 0
