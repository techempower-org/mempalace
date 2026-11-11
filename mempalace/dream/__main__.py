"""``python -m mempalace.dream inventory …``: run a dream step and write its report."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import inventory


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mempalace.dream")
    sub = ap.add_subparsers(dest="step", required=True)
    p = sub.add_parser("inventory", help="Day 1: checks that need no model")
    p.add_argument("--memory-root", type=Path, required=True, help="Folder of <project>/memory/")
    p.add_argument(
        "--claude-md", type=Path, action="append", default=[], help="A CLAUDE.md (repeatable)"
    )
    p.add_argument(
        "--projects-root",
        type=Path,
        action="append",
        default=[],
        help="Also every <root>/*/CLAUDE.md, one level down, worktrees skipped (repeatable)",
    )
    p.add_argument("--out", type=Path, required=True, help="Report folder (keep it private)")
    p.add_argument("--day", help="Report date (default today)")
    p.add_argument("--threshold", type=float, default=inventory.NEAR_DUPLICATE)
    p.add_argument("--no-embed", action="store_true", help="Skip the near-duplicate check")
    args = ap.parse_args(argv)

    embed = None if args.no_embed else inventory.daemon_embedder()
    claude_md = list(args.claude_md)
    for root in args.projects_root:
        claude_md += inventory.discover_claude_md(root)
    proposals = inventory.run(args.memory_root, claude_md, embed, args.threshold)
    js, md = inventory.write_report(proposals, args.out, args.day)
    counts = Counter(p.kind for p in proposals)
    print(f"{len(proposals)} proposals {dict(counts)} -> {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
