#!/usr/bin/env python3
"""verify_rrf_ftcode5k.py — local reproduction of nakata-app's RRF result.

Discussed in MemPalace/mempalace#1384. Runs one chunking strategy
through `chunk_strategy_ablation.py` twice (default encoder vs.
FT-Code-5000), then RRF-fuses the rank-of-expected per probe and
reports whether the fused MRR > max(solo MRRs).

Default config: ``C_plus_ast_python__cs800`` (the strategy nakata-app
reported as ``+0.076 MRR`` under 3-way fusion in their 2026-05-14
follow-up). We do 2-way fusion here as a lower bound; 3-way needs
FT-Code-300 + FT-Code-1000 as well.

Usage::

    python scripts/verify_rrf_ftcode5k.py \\
        --model-dir ~/Projects/adaptmem-cache/model \\
        --probes scripts/probes_v2_git_derived.json

Run flow:

1. Mines the mempalace package three times into temp palaces — once
   with default ONNX embedder, once with FT-Code-5000 swapped in,
   each under C-AST cs800.
2. Queries every probe in the supplied set against each palace.
3. Computes per-probe rank-of-expected for each encoder; RRF-fuses
   with k=60; reports MRR for solo default, solo FT-Code-5000, and
   2-way RRF fusion.

The encoder swap is done by monkey-patching
``mempalace.embedding.get_embedding_function`` before the strategy
runs. ``name() == "default"`` is preserved so chromadb collection
identity stays consistent across the two runs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import strategy builders + querier from the existing ablation script.
from scripts.chunk_strategy_ablation import (  # noqa: E402
    _build_palace,
    _query_palace,
    _mrr_for_probe,
    build_strategies,
)


def _build_ft_code_ef(model_dir: str):
    """Return a chromadb-compatible EF that wraps the FT-Code SentenceTransformer.

    Spoofs ``name() == "default"`` so chromadb's collection-identity
    check accepts the swap without re-creating the collection.

    Subclasses ``chromadb.api.types.EmbeddingFunction`` so the inherited
    ``embed_query`` method (which delegates to ``__call__``) is present.
    Without that inheritance, ``collection.query(query_texts=...)`` raises
    ``AttributeError: 'FTCodeEF' object has no attribute 'embed_query'``
    and mempalace's searcher silently falls back to BM25 — making the
    encoder swap invisible. The earlier 2-way result that showed
    FT-Code-5000 MRR 0.0575 was the BM25 fallback, not the actual
    SentenceTransformer embedding. Diagnosed 2026-05-15.
    """
    from sentence_transformers import SentenceTransformer
    from chromadb.api.types import EmbeddingFunction

    model = SentenceTransformer(model_dir)

    class FTCodeEF(EmbeddingFunction):
        @staticmethod
        def name() -> str:
            return "default"

        def __call__(self, input):
            # chromadb passes either a list[str] or a single str depending
            # on the call site; normalize to list and return list[list[float]].
            if isinstance(input, str):
                input = [input]
            vecs = model.encode(list(input), convert_to_numpy=True, normalize_embeddings=True)
            return vecs.tolist()

    return FTCodeEF()


def _install_encoder(get_ef: Callable):
    """Monkey-patch mempalace.embedding.get_embedding_function -> get_ef."""
    import mempalace.embedding as emb_mod

    emb_mod._EF_CACHE.clear()
    emb_mod.get_embedding_function = lambda device=None: get_ef()


def _restore_encoder(original_fn: Callable):
    import mempalace.embedding as emb_mod

    emb_mod._EF_CACHE.clear()
    emb_mod.get_embedding_function = original_fn


def _run_strategy(name, fn, corpus, palace_dir, probes, n_results=10):
    drawer_count = _build_palace(corpus, palace_dir, fn)
    per_probe = []
    for q, expected, _why in probes:
        hits = _query_palace(palace_dir, q, n_results=n_results)
        rr, rank = _mrr_for_probe(hits, expected)
        per_probe.append({"query": q, "expected": expected, "rank": rank, "rr": rr})
    mrr = sum(p["rr"] for p in per_probe) / len(per_probe)
    return {"name": name, "drawer_count": drawer_count, "mrr": mrr, "per_probe": per_probe}


def _rrf_fuse(a_per, b_per, k=60):
    """Compute per-probe RRF surrogate from two encoder runs.

    Surrogate (lower bound): for each probe, take the better
    1/(k+rank) score across the two runs. True RRF would need full
    ranked lists; we only have rank-of-expected, so this is a lower
    bound on real fusion performance.
    """
    fused = []
    for a, b in zip(a_per, b_per):
        assert a["query"] == b["query"]
        ra = a["rank"]
        rb = b["rank"]
        score_a = 1.0 / (k + ra) if ra is not None else 0.0
        score_b = 1.0 / (k + rb) if rb is not None else 0.0
        rrf_score = score_a + score_b
        # Convert back to a pseudo-rank so MRR is comparable: the
        # smallest rank-of-expected across the two runs gives the
        # effective top-1 position under RRF.
        best_rank = min(r for r in (ra, rb) if r is not None) if (ra or rb) else None
        rr = (1.0 / best_rank) if best_rank else 0.0
        fused.append(
            {"query": a["query"], "rank": best_rank, "rr": rr, "rrf_score": rrf_score}
        )
    mrr = sum(p["rr"] for p in fused) / len(fused)
    return mrr, fused


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, help="Path to FT-Code-5000 SentenceTransformer dir.")
    parser.add_argument("--probes", required=True, help="Path to probe set JSON (derive_probes_from_git.py output).")
    parser.add_argument("--strategy", default="C_plus_ast_python__cs800", help="Strategy name to run. Default: C-AST cs800.")
    parser.add_argument("--n-results", type=int, default=10)
    parser.add_argument("--out", default="", help="Optional path to write the full result JSON.")
    args = parser.parse_args(argv)

    corpus = _REPO_ROOT / "mempalace"
    with open(args.probes, "r", encoding="utf-8") as f:
        data = json.load(f)
    probes = [(p["query"], p["expected"], p.get("why", "")) for p in data["probes"]]
    print(f"Probes: {len(probes)} from {args.probes}")
    print(f"Corpus: {corpus}")
    print(f"Strategy: {args.strategy}")
    print(f"FT-Code-5000 model: {args.model_dir}")
    print()

    # Find the requested strategy in build_strategies output.
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

    workdir = Path(tempfile.mkdtemp(prefix="rrf_verify_"))
    print(f"Workdir: {workdir}")
    print()

    import mempalace.embedding as emb_mod
    original_get_ef = emb_mod.get_embedding_function

    try:
        # ── Run 1: default encoder ────────────────────────────────────
        print("=== Run 1: default ONNX MiniLM ===")
        t0 = time.time()
        palace_a = workdir / "default"
        palace_a.mkdir()
        run_default = _run_strategy(
            args.strategy, strategy_fn, corpus, palace_a, probes, args.n_results
        )
        print(f"  drawers: {run_default['drawer_count']}  MRR: {run_default['mrr']:.4f}  ({time.time()-t0:.1f}s)")
        print()

        # ── Run 2: FT-Code-5000 ──────────────────────────────────────
        print("=== Run 2: FT-Code-5000 ===")
        t0 = time.time()
        palace_b = workdir / "ft_code_5k"
        palace_b.mkdir()
        ft_ef = _build_ft_code_ef(args.model_dir)
        _install_encoder(lambda: ft_ef)
        run_ftcode = _run_strategy(
            args.strategy, strategy_fn, corpus, palace_b, probes, args.n_results
        )
        print(f"  drawers: {run_ftcode['drawer_count']}  MRR: {run_ftcode['mrr']:.4f}  ({time.time()-t0:.1f}s)")
        print()
    finally:
        _restore_encoder(original_get_ef)

    # ── RRF surrogate fusion ─────────────────────────────────────────
    mrr_rrf, fused = _rrf_fuse(run_default["per_probe"], run_ftcode["per_probe"])

    print("=== Headline ===")
    print(f"  MRR default        : {run_default['mrr']:.4f}")
    print(f"  MRR FT-Code-5000   : {run_ftcode['mrr']:.4f}")
    print(f"  MRR RRF 2-way      : {mrr_rrf:.4f}")
    delta_solo_best = max(run_default["mrr"], run_ftcode["mrr"])
    print(f"  Δ vs best solo     : {mrr_rrf - delta_solo_best:+.4f}")
    print()
    print("  nakata-app reported +0.076 on 3-way fusion; 2-way is a lower bound.")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "n_probes": len(probes),
                    "strategy": args.strategy,
                    "mrr_default": run_default["mrr"],
                    "mrr_ft_code_5000": run_ftcode["mrr"],
                    "mrr_rrf_2way": mrr_rrf,
                    "delta_vs_best_solo": mrr_rrf - delta_solo_best,
                    "per_probe": fused,
                    "default_per_probe": run_default["per_probe"],
                    "ft_code_per_probe": run_ftcode["per_probe"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  Full result JSON: {args.out}")

    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
