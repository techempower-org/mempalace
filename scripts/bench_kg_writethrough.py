#!/usr/bin/env python3
"""Measure KG write-through: per-drawer commits vs one commit per batch.

palace-daemon#265. Runs three arms against a **scratch** palace — never
production — and reports drawers/s plus the actual commit count observed by
postgres:

    off          write-through disabled (the #249 control: always re-run
                 with the suspect off before attributing time to it)
    per-drawer   today's hook, commit=True per mention
    batched      stage A, commit=False per mention + one kg.commit()

Commits are counted from ``pg_stat_database.xact_commit`` around each arm,
so the number comes from the server rather than from our own bookkeeping —
counting our own calls would just re-assert the thing under test.

Usage::

    python scripts/bench_kg_writethrough.py \\
        --dsn postgresql://user:pw@host/scratch_db \\
        --scratch-db scratch_db [--drawers 300]

Refuses to run unless the DSN's database name equals ``--scratch-db``;
there is no override flag. Exits non-zero when the arms do not do equal
work, or when the edge count cannot be read at all.
"""

from __future__ import annotations

import argparse
import sys
import time

# Named explicitly rather than pattern-matched: the point is that this
# exact string must never be accepted as a scratch database.
_KNOWN_PRODUCTION_DBS = frozenset({"mempalace_2026_05_13"})


class _Entity:
    __slots__ = ("name", "type", "count")

    def __init__(self, name):
        self.name = name
        self.type = "unknown"
        self.count = 1


def _make_drawers(n, entities_per_drawer):
    """Drawers whose entity sets overlap, like a real corpus."""
    return [
        {
            "drawer_id": f"bench_drawer_{i}",
            "document": " ".join(
                f"Entity{(i * entities_per_drawer + j) % (n // 2 or 1)}"
                for j in range(entities_per_drawer)
            ),
            "metadata": {},
        }
        for i in range(n)
    ]


def _extractor(text):
    return [_Entity(word.lower()) for word in text.split()]


def _dbname(dsn: str) -> str:
    """The database a DSN resolves to, or '' when it cannot be read."""
    from urllib.parse import urlparse

    try:
        return (urlparse(dsn).path or "").lstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def _xact_commits(dsn, settle=True):
    """Commit count as postgres sees it, not as we counted our own calls.

    ``pg_stat_database`` is updated by each backend at transaction end but
    throttled (PGSTAT_MIN_INTERVAL), so a read taken immediately after a
    burst under-reports it. A first pass here measured 1,653 commits for a
    run that issued 2,000 — the gap was the instrument, not the code. The
    settle below closes it; ``--no-settle`` shows the lag if you want to
    see it.
    """
    import psycopg

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            if settle:
                cur.execute("SELECT pg_sleep(1.5)")
            cur.execute(
                "SELECT xact_commit FROM pg_stat_database WHERE datname = current_database()"
            )
            return cur.fetchone()[0]


def _edge_count(dsn):
    """Ground truth: how many MENTIONS edges exist.

    Without this the comparison is not valid — an arm that silently dropped
    work would look fast. Both arms must create the same number of edges.
    """
    import psycopg

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("LOAD 'age'")
                cur.execute('SET search_path = ag_catalog, "$user", public')
                cur.execute(
                    "SELECT * FROM cypher('mempalace_kg', "
                    "$$ MATCH ()-[r:MENTIONS]->() RETURN count(r) $$) AS (n agtype)"
                )
                row = cur.fetchone()
                return int(str(row[0])) if row else 0
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({str(exc).splitlines()[0][:40]})"


def _rollbacks(dsn):
    import psycopg

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT xact_rollback FROM pg_stat_database WHERE datname = current_database()"
            )
            return cur.fetchone()[0]


def _run_arm(name, hook, drawers, dsn, per_drawer, settle=True):
    before = _xact_commits(dsn, settle=settle)
    rb_before = _rollbacks(dsn)
    edges_before = _edge_count(dsn)
    start = time.time()
    if hook is not None:
        if per_drawer:
            for drawer in drawers:
                hook(**drawer)
        else:
            hook(drawers)
    elapsed = time.time() - start
    commits = _xact_commits(dsn, settle=settle) - before
    rollbacks = _rollbacks(dsn) - rb_before
    edges_after = _edge_count(dsn)
    edges = (
        edges_after - edges_before
        if isinstance(edges_after, int) and isinstance(edges_before, int)
        else edges_after
    )
    rate = len(drawers) / elapsed if elapsed > 0.001 else None
    return {
        "arm": name,
        "seconds": elapsed,
        "commits": commits,
        "rollbacks": rollbacks,
        "edges": edges,
        "drawers_per_s": rate,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="SCRATCH palace DSN (not production)")
    parser.add_argument("--drawers", type=int, default=300)
    parser.add_argument("--entities", type=int, default=10, help="entities per drawer")
    parser.add_argument(
        "--no-settle",
        action="store_true",
        help="skip the pg_stat settle wait (demonstrates the under-count)",
    )
    parser.add_argument(
        "--scratch-db",
        required=True,
        help="name of the scratch database; the DSN's dbname must equal it",
    )
    args = parser.parse_args(argv)

    # Allowlist, not denylist. A hostname cannot carry the signal here:
    # production is postgresql://...@localhost:5433/mempalace_2026_05_13
    # (docker on the palace host), so "is the host familiar?" lets the real
    # DSN straight through. The operator must name the scratch database and
    # the DSN must agree -- and there is deliberately no override flag,
    # because an override is the thing that gets typed at 2am.
    # Belt as well as braces: the allowlist above already refuses anything
    # the operator has not named, but naming the production database is the
    # one mistake it cannot catch. Costs a line, closes the residual.
    if args.scratch_db in _KNOWN_PRODUCTION_DBS:
        print(
            f"refusing: {args.scratch_db!r} is a known production database. "
            "This benchmark writes thousands of graph edges.",
            file=sys.stderr,
        )
        return 2

    dbname = _dbname(args.dsn)
    if dbname != args.scratch_db:
        print(
            f"refusing: DSN database is {dbname!r} but --scratch-db is "
            f"{args.scratch_db!r}. This benchmark writes thousands of graph edges; "
            "point it at a scratch database and name that database explicitly.",
            file=sys.stderr,
        )
        return 2

    from mempalace.kg_writethrough import make_age_batch_writethrough, make_age_writethrough
    from mempalace.knowledge_graph_age import KnowledgeGraphAGE

    drawers = _make_drawers(args.drawers, args.entities)
    mentions = args.drawers * args.entities

    results = []
    results.append(
        _run_arm("off", None, drawers, args.dsn, per_drawer=False, settle=not args.no_settle)
    )

    kg = KnowledgeGraphAGE(dsn=args.dsn)
    results.append(
        _run_arm(
            "per-drawer",
            make_age_writethrough(kg, _extractor),
            drawers,
            args.dsn,
            per_drawer=True,
            settle=not args.no_settle,
        )
    )

    kg2 = KnowledgeGraphAGE(dsn=args.dsn)
    results.append(
        _run_arm(
            "batched",
            make_age_batch_writethrough(kg2, _extractor),
            drawers,
            args.dsn,
            per_drawer=False,
            settle=not args.no_settle,
        )
    )

    print(f"\n{args.drawers} drawers x {args.entities} entities = {mentions} mentions\n")
    print(f"{'arm':<12} {'seconds':>9} {'drawers/s':>11} {'commits':>9} {'rollbk':>7} {'edges':>8}")
    print("-" * 62)
    for r in results:
        rate = f"{r['drawers_per_s']:.1f}" if r["drawers_per_s"] else "n/a"
        print(
            f"{r['arm']:<12} {r['seconds']:>9.2f} {rate:>11} {r['commits']:>9} "
            f"{r['rollbacks']:>7} {str(r['edges']):>8}"
        )

    per, bat = results[1], results[2]

    # The edge counts are this benchmark's validity condition, so failing
    # them has to fail the RUN. A benchmark that prints a warning and exits
    # 0 will eventually be quoted from a broken run by someone who read
    # only the table.
    for r in results:
        if not isinstance(r["edges"], int):
            print(
                f"\nFAIL: could not count edges for the {r['arm']!r} arm "
                f"({r['edges']}). Without a ground-truth edge count the timings "
                "prove nothing -- an arm that silently dropped work would look "
                "fast. Refusing to report a result.",
                file=sys.stderr,
            )
            return 3
    if per["edges"] != bat["edges"]:
        print(
            f"\nFAIL: the arms did NOT do equal work -- per-drawer wrote "
            f"{per['edges']} edges, batched wrote {bat['edges']}. The timing "
            "comparison is not valid.",
            file=sys.stderr,
        )
        return 4
    if bat["seconds"]:
        print(f"\nspeedup           {per['seconds'] / bat['seconds']:.1f}x")
    print(
        f"commits per drawer  per-drawer {per['commits'] / args.drawers:.1f}  ->  "
        f"batched {bat['commits'] / args.drawers:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
