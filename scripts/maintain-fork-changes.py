#!/usr/bin/env python3
"""maintain-fork-changes.py — resolve fork-change entry commit shas.

A squash merge creates a NEW commit, so the sha an entry needs does not
exist while the PR is open. The entry therefore carries ``commit: HEAD``
on the branch and this script resolves it **after** the merge: the merge
step records the final sha, not the lane. Writing a branch sha instead is
what left 13 entries pointing at commits unreachable from ``main``
(#472/#473) — they passed every check, because a dangling object still
resolves locally and in GitHub's unreachable-object store until it is
garbage-collected.

Two passes, both entry-wise over ``docs/fork-changes/``:

1. **Resolve ``commit: HEAD``** from the entry's ``fork_pr:`` via the
   GitHub REST API (``pulls/N`` → ``merge_commit_sha``). Exact, because
   the API reports the commit the merge actually created. An entry with
   no ``fork_pr`` is reported and left alone — there is nothing reliable
   to infer from.

2. **Repair a stale sha** whose commit is no longer an ancestor of the
   branch, by matching the dangling commit's *subject* to the squash
   subject (a squash subject is the branch subject plus a trailing
   ``(#PR)``). Content-based, and requires a UNIQUE match.

## Why not the previous resolver (#476)

It scanned the 12 lines following the ``commit:`` line for any ``#NN``
and mapped that number to a recent commit. Those lines are prose, and the
first issue number in an entry's prose is usually not that entry's PR.
``#NN`` was also unqualified, so an upstream number was
indistinguishable from a fork one. Measured on the 12 real entries this
repo needed fixed: **3 resolved, all three wrong**, one of them matched
against **upstream** ``#1829``, and the other 9 were silently left on
``HEAD`` while the run reported success.

The failure direction is the point. Leaving an entry unresolved is
harmless and visible; writing a confident wrong sha is neither, and
``check-docs.sh`` cannot catch it — asserting that a sha *resolves* is
satisfied by any real commit. So both passes here refuse ambiguity
instead of picking, and unresolved entries make the exit code non-zero
under ``--check``.

De-duplication is gone: one file per entry means a duplicate id is a hard
error in ``scripts/fork_changes.py`` at load time, not something to clean
up afterwards.

Usage::

    scripts/maintain-fork-changes.py                 # apply both passes
    scripts/maintain-fork-changes.py --check         # exit 1 if anything
                                                     # would change or is
                                                     # unresolved
    scripts/maintain-fork-changes.py --no-resolve-head
    scripts/maintain-fork-changes.py --no-repair
    scripts/maintain-fork-changes.py --branch=origin/main
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from typing import Callable, Iterable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fork_changes  # noqa: E402

REPO = "techempower-org/mempalace"

#: A squash-merge subject ends with one or more ``(#N)`` markers.
PR_TAIL_RE = re.compile(r"(?:\s*\(#\d+\))+$")


def _run(args: list[str]) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)


def git_subject(sha: str) -> str | None:
    """Subject line of ``sha``, or None when the object is gone."""
    try:
        return _run(["git", "log", "-1", "--format=%s", sha]).strip()
    except subprocess.CalledProcessError:
        return None


def git_is_ancestor(sha: str, branch: str) -> bool:
    """True when ``sha`` is reachable from ``branch``.

    This — not ``git cat-file -e`` — is the honest test for "the commit
    this entry names is on the branch we are documenting".
    """
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, branch],
            capture_output=True,
        ).returncode
        == 0
    )


def git_subjects(branch: str) -> list[tuple[str, str]]:
    """``(sha, subject)`` for every commit on ``branch``, newest first."""
    out = _run(["git", "log", "--format=%H%x00%s", branch]).strip()
    rows = []
    for line in out.split("\n"):
        if "\x00" in line:
            sha, subject = line.split("\x00", 1)
            rows.append((sha, subject))
    return rows


def gh_merge_commit_sha(pr: int, repo: str = REPO) -> str | None:
    """``merge_commit_sha`` for a MERGED pull request, else None.

    REST rather than GraphQL on purpose: GraphQL has been rate-limited
    for this account during waves, and this is the one lookup that must
    work at merge time.
    """
    try:
        raw = _run(["gh", "api", f"repos/{repo}/pulls/{pr}"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not data.get("merged_at"):
        return None
    sha = data.get("merge_commit_sha")
    return sha[:7] if isinstance(sha, str) and sha else None


def resolve_head_entries(
    entries: Iterable[dict],
    fetch: Callable[[int], str | None] = gh_merge_commit_sha,
) -> tuple[list[tuple[dict, str]], list[tuple[dict, str]]]:
    """``commit: HEAD`` → the squash sha, via ``fork_pr``.

    Returns ``(changes, unresolved)`` where a change is
    ``(entry, new_sha)`` and an unresolved item is ``(entry, reason)``.
    """
    changes: list[tuple[dict, str]] = []
    unresolved: list[tuple[dict, str]] = []
    for entry in entries:
        if str(entry.get("commit", "")).strip() != "HEAD":
            continue
        pr = entry.get("fork_pr")
        if not pr:
            unresolved.append((entry, "no fork_pr: — cannot resolve HEAD"))
            continue
        try:
            sha = fetch(int(pr))
        except (TypeError, ValueError):
            unresolved.append((entry, f"fork_pr: {pr!r} is not a number"))
            continue
        if not sha:
            unresolved.append((entry, f"PR #{pr} is not merged (or unreachable)"))
            continue
        changes.append((entry, sha))
    return changes, unresolved


def squash_subject_match(subject: str, branch_subjects: Iterable[tuple[str, str]]) -> list[str]:
    """Shas whose subject is ``subject`` plus a trailing ``(#PR)``.

    Exact prefix, not fuzzy: a squash subject that merely *resembles* the
    branch subject is not evidence, and a wrong sha is worse than none.
    """
    return [
        sha
        for sha, candidate in branch_subjects
        if candidate != subject and PR_TAIL_RE.sub("", candidate).strip() == subject.strip()
    ]


def repair_dangling(
    entries: Iterable[dict],
    branch: str = "origin/main",
    is_ancestor: Callable[[str, str], bool] = git_is_ancestor,
    subject_of: Callable[[str], str | None] = git_subject,
    branch_subjects: list[tuple[str, str]] | None = None,
    legacy_ids: dict[str, str] | None = None,
) -> tuple[list[tuple[dict, str]], list[tuple[dict, str]]]:
    """Re-point entries whose commit is not an ancestor of ``branch``.

    ``legacy_ids`` maps an entry id to the ONE sha it is exempt for; any
    other value reports normally.
    """
    legacy_ids = legacy_ids or {}
    subjects = branch_subjects if branch_subjects is not None else git_subjects(branch)
    changes: list[tuple[dict, str]] = []
    unresolved: list[tuple[dict, str]] = []
    for entry in entries:
        sha = str(entry.get("commit", "")).strip()
        if not sha or sha == "HEAD":
            continue
        # Exempt only for the exact sha recorded beside the id.
        if legacy_ids.get(str(entry.get("id"))) == sha:
            continue
        if is_ancestor(sha, branch):
            continue
        subject = subject_of(sha)
        if subject is None:
            unresolved.append((entry, f"{sha} is not an ancestor and the object is gone"))
            continue
        hits = squash_subject_match(subject, subjects)
        if len(hits) != 1:
            unresolved.append(
                (entry, f"{sha} is not an ancestor; {len(hits)} subject matches — not guessing")
            )
            continue
        changes.append((entry, hits[0][:7]))
    return changes, unresolved


def load_legacy_ids(path: pathlib.Path) -> dict[str, str]:
    """``{entry_id: known_bad_sha}`` for documented-unrecoverable entries.

    Keyed on the PAIR on purpose. Keyed on the id alone, the file would
    grant an entry a permanent exemption rather than record one
    known-bad sha: its ``commit:`` could later be changed to anything —
    including a plausible wrong sha, which *is* an ancestor and so passes
    every check — and never be looked at again. Pinning the sha makes a
    resolved or altered entry start failing until its line is removed,
    which is the prompt we actually want.
    """
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(
                f"{path.name}:{lineno}: needs '<id> <sha>', got {line!r} — "
                "an id alone would exempt the entry forever"
            )
        out[parts[0]] = parts[1]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="report only; exit 1 on work to do")
    ap.add_argument("--no-resolve-head", action="store_true")
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--branch", default="origin/main")
    ap.add_argument("--dir", default=str(fork_changes.ENTRIES_DIR))
    ap.add_argument("--legacy-file", default="docs/fork-changes-legacy-shas.txt")
    args = ap.parse_args(argv)

    try:
        entries = fork_changes.load_entries(args.dir)
    except fork_changes.ManifestError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    try:
        legacy = load_legacy_ids(pathlib.Path(args.legacy_file))
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    changes: list[tuple[dict, str]] = []
    unresolved: list[tuple[dict, str]] = []

    if not args.no_resolve_head:
        # Looked up on the module at call time (not bound as a default)
        # so a test can substitute it and never touch the network.
        c, u = resolve_head_entries(entries, fetch=gh_merge_commit_sha)
        changes += c
        unresolved += u

    if not args.no_repair:
        c, u = repair_dangling(entries, args.branch, legacy_ids=legacy)
        changes += c
        unresolved += u

    for entry, sha in changes:
        old = entry.get("commit")
        print(f"  {entry['id']}: commit {old} → {sha}")
    for entry, why in unresolved:
        print(f"  ! {entry['id']}: {why}", file=sys.stderr)

    if args.check:
        if changes or unresolved:
            print(
                f"✗ {len(changes)} entry sha(s) to resolve, {len(unresolved)} unresolved",
                file=sys.stderr,
            )
            return 1
        print("✦ fork-change entry shas are clean")
        return 0

    for entry, sha in changes:
        path = pathlib.Path(entry["_path"])
        payload = {k: v for k, v in entry.items() if k != "_path"}
        payload["commit"] = sha
        path.write_text(fork_changes.dump_entry(payload))

    if changes:
        print(f"✦ resolved {len(changes)} entry sha(s) — re-run the renderers")
    else:
        print("✦ fork-change entry shas are clean")
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
