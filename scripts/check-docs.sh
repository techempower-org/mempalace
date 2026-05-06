#!/usr/bin/env bash
# check-docs.sh — sanity-check that fork docs are still in sync with reality.
#
# What it checks:
#   1. Test count in README matches `pytest --collect-only -q` reality.
#   2. Every fork commit hash referenced in CLAUDE.md / README.md /
#      FORK_CHANGELOG.md actually resolves via `git cat-file -e`.
#   3. FORK_CHANGELOG.md is in sync with docs/fork-changes.yaml
#      (re-runs render-docs.py --check internally).
#   4. Every upstream PR mentioned (#NNNN) has a state matching what the
#      doc claims (OPEN / MERGED / CLOSED). Uses `gh pr view`; skipped
#      gracefully if `gh` isn't authenticated.
#   5. Every `commit:` hash in docs/fork-changes.yaml is referenced
#      somewhere in CLAUDE.md (catches drift between the structured DB
#      and the hand-maintained row inventory).
#
# Exit codes:
#   0 — clean
#   1 — at least one drift detected
#   2 — internal error (e.g., not in a git repo)
#
# Usage:
#   scripts/check-docs.sh                  # interactive run
#   scripts/check-docs.sh --quiet          # only print failures
#   STRICT_PR_STATE=1 scripts/check-docs.sh  # warn → error on PR-state drift

set -uo pipefail
shopt -s nullglob

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "✗ not a git repo" >&2
    exit 2
}
cd "$REPO_ROOT"

quiet=0
[ "${1:-}" = "--quiet" ] && quiet=1

step()  { (( quiet )) || printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok()    { (( quiet )) || printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
fail()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; ((failures++)); }

failures=0

# ── 1. test count ────────────────────────────────────────────────────────
step "1/5  test count in README"
readme_count=$(grep -oE '^[0-9]+ tests pass on `main`' README.md | grep -oE '^[0-9]+' || echo "")
if [ -z "$readme_count" ]; then
    warn "README has no '<N> tests pass on \`main\`' line — skipping"
else
    # Prefer the repo venv's pytest so the check works without an
    # activated environment. Falls back to whatever pytest is on PATH.
    pytest_bin="$REPO_ROOT/venv/bin/pytest"
    [ -x "$pytest_bin" ] || pytest_bin="$(command -v pytest 2>/dev/null || true)"
    if [ -z "$pytest_bin" ]; then
        warn "no pytest available — skipping"
    else
        actual_count=$("$pytest_bin" --collect-only -q 2>/dev/null \
            | grep -E "[0-9]+/[0-9]+ tests collected" \
            | head -1 | awk -F'/' '{print $1}' || echo "")
        if [ -z "$actual_count" ]; then
            actual_count=$("$pytest_bin" --collect-only -q 2>/dev/null \
                | grep -E "^[0-9]+ tests collected" \
                | head -1 | awk '{print $1}' || echo "")
        fi
        if [ -z "$actual_count" ]; then
            warn "pytest --collect-only produced no count — skipping"
        elif [ "$readme_count" != "$actual_count" ]; then
            fail "README says $readme_count, pytest collects $actual_count"
        else
            ok "README $readme_count == pytest $actual_count"
        fi
    fi
fi

# ── 2. commit hash references ────────────────────────────────────────────
step "2/5  commit hashes referenced in docs resolve"
docs=(README.md CLAUDE.md FORK_CHANGELOG.md)
# Strip cross-repo URLs first so we only check hashes that should resolve
# in *this* fork. Pattern: anything inside (https://github.com/<other>/<repo>/commit/HASH)
# where <other>/<repo> is not jphein/mempalace.
# For each line, skip the line entirely if it mentions a sibling repo
# (palace-daemon / multipass-structural-memory-eval) — we can't tell which
# hashes on that line are fork-mempalace vs cross-repo without parsing
# linked URLs by repo. Treating the whole line as cross-repo is the
# conservative under-call: false negatives (missing a real bad hash
# adjacent to a sibling-repo mention) but no false positives.
mapfile -t hashes < <(
    for d in "${docs[@]}"; do
        grep -v -E 'palace-daemon|multipass-structural-memory-eval|/jphein/[a-z-]+/commit/' "$d" 2>/dev/null
    done | grep -hoE '`[0-9a-f]{7,40}`' | tr -d '`' | sort -u
)
unresolved=0
for h in "${hashes[@]}"; do
    if ! git cat-file -e "$h" 2>/dev/null; then
        fail "commit hash \`$h\` referenced in docs but does not resolve in this repo"
        ((unresolved++))
    fi
done
if (( unresolved == 0 )) && (( ${#hashes[@]} > 0 )); then
    ok "all ${#hashes[@]} fork hash references resolve"
fi

# ── 3. FORK_CHANGELOG.md is up-to-date with the canonical YAML ───────────
step "3/5  FORK_CHANGELOG.md regenerates clean"
render_bin="$REPO_ROOT/scripts/render-docs.py"
if [ -x "$render_bin" ]; then
    py="$REPO_ROOT/venv/bin/python"
    [ -x "$py" ] || py="$(command -v python3 2>/dev/null || true)"
    if [ -z "$py" ]; then
        warn "no python interpreter — skipping render check"
    elif "$py" "$render_bin" --check >/dev/null 2>&1; then
        ok "FORK_CHANGELOG.md matches docs/fork-changes.yaml"
    else
        fail "FORK_CHANGELOG.md is stale — run scripts/render-docs.py to regenerate"
    fi
else
    warn "scripts/render-docs.py not present — skipping render check"
fi

# ── 4. upstream PR states ────────────────────────────────────────────────
step "4/5  upstream PR states match doc claims"
if ! command -v gh >/dev/null 2>&1; then
    warn "gh not on PATH — skipping PR state check"
elif ! gh auth status >/dev/null 2>&1; then
    warn "gh not authenticated — skipping PR state check"
else
    # Pull every #NNNN reference from the docs, dedupe.
    mapfile -t pr_numbers < <(grep -hoE '#[0-9]{2,5}' "${docs[@]}" 2>/dev/null \
        | grep -oE '[0-9]+' | sort -u)
    drift=0
    for n in "${pr_numbers[@]}"; do
        # Heuristic: only check PRs (not issues). gh handles either; on
        # state==null we assume it's an issue and skip.
        state=$(gh pr view "$n" --repo MemPalace/mempalace --json state \
            --jq '.state' 2>/dev/null || echo "")
        [ -z "$state" ] && continue
        # Pull all doc lines mentioning this PR for context comparison.
        # We don't try to parse exhaustively; just flag when a doc says
        # MERGED but gh says OPEN, or vice versa.
        doc_says_merged=0; doc_says_open=0; doc_says_closed=0
        for d in "${docs[@]}"; do
            line=$(grep -E "(#$n|/$n)" "$d" 2>/dev/null | head -1 | tr A-Z a-z)
            [[ "$line" == *"merged"* ]] && doc_says_merged=1
            [[ "$line" == *"open"*    ]] && doc_says_open=1
            [[ "$line" == *"closed"*  ]] && doc_says_closed=1
        done
        # Skip narrative paragraphs that mention multiple PRs — words
        # like "merged" / "open" usually refer to *other* PRs on the
        # same line, not the one we're checking. Only check lines that
        # mention this PR alone.
        for d in "${docs[@]}"; do
            line=$(grep -E "(#$n[^0-9]|/$n[^0-9])" "$d" 2>/dev/null | head -1)
            other_prs=$(echo "$line" | grep -oE '#[0-9]{2,5}' | grep -v "^#$n$" | wc -l)
            if (( other_prs > 0 )); then
                doc_says_merged=0; doc_says_open=0; doc_says_closed=0
            fi
        done
        # If both states appear, it's commentary too.
        if (( doc_says_merged )) && (( doc_says_open )); then
            continue
        fi
        case "$state" in
            MERGED)
                if (( doc_says_open )) && (( ! doc_says_merged )); then
                    if [ "${STRICT_PR_STATE:-0}" = "1" ]; then
                        fail "PR #$n is MERGED upstream, docs still say OPEN"
                    else
                        warn "PR #$n is MERGED upstream, docs still say OPEN"
                    fi
                    ((drift++))
                fi
                ;;
            OPEN)
                if (( doc_says_merged )) && (( ! doc_says_open )); then
                    if [ "${STRICT_PR_STATE:-0}" = "1" ]; then
                        fail "PR #$n is OPEN upstream, docs say MERGED"
                    else
                        warn "PR #$n is OPEN upstream, docs say MERGED"
                    fi
                    ((drift++))
                fi
                ;;
            CLOSED)
                if (( doc_says_open )) && (( ! doc_says_closed )); then
                    if [ "${STRICT_PR_STATE:-0}" = "1" ]; then
                        fail "PR #$n is CLOSED (not merged), docs say OPEN"
                    else
                        warn "PR #$n is CLOSED (not merged), docs say OPEN"
                    fi
                    ((drift++))
                fi
                ;;
        esac
    done
    if (( drift == 0 )) && (( ${#pr_numbers[@]} > 0 )); then
        ok "all ${#pr_numbers[@]} PR references match upstream state"
    fi
fi

# ── 5. fork-only YAML commits referenced in CLAUDE.md ────────────────────
# Every entry in docs/fork-changes.yaml has a `commit:` field. CLAUDE.md
# is the row inventory for what's still ahead of upstream. The two
# represent the same set of changes through different lenses, but the
# anchors differ:
#
#   - YAML entries with a `pr:` field are represented in CLAUDE.md by
#     the *PR table entry* for that PR (e.g. "#1087 | open …"), not by
#     the commit hash — so the hash check would be redundant noise.
#
#   - YAML entries with `pr_state: MERGED` are upstreamed; CLAUDE.md
#     replaces the row's commit hash with "merged via #NNNN" once the
#     PR ships, by design.
#
# So this check focuses on the only category that should always have a
# matching CLAUDE.md row: entries that are fork-only with no PR yet
# (no `pr:` field, no `pr_state`). If those don't appear in CLAUDE.md,
# the row inventory genuinely missed an update.
step "5/5  fork-only YAML commits referenced in CLAUDE.md"
yaml_path="$REPO_ROOT/docs/fork-changes.yaml"
if [ ! -f "$yaml_path" ]; then
    warn "docs/fork-changes.yaml not present — skipping"
elif [ ! -f "$REPO_ROOT/CLAUDE.md" ]; then
    warn "CLAUDE.md not present — skipping"
else
    # Walk the YAML in awk — for each entry block, capture commit, pr,
    # and pr_state. Emit only commits where both pr and pr_state are
    # absent (truly fork-only, not yet upstreamed and not staged for
    # upstream). Entry boundaries: lines starting with `  - id:`.
    mapfile -t yaml_commits < <(
        awk '
            function flush() {
                if (commit && !pr && pr_state == "") print commit
                commit = ""; pr = ""; pr_state = ""
            }
            /^  - id:/ { flush() }
            /^[[:space:]]+commit:[[:space:]]/ {
                gsub(/^[[:space:]]+commit:[[:space:]]+"?/, "")
                gsub(/"?[[:space:]]*$/, "")
                commit = $0
            }
            /^[[:space:]]+pr:[[:space:]]/ {
                gsub(/^[[:space:]]+pr:[[:space:]]+"?/, "")
                gsub(/"?[[:space:]]*$/, "")
                pr = $0
            }
            /^[[:space:]]+pr_state:[[:space:]]/ {
                gsub(/^[[:space:]]+pr_state:[[:space:]]+"?/, "")
                gsub(/"?[[:space:]]*$/, "")
                pr_state = $0
            }
            END { flush() }
        ' "$yaml_path" | sort -u
    )
    missing=0
    for h in "${yaml_commits[@]}"; do
        if ! grep -qF "$h" "$REPO_ROOT/CLAUDE.md"; then
            fail "fork-only YAML commit \`$h\` not referenced in CLAUDE.md row inventory"
            ((missing++))
        fi
    done
    if (( missing == 0 )) && (( ${#yaml_commits[@]} > 0 )); then
        ok "all ${#yaml_commits[@]} fork-only YAML commits referenced in CLAUDE.md"
    elif (( ${#yaml_commits[@]} == 0 )); then
        # Every YAML entry currently has either a pr: or pr_state:.
        # That's possible during quiet periods; not a drift signal.
        ok "no fork-only YAML commits to check (every entry is either upstreamed or has an open PR)"
    fi
fi

# ── summary ──────────────────────────────────────────────────────────────
if (( failures == 0 )); then
    (( quiet )) || printf '\n\033[1;32m✦ docs clean\033[0m\n'
    exit 0
else
    printf '\n\033[1;31m✗ %d issue(s) found\033[0m\n' "$failures" >&2
    exit 1
fi
