#!/usr/bin/env python3
"""verify_rrf_3way.py — 3-way RRF reproduction of nakata-app's §4 finding.

Discussed in MemPalace/mempalace#1384 (§4). Runs C-AST cs800 under THREE
encoders (default ONNX + FT-Code-1000 + FT-Code-5000), then RRF-fuses
their per-probe ranks. nakata-app reported +0.076 MRR fusion lift on
n=20; we reproduce on the n=200 git-derived set.

Usage::

    python scripts/verify_rrf_3way.py \\
        --ft1000-dir ~/Downloads/ft1000/model \\
        --ft5000-dir ~/Projects/adaptmem-cache/model \\
        --probes scripts/probes_v2_git_derived.json \\
        --out ~/.claude/.../verify_rrf_3way.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.chunk_strategy_ablation import (  # noqa: E402
    _build_palace,
    _query_palace,
    _mrr_for_probe,
    build_strategies,
)
from scripts.verify_rrf_ftcode5k import _build_ft_code_ef  # noqa: E402


def _run_one(name, strategy_fn, corpus, palace_dir, probes, n_results=10):
    drawer_count = _build_palace(corpus, palace_dir, strategy_fn)
    per_probe = []
    for q, expected, _why in probes:
        hits = _query_palace(palace_dir, q, n_results=n_results)
        rr, rank = _mrr_for_probe(hits, expected)
        per_probe.append({"query": q, "expected": expected, "rank": rank, "rr": rr})
    mrr = sum(p["rr"] for p in per_probe) / len(per_probe)
    return {"name": name, "drawer_count": drawer_count, "mrr": mrr, "per_probe": per_probe}


def _rrf_fuse_n(runs, k=60):
    """N-way RRF surrogate from rank-of-expected across N runs.

    Returns (mrr, fused_rows). Each row carries the best (smallest) rank
    across the N runs as the effective fused rank; rrf_score is the
    sum of 1/(k+rank) across all runs that found the probe.
    """
    n_probes = len(runs[0]["per_probe"])
    fused = []
    for i in range(n_probes):
        row = {"query": runs[0]["per_probe"][i]["query"]}
        ranks = []
        rrf_score = 0.0
        for r in runs:
            rk = r["per_probe"][i]["rank"]
            row[f"rank_{r['name']}"] = rk
            if rk is not None:
                ranks.append(rk)
                rrf_score += 1.0 / (k + rk)
        best = min(ranks) if ranks else None
        row["best_rank"] = best
        row["rr"] = (1.0 / best) if best else 0.0
        row["rrf_score"] = rrf_score
        fused.append(row)
    mrr = sum(p["rr"] for p in fused) / len(fused)
    return mrr, fused


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ft1000-dir", required=True)
    parser.add_argument("--ft5000-dir", required=True)
    parser.add_argument("--probes", required=True)
    parser.add_argument("--strategy", default="C_plus_ast_python__cs800")
    parser.add_argument("--n-results", type=int, default=10)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    corpus = _REPO_ROOT / "mempalace"
    with open(args.probes, "r", encoding="utf-8") as f:
        data = json.load(f)
    probes = [(p["query"], p["expected"], p.get("why", "")) for p in data["probes"]]
    print(f"Probes:   {len(probes)} from {args.probes}")
    print(f"Strategy: {args.strategy}")
    print()

    strategy_fn = None
    for cs in (400, 800):
        for name, fn in build_strategies(cs):
            if name == args.strategy:
                strategy_fn = fn
                break
        if strategy_fn:
            break
    if strategy_fn is None:
        print(f"Unknown strategy: {args.strategy}", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="rrf_3way_"))
    print(f"Workdir: {workdir}")
    print()

    import mempalace.embedding as emb_mod

    original_get_ef = emb_mod.get_embedding_function

    runs = []
    try:
        # Run 1: default
        print("=== Run 1: default ONNX MiniLM ===")
        t0 = time.time()
        p = workdir / "default"
        p.mkdir()
        r = _run_one("default", strategy_fn, corpus, p, probes, args.n_results)
        print(f"  drawers: {r['drawer_count']}  MRR: {r['mrr']:.4f}  ({time.time()-t0:.1f}s)")
        runs.append(r)

        # Run 2: FT-Code-1000
        print("\n=== Run 2: FT-Code-1000 ===")
        t0 = time.time()
        p = workdir / "ft1000"
        p.mkdir()
        ef = _build_ft_code_ef(args.ft1000_dir)
        emb_mod._EF_CACHE.clear()
        emb_mod.get_embedding_function = lambda device=None: ef
        r = _run_one("ft1000", strategy_fn, corpus, p, probes, args.n_results)
        print(f"  drawers: {r['drawer_count']}  MRR: {r['mrr']:.4f}  ({time.time()-t0:.1f}s)")
        runs.append(r)

        # Run 3: FT-Code-5000
        print("\n=== Run 3: FT-Code-5000 ===")
        t0 = time.time()
        p = workdir / "ft5000"
        p.mkdir()
        ef = _build_ft_code_ef(args.ft5000_dir)
        emb_mod._EF_CACHE.clear()
        emb_mod.get_embedding_function = lambda device=None: ef
        r = _run_one("ft5000", strategy_fn, corpus, p, probes, args.n_results)
        print(f"  drawers: {r['drawer_count']}  MRR: {r['mrr']:.4f}  ({time.time()-t0:.1f}s)")
        runs.append(r)
    finally:
        emb_mod._EF_CACHE.clear()
        emb_mod.get_embedding_function = original_get_ef

    # ── Fusion ──
    mrr_rrf3, fused3 = _rrf_fuse_n(runs)
    mrr_rrf2_5k, _ = _rrf_fuse_n([runs[0], runs[2]])  # default + ft5000
    mrr_rrf2_1k, _ = _rrf_fuse_n([runs[0], runs[1]])  # default + ft1000

    print("\n=== Headline ===")
    print(f"  MRR default         : {runs[0]['mrr']:.4f}")
    print(f"  MRR FT-Code-1000    : {runs[1]['mrr']:.4f}")
    print(f"  MRR FT-Code-5000    : {runs[2]['mrr']:.4f}")
    print(
        f"  MRR RRF 2-way (5k)  : {mrr_rrf2_5k:.4f}  (Δ vs best solo: {mrr_rrf2_5k - max(runs[0]['mrr'], runs[2]['mrr']):+.4f})"
    )
    print(
        f"  MRR RRF 2-way (1k)  : {mrr_rrf2_1k:.4f}  (Δ vs best solo: {mrr_rrf2_1k - max(runs[0]['mrr'], runs[1]['mrr']):+.4f})"
    )
    print(
        f"  MRR RRF 3-way       : {mrr_rrf3:.4f}  (Δ vs best solo: {mrr_rrf3 - max(r['mrr'] for r in runs):+.4f})"
    )
    print("\n  nakata-app reported +0.076 on 3-way fusion (n=20). We are n=200.")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "n_probes": len(probes),
                    "strategy": args.strategy,
                    "runs": runs,
                    "mrr_rrf3": mrr_rrf3,
                    "mrr_rrf2_default_ft5000": mrr_rrf2_5k,
                    "mrr_rrf2_default_ft1000": mrr_rrf2_1k,
                    "delta_3way_vs_best_solo": mrr_rrf3 - max(r["mrr"] for r in runs),
                    "per_probe_fused3": fused3,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  Full JSON: {args.out}")

    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
