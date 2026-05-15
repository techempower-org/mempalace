#!/usr/bin/env python3
"""eval_multi_encoder_rrf.py — eval the multi-encoder RRF retrieval path end-to-end.

RESEARCH FEATURE eval. Uses the production code path
(``mempalace.multi_encoder.fused_query`` invoked via
``mempalace.searcher.search_memories``) rather than a surrogate, so the
numbers it reports are what we'd ship.

Differs from ``scripts/verify_rrf_ftcode5k.py`` in three ways:

1. **End-to-end path:** queries flow through ``search_memories`` with
   ``PALACE_USE_MULTI_ENCODER_RRF=1`` set, not a bespoke surrogate.
   The single-encoder baseline runs the same function with the env
   unset.
2. **Real RRF over full ranked lists**, not a min(rank) surrogate.
3. **N encoders, not 2.** Defaults to default ONNX + FT-Code-1000 +
   FT-Code-5000 — the 3-way roster from `#82`.

Steps (per run):

* Mine the corpus (default: ``mempalace/`` package) once per encoder
  into a temp palace, by monkey-patching ``mempalace.embedding`` so
  the chunks land with that encoder's vectors.
* Set env to point the multi-encoder roster at the temp palaces.
* For each probe in the supplied probe-set JSON, run
  ``search_memories(query, palace_path=<default palace>)`` twice —
  once with the env enabled, once disabled.
* Compute MRR@K, Recall@5, Recall@10 across the probe set for both
  runs.
* Print a summary table; optionally dump per-probe JSON for diffing.

Usage::

    python scripts/eval_multi_encoder_rrf.py \\
        --probes scripts/probes_v2_git_derived.json \\
        --encoders default,ft-code-1000,ft-code-5000 \\
        --model-paths ft-code-1000=/home/jp/Downloads/ft1000/model,ft-code-5000=/home/jp/Projects/adaptmem-cache/model \\
        --out /tmp/rrf_eval.json

Defaults match what's locally available on katana/familiar.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── encoder swap (monkey-patch get_embedding_function during mining) ──


def _build_sentence_transformer_ef(model_dir: str):
    """Wrap a SentenceTransformer in a chromadb-compatible EF.

    The EF spoofs ``name() == "default"`` so chromadb's collection
    identity check accepts it without recreating the collection. Same
    trick as ``scripts/verify_rrf_ftcode5k.py``.
    """
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_dir)

    class FTEncoderEF:
        @staticmethod
        def name() -> str:
            return "default"

        def __call__(self, input):
            if isinstance(input, str):
                input = [input]
            vecs = model.encode(list(input), convert_to_numpy=True, normalize_embeddings=True)
            return vecs.tolist()

    return FTEncoderEF()


def _install_encoder_for_mining(ef_factory: Callable[[], Any]) -> Callable[[], None]:
    """Monkey-patch ``mempalace.embedding.get_embedding_function`` to use ``ef_factory()``.

    Returns a callable that restores the original.
    """
    import mempalace.embedding as emb_mod

    original_get_ef = emb_mod.get_embedding_function
    original_cache = dict(emb_mod._EF_CACHE)
    emb_mod._EF_CACHE.clear()
    emb_mod.get_embedding_function = lambda device=None: ef_factory()  # type: ignore[assignment]

    def restore() -> None:
        emb_mod._EF_CACHE.clear()
        emb_mod._EF_CACHE.update(original_cache)
        emb_mod.get_embedding_function = original_get_ef

    return restore


# ── mining ────────────────────────────────────────────────────────────


def _mine_corpus(corpus_dir: Path, palace_dir: Path) -> int:
    """Mine ``corpus_dir`` into ``palace_dir`` with the currently-active EF.
    Returns total drawer count after mining.
    """
    from mempalace import miner as miner_mod
    from mempalace.config import MempalaceConfig
    from mempalace.palace import get_collection

    os.environ["MEMPALACE_PALACE_PATH"] = str(palace_dir)
    cfg = MempalaceConfig()
    assert cfg.palace_path == str(palace_dir)
    files = miner_mod.scan_project(str(corpus_dir))
    miner_mod.mine(
        project_dir=str(corpus_dir),
        palace_path=str(palace_dir),
        files=files,
    )
    col = get_collection(str(palace_dir), create=False)
    return col.count()


# ── eval ──────────────────────────────────────────────────────────────


def _run_probes(palace_path: str, probes: list, n_results: int) -> dict:
    """Run all probes through search_memories at the given palace_path.

    Returns a dict with MRR, Recall@5, Recall@10, per-probe breakdown.
    The currently-set env decides whether multi-encoder RRF fires.
    """
    from mempalace.searcher import search_memories

    rrs: list[float] = []
    r5 = 0
    r10 = 0
    per_probe = []
    t0 = time.time()
    for query, expected, _why in probes:
        result = search_memories(query, palace_path, n_results=n_results)
        hits = result.get("results") or []
        target = Path(expected).name
        rank: int | None = None
        for i, hit in enumerate(hits, start=1):
            sf = (hit.get("source_file") or "").strip()
            if Path(sf).name == target:
                rank = i
                break
        rr = (1.0 / rank) if rank else 0.0
        rrs.append(rr)
        if rank is not None and rank <= 5:
            r5 += 1
        if rank is not None and rank <= 10:
            r10 += 1
        per_probe.append(
            {
                "query": query,
                "expected": expected,
                "rank": rank,
                "rr": round(rr, 4),
                "top3": [Path((h.get("source_file") or "")).name for h in hits[:3]],
            }
        )
    elapsed = time.time() - t0
    n = len(probes)
    return {
        "n_probes": n,
        "mrr": sum(rrs) / n if n else 0.0,
        "recall_at_5_pct": 100 * r5 / n if n else 0.0,
        "recall_at_10_pct": 100 * r10 / n if n else 0.0,
        "elapsed_secs": round(elapsed, 2),
        "qps": round(n / elapsed, 2) if elapsed > 0 else None,
        "per_probe": per_probe,
    }


# ── env management ────────────────────────────────────────────────────


def _set_multi_encoder_env(
    encoder_names: list[str], model_paths: dict[str, str], palaces: dict[str, str]
) -> dict[str, str | None]:
    """Set the PALACE_RRF_* env, returning original values for restore."""
    saved: dict[str, str | None] = {}
    for key in (
        "PALACE_USE_MULTI_ENCODER_RRF",
        "PALACE_RRF_ENCODERS",
        "PALACE_RRF_ENCODER_PATHS",
        "PALACE_RRF_PALACES",
    ):
        saved[key] = os.environ.get(key)
    os.environ["PALACE_USE_MULTI_ENCODER_RRF"] = "1"
    os.environ["PALACE_RRF_ENCODERS"] = ",".join(encoder_names)
    os.environ["PALACE_RRF_ENCODER_PATHS"] = ",".join(f"{n}={p}" for n, p in model_paths.items())
    os.environ["PALACE_RRF_PALACES"] = ",".join(f"{n}={p}" for n, p in palaces.items())
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


# ── main ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probes",
        required=True,
        help="JSON probe set (e.g. scripts/probes_v2_git_derived.json).",
    )
    parser.add_argument(
        "--corpus",
        default=str(_REPO_ROOT / "mempalace"),
        help="Directory to mine (default: this repo's mempalace package).",
    )
    parser.add_argument(
        "--encoders",
        default="default,ft-code-1000,ft-code-5000",
        help="Comma-separated encoder roster.",
    )
    parser.add_argument(
        "--model-paths",
        default="",
        help=(
            "Comma-separated name=path pairs for non-default encoders. "
            "Required for any encoder not named 'default'."
        ),
    )
    parser.add_argument("--n-results", type=int, default=10)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap probes processed (0 = all). Useful for quick smoke tests.",
    )
    parser.add_argument(
        "--keep-palaces",
        action="store_true",
        help="Don't delete the temp palaces after the run.",
    )
    parser.add_argument(
        "--baseline-encoder",
        default="default",
        help=(
            "Which encoder's palace to use as the single-encoder baseline. "
            "Default: 'default' (built-in ONNX MiniLM)."
        ),
    )
    parser.add_argument("--out", default="", help="Optional path to write full JSON.")
    args = parser.parse_args(argv)

    corpus = Path(args.corpus).resolve()
    if not corpus.is_dir():
        print(f"Corpus not found: {corpus}", file=sys.stderr)
        return 2

    encoder_names = [n.strip() for n in args.encoders.split(",") if n.strip()]
    model_paths: dict[str, str] = {}
    for part in args.model_paths.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            print(f"--model-paths entry malformed (no '='): {part!r}", file=sys.stderr)
            return 2
        name, _, path = part.partition("=")
        model_paths[name.strip()] = path.strip()

    for name in encoder_names:
        if name != "default" and name not in model_paths:
            print(f"Encoder {name!r} listed but no path supplied in --model-paths", file=sys.stderr)
            return 2

    if args.baseline_encoder not in encoder_names:
        print(
            f"--baseline-encoder {args.baseline_encoder!r} must be one of {encoder_names}",
            file=sys.stderr,
        )
        return 2

    with open(args.probes, "r", encoding="utf-8") as f:
        probe_data = json.load(f)
    probes = [(p["query"], p["expected"], p.get("why", "")) for p in probe_data["probes"]]
    if args.limit:
        probes = probes[: args.limit]

    print(f"Probes:    {len(probes)} from {args.probes}")
    print(f"Corpus:    {corpus}")
    print(f"Encoders:  {encoder_names}")
    print(f"Baseline:  {args.baseline_encoder}")
    print()

    workdir = Path(tempfile.mkdtemp(prefix="mer_eval_"))
    palaces: dict[str, str] = {}
    drawer_counts: dict[str, int] = {}
    print(f"Workdir:   {workdir}")
    print()

    # ── Mine one palace per encoder ───────────────────────────────────
    for name in encoder_names:
        palace_dir = workdir / f"palace_{name}"
        palace_dir.mkdir()
        palaces[name] = str(palace_dir)

        print(f"=== Mining with encoder={name} ===")
        t0 = time.time()
        if name == "default":
            count = _mine_corpus(corpus, palace_dir)
        else:
            ef = _build_sentence_transformer_ef(model_paths[name])
            restore = _install_encoder_for_mining(lambda ef=ef: ef)
            try:
                count = _mine_corpus(corpus, palace_dir)
            finally:
                restore()
        drawer_counts[name] = count
        print(f"  drawers: {count}  ({time.time() - t0:.1f}s)")
        print()

    # ── Single-encoder baseline (no env) ─────────────────────────────
    baseline_palace = palaces[args.baseline_encoder]
    print(f"=== Baseline: single encoder ({args.baseline_encoder}) ===")
    saved_disable = os.environ.pop("PALACE_USE_MULTI_ENCODER_RRF", None)
    try:
        baseline = _run_probes(baseline_palace, probes, args.n_results)
    finally:
        if saved_disable is not None:
            os.environ["PALACE_USE_MULTI_ENCODER_RRF"] = saved_disable
    print(
        f"  MRR: {baseline['mrr']:.4f}  Recall@5: {baseline['recall_at_5_pct']:.1f}%  "
        f"Recall@10: {baseline['recall_at_10_pct']:.1f}%  "
        f"({baseline['elapsed_secs']}s @ {baseline['qps']} qps)"
    )
    print()

    # ── Multi-encoder RRF ────────────────────────────────────────────
    print(f"=== Multi-encoder RRF: {','.join(encoder_names)} ===")
    saved = _set_multi_encoder_env(encoder_names, model_paths, palaces)
    try:
        # Clear the encoder cache so the run loads fresh — important
        # when running this script back-to-back with different rosters.
        from mempalace import multi_encoder as mc

        mc.reset_encoder_cache()
        mer = _run_probes(baseline_palace, probes, args.n_results)
    finally:
        _restore_env(saved)
    print(
        f"  MRR: {mer['mrr']:.4f}  Recall@5: {mer['recall_at_5_pct']:.1f}%  "
        f"Recall@10: {mer['recall_at_10_pct']:.1f}%  "
        f"({mer['elapsed_secs']}s @ {mer['qps']} qps)"
    )
    print()

    # ── Headline ─────────────────────────────────────────────────────
    delta_mrr = mer["mrr"] - baseline["mrr"]
    delta_r5 = mer["recall_at_5_pct"] - baseline["recall_at_5_pct"]
    delta_r10 = mer["recall_at_10_pct"] - baseline["recall_at_10_pct"]
    print("=== Headline ===")
    print(f"  ΔMRR        : {delta_mrr:+.4f}  (single → fused)")
    print(f"  ΔRecall@5   : {delta_r5:+.2f} pp")
    print(f"  ΔRecall@10  : {delta_r10:+.2f} pp")
    print(f"  Query latency multiplier: ~{len(encoder_names)}x (Nx encoders, N palaces)")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "n_probes": len(probes),
                    "encoders": encoder_names,
                    "model_paths": model_paths,
                    "drawer_counts": drawer_counts,
                    "baseline": baseline,
                    "multi_encoder_rrf": mer,
                    "delta_mrr": delta_mrr,
                    "delta_recall_at_5_pp": delta_r5,
                    "delta_recall_at_10_pp": delta_r10,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  Full result JSON: {args.out}")

    if not args.keep_palaces:
        shutil.rmtree(workdir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
