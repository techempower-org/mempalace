#!/usr/bin/env bash
# ship-prep.sh — one command that regenerates every derived doc so a
# fork-ahead PR passes check-docs without manual rebase-side work.
#
# What it does:
#   1. Run scripts/maintain-fork-changes.py to resolve `commit: HEAD`
#      placeholders from each entry's `fork_pr` after merge (#476).
#   2. Find pytest (same fallback chain as scripts/check-docs.sh — main
#      checkout's venv when the worktree has none).
#   3. Count tests via `pytest --collect-only -q` and REPORT it. The
#      committed "<N> tests pass on `main`" literal was removed in #473
#      (every test-adding PR edited the same two lines), so there is
#      nothing to rewrite — a literal that creeps back is flagged.
#   5. Run scripts/render-docs.py --target all  (FORK_CHANGELOG + README table)
#   6. Run scripts/render-llms-full.py          (llms-full.txt)
#   7. Run scripts/render-api-docs.py           (website/reference/python-api/)
#   8. Run scripts/check-docs.sh to verify the result is clean.
#
# Exit codes:
#   0 — everything clean and ready to commit
#   1 — one of the steps failed (likely a stale derived doc or a real
#       drift requiring a docs/fork-changes/ entry)
#   2 — internal error (not in a git repo, no pytest at all)
#
# Usage:
#   scripts/ship-prep.sh              # full chain
#   scripts/ship-prep.sh --no-check   # skip the final check-docs.sh pass
#
# Filed as #312.

set -uo pipefail
shopt -s nullglob

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "✗ not a git repo" >&2
    exit 2
}
cd "$REPO_ROOT"

run_check=1
[ "${1:-}" = "--no-check" ] && run_check=0

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ── locate pytest (matches scripts/check-docs.sh logic) ─────────────────
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
    echo "✗ no pytest available — install with: uv venv && uv pip install -e '.[dev]'" >&2
    exit 2
fi

# ── 1. fork-changes entry maintenance (resolve commit: HEAD, #476) ─────
step "1/6  docs/fork-changes/ maintenance"
python scripts/maintain-fork-changes.py || fail "maintain-fork-changes.py failed"
ok "fork-changes entries clean"

# ── 2. test count (derived; nothing to bump since #473) ─────────────────
step "2/6  test count (derived, not committed)"
actual_count=$("$pytest_bin" --collect-only -q 2>/dev/null \
    | grep -E "[0-9]+/[0-9]+ tests collected" \
    | head -1 | awk -F'/' '{print $1}' || echo "")
if [ -z "$actual_count" ]; then
    actual_count=$("$pytest_bin" --collect-only -q 2>/dev/null \
        | grep -E "^[0-9]+ tests collected" \
        | head -1 | awk '{print $1}' || echo "")
fi
[ -n "$actual_count" ] || fail "could not parse pytest --collect-only output"

# The committed literal is gone (#473): a number in README/CLAUDE.md meant every
# test-adding PR edited the same two lines. This step used to fail when the
# phrase was missing and `sed` the number in place — both jobs are obsolete, and
# the hard failure would have fired on the very commit that removed the phrase.
# The count is reported here because it is useful during release prep, and a
# literal that creeps back is flagged, but nothing depends on one existing.
readme_count=$(grep -oE '[0-9]+ tests pass on `main`' README.md \
    | grep -oE '^[0-9]+' || echo "")
if [ -z "$readme_count" ]; then
    ok "suite collects $actual_count tests (no committed literal — derived, per #473)"
elif [ "$readme_count" = "$actual_count" ]; then
    warn "README carries a hard-coded count ($readme_count) that happens to match; prefer removing it (#473)"
else
    warn "README carries a STALE hard-coded count ($readme_count, suite collects $actual_count) — remove it rather than bumping it (#473)"
fi

# ── 3. render-docs.py (FORK_CHANGELOG + README table) ───────────────────
step "3/6  scripts/render-docs.py --target all"
python scripts/render-docs.py --target all || fail "render-docs.py failed"
ok "rendered FORK_CHANGELOG.md + README table"

# ── 4. render-llms-full.py ──────────────────────────────────────────────
step "4/6  scripts/render-llms-full.py"
python scripts/render-llms-full.py || fail "render-llms-full.py failed"
ok "rendered website/public/llms-full.txt"

# ── 5. render-api-docs.py ───────────────────────────────────────────────
step "5/6  scripts/render-api-docs.py"
python scripts/render-api-docs.py || fail "render-api-docs.py failed"
ok "rendered website/reference/python-api/"

# ── 6. final verification ───────────────────────────────────────────────
if [ "$run_check" -eq 0 ]; then
    step "6/6  scripts/check-docs.sh (SKIPPED via --no-check)"
    ok "skipped"
else
    step "6/6  scripts/check-docs.sh"
    if bash scripts/check-docs.sh --quiet; then
        ok "docs clean — ready to commit"
    else
        fail "check-docs reports drift; see output above"
    fi
fi

printf '\n\033[1;32m✦ ship-prep complete\033[0m\n'
