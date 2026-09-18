#!/usr/bin/env bash
# check-entry-coverage.sh — every merged fork PR must leave a fork-changes entry.
#
# check-docs.sh verified render parity, sha resolution, sha ancestry and
# upstream PR states, but never that a merged fork PR documented itself. #517
# was green on every check with no entry at all, and nothing would have
# surfaced it later: `--next-seq` and the renderers are happy with any subset
# (#519). The checker answered a narrower question than its name — the same
# shape as #516 and #505.
#
# Method: every squash-merge commit carries a trailing `(#NNN)`. For each one
# since BASELINE there must be either a `fork_pr: NNN` in docs/fork-changes/,
# or a line in the allowlist saying why not.
#
# Offline by design: `git log` only, never the GitHub API. A docs check that
# needs the network is one that gets skipped.
#
# Usage:
#   scripts/check-entry-coverage.sh                 # warn only, exit 0
#   scripts/check-entry-coverage.sh --strict        # exit 1 on any miss
#   scripts/check-entry-coverage.sh --baseline SHA  # override the baseline
#   scripts/check-entry-coverage.sh --quiet         # only print problems
#
# Exit: 0 clean (or warn-only), 1 missing entries under --strict, 2 internal.

set -uo pipefail
shopt -s nullglob

# The commit that introduced docs/fork-changes/ AND the `fork_pr` field (#480).
# Before it the field did not exist, so "missing" is meaningless for the ~137
# older entries — a baseline is what keeps this check about drift rather than
# about history.
BASELINE=6da87755

strict=0
quiet=0
while [ $# -gt 0 ]; do
    case "$1" in
        --strict)   strict=1 ;;
        --quiet)    quiet=1 ;;
        --baseline) shift; BASELINE="${1:-}" ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "✗ not a git repo" >&2; exit 2
}
cd "$REPO_ROOT"

say()  { (( quiet )) || printf '%s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; }

git cat-file -e "${BASELINE}^{commit}" 2>/dev/null || {
    echo "✗ baseline '$BASELINE' does not resolve in this repo" >&2; exit 2
}

# ── squash-merge commits in range ────────────────────────────────────────
log="$(git log --oneline "${BASELINE}..HEAD" 2>/dev/null)" || {
    echo "✗ cannot read git log ${BASELINE}..HEAD" >&2; exit 2
}
declare -a prs=() ; declare -A title_of=()
while IFS= read -r line; do
    [ -n "$line" ] || continue
    if [[ "$line" =~ \(#([0-9]+)\)[[:space:]]*$ ]]; then
        n="${BASH_REMATCH[1]}"
        prs+=("$n")
        # First (newest) mention wins; the subject minus the sha.
        [ -n "${title_of[$n]:-}" ] || title_of[$n]="$(printf '%s' "$line" | cut -d' ' -f2-)"
    fi
done <<< "$log"

# ── fork_pr values already documented ────────────────────────────────────
declare -A documented=()
for f in docs/fork-changes/*.yaml; do
    while IFS= read -r n; do
        [ -n "$n" ] && documented[$n]=1
    done < <(grep -hoE '^fork_pr:[[:space:]]*[0-9]+' "$f" 2>/dev/null | grep -oE '[0-9]+')
done

# ── allowlist: `NNN  reason` ─────────────────────────────────────────────
# A bare number is refused. An allowlist without reasons is a mute button,
# and the next person cannot tell a deliberate omission from an abandoned one.
ALLOWLIST=docs/fork-changes-no-entry.txt
declare -A allowed=()
allowlist_problems=0
if [ -f "$ALLOWLIST" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%$'\r'}"
        trimmed="$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        [ -n "$trimmed" ] || continue
        case "$trimmed" in \#*) continue ;; esac
        num="$(printf '%s' "$trimmed" | cut -d' ' -f1)"
        rest="$(printf '%s' "$trimmed" | cut -s -d' ' -f2- | sed 's/^[[:space:]]*//')"
        if ! [[ "$num" =~ ^[0-9]+$ ]]; then
            fail "$ALLOWLIST: '$trimmed' does not start with a PR number"
            allowlist_problems=$((allowlist_problems+1)); continue
        fi
        if [ -z "$rest" ]; then
            fail "$ALLOWLIST: #$num has no reason — an allowlist records WHY, or it is a mute button"
            allowlist_problems=$((allowlist_problems+1)); continue
        fi
        allowed[$num]=1
    done < "$ALLOWLIST"
fi

# ── the comparison ───────────────────────────────────────────────────────
declare -a missing=()
declare -A seen=()
for n in "${prs[@]}"; do
    [ -n "${seen[$n]:-}" ] && continue
    seen[$n]=1
    [ -n "${documented[$n]:-}" ] && continue
    [ -n "${allowed[$n]:-}" ] && continue
    missing+=("$n")
done

say "  examined ${#seen[@]} squash-merge commit(s) since ${BASELINE}"
say "  documented: ${#documented[@]} fork_pr value(s) · allowlisted: ${#allowed[@]}"

if [ ${#missing[@]} -eq 0 ] && [ "$allowlist_problems" -eq 0 ]; then
    (( quiet )) || printf '  \033[32m✓\033[0m every merged fork PR since %s has an entry or an allowlist line\n' "$BASELINE"
    exit 0
fi

for n in $(printf '%s\n' "${missing[@]}" | sort -n); do
    msg="#$n has no docs/fork-changes entry — ${title_of[$n]:-<title unknown>}"
    if (( strict )); then fail "$msg"; else warn "$msg"; fi
done

if (( strict )); then
    exit 1
fi
[ "$allowlist_problems" -gt 0 ] && exit 1
printf '  \033[33m!\033[0m %d PR(s) missing an entry (warn-only; --strict to fail)\n' "${#missing[@]}" >&2
exit 0
