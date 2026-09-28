"""The JSON hallway store streams, so a mine's memory does not grow with the palace (#551).

On the palace host, ``hallways.json`` reached 1.56 GB / 2.6M records. Every
convo mine that filed a drawer ran ``compute_hallways_for_wing``, and the JSON
store called ``json.load`` on the whole file twice (the dynamics lookup, then
the replace). A bare ``json.load`` of that file was memcg-killed at 8,368,656 kB
anon-rss under an 8G cap, the same signature as the palace-daemon kills.

The RSS gate below runs the store's mine-path operations in a fresh
subprocess and bounds the peak RSS *growth* over the post-import baseline.
The old store materializes every record and fails it. The streaming store
holds one wing plus a 1 MiB read buffer and passes.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap

import pytest

from mempalace.hallway_store import JsonHallwayStore

# Records in the synthetic palace, and the bound on peak RSS growth while the
# mine-path operations run over it. At 200K records (~111 MB on disk) a full
# json.load grows peak RSS by ~235 MB (measured). The streamed store's working set
# is the target wing (500 records) plus one read buffer.
_RECORDS = 200_000
_TARGET_WING_RECORDS = 500
_RSS_GROWTH_BOUND_MB = 64


@pytest.fixture
def hallways_mod(monkeypatch):
    """The live ``mempalace.hallways``, re-bound as the package attribute.

    ``tests/test_hallways.py`` imports the module inside
    ``patch.dict("sys.modules", ...)``, which evicts it again on exit. Later
    ``from .hallways import x`` then loads a second module object, while
    ``from . import hallways`` still returns the first. Binding both to one
    object keeps these tests independent of collection order.
    """
    import mempalace

    mod = importlib.import_module("mempalace.hallways")
    monkeypatch.setattr(mempalace, "hallways", mod, raising=False)
    return mod


def _record(i: int, wing: str) -> dict:
    a, b = f"entity_{i:06d}", f"entity_{i + 1:06d}"
    return {
        "id": f"hallway_{wing}_{a}_{b}_{i:08x}",
        "wing": wing,
        "entity_a": a,
        "entity_b": b,
        "co_occurrence_count": 2 + i % 50,
        "rooms": ["problems", "decisions", "references"][: 1 + i % 3],
        "label": f"{a} ↔ {b} (co-occur in {2 + i % 50} drawers across 2 rooms: problems, decisions)",
        "created_at": "2026-09-27T20:00:00+00:00",
        "created_by": "auto",
        "strength": 0.5,
        "stability": 1.0,
        "last_activated": "2026-09-27T20:00:00+00:00",
        "access_count": i % 7,
    }


def _use_file(monkeypatch, path, hallways_mod) -> None:
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda config=None: str(path))
    monkeypatch.setattr(hallways_mod, "_legacy_hallway_file", lambda: str(path) + ".legacy-absent")


def _write_synthetic(path, n: int, target_every: int) -> None:
    records = (
        _record(i, "target" if i % target_every == 0 else f"wing_{i % 40}") for i in range(n)
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write('{\n  "schema_version": 1,\n  "hallways": [\n')
        f.write(",\n".join(json.dumps(r, ensure_ascii=False) for r in records))
        f.write("\n  ]\n}")


_CHILD = textwrap.dedent(
    """
    import json, resource, sys
    from mempalace import hallways
    from mempalace.hallway_store import JsonHallwayStore

    path = sys.argv[1]
    hallways._get_hallway_file = lambda config=None: path
    store = JsonHallwayStore(None)
    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    # What compute_hallways_for_wing and the entity-tunnel step do on a mine.
    dynamics = store.dynamics_for_wing("target")
    fresh = [dict(r, co_occurrence_count=r.get("co_occurrence_count", 0) + 1)
             for r in store.list(wing="target")]
    store.replace_wing("target", fresh)
    total = store.count()
    # The projects-mode entity-tunnel step makes one pass over every wing.
    streamed = sum(1 for _ in hallways.iter_hallways())

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"growth_kb": peak - baseline, "dynamics": len(dynamics),
                      "fresh": len(fresh), "total": total,
                      "streamed": streamed}))
    """
)


def test_mine_path_peak_rss_does_not_scale_with_palace(tmp_path):
    """RED on the whole-file json.load store, GREEN on the streaming one."""
    path = tmp_path / "hallways.json"
    _write_synthetic(path, _RECORDS, target_every=_RECORDS // _TARGET_WING_RECORDS)
    env = dict(os.environ)
    env.pop("MEMPALACE_HALLWAY_BACKEND", None)
    try:
        out = subprocess.run(
            [sys.executable, "-c", _CHILD, str(path)],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
            check=True,
        )
    finally:
        # ~111 MB. pytest keeps the last three basetemps, and /tmp is a small
        # tmpfs on some hosts (512 MB on the palace host), so don't leave it.
        path.unlink(missing_ok=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])

    assert result["dynamics"] == _TARGET_WING_RECORDS
    assert result["fresh"] == _TARGET_WING_RECORDS
    assert result["total"] == _RECORDS  # the replace preserved every other wing
    assert result["streamed"] == _RECORDS
    growth_mb = result["growth_kb"] / 1024
    assert growth_mb < _RSS_GROWTH_BOUND_MB, (
        f"mine-path hallway operations grew peak RSS by {growth_mb:.0f} MB over "
        f"{_RECORDS} records (bound {_RSS_GROWTH_BOUND_MB} MB): the JSON store is "
        "materializing the whole file again (#551)"
    )


def test_save_is_byte_identical_to_json_dump(tmp_path, monkeypatch, hallways_mod):
    path = tmp_path / "hallways.json"
    _use_file(monkeypatch, path, hallways_mod)
    records = [_record(i, "w") for i in range(5)]
    records[2]["label"] = 'quotes " and \\ backslash and \n newline and ✦'
    expected = json.dumps(
        {"schema_version": hallways_mod._SCHEMA_VERSION, "hallways": records},
        indent=2,
        ensure_ascii=False,
    )
    hallways_mod._save_hallways(iter(records))
    assert path.read_text(encoding="utf-8") == expected
    hallways_mod._save_hallways([])
    assert path.read_text(encoding="utf-8") == json.dumps(
        {"schema_version": hallways_mod._SCHEMA_VERSION, "hallways": []}, indent=2
    )


def test_replace_wing_refuses_to_overwrite_a_truncated_file(tmp_path, monkeypatch, hallways_mod):
    """A file cut mid-array must survive a replace. The old store read it as []
    and overwrote it with the one wing, silently dropping every other wing."""
    path = tmp_path / "hallways.json"
    _use_file(monkeypatch, path, hallways_mod)
    hallways_mod._save_hallways([_record(i, f"w{i % 3}") for i in range(30)])
    truncated = path.read_text(encoding="utf-8")[:-40]
    path.write_text(truncated, encoding="utf-8")

    store = JsonHallwayStore(None)
    with pytest.raises(ValueError):
        store.replace_wing("w0", [_record(99, "w0")])
    assert path.read_text(encoding="utf-8") == truncated
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".hallways-")]
    # Reads keep the historical contract: a corrupt file reads as empty.
    assert store.list() == []
    assert store.dynamics_for_wing("w0") == {}
    assert store.count() == 0


def test_delete_without_a_match_does_not_rewrite(tmp_path, monkeypatch, hallways_mod):
    path = tmp_path / "hallways.json"
    _use_file(monkeypatch, path, hallways_mod)
    hallways_mod._save_hallways([_record(i, "w") for i in range(3)])
    before = os.stat(path)
    store = JsonHallwayStore(None)
    assert store.delete("no-such-id") is False
    after = os.stat(path)
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".hallways-")]
    victim = _record(1, "w")["id"]
    assert store.delete(victim) is True
    assert [h["id"] for h in store.list()] == [_record(0, "w")["id"], _record(2, "w")["id"]]


def test_list_offset_and_limit_match_slicing(tmp_path, monkeypatch, hallways_mod):
    path = tmp_path / "hallways.json"
    _use_file(monkeypatch, path, hallways_mod)
    records = [_record(i, "a" if i % 2 else "b") for i in range(25)]
    hallways_mod._save_hallways(records)
    store = JsonHallwayStore(None)
    for wing in (None, "a", "b", "absent"):
        pool = [r for r in records if wing is None or r["wing"] == wing]
        for offset in (None, 0, 3, 40):
            for limit in (None, 0, 1, 5, 100):
                got = store.list(wing=wing, offset=offset, limit=limit)
                want = pool[offset or 0 :]
                want = want if limit is None else want[:limit]
                assert got == want, (wing, offset, limit)
        assert store.count(wing) == len(pool)


def test_iter_hallways_stops_at_truncation_without_raising(tmp_path, monkeypatch, hallways_mod):
    path = tmp_path / "hallways.json"
    _use_file(monkeypatch, path, hallways_mod)
    monkeypatch.delenv("MEMPALACE_HALLWAY_BACKEND", raising=False)
    records = [_record(i, "w") for i in range(10)]
    hallways_mod._save_hallways(records)
    assert list(hallways_mod.iter_hallways()) == records
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: text.index(records[6]["id"])], encoding="utf-8")
    got = list(hallways_mod.iter_hallways())
    assert got == records[: len(got)] and 0 < len(got) < 10


def test_entity_tunnel_step_streams_instead_of_listing(monkeypatch, hallways_mod):
    """The projects-mode entity-tunnel step must not build the full list (#551)."""
    from mempalace import miner

    palace_graph = importlib.import_module("mempalace.palace_graph")

    def _no_full_list(*a, **k):
        raise AssertionError("list_hallways() materializes every record")

    monkeypatch.setattr(hallways_mod, "list_hallways", _no_full_list)
    monkeypatch.setattr(
        hallways_mod, "iter_hallways", lambda config=None: iter([_record(1, "a"), _record(1, "b")])
    )
    seen = {}

    def _fake_entity_tunnels(wing, hallways, config=None):
        seen["records"] = list(hallways)
        return ["t"]

    monkeypatch.setattr(palace_graph, "entity_tunnels_for_wing", _fake_entity_tunnels)
    assert miner._compute_entity_tunnels_for_wing("a") == 1
    assert [h["wing"] for h in seen["records"]] == ["a", "b"]
