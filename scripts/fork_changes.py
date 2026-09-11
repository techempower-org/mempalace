#!/usr/bin/env python3
"""fork_changes.py — the single loader for the fork-change manifest.

Entries live one per file in ``docs/fork-changes/<date>-<id>.yaml``. They
used to be a list under ``entries:`` in one ``docs/fork-changes.yaml``,
which gave every pull request the same insertion point: measured across a
10-PR wave on 2026-09-10/11, every PR conflicted with every other one on
that file (plus the four artefacts rendered from it) with **zero** source
conflicts. One file per entry makes concurrent PRs touch disjoint paths.

Presentation order therefore has to come from the data rather than from
position in a shared file. It is ``seq`` descending, ties broken on
``(date desc, id asc)``.

The property that matters is that a ``seq`` *collision* is harmless. Two
lanes both choosing ``max + 1`` write separate files and order
deterministically, so a collision costs nothing, while a textual conflict
blocks a merge. That is what lets ordering stay explicit — and explicit
is what keeps ``FORK_CHANGELOG.md`` in the hand-curated narrative order
it has always been in, instead of re-sorting 112 of 137 entries the first
time the layout changed.

Adding an entry:

    docs/fork-changes/2026-09-11-my-change.yaml

      seq: <one above the current maximum -- `fork_changes.py --next-seq`>
      id: my-change
      date: 2026-09-11
      bucket: Fixed            # Added | Changed | Fixed | Performance
      commit: HEAD             # resolved to the squash sha at merge time
      area: CLI
      summary: "one line, <100 chars"
      body: |
        Markdown paragraph(s).
      fork_pr: 999             # optional; lets the merge step resolve `commit`
      pr: 1234                 # optional; UPSTREAM PR number (MemPalace/mempalace)

``commit: HEAD`` is the correct value to write on a branch: the
squash-merge sha does not exist until the merge happens, and
``scripts/maintain-fork-changes.py`` resolves it afterwards. Writing a
branch sha instead is what left 13 entries pointing at commits
unreachable from main (#472/#473).
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency guard
    print(f"PyYAML required (`pip install pyyaml`): {exc}", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRIES_DIR = REPO_ROOT / "docs" / "fork-changes"

#: Field order used when writing an entry, so hand-edits and generated
#: files look the same in review.
FIELD_ORDER = (
    "seq",
    "id",
    "date",
    "bucket",
    "commit",
    "area",
    "summary",
    "body",
    "tests",
    "fork_pr",
    "pr",
    "pr_state",
    "files",
    "supersedes",
)


class ManifestError(RuntimeError):
    """A fork-change entry file is missing, malformed, or duplicated."""


def _date_str(value: Any) -> str:
    """Normalise a YAML date (which may parse to ``datetime.date``)."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value or "")


def entry_filename(entry: dict) -> str:
    """``<date>-<id>.yaml`` — date first so a directory listing reads
    chronologically, which is the one ordering a human scanning the
    folder wants."""
    return f"{_date_str(entry.get('date'))}-{entry['id']}.yaml"


def entry_sort_key(entry: dict) -> tuple:
    """Newest first: ``seq`` desc, then date desc, then id ascending.

    ``id`` ascending (rather than descending) is deliberate: it is the
    only component that is not a recency proxy, so making it the one
    ascending term keeps the tie order stable and readable rather than
    arbitrary. A missing ``seq`` sorts last instead of raising, because a
    hand-written entry that forgets it should still render.
    """
    try:
        seq = int(entry.get("seq", 0) or 0)
    except (TypeError, ValueError):
        seq = 0
    return (-seq, _invert(_date_str(entry.get("date"))), str(entry.get("id", "")))


def _invert(text: str) -> tuple:
    """Sort key that reverses a string's order within an otherwise
    ascending tuple (so dates descend while ids ascend)."""
    return tuple(-ord(c) for c in text)


def load_entries(dirpath: Path | str = ENTRIES_DIR) -> list[dict]:
    """Every entry in ``dirpath``, in presentation order."""
    d = Path(dirpath)
    if not d.is_dir():
        raise ManifestError(f"fork-change entry directory not found: {d}")

    entries: list[dict] = []
    seen: dict[str, Path] = {}
    for path in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ManifestError(f"{path.name}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ManifestError(f"{path.name}: expected a mapping, got {type(data).__name__}")
        entry_id = data.get("id")
        if not entry_id:
            raise ManifestError(f"{path.name}: missing 'id'")
        if entry_id in seen:
            raise ManifestError(
                f"duplicate entry id {entry_id!r} in {path.name} and {seen[entry_id].name}"
            )
        seen[entry_id] = path
        data["date"] = _date_str(data.get("date"))
        data.setdefault("_path", str(path))
        entries.append(data)

    entries.sort(key=entry_sort_key)
    return entries


def meta_path(dirpath: Path | str = ENTRIES_DIR) -> Path:
    """``docs/fork-changes-meta.yaml`` — a sibling of the entries dir."""
    d = Path(dirpath)
    return d.parent / f"{d.name}-meta.yaml"


def load_meta(dirpath: Path | str = ENTRIES_DIR) -> dict:
    """Non-entry manifest keys (currently ``merged_upstream``).

    Kept OUTSIDE the entries directory, and out of any per-entry file,
    because it is edited when an *upstream* PR merges rather than once
    per fork PR. A shared file on that cadence does not reintroduce the
    every-PR-conflicts problem (#473); folding it into the entries dir
    would instead make every entry file a candidate mapping for it.
    """
    p = meta_path(dirpath)
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ManifestError(f"{p.name}: expected a mapping, got {type(data).__name__}")
    return data


def load_manifest(dirpath: Path | str = ENTRIES_DIR) -> dict:
    """The whole manifest: entries in presentation order, plus meta keys.

    Shaped like the old single-file manifest so every existing consumer
    (``render-docs.py`` especially) keeps working unchanged.
    """
    manifest: dict = {"entries": load_entries(dirpath)}
    manifest.update(load_meta(dirpath))
    manifest.setdefault("merged_upstream", {})
    return manifest


def next_seq(dirpath: Path | str = ENTRIES_DIR) -> int:
    """One above the highest ``seq`` present.

    Two lanes calling this concurrently get the same number, which is
    fine — see the module docstring.
    """
    try:
        entries = load_entries(dirpath)
    except ManifestError:
        return 1
    best = 0
    for e in entries:
        try:
            best = max(best, int(e.get("seq", 0) or 0))
        except (TypeError, ValueError):
            continue
    return best + 1


class _BlockDumper(yaml.SafeDumper):
    """Emit multi-line strings as ``|`` blocks so diffs stay readable."""


def _str_representer(dumper: yaml.SafeDumper, data: str):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockDumper.add_representer(str, _str_representer)


def dump_entry(entry: dict) -> str:
    """Serialise one entry, field order normalised, body as a block scalar.

    Round-trip fidelity of the *string values* is what matters: the
    rendered markdown is built from ``entry["body"]`` etc., so a dumper
    that re-wrapped a paragraph would silently change FORK_CHANGELOG
    output. ``width`` is therefore effectively unbounded.
    """
    ordered = {k: entry[k] for k in FIELD_ORDER if k in entry and k != "_path"}
    # Anything unexpected still round-trips rather than being dropped.
    for k, v in entry.items():
        if k not in ordered and k != "_path":
            ordered[k] = v
    return yaml.dump(
        ordered,
        Dumper=_BlockDumper,
        sort_keys=False,
        allow_unicode=True,
        width=10**9,
        default_flow_style=False,
    )


def write_entry(entry: dict, dirpath: Path | str = ENTRIES_DIR) -> Path:
    """Write one entry to its canonical path."""
    d = Path(dirpath)
    d.mkdir(parents=True, exist_ok=True)
    path = d / entry_filename(entry)
    path.write_text(dump_entry(entry))
    return path


def iter_commit_refs(entries: Iterable[dict], include_head: bool = False) -> list[tuple[str, str]]:
    """``(entry_id, sha)`` for entries carrying a commit value.

    ``HEAD`` placeholders are skipped by default: on a PR branch they are
    the documented pre-merge value, not a defect.

    ``include_head`` emits them too, for the strict check that runs where
    entries MUST already be resolved (a push to main). That the exclusion
    lives *here* is the important part: a "missing sha" cannot be caught
    by hardening the ancestry predicate alone, because an excluded entry
    never reaches it. ``git merge-base --is-ancestor HEAD HEAD`` also
    exits 0, so both the enumeration and the predicate have to know
    about the literal.
    """
    out = []
    for e in entries:
        sha = str(e.get("commit", "") or "").strip()
        if not sha:
            continue
        if sha == "HEAD" and not include_head:
            continue
        out.append((str(e.get("id")), sha))
    return out


if __name__ == "__main__":  # small CLI so shell callers need no inline python
    import argparse
    import json

    ap = argparse.ArgumentParser(description="fork-change manifest helper")
    ap.add_argument("--next-seq", action="store_true", help="print one above the max seq")
    ap.add_argument("--count", action="store_true", help="print the number of entries")
    ap.add_argument("--commit-refs", action="store_true", help="print 'id<TAB>sha' per line")
    ap.add_argument(
        "--include-head",
        action="store_true",
        help="with --commit-refs, also emit entries whose commit is the literal HEAD",
    )
    ap.add_argument("--json", action="store_true", help="dump all entries as JSON")
    ap.add_argument("--dir", default=str(ENTRIES_DIR))
    args = ap.parse_args()

    if args.next_seq:
        print(next_seq(args.dir))
    elif args.count:
        print(len(load_entries(args.dir)))
    elif args.commit_refs:
        for entry_id, sha in iter_commit_refs(load_entries(args.dir), args.include_head):
            print(f"{entry_id}\t{sha}")
    elif args.json:
        print(json.dumps(load_entries(args.dir), indent=2, default=str))
    else:
        ap.print_help()
