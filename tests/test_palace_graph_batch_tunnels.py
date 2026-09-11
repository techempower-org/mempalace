"""`create_tunnels` — one load + one save for a whole batch (#474).

`create_tunnel` does a full `_load_tunnels` AND a full atomic `_save_tunnels`
on every call, and it is called from inside two loops: the per-entity loop in
`entity_tunnels_for_wing` and the per-wing loop in `compute_topic_tunnels`.
N tunnels therefore cost N full loads and N full rewrites. Measured on a
throwaway palace:

    tunnels   cumulative   ms/tunnel in that block   tunnels.json
        100       0.61 s            6.10                46 KB
      1,000      11.54 s           15.14               420 KB
      2,000      37.87 s           26.33               837 KB

Per-tunnel cost rises linearly with the tunnels already on disk, so the total
is O(n²) — ~16 min at 10K tunnels, which is the 29-minute mine reported on
#474. Note the file is 837 KB at 2,000 tunnels: this was never a big-file
problem, it is a per-call-persist problem.

`create_tunnel` is public API with callers outside those loops, so its
per-call semantics must not change underneath them. The fix is a batching
seam: `create_tunnels` does the load/mutate/save once, and `create_tunnel`
becomes a one-element batch.

Run with::

    cd <worktree>
    PYTHONPATH=$PWD /home/jp/Projects/memorypalace/.venv/bin/python \
        -m pytest tests/test_palace_graph_batch_tunnels.py -q
"""

import pytest

from mempalace import palace_graph as pg
from mempalace.config import MempalaceConfig


@pytest.fixture
def cfg(tmp_path):
    # NOTE the nesting: `config.tunnel_file` is dirname(palace_path)/tunnels.json,
    # i.e. the palace's PARENT. A palace rooted directly at `tmp_path` therefore
    # shares one tunnels.json with every other test in the run, and these tests
    # pollute each other. Nest one level so the tunnel file lands inside tmp_path.
    return MempalaceConfig(palace_path=str(tmp_path / "palace"))


@pytest.fixture
def io_counts(monkeypatch):
    """Count full-file loads and saves."""
    counts = {"load": 0, "save": 0}
    real_load, real_save = pg._load_tunnels, pg._save_tunnels

    def load(config=None):
        counts["load"] += 1
        return real_load(config)

    def save(tunnels, config=None):
        counts["save"] += 1
        return real_save(tunnels, config)

    monkeypatch.setattr(pg, "_load_tunnels", load)
    monkeypatch.setattr(pg, "_save_tunnels", save)
    return counts


def _spec(i, kind="entity"):
    return dict(
        source_wing="wing_a",
        source_room=f"entity:E{i}",
        target_wing=f"wing_b{i % 7}",
        target_room=f"entity:E{i}",
        label=f"shared entity E{i}",
        kind=kind,
    )


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------


def test_batch_does_one_load_and_one_save_for_many_tunnels(cfg, io_counts):
    created = pg.create_tunnels([_spec(i) for i in range(25)], config=cfg)

    assert len(created) == 25
    assert io_counts["save"] == 1, "a batch must persist exactly once"
    assert io_counts["load"] <= 1, "a batch must read the file at most once"


def test_sequential_calls_are_unchanged_for_single_callers(cfg, io_counts):
    """Control: `create_tunnel` keeps per-call semantics for outside callers."""
    pg.create_tunnel(config=cfg, **_spec(1))
    pg.create_tunnel(config=cfg, **_spec(2))

    assert io_counts["save"] == 2
    assert len(pg._load_tunnels(cfg)) == 2


def test_empty_batch_writes_nothing(cfg, io_counts):
    assert pg.create_tunnels([], config=cfg) == []
    assert io_counts["save"] == 0


# ---------------------------------------------------------------------------
# semantics that must survive batching
# ---------------------------------------------------------------------------


def test_batch_matches_sequential_exactly(tmp_path):
    """The strongest guard: same specs, same resulting file, either route."""
    specs = [_spec(i) for i in range(12)] + [_spec(3), _spec(7)]  # two repeats

    seq_cfg = MempalaceConfig(palace_path=str(tmp_path / "seq" / "palace"))
    (tmp_path / "seq").mkdir()
    for s in specs:
        pg.create_tunnel(config=seq_cfg, **s)

    batch_cfg = MempalaceConfig(palace_path=str(tmp_path / "batch" / "palace"))
    (tmp_path / "batch").mkdir()
    pg.create_tunnels(specs, config=batch_cfg)

    def shape(config):
        return sorted(
            (
                t["id"],
                t["source"]["wing"],
                t["source"]["room"],
                t["target"]["wing"],
                t["target"]["room"],
                t["label"],
                t["kind"],
            )
            for t in pg._load_tunnels(config)
        )

    assert shape(seq_cfg), "control: the sequential route wrote something"
    assert shape(batch_cfg) == shape(seq_cfg)


def test_undirected_identity_holds_inside_a_batch(cfg):
    """create_tunnel(A, B) and create_tunnel(B, A) are the same tunnel."""
    a = dict(
        source_wing="w1",
        source_room="r1",
        target_wing="w2",
        target_room="r2",
        label="first",
        kind="entity",
    )
    b = dict(
        source_wing="w2",
        source_room="r2",
        target_wing="w1",
        target_room="r1",
        label="second",
        kind="entity",
    )

    pg.create_tunnels([a, b], config=cfg)

    tunnels = pg._load_tunnels(cfg)
    assert len(tunnels) == 1, "reversed endpoints must not create a second tunnel"
    assert tunnels[0]["label"] == "second", "the later spec wins, as sequentially"


def test_repeat_in_batch_preserves_created_at_and_sets_updated_at(cfg):
    first = dict(_spec(1), label="original")
    again = dict(_spec(1), label="revised")

    pg.create_tunnels([first], config=cfg)
    created_at = pg._load_tunnels(cfg)[0]["created_at"]
    pg.create_tunnels([again], config=cfg)

    t = pg._load_tunnels(cfg)[0]
    assert t["label"] == "revised"
    assert t["created_at"] == created_at, "re-creation must not reset created_at"
    assert t.get("updated_at"), "an update must stamp updated_at"


def test_batch_preserves_dynamics_fields_across_recreate(cfg):
    """L7 dynamics accumulate through use; a rebuild must not wipe them."""
    pg.create_tunnels([_spec(1)], config=cfg)
    stored = pg._load_tunnels(cfg)
    stored[0]["strength"] = 0.93
    stored[0]["access_count"] = 17
    pg._save_tunnels(stored, cfg)

    pg.create_tunnels([dict(_spec(1), label="rebuilt")], config=cfg)

    t = pg._load_tunnels(cfg)[0]
    assert t["strength"] == 0.93
    assert t["access_count"] == 17
    assert t["label"] == "rebuilt"


def test_new_tunnels_get_dynamics_defaults(cfg):
    pg.create_tunnels([_spec(1)], config=cfg)
    t = pg._load_tunnels(cfg)[0]
    for field in ("strength", "stability", "last_activated", "access_count"):
        assert field in t


def test_existing_tunnels_from_other_wings_are_preserved(cfg):
    pg.create_tunnels([_spec(1)], config=cfg)
    pg.create_tunnels([_spec(2)], config=cfg)

    assert len(pg._load_tunnels(cfg)) == 2


def test_explicit_kind_still_validates_rooms_and_writes_nothing_on_failure(
    cfg, io_counts, monkeypatch
):
    """An explicit tunnel to a nonexistent room raises, as it did per-call —
    and a failed batch must not half-write."""
    monkeypatch.setattr(pg, "_get_collection", lambda config=None: object())
    monkeypatch.setattr(pg, "_check_room_exists", lambda w, r, col: False)

    with pytest.raises(ValueError):
        pg.create_tunnels([_spec(1, kind="explicit")], config=cfg)

    assert io_counts["save"] == 0, "a rejected batch must not persist anything"
