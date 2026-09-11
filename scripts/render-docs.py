#!/usr/bin/env python3
"""Render fork docs from the canonical YAML manifest.

Reads ``docs/fork-changes/`` (one file per entry) and regenerates the fork-ahead
narrative in:

  - ``FORK_CHANGELOG.md``                                         (today)
  - ``README.md`` (fork-change-queue table — between markers)     (today)
  - ``CLAUDE.md`` (row-by-row inventory — between markers)        (planned)
  - scratch/promises.md (tracker entries — between markers)       (planned)

Usage::

    scripts/render-docs.py              # write all targets
    scripts/render-docs.py --check      # exit 1 if any target would change
    scripts/render-docs.py --target changelog   # only render the changelog
    scripts/render-docs.py --target readme      # only render the README table
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any


# Shared manifest loader. scripts/ is not a package, so its own
# directory goes on sys.path rather than relying on the caller's cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fork_changes  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRIES_DIR = REPO_ROOT / "docs" / "fork-changes"

CHANGELOG_PATH = REPO_ROOT / "FORK_CHANGELOG.md"
CHANGELOG_HEADER = """\
# Fork Changelog (techempower-org/mempalace)

Fork-ahead changes that aren't yet in upstream `MemPalace/mempalace`.
Upstream's release history lives in [`CHANGELOG.md`](CHANGELOG.md);
this file is the supplement.

> **This file is generated.** Add or edit a file in `docs/fork-changes/` and run
> `scripts/render-docs.py` to regenerate. Hand-edits will be
> overwritten on the next render.

Date-based sections, not semver — the fork tracks `upstream/develop` and
doesn't cut its own release tags. When a fork-ahead row lands upstream,
move the entry to the **Merged into upstream** section at the bottom
(kept ~30 days, then trimmed).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---
"""

BUCKET_ORDER = ("Added", "Changed", "Fixed", "Performance")

README_PATH = REPO_ROOT / "README.md"
README_BEGIN_MARKER = "<!-- BEGIN FORK-QUEUE -->"
README_END_MARKER = "<!-- END FORK-QUEUE -->"


def load_manifest(path: Path = ENTRIES_DIR) -> dict:
    """Entries come from ``docs/fork-changes/`` one file each (#473).

    Delegated to ``scripts/fork_changes.py`` so the renderer, check-docs
    and maintain-fork-changes cannot drift on ordering or validation.
    """
    try:
        return fork_changes.load_manifest(path)
    except fork_changes.ManifestError as exc:
        raise SystemExit(str(exc)) from exc


def commit_link(sha: str) -> str:
    """Render a 7-char SHA as a markdown link to the fork commit.

    An unresolved ``HEAD`` placeholder is rendered as plain text, NOT a
    link: ``.../commit/HEAD`` is a valid GitHub URL that resolves to
    whatever is at main's tip, so it silently points at an unrelated
    commit and looks authoritative while doing it. A reader cannot tell
    it apart from a real reference. Better to say "pending" than to
    publish a link that is wrong in a way nobody can see (#476).
    """
    if str(sha).strip() == "HEAD":
        return "`HEAD` — pending resolution"
    return f"[`{sha}`](https://github.com/techempower-org/mempalace/commit/{sha})"


def render_entry(entry: dict[str, Any]) -> str:
    """Emit one bullet for an entry."""
    summary = entry.get("summary", "").strip()
    body = entry.get("body", "").strip()
    bullet = f"- **{summary}** ({commit_link(entry['commit'])})\n"
    # Reflow body: prepend each line with a 2-space indent so the
    # markdown bullet wraps the paragraph cleanly.
    if body:
        for line in body.splitlines():
            bullet += f"  {line}\n" if line.strip() else "\n"
    extras = []
    if entry.get("tests"):
        extras.append(f"  *Tests:* {entry['tests']}")
    if entry.get("pr"):
        pr = entry["pr"]
        state = entry.get("pr_state", "")
        state_str = f" ({state})" if state else ""
        extras.append(
            f"  *Upstream:* [PR #{pr}](https://github.com/MemPalace/mempalace/pull/{pr}){state_str}"
        )
    if entry.get("files"):
        extras.append("  *Files:* " + ", ".join(f"`{p}`" for p in entry["files"]))
    if extras:
        bullet += "\n" + "\n".join(extras) + "\n"
    return bullet


def render_changelog(manifest: dict) -> str:
    """Render FORK_CHANGELOG.md content from the manifest."""
    out = [CHANGELOG_HEADER]

    # Group by date (newest first) then by bucket.
    by_date: dict[str, dict[str, list[dict]]] = OrderedDict()
    for entry in manifest["entries"]:
        date = str(entry["date"])
        bucket = entry.get("bucket", "Changed")
        by_date.setdefault(date, {b: [] for b in BUCKET_ORDER}).setdefault(bucket, []).append(entry)

    # Sort dates descending — manifest is presentation order but a date
    # may straddle entries; sorting keeps headings stable.
    for date in sorted(by_date.keys(), reverse=True):
        out.append(f"\n## [{date}]\n")
        buckets = by_date[date]
        for bucket in BUCKET_ORDER:
            entries_in_bucket = buckets.get(bucket, [])
            if not entries_in_bucket:
                continue
            out.append(f"\n### {bucket}\n")
            for entry in entries_in_bucket:
                out.append("\n" + render_entry(entry))

    out.append("\n---\n\n## Merged into upstream (recent)\n")
    merged = manifest.get("merged_upstream", {})
    for note in merged.get("notes", []):
        out.append(f"\n*{note}*\n")
    out.append("")
    for m in merged.get("entries", []):
        pr = m.get("pr")
        title = m.get("title", "")
        merged_at = m.get("merged") or m.get("released_in") or ""
        link = (
            f"[PR #{pr}](https://github.com/MemPalace/mempalace/pull/{pr})"
            if pr
            else "(see upstream)"
        )
        when = f" — {merged_at}" if merged_at else ""
        out.append(f"- {link} — {title}{when}")
    out.append("")  # trailing newline

    return "\n".join(out)


def _queue_entries(manifest: dict) -> list[dict[str, Any]]:
    """Entries that belong on the fork-change queue.

    The queue is the "still in-flight" view — entries that haven't yet
    merged upstream. ``pr_state: MERGED`` is the explicit exclusion; the
    "Merged into upstream" section of FORK_CHANGELOG.md is the home for
    those rows. Entries without a ``pr`` field are fork-only and stay
    on the queue (they're things we ship that may or may not ever go
    upstream).
    """
    out = []
    for entry in manifest["entries"]:
        if entry.get("pr_state") == "MERGED":
            continue
        out.append(entry)
    return out


def _escape_table_cell(text: str) -> str:
    """Escape a string for inclusion in a Markdown table cell."""
    return text.replace("\n", " ").replace("|", "\\|").strip()


def render_readme_queue(manifest: dict) -> str:
    """Render the fork-change-queue table for README.md.

    Columns: description, upstream PR (link + state), fork commit.
    """
    entries = _queue_entries(manifest)

    lines = [
        "<!-- This table is generated by scripts/render-docs.py from docs/fork-changes/. Hand-edits will be overwritten. -->",
        "",
        "| Description | Upstream PR | Fork commit |",
        "|---|---|---|",
    ]
    # Deliberately unnumbered (#473): a leading row number meant one new
    # entry renumbered every row below it, so any two concurrent PRs
    # conflicted across the whole table instead of on one line.
    for entry in entries:
        summary = _escape_table_cell(entry.get("summary", ""))
        pr = entry.get("pr")
        if pr:
            state = entry.get("pr_state", "")
            state_str = f" ({state})" if state else ""
            pr_cell = f"[#{pr}](https://github.com/MemPalace/mempalace/pull/{pr}){state_str}"
        else:
            pr_cell = "—"
        commit = entry.get("commit", "")
        commit_cell = commit_link(commit) if commit else "—"
        lines.append(f"| {summary} | {pr_cell} | {commit_cell} |")

    return "\n".join(lines)


def replace_between_markers(
    existing: str,
    begin_marker: str,
    end_marker: str,
    replacement: str,
) -> str:
    """Replace the content between two markers in ``existing``.

    The markers themselves are preserved. Raises ``ValueError`` if either
    marker is missing or if ``begin_marker`` appears after ``end_marker``.
    """
    pattern = re.compile(
        re.escape(begin_marker) + r".*?" + re.escape(end_marker),
        re.DOTALL,
    )
    if not pattern.search(existing):
        if begin_marker not in existing:
            raise ValueError(f"begin marker not found: {begin_marker!r}")
        if end_marker not in existing:
            raise ValueError(f"end marker not found: {end_marker!r}")
        raise ValueError(
            f"markers found but not in order: {begin_marker!r} must precede {end_marker!r}"
        )
    return pattern.sub(f"{begin_marker}\n{replacement}\n{end_marker}", existing, count=1)


def render_readme(manifest: dict, existing: str) -> str:
    """Render README.md by replacing the fork-queue section in ``existing``."""
    table = render_readme_queue(manifest)
    return replace_between_markers(existing, README_BEGIN_MARKER, README_END_MARKER, table)


def write_or_check(path: Path, content: str, check_only: bool) -> bool:
    """Write ``content`` to ``path`` (or compare in --check mode).

    Returns True if the file changed (or would change).
    """
    existing = path.read_text() if path.is_file() else ""
    if existing == content:
        return False
    if check_only:
        print(f"DRIFT: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return True
    path.write_text(content)
    print(f"wrote {path.relative_to(REPO_ROOT)}")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--check", action="store_true", help="exit 1 if any target is stale")
    p.add_argument(
        "--target",
        choices=["changelog", "readme", "all"],
        default="all",
        help="which destination(s) to render",
    )
    args = p.parse_args()

    manifest = load_manifest()
    drift = False

    if args.target in ("changelog", "all"):
        rendered = render_changelog(manifest)
        if write_or_check(CHANGELOG_PATH, rendered, args.check):
            drift = True

    if args.target in ("readme", "all"):
        if not README_PATH.is_file():
            if args.target == "readme":
                print(f"README not found: {README_PATH}", file=sys.stderr)
                return 2
        else:
            existing = README_PATH.read_text()
            has_markers = README_BEGIN_MARKER in existing and README_END_MARKER in existing
            if has_markers:
                try:
                    rendered_readme = render_readme(manifest, existing)
                except ValueError as exc:
                    print(f"README render failed: {exc}", file=sys.stderr)
                    return 2
                if write_or_check(README_PATH, rendered_readme, args.check):
                    drift = True
            elif args.target == "readme":
                print(
                    f"README has no fork-queue markers ({README_BEGIN_MARKER} / "
                    f"{README_END_MARKER}) — nothing to render",
                    file=sys.stderr,
                )
                return 2
            else:
                print("README: no fork-queue markers, skipping")

    # CLAUDE + promises rendering planned for follow-on commits.

    if args.check and drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
