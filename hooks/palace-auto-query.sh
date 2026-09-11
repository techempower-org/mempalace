#!/usr/bin/env bash
# UserPromptSubmit hook — auto-query MemPalace for context injection.
#
# Pre-filters with shell regex to avoid 250ms Python startup on messages
# that clearly won't trigger. When the filter matches, delegates to
# `python -m mempalace.auto_query` which runs the full signal → route →
# MCP → format pipeline.
#
# Output contract (2026-09-03): the classifier prints the injection block on
# stdout and a one-line `RECEIPT: {json}` on stderr whenever a query FIRED —
# including one that found nothing. The receipt becomes a visible terminal
# line (`systemMessage`) so every palace query is seen by the human, not only
# by the model. additionalContext alone is invisible to the user.

set -euo pipefail

MEMPALACE_DIR="${MEMPALACE_DIR:-/home/jp/Projects/memorypalace}"
MEMPALACE_PYTHON="${MEMPALACE_PYTHON:-${MEMPALACE_DIR}/.venv/bin/python3}"
LOG="${PALACE_AUTO_QUERY_LOG:-/tmp/palace-auto-query.log}"

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | jq -r '.prompt // .userPrompt // ""' 2>/dev/null)

# Fast exit: empty prompt
if [ -z "$PROMPT" ]; then
  exit 0
fi

# Determine project wing from CWD
PROJECT_WING=""
CWD=$(echo "$INPUT" | jq -r '.cwd // ""' 2>/dev/null || echo "")
if [ -n "$CWD" ]; then
  BASENAME=$(basename "$CWD")
  PROJECT_WING="$BASENAME"
fi

# Session ID — prefer the harness-provided id in the hook's stdin JSON, then
# the CLAUDE_CODE_SESSION_ID env var, then a time-based fallback. The old
# CLAUDE_SESSION_ID env var is never set by Claude Code, so the fallback fired
# on every prompt -> a fresh id per second -> the turn counter reset to 1 each
# turn and the periodic depth signal could never fire.
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
if [ -z "$SESSION_ID" ]; then
  SESSION_ID="${CLAUDE_CODE_SESSION_ID:-$(date +%s)}"
fi

# Turn index — computed BEFORE the pre-filter so turn-1 resumption works
TURN_FILE="/tmp/palace-aq-turn-${SESSION_ID}"
if [ -f "$TURN_FILE" ]; then
  TURN=$(cat "$TURN_FILE")
  TURN=$((TURN + 1))
else
  TURN=1
fi
echo "$TURN" > "$TURN_FILE" 2>/dev/null || true

# Shell-level pre-filter — skip Python entirely if no signal pattern matches.
# Must be a SUPERSET of Python's signal extraction — err on the side of
# letting prompts through. Five checks:
#   1. Temporal/explicit recall + session-resumption patterns (case-insensitive)
#   2. Capitalized entity names (potential wing/entity matches)
#   3. Identifier shapes (ALLCAPS, snake_case, camelCase, hex, alnum codes)
#   4. Turn 1 always passes (task resumption + first depth refresh)
#   5. Every 10th turn passes (periodic depth refresh)
MATCH=0
echo "$PROMPT" | grep -qiE 'remind|remember|do (we|you) (have|know)|did (we|you)|what (did|was|were) |history of|have we|prior to|earlier|last (time|week|session|night|run|month|sprint)|yesterday|previously|recently|while ago|days ago|back when|used to|that time|when (did|we|was)|before we|recall|check (if|whether)|was there|were there|where (were|was)|where we left|pick(ing)? up where|catch me up|up to speed|recap|refresh (my|your) memory|resume (the|our|this)|what.?s (the|still|left|remaining|outstanding)|what is (the|still|left)|which (prs?|pull requests|issues|tickets|branches|agents|lanes)|what (were|was) (we|i)' && MATCH=1
[ "$MATCH" -eq 0 ] && echo "$PROMPT" | grep -qE '[A-Z][a-zA-Z]{2,}' && MATCH=1
[ "$MATCH" -eq 0 ] && echo "$PROMPT" | grep -qE '[A-Za-z]+_[A-Za-z0-9_]+|[A-Z]{3,}|[a-z]+[A-Z][a-z]|0x[0-9a-fA-F]{2,}|[A-Za-z]+[0-9]{2,}' && MATCH=1
[ "$MATCH" -eq 0 ] && [ "$TURN" -eq 1 ] && MATCH=1
[ "$MATCH" -eq 0 ] && [ "$((TURN % 10))" -eq 0 ] && MATCH=1
if [ "$MATCH" -eq 0 ]; then
  echo "$(date -Iseconds) skip:no-signal" >> "$LOG" 2>/dev/null || true
  exit 0
fi

# Run the classifier — stdout is the injection, stderr carries the receipt.
ERRFILE=$(mktemp "${TMPDIR:-/tmp}/palace-aq-err.XXXXXX")
trap 'rm -f "$ERRFILE"' EXIT
INJECTION=$(PYTHONPATH="$MEMPALACE_DIR" "$MEMPALACE_PYTHON" -m mempalace.auto_query \
  --prompt "$PROMPT" \
  --wing "$PROJECT_WING" \
  --session-id "$SESSION_ID" \
  --turn "$TURN" \
  2>"$ERRFILE") || true
# Unanchored: library warnings may share the line or omit their trailing newline.
RECEIPT_JSON_RAW=$(grep -o -m1 'RECEIPT: {.*}' "$ERRFILE" 2>/dev/null | head -1 | sed 's/^RECEIPT: //' || true)

# Visible receipt line:  ✦ palace ← "<query>" [wing] → N hits (Nms)
#                    or:  ✦ palace ← "<query>" [wing] → 0 hits above 0.50 (best 0.44) (Nms)
RECEIPT=""
if [ -n "$RECEIPT_JSON_RAW" ]; then
  RECEIPT=$(printf '%s' "$RECEIPT_JSON_RAW" | jq -r '
    def q: (.query // "?") | if length > 60 then .[0:57] + "…" else . end;
    def w: if (.wing // "") != "" then " [" + .wing + "]" else "" end;
    def ms: (if (.route // "") != "" then " via " + .route else "" end)
            + (if (.cached // false) then " (cached)" elif .latency_ms != null then " (" + (.latency_ms|tostring) + "ms)" else "" end);
    def best: if .best != null then (.best*100|round/100|tostring) else "n/a" end;
    def floor: if .floor != null then (.floor*100|round/100|tostring) else "" end;
    if (.error // "") != "" then
      "✦ palace ← \"" + q + "\"" + w + " → ✗ " + .error + ms
    elif (.hits // 0) > 0 then
      "✦ palace ← \"" + q + "\"" + w + " → " + (.hits|tostring) + " hits" + ms
    elif (.raw_hits // 0) > 0 then
      "✦ palace ← \"" + q + "\"" + w + " → 0 hits above " + floor + " (best " + best + "; " + (.raw_hits|tostring) + " filtered)" + ms
    else
      "✦ palace ← \"" + q + "\"" + w + " → 0 hits" + ms
    end' 2>/dev/null || true)
fi

if [ -n "$INJECTION" ]; then
  ESCAPED=$(printf '%s' "$INJECTION" | jq -Rs .)
  if [ -n "$RECEIPT" ]; then
    RJ=$(printf '%s' "$RECEIPT" | jq -Rs .)
    printf '{"systemMessage":%s,"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":%s}}\n' "$RJ" "$ESCAPED"
  else
    printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":%s}}\n' "$ESCAPED"
  fi
  echo "$(date -Iseconds) fire:injected" >> "$LOG" 2>/dev/null || true
elif [ -n "$RECEIPT" ]; then
  RJ=$(printf '%s' "$RECEIPT" | jq -Rs .)
  printf '{"systemMessage":%s}\n' "$RJ"
  echo "$(date -Iseconds) fire:receipt-only" >> "$LOG" 2>/dev/null || true
else
  echo "$(date -Iseconds) skip:no-injection" >> "$LOG" 2>/dev/null || true
fi
