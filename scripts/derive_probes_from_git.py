#!/usr/bin/env python3
"""derive_probes_from_git.py — derive 100+ probes from this repo's git log.

Replaces the hand-curated 20-probe set in ``chunk_strategy_ablation.py``
with a much larger probe set generated from real commit subjects + the
file each commit primarily touched.

Discussed in MemPalace/mempalace#1384 — nakata-app's bootstrap CIs at
n=20 are noise-bound (every strategy's Δ overlaps zero). A 100+ probe
set gives the paired-bootstrap real statistical power.

Output: a JSON file with shape::

    {
      "probes": [
        {"query": "...", "expected": "<basename>.py", "why": "<hash> <subj>"},
        ...
      ]
    }

JSON (not YAML) so the harness can load it with stdlib only.

Usage::

    python scripts/derive_probes_from_git.py
    python scripts/derive_probes_from_git.py --out scripts/probes_v2.json
    python scripts/derive_probes_from_git.py --since "12 months ago"

The script is deterministic given the same git tree. Probes are not
edited by hand — every entry's provenance traces back to one commit.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Conventional commit prefix: type(scope): body  or  type: body.
_CONV_RE = re.compile(r"^([a-z]+)(?:\([^)]+\))?!?:\s*(.+)$")

# Subjects we never want as probes — pure noise.
_SKIP_PATTERNS = [
    re.compile(r"^merge\b", re.I),
    re.compile(r"^revert\b", re.I),
    re.compile(r"^bump\s|^v\d+\.\d+\.\d+\b", re.I),
    re.compile(r"^release\b", re.I),
    re.compile(r"^wip\b", re.I),
]

# Conventional types we drop entirely (no useful probe signal).
_SKIP_TYPES = {"chore", "style", "ci", "build", "release", "revert", "merge"}

# File patterns we treat as "primary candidates". Order = preference.
_PRIMARY_GLOBS = [
    re.compile(r"^mempalace/.*\.py$"),
    re.compile(r"^mempalace/backends/.*\.py$"),
    re.compile(r"^docs/.*\.md$"),
    re.compile(r"^scripts/.*\.py$"),
]

# Files we never treat as primary (boilerplate / generated).
_SKIP_FILES = [
    re.compile(r"^tests?/"),
    re.compile(r"__init__\.py$"),
    re.compile(r"CHANGELOG\.md$"),
    re.compile(r"\.lock$"),
    re.compile(r"pyproject\.toml$"),
    re.compile(r"requirements.*\.txt$"),
]


def _run_git(args: list[str]) -> str:
    out = subprocess.run(
        ["git"] + args,
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    return out.stdout


def _parse_log(since: str) -> list[tuple[str, str, list[str]]]:
    """Return [(short_hash, subject, [files]), ...] from git log."""
    raw = _run_git(
        [
            "log",
            f"--since={since}",
            "--pretty=format:COMMIT|%h|%s",
            "--name-only",
        ]
    )
    commits: list[tuple[str, str, list[str]]] = []
    cur: tuple[str, str, list[str]] | None = None
    for line in raw.splitlines():
        if line.startswith("COMMIT|"):
            if cur is not None:
                commits.append(cur)
            _, h, subj = line.split("|", 2)
            cur = (h, subj, [])
        elif line.strip() and cur is not None:
            cur[2].append(line.strip())
    if cur is not None:
        commits.append(cur)
    return commits


def _should_skip_subject(subj: str) -> bool:
    if any(p.search(subj) for p in _SKIP_PATTERNS):
        return True
    m = _CONV_RE.match(subj)
    if m and m.group(1).lower() in _SKIP_TYPES:
        return True
    if len(subj) < 25:
        return True
    return False


def _pick_primary_file(subj: str, files: list[str]) -> str | None:
    # Drop boilerplate.
    candidates = [f for f in files if not any(s.search(f) for s in _SKIP_FILES)]
    if not candidates:
        return None

    # Prefer file whose basename (without ext) appears in subject.
    subj_lower = subj.lower()
    for f in candidates:
        base = Path(f).stem.lower()
        if len(base) >= 4 and base in subj_lower:
            return f

    # Otherwise: first file matching the priority globs.
    for pat in _PRIMARY_GLOBS:
        for f in candidates:
            if pat.match(f):
                return f

    return candidates[0]


def _subject_to_query(subj: str) -> str:
    """Strip the conventional prefix; lightly rephrase to a question/topic.

    Keep deterministic — don't paraphrase, just clean.
    """
    m = _CONV_RE.match(subj)
    body = m.group(2) if m else subj
    body = body.strip().rstrip(".")
    # Capitalize first letter for prose-y feel.
    if body and body[0].islower():
        body = body[0].upper() + body[1:]
    return body


def _dedupe(probes: list[dict]) -> list[dict]:
    """Drop probes that share the same (expected file, lowercase query prefix)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for p in probes:
        key = (p["expected"], p["query"][:40].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="14 months ago")
    parser.add_argument(
        "--out", default=str(Path(__file__).resolve().parent / "probes_v2_git_derived.json")
    )
    parser.add_argument("--max", type=int, default=200, help="Max probes to emit.")
    args = parser.parse_args(argv)

    commits = _parse_log(args.since)
    print(f"Read {len(commits)} commits from git log since {args.since!r}", file=sys.stderr)

    probes: list[dict] = []
    for h, subj, files in commits:
        if _should_skip_subject(subj):
            continue
        primary = _pick_primary_file(subj, files)
        if not primary:
            continue
        # Probe runner matches by basename, so emit the basename.
        expected = Path(primary).name
        probes.append(
            {
                "query": _subject_to_query(subj),
                "expected": expected,
                "why": f"{h} {subj}",
            }
        )

    probes = _dedupe(probes)
    probes = probes[: args.max]
    print(f"Emitting {len(probes)} probes to {args.out}", file=sys.stderr)

    out_path = Path(args.out)
    payload = {
        "_meta": {
            "generator": "scripts/derive_probes_from_git.py",
            "since": args.since,
            "n_probes": len(probes),
        },
        "probes": probes,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
