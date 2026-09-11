#!/usr/bin/env bash
# check-docs.sh — sanity-check that fork docs are still in sync with reality.
#
# What it checks:
#   1. Test count is DERIVED from `pytest --collect-only -q` and reported;
#      no committed literal is asserted (#473).
#   2. Every fork commit hash referenced in CLAUDE.md / README.md /
#      FORK_CHANGELOG.md actually resolves via `git cat-file -e`.
#   3. FORK_CHANGELOG.md is in sync with docs/fork-changes/ (one file per entry)
#      (re-runs render-docs.py --check internally).
#   4. Every upstream PR mentioned (#NNNN) has a state matching what the
#      doc claims (OPEN / MERGED / CLOSED). Uses `gh pr view`; skipped
#      gracefully if `gh` isn't authenticated.
#   5. website/public/llms-full.txt regenerates clean from its sources.
#   6. website/reference/python-api/ regenerates clean from mempalace/
#      docstrings (re-runs render-api-docs.py --check internally).
#   7. Every "N tools" / "N MCP tools" claim in our docs matches
#      len(mempalace.mcp_server.TOOLS). Competitor counts (README
#      landscape table, ECOSYSTEM.md, llms-full.txt) are excluded.
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
# DERIVED, never asserted against a committed literal (#473). A number in
# README/CLAUDE.md meant every PR that added a test edited the same two lines,
# so a 10-PR wave conflicted there every time and each merge forced the rest to
# re-derive it. The count is reported here for whoever is looking; it is not a
# gate, because there is no longer a literal that can go stale. A literal that
# nothing can contradict is the only kind worth keeping.
step "1/7  test count (derived)"
readme_count=$(grep -oE '[0-9]+ tests pass on `main`' README.md | grep -oE '^[0-9]+' || echo "")
if false; then
    :
else
    # Prefer the repo venv's pytest so the check works without an
    # activated environment. In a git worktree (`.claude/worktrees/...`)
    # `REPO_ROOT` is the worktree dir which has no `.venv`; fall back to
    # the main checkout's `.venv` via `git rev-parse --git-common-dir`
    # so worktree shells get the same behavior as the main checkout.
    # Final fallback is whatever pytest is on PATH; a missing pytest
    # fails hard (#311) rather than silently skipping, because a
    # silent skip lets stale README counts reach CI.
    pytest_bin="$REPO_ROOT/.venv/bin/pytest"
    if [ ! -x "$pytest_bin" ]; then
        common_dir="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
        if [ -n "$common_dir" ]; then
            main_root="$(dirname "$(realpath "$common_dir")")"
            [ -x "$main_root/.venv/bin/pytest" ] && pytest_bin="$main_root/.venv/bin/pytest"
        fi
    fi
    [ -x "$pytest_bin" ] || pytest_bin="$(command -v pytest 2>/dev/null || true)"
    if [ -z "$pytest_bin" ] || [ ! -x "$pytest_bin" ]; then
        fail "no pytest available — install with: uv venv && uv pip install -e '.[dev]'"
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
        elif [ -n "$readme_count" ]; then
            # A literal crept back in. Not a hard failure (it may be a
            # deliberate release note), but say so, because it will go stale
            # and nothing else will notice.
            warn "docs carry a hard-coded test count ($readme_count); suite collects $actual_count — prefer no literal (#473)"
        else
            ok "suite collects $actual_count tests (no committed literal to drift)"
        fi
    fi
fi

# ── 2. commit hash references ────────────────────────────────────────────
step "2/7  commit hashes referenced in docs resolve"
docs=(README.md CLAUDE.md FORK_CHANGELOG.md)
# Strip cross-repo URLs first so we only check hashes that should resolve
# in *this* fork. Pattern: anything inside (https://github.com/<other>/<repo>/commit/HASH)
# where <other>/<repo> is not techempower-org/mempalace.
# For each line, skip the line entirely if it mentions a sibling repo
# (palace-daemon / multipass-structural-memory-eval) or upstream — we
# can't tell which hashes on that line are fork-mempalace vs cross-repo
# without parsing linked URLs by repo, and upstream-sync lines reference
# MemPalace/mempalace commits that only resolve when the upstream remote
# is fetched (never true on CI's shallow origin-only checkout: a squash-
# merged sync severs the ancestry, so e.g. `da5a48c` resolved on the sync
# PR itself but not on any later branch). Treating the whole line as
# cross-repo is the conservative under-call: false negatives (missing a
# real bad hash adjacent to such a mention) but no false positives.
mapfile -t hashes < <(
    for d in "${docs[@]}"; do
        grep -v -E 'palace-daemon|multipass-structural-memory-eval|upstream|/(jphein|techempower-org)/[a-z-]+/commit/' "$d" 2>/dev/null
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

# ── 2b. every entry's commit is an ANCESTOR of HEAD (#472) ───────────────
# Resolving is not enough. A rebase or a squash merge rewrites the commit,
# the entry keeps the old sha, and `git cat-file -e` still succeeds because
# the object is merely dangling — it stays alive locally and in GitHub's
# unreachable-object store until gc. That is exactly how 13 entries came to
# reference commits unreachable from main while this script reported green.
# Checked per ENTRY (not by scraping markdown) so the failure names the file
# a human has to edit.
step "2b/7 fork-change entry commits are ancestors of HEAD"
py_entries="$REPO_ROOT/.venv/bin/python"
[ -x "$py_entries" ] || py_entries="$(command -v python3 2>/dev/null || true)"
legacy_file="$REPO_ROOT/docs/fork-changes-legacy-shas.txt"
if [ -n "$py_entries" ] && [ -d "$REPO_ROOT/docs/fork-changes" ]; then
    non_ancestors=0
    checked=0
    skipped=0
    while IFS=$'\t' read -r entry_id sha; do
        [ -n "$sha" ] || continue
        # Documented-unrecoverable entries: see the header of that file.
        # Matched on the PAIR (id AND sha). Matching the id alone would
        # exempt the entry forever, so its commit could later be edited to
        # any value — including a plausible wrong sha, which is a real
        # ancestor and passes — without anyone being prompted again.
        if [ -f "$legacy_file" ] \
           && grep -qE "^[[:space:]]*${entry_id}[[:space:]]+${sha}([[:space:]]|#|$)" "$legacy_file"; then
            ((skipped++))
            continue
        fi
        ((checked++))
        if ! git merge-base --is-ancestor "$sha" HEAD 2>/dev/null; then
            if git cat-file -e "$sha" 2>/dev/null; then
                fail "entry '$entry_id' commit \`$sha\` exists but is NOT an ancestor of HEAD (stale branch sha? use commit: HEAD and let the merge resolve it)"
            else
                fail "entry '$entry_id' commit \`$sha\` does not resolve at all"
            fi
            ((non_ancestors++))
        fi
    done < <("$py_entries" "$REPO_ROOT/scripts/fork_changes.py" --commit-refs 2>/dev/null)
    if (( non_ancestors == 0 )); then
        ok "all $checked entry commits are ancestors of HEAD ($skipped documented-legacy skipped)"
    fi
else
    warn "skipping entry-ancestry check (no python or no docs/fork-changes/)"
fi

# ── 3. FORK_CHANGELOG.md is up-to-date with the canonical entries ────────
step "3/7  FORK_CHANGELOG.md regenerates clean"
render_bin="$REPO_ROOT/scripts/render-docs.py"
if [ -x "$render_bin" ]; then
    py="$REPO_ROOT/.venv/bin/python"
    [ -x "$py" ] || py="$(command -v python3 2>/dev/null || true)"
    if [ -z "$py" ]; then
        warn "no python interpreter — skipping render check"
    elif "$py" "$render_bin" --check >/dev/null 2>&1; then
        ok "FORK_CHANGELOG.md matches docs/fork-changes/"
    else
        fail "FORK_CHANGELOG.md is stale — run scripts/render-docs.py to regenerate"
    fi
else
    warn "scripts/render-docs.py not present — skipping render check"
fi

# ── 4. upstream PR states ────────────────────────────────────────────────
step "4/7  upstream PR states match doc claims"
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

# ── 5. llms-full.txt regenerates clean from its sources ─────────────────
step "5/7  llms-full.txt regenerates clean"
llms_bin="$REPO_ROOT/scripts/render-llms-full.py"
if [ -x "$llms_bin" ]; then
    py="$REPO_ROOT/.venv/bin/python"
    [ -x "$py" ] || py="$(command -v python3 2>/dev/null || true)"
    if [ -z "$py" ]; then
        warn "no python interpreter — skipping llms-full check"
    elif "$py" "$llms_bin" --check >/dev/null; then
        ok "website/public/llms-full.txt matches its sources"
    else
        fail "website/public/llms-full.txt is stale — run scripts/render-llms-full.py"
    fi
else
    warn "scripts/render-llms-full.py not present — skipping llms-full check"
fi

# ── 6. Python API reference regenerates clean from source docstrings ────
step "6/7  python-api/ regenerates clean"
api_bin="$REPO_ROOT/scripts/render-api-docs.py"
if [ -x "$api_bin" ]; then
    py="$REPO_ROOT/.venv/bin/python"
    [ -x "$py" ] || py="$(command -v python3 2>/dev/null || true)"
    if [ -z "$py" ]; then
        warn "no python interpreter — skipping api-docs check"
    elif "$py" "$api_bin" --check >/dev/null 2>&1; then
        ok "website/reference/python-api/ matches mempalace/ docstrings"
    else
        fail "website/reference/python-api/ is stale — run scripts/render-api-docs.py"
    fi
else
    warn "scripts/render-api-docs.py not present — skipping api-docs check"
fi

# ── 7. MCP tool count claims match mcp_server.TOOLS ─────────────────────
step "7/7  MCP tool count in docs matches mcp_server.TOOLS"
py="$REPO_ROOT/.venv/bin/python"
[ -x "$py" ] || py="$(command -v python3 2>/dev/null || true)"
if [ -z "$py" ]; then
    warn "no python interpreter — skipping tool-count check"
else
    # Count via AST (import-free, stdlib only — mirrors render-api-docs.py,
    # avoids importing the runtime stack just to count a dict literal).
    expected=$("$py" - <<'PYEOF' 2>/dev/null || echo ""
import ast, sys
tree = ast.parse(open("mempalace/mcp_server.py", encoding="utf-8").read())
for n in ast.walk(tree):
    if (isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) == "TOOLS" for t in n.targets)
            and isinstance(n.value, ast.Dict)):
        print(len(n.value.keys)); sys.exit(0)
sys.exit(1)
PYEOF
)
    if [ -z "$expected" ]; then
        warn "could not parse TOOLS from mempalace/mcp_server.py — skipping tool-count check"
    else
        # Competitor tool counts live in the landscape comparison table
        # (README.md), the ecosystem notes (docs/ECOSYSTEM.md), and
        # website/public/llms-full.txt (which embeds the README verbatim).
        # Those are other projects' surfaces, not ours, so exclude them.
        # Every remaining "N tools" / "N MCP tools" mention in tracked docs
        # must equal len(TOOLS). The total count is always the integer
        # immediately before "tools" (even in "5 of 34 tools", the 34 is
        # the total), so a single regex captures the right number.
        tc_drift=0
        while IFS= read -r line; do
            # A single line can hold more than one "N tools" match, so check
            # every number on it, not just the first.
            for n in $(printf '%s\n' "$line" | grep -oE '[0-9]+ (MCP )?tools' | grep -oE '^[0-9]+'); do
                if [ "$n" != "$expected" ]; then
                    fail "tool-count drift: ${line%%:*} claims '$n tools' (expected $expected)"
                    tc_drift=1
                fi
            done
        done < <(git grep -nE '[0-9]+ (MCP )?tools' -- '*.md' '*.json' \
                   ':!README.md' ':!docs/ECOSYSTEM.md' ':!website/public/llms-full.txt' \
                   ':!FORK_CHANGELOG.md' ':!docs/specs/*' 2>/dev/null)
        # FORK_CHANGELOG.md is a historical record rendered from
        # docs/fork-changes/ — entries legitimately state the tool count
        # AS OF their date (e.g. "stays at 39 tools" from the v3.5 sync), so
        # it is excluded rather than rewritten every time the surface grows.
        # docs/specs/ are dated design documents with the same property:
        # they describe the surface as measured when the spec was written.
        (( tc_drift == 0 )) && ok "all doc tool-count claims == $expected"
    fi
fi

# Check 6 (fork-only YAML commits → CLAUDE.md row inventory) retired
# 2026-05-11: the CLAUDE.md row inventory it validated was removed in
# favor of a pointer block to FORK_CHANGELOG.md + techempower-org/mempalace
# issues. Check 3 (FORK_CHANGELOG.md ↔ YAML) already guarantees the
# meaningful sync property — every fork-only YAML commit appears in the
# rendered FORK_CHANGELOG.md by construction, so a separate CLAUDE.md
# check would be redundant.

# ── summary ──────────────────────────────────────────────────────────────
if (( failures == 0 )); then
    (( quiet )) || printf '\n\033[1;32m✦ docs clean\033[0m\n'
    exit 0
else
    printf '\n\033[1;31m✗ %d issue(s) found\033[0m\n' "$failures" >&2
    exit 1
fi
