#!/usr/bin/env python3
"""failure_shape_recall.py — measure whether a failure-shape record is retrievable
from the phrasing an agent ARRIVES with.

Read-only. Issues `mempalace search --json` per trigger and reports recall@3 and the
first-hit rank, **scored separately per partition**. Partitions are not cosmetic: a
trigger written by the author of the record it targets measures whether the author can
find their own words, which is the regime the index exists to escape. See
`docs/specs/2026-09-17-failure-shape-index.md` §5.

Trigger table (CSV, header required):

    trigger,expected_slug,partition

`partition` is free text; rows whose trigger is `TBD(<who>)` are counted as pending and
never scored. A partial table is expected — the independent partitions are written by
lanes that have not read the records.

Usage:
    scripts/failure_shape_recall.py --triggers <csv> [--wing memorypalace] [--limit 30]
    scripts/failure_shape_recall.py --check-set-only
"""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
RECORDS = REPO / "docs" / "failure-shapes"
SPEC = REPO / "docs" / "specs" / "2026-09-17-failure-shape-index.md"
PENDING = re.compile(r"^TBD\(.+\)$")


def _front_matter_slug(path: pathlib.Path) -> str | None:
    """Read `slug:` from the file's own front matter.

    Deliberately not derived from the filename, so a renamed file can be detected;
    `record_slugs` then compares the two, which is what actually detects it.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    block = text.split("---", 2)[1]
    for line in block.splitlines():
        if line.startswith("slug:"):
            return line.split(":", 1)[1].strip()
    return None


def record_slugs() -> tuple:
    """Slugs on disk, plus any file whose name and front matter disagree.

    Both halves are needed. Reading the slug from front matter is what lets a RENAMED
    file be detected at all; comparing it to the stem is what actually detects it. The
    first without the second passes a rename silently — measured, 2026-09-18.
    """
    slugs, mismatched = set(), []
    for path in RECORDS.glob("*.md"):
        slug = _front_matter_slug(path)
        if not slug:
            mismatched.append((path.name, "<no slug in front matter>"))
            continue
        slugs.add(slug)
        if slug != path.stem:
            mismatched.append((path.name, slug))
    return slugs, mismatched


def spec_slugs() -> set:
    """Slugs the spec enumerates, from its record tables."""
    text = SPEC.read_text(encoding="utf-8")
    block = text.split("### The runnable table")[1]
    return set(re.findall(r"^\| `([a-z][a-z0-9-]+)` \|", block, flags=re.M))


def check_set(verbose: bool = True) -> int:
    """Two-way set diff between the record files and the spec's enumeration."""
    (files, mismatched), spec = record_slugs(), spec_slugs()
    missing_files = sorted(spec - files)
    missing_spec = sorted(files - spec)
    if verbose:
        print(f"records on disk: {len(files)}   enumerated in spec: {len(spec)}")
        if missing_files:
            print(f"  in spec, no file:  {', '.join(missing_files)}")
        if missing_spec:
            print(f"  file, not in spec: {', '.join(missing_spec)}")
        for name, slug in mismatched:
            print(f"  filename/slug disagree: {name} declares slug {slug!r}")
        if not missing_files and not missing_spec and not mismatched:
            print("  ✓ slug sets are equal, and every filename matches its own slug")
    return 1 if (missing_files or missing_spec or mismatched) else 0


def search(trigger: str, wing: str, limit: int) -> list:
    cmd = [
        "mempalace",
        "search",
        trigger,
        "--wing",
        wing,
        "--limit",
        str(limit),
        "--format",
        "json",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  ! search failed ({exc}); recorded as no-answer", file=sys.stderr)
        return []
    if out.returncode != 0:
        print(f"  ! exit {out.returncode}: {out.stderr.strip()[:120]}", file=sys.stderr)
        return []
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    return data.get("results", data if isinstance(data, list) else [])


def control_record() -> tuple:
    """Pick one record deterministically and return (slug, a verbatim phrase from it).

    The phrase is the record's own `answered` text, which appears in the body verbatim.
    Querying it is a positive control on the CORPUS: if a record's own words cannot be
    retrieved, no arrival-phrasing score below is interpretable.
    """
    for path in sorted(RECORDS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        slug = _front_matter_slug(path)
        marker = "**Actually answered:** "
        for line in text.splitlines():
            if line.startswith(marker):
                phrase = line[len(marker) :].strip()
                if slug and len(phrase) >= 20:
                    return slug, phrase
    return None, None


def corpus_is_indexed(wing: str, limit: int, search_fn=None) -> tuple:
    """Refuse to score a corpus that is not in the index yet.

    An unindexed corpus makes every partition read recall@3 = 0, which lands exactly on
    the falsifier row "baseline low and unchanged" and reads as a clean refutation of the
    idea. The honest answer is "the drawers are not there". Asked: is this retrievable by
    arrival phrasing? Answered: is it in the index at all?
    """
    search_fn = search_fn or search
    slug, phrase = control_record()
    if not slug:
        return False, "corpus not indexed: no record with a usable verbatim phrase"
    hits = search_fn(phrase, wing, limit)
    if rank_of(hits, slug) is None:
        return False, f"corpus not indexed: verbatim text of {slug} not retrievable"
    return True, f"corpus control ok: verbatim text of {slug} retrievable"


def rank_of(hits: list, slug: str):
    """1-based rank of the first hit naming this slug, else None."""
    for i, h in enumerate(hits, 1):
        blob = json.dumps(h)
        if slug in blob:
            return i
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--triggers", type=pathlib.Path, help="CSV: trigger,expected_slug,partition")
    ap.add_argument("--wing", default="memorypalace")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--check-set-only", action="store_true", help="run the slug set diff and exit")
    ap.add_argument(
        "--force-score",
        action="store_true",
        help="score even if the corpus control fails; the refusal reason is printed above the table",
    )
    args = ap.parse_args(argv)

    rc = check_set()
    if args.check_set_only:
        return rc
    if not args.triggers:
        ap.error("--triggers is required unless --check-set-only")

    indexed, reason = corpus_is_indexed(args.wing, args.limit)
    if not indexed and not args.force_score:
        print(f"\nREFUSING TO SCORE — {reason}", file=sys.stderr)
        print(
            "Every partition would read recall@3 = 0 and look like a refutation. "
            "Re-run once the drawers are indexed, or pass --force-score.",
            file=sys.stderr,
        )
        return 2
    if not indexed:
        print(f"\n!! SCORING ANYWAY (--force-score) — {reason}")
        print("!! The numbers below measure indexing, not retrievability. Do not cite them.")
    else:
        print(f"\n{reason}")

    rows = list(csv.DictReader(args.triggers.open(encoding="utf-8")))
    parts: dict = {}
    for row in rows:
        trig = (row.get("trigger") or "").strip()
        slug = (row.get("expected_slug") or "").strip()
        part = (row.get("partition") or "unlabelled").strip()
        b = parts.setdefault(part, {"scored": [], "pending": 0})
        if not trig or PENDING.match(trig):
            b["pending"] += 1
            continue
        hits = search(trig, args.wing, args.limit)
        b["scored"].append((trig, slug, rank_of(hits, slug)))

    print("\npartition scores — reported separately by design (see spec §5)")
    for part in sorted(parts):
        b = parts[part]
        n = len(b["scored"])
        if not n:
            print(f"\n  {part}: 0 scored, {b['pending']} pending")
            continue
        at3 = sum(1 for _, _, r in b["scored"] if r and r <= 3)
        at10 = sum(1 for _, _, r in b["scored"] if r and r <= 10)
        found = [r for _, _, r in b["scored"] if r]
        print(f"\n  {part}: n={n} scored, {b['pending']} pending")
        print(f"    recall@3  {at3}/{n}")
        print(f"    recall@10 {at10}/{n}")
        print(f"    not found in top {args.limit}: {n - len(found)}/{n}")
        for trig, slug, r in b["scored"]:
            print(f"      {'rank %2d' % r if r else '  miss '}  {slug:<34} {trig[:52]}")
    print("\nNo aggregate across partitions is printed: pooling an authored partition with an")
    print("independent one reports the author's own recall as if it were a reader's.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
