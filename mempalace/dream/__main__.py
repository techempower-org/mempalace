"""``python -m mempalace.dream inventory|contradictions …``: run a dream step, write its report."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import contradictions, inventory


def _inputs(p) -> None:
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


def _claude_md(args):
    paths = list(args.claude_md)
    for root in args.projects_root:
        paths += inventory.discover_claude_md(root)
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mempalace.dream")
    sub = ap.add_subparsers(dest="step", required=True)
    p = sub.add_parser("inventory", help="Day 1: checks that need no model")
    _inputs(p)
    p.add_argument("--threshold", type=float, default=inventory.NEAR_DUPLICATE)
    p.add_argument("--no-embed", action="store_true", help="Skip the near-duplicate check")
    c = sub.add_parser(
        "contradictions", help="Day 2: feedback memories against rules, judged by a local model"
    )
    _inputs(c)
    c.add_argument("--floor", type=float, default=contradictions.PAIR_FLOOR)
    c.add_argument("--max-pairs", type=int, default=contradictions.MAX_PAIRS)
    args = ap.parse_args(argv)

    if args.step == "contradictions":
        props, stats = contradictions.contradictions(
            inventory.read_memories(args.memory_root),
            contradictions.read_rules(_claude_md(args)),
            inventory.daemon_embedder(),
            contradictions.local_judge(),
            floor=args.floor,
            cap=args.max_pairs,
        )
        out = args.out / "contradictions"
        js, md = inventory.write_report(props, out, args.day)
        js.with_suffix(".stats.json").write_text(json.dumps(stats, indent=2) + "\n")
        print(f"{len(props)} contradictions {json.dumps(stats)} -> {md}")
        return 0 if stats["error"] is None else 3

    embed = None if args.no_embed else inventory.daemon_embedder()
    proposals = inventory.run(args.memory_root, _claude_md(args), embed, args.threshold)
    js, md = inventory.write_report(proposals, args.out, args.day)
    counts = Counter(p.kind for p in proposals)
    print(f"{len(proposals)} proposals {dict(counts)} -> {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
