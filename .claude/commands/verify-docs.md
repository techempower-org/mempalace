---
description: Verify fork docs are still in sync with reality — versions, URLs, test counts, commit hashes, PR states, drawer counts.
allowed-tools: Bash, Read, Grep, Glob, WebFetch
---

# /verify-docs

Sanity-check this fork's documentation against current reality. Goes broader than `scripts/check-docs.sh`: pattern-matches version strings, walks file paths, HEAD-checks URLs, and reports a summary table.

Knowledge lives across many layers in this project (global CLAUDE.md, project CLAUDE.md, README, FORK_CHANGELOG, docs/, code) and goes stale silently. This command surfaces drift before it causes wrong assumptions.

## Steps

Run all checks; do not stop on first failure. Collect results into a summary table at the end.

### 1. Run `scripts/check-docs.sh`

```bash
scripts/check-docs.sh
```

That script covers the deterministic checks: test count, fork commit hashes, FORK_CHANGELOG.md ↔ docs/fork-changes/ render parity, and upstream PR state drift. Capture its exit code and per-check output.

### 2. Version-string drift

The canonical fork version lives in `mempalace/version.py` (`__version__`). Read it, then grep README.md and CLAUDE.md for **all** `\bv?3\.\d+\.\d+\b` matches.

For each unique version string found in docs:

- If the string is described as **this fork's version** (e.g., the README's `version-shield` badge, "this fork carries…", `pyproject.toml`), it MUST match `__version__`.
- If the string is described as **upstream's version** (badges, "v3.3.5 release", "upstream/develop"), don't fail — but flag if it diverges from the most recent upstream release tag (`gh release list --repo MemPalace/mempalace --limit 3`).
- Historical references in changelogs (FORK_CHANGELOG.md, "in v3.3.0:", etc.) are fine — skip.

Also cross-check `pyproject.toml`'s `version = "..."` against `mempalace/version.py`.

### 3. File-path existence

Grep README.md, CLAUDE.md, FORK_CHANGELOG.md, and docs/**/*.md for paths that look like repo-relative files: `\b(mempalace|tests|scripts|docs|hooks|integrations|benchmarks|examples)/[\w./-]+\.\w+`.

For each unique path: check the file exists. Skip paths inside fenced code blocks that are obviously example output, and skip paths the docs themselves mark as "retired" / "removed" / "deleted".

Report any referenced-but-missing paths.

### 4. PR numbers `#NNNN` against upstream state

For every `#\d{2,5}` reference in the doc set, query `gh pr view N --repo MemPalace/mempalace --json state,title` (skip if `gh` is unauthenticated; warn instead of fail).

Compare:

- Doc says "merged" / "shipped" / "landed" / "in vX.Y.Z" → upstream state must be `MERGED`.
- Doc says "open" / "pending" / "in review" → upstream state must be `OPEN`.
- Doc says "closed" (not merged) → upstream state must be `CLOSED` (and `mergedAt` null).

`scripts/check-docs.sh` already does this with heuristics for narrative paragraphs; defer to its output and only add findings it missed (e.g., docs/ subdirectory files, which the script doesn't scan).

### 5. Upstream commit hashes

Pattern: backtick-wrapped 7-40 hex chars adjacent to a GitHub commit URL or in prose. For each one, `git cat-file -e HASH` to confirm it resolves.

The deterministic version of this check is in `scripts/check-docs.sh` step 2 — defer to it for README/CLAUDE/FORK_CHANGELOG, and only extend coverage to `docs/**/*.md`.

For cross-repo commit URLs (palace-daemon, multipass-structural-memory-eval), don't try to resolve in this repo — just note them as out-of-scope.

### 6. URLs

Grep all docs for `https?://[^\s\)>"]+`. For each unique URL:

- **Skip** known-flaky external hosts: `pypi.org`, `pypistats.org`, `img.shields.io`, anything matching `\.png|\.gif|\.jpg|\.svg` (badge/image URLs return 200 even when the underlying data is stale).
- **HEAD-check** the rest with `curl -sS -o /dev/null -w '%{http_code}' --max-time 8 --retry 1 -L URL`. Treat 200/301/302 as pass; 404 as fail; 5xx/timeout as warn (transient).
- For GitHub URLs that 404, double-check by replacing `MemPalace/` with `techempower-org/` (and vice versa) — common drift from the transfer.

Don't fire 100+ requests in parallel; batch in waves of ~5 with a small sleep to stay polite.

### 7. Test count

`scripts/check-docs.sh` covers the README. Additionally check that any `\b\d{3,4} tests?\b` mention in CLAUDE.md or docs/**/*.md is within ±5 of `pytest --collect-only -q`'s actual count (small drift is fine; >5 means stale).

### 8. Drawer count

If palace-daemon is configured (`PALACE_DAEMON_URL` or `~/.config/palace-daemon/env`):

```bash
curl -sS --max-time 5 "$PALACE_DAEMON_URL/stats" | jq -r '.drawer_count // .total_drawers // empty'
```

Compare against any `\b\d{2,3}K\+? drawers?\b` or `\b\d{5,7} drawers?\b` mentions in docs. Allow generous tolerance (the README says "300K+"; anything 250K-400K passes). Fail only if the doc number is off by >2x.

If the daemon isn't reachable, warn and skip — don't fail.

### 9. `docs/fork-changes/` PR-state cross-check

For each `pr:` field that has a `pr_state:`, query the actual upstream state and flag mismatches. `pr:` is the **UPSTREAM** PR number (`MemPalace/mempalace`); `fork_pr:` is this repo's and is not checked here. The deterministic check in step 4 covers `#NNNN` in prose; this step covers the structured entries the renderer trusts.

```bash
python3 -c "
import yaml, subprocess, sys
sys.path.insert(0, 'scripts')
import fork_changes
for e in fork_changes.load_entries():
    pr = e.get('pr')
    claimed = e.get('pr_state')
    if not pr or not claimed: continue
    r = subprocess.run(['gh','pr','view',str(pr),'--repo','MemPalace/mempalace','--json','state','--jq','.state'], capture_output=True, text=True)
    actual = r.stdout.strip() or 'UNKNOWN'
    marker = 'OK ' if actual == claimed else 'DRIFT'
    print(f'{marker}  #{pr}: yaml={claimed} actual={actual} ({e[\"id\"]})')
"
```

## Output format

After running all checks, emit a single summary table the operator can scan in 5 seconds:

```
┌─────────────────────────────────────────────────────────────────┐
│ verify-docs summary — <date>                                    │
├──────────────────────────────────────┬────────┬─────────────────┤
│ Check                                │ Status │ Notes           │
├──────────────────────────────────────┼────────┼─────────────────┤
│ 1. scripts/check-docs.sh             │ PASS   │                 │
│ 2. version strings                   │ FAIL   │ README badge…   │
│ 3. file paths exist                  │ PASS   │ 47 checked      │
│ 4. PR numbers vs upstream            │ WARN   │ #1378 closed    │
│ 5. commit hashes resolve             │ PASS   │                 │
│ 6. URLs reachable                    │ WARN   │ 2 timed out     │
│ 7. test count (docs/)                │ PASS   │                 │
│ 8. drawer count                      │ SKIP   │ daemon offline  │
│ 9. fork-changes pr_state             │ PASS   │                 │
└──────────────────────────────────────┴────────┴─────────────────┘
```

Then list each non-PASS finding with:

- The file:line where the stale claim lives
- The current reality
- A diff-style suggestion (`v3.3.4 → v3.3.5`)

Do **not** auto-fix. Print the suggestions; the operator decides whether to land them as a docs PR.

## Notes

- This command is a checker, not a fixer. Iron Law: no edits to docs from this command — only reporting.
- `scripts/check-docs.sh` is the source of truth for the deterministic 4 checks. Don't reimplement them here; defer and extend.
- Run from the repo root. Worktrees are fine (`git rev-parse --show-toplevel` is what scripts/check-docs.sh keys off).
- Performance budget: full run under 60s on a warm cache. URL checks dominate; cap concurrency and timeouts to keep it tight.
